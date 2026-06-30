"""real_env — 本物の Cloud Run バックエンド（Step 3）。

- observe: サービスを直接 HTTP プローブして実 5xx 率を測り、Run Admin で現/正常リビジョンを読む。
  error_rate がしきい値超なら Cloud Logging から直近 ERROR ログを数行取得（診断の証跡）。
- inject_fault: Run Admin で実トラフィックを 'bad' タグのリビジョンへ 100% 振替（=本物の障害注入）。
- apply_rollback: Run Admin で 'healthy' タグのリビジョンへ 100% 戻す（=本物のロールバック）。
- run_v2 / httpx / logging は遅延 import。テストではクライアント/プローブ/振替関数を注入してモック可能。
- SimEnvironment と同じインターフェイス（observe/inject_fault/apply_rollback/reset/snapshot/service）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional

from agent.config import Config
from agent.config import config as default_config
from agent.models import CodeFix, Observation
from agent.observe import fetch_recent_error_logs, seconds_between
from agent.selfheal import BUGGY_FEATURE_SOURCE

HEALTHY_TAG = "healthy"
BAD_TAG = "bad"
FEATURE_BUG_TAG = "feature-bug"   # 新機能＋仕込みバグ版（self_heal シナリオ）
FIXED_TAG = "fixed"               # 事前ビルド済みの修正版（新IAM不要のデプロイ相当）


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def error_rate_from_statuses(statuses: List[int]) -> float:
    if not statuses:
        return 0.0
    return sum(1 for s in statuses if s >= 500) / len(statuses)


def tag_to_revision(service, tag: str) -> str:
    """Service の traffic / traffic_statuses から tag → revision 名を解決（タグは選択子でないため）。"""
    for t in getattr(service, "traffic", []):
        if getattr(t, "tag", "") == tag and getattr(t, "revision", ""):
            return t.revision
    for s in getattr(service, "traffic_statuses", []):
        if getattr(s, "tag", "") == tag and getattr(s, "revision", ""):
            return s.revision
    return ""


def serving_revision(service) -> str:
    """現在 100% を配信しているリビジョン（_LATEST は latest_ready_revision で解決）。"""
    for s in getattr(service, "traffic_statuses", []):
        if getattr(s, "percent", 0) == 100:
            return s.revision or getattr(service, "latest_ready_revision", "")
    return getattr(service, "latest_ready_revision", "")


class RealEnvironment:
    def __init__(self, cfg: Config = default_config, services_client=None, prober=None,
                 log_fetcher=None, route_fn=None, deploy_fn=None):
        self.cfg = cfg
        self.service = cfg.target_services[0] if cfg.target_services else "sample-service"
        self._services = services_client
        self._prober = prober            # callable(url, n) -> List[int]
        self._log_fetcher = log_fetcher  # callable() -> List[str]
        self._route_fn = route_fn        # callable(revision_name) -> None（テスト注入用）
        self._deploy_fn = deploy_fn      # callable(CodeFix) -> revision_name（ライブビルド注入用・任意）
        self._incident_started_at: Optional[str] = None
        # snapshot 用キャッシュ（毎ポーリングで API を叩かない）
        self._cur_rev = ""
        self._healthy_rev = ""
        self._bad_rev = ""
        self._feature_bug_rev = ""
        self._fixed_rev = ""
        self._scenario = "healthy"
        self._last_error_rate = 0.0
        self.history: list = []

    def _client(self):
        if self._services is None:
            from google.cloud import run_v2
            self._services = run_v2.ServicesClient()
        return self._services

    def _svc_name(self) -> str:
        return f"projects/{self.cfg.project_id}/locations/{self.cfg.region}/services/{self.service}"

    def _get_service(self):
        return self._client().get_service(name=self._svc_name())

    def _route_100(self, revision_name: str) -> None:
        if self._route_fn is not None:
            self._route_fn(revision_name)
            return
        from google.cloud import run_v2
        client = self._client()
        service = client.get_service(name=self._svc_name())
        # 既存のタグ（healthy/bad 等）を保持したまま percent だけ付け替える
        # （単一エントリで上書きするとタグが消え、次回の tag→revision 解決が壊れるため）
        tagged = {}
        for t in list(service.traffic) + list(getattr(service, "traffic_statuses", [])):
            tag, rev = getattr(t, "tag", ""), getattr(t, "revision", "")
            if tag and rev and tag not in tagged:
                tagged[tag] = rev
        at = run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION
        new_traffic, target_tagged = [], False
        for tag, rev in tagged.items():
            if rev == revision_name:
                target_tagged = True
            new_traffic.append(run_v2.TrafficTarget(
                type_=at, revision=rev, percent=(100 if rev == revision_name else 0), tag=tag))
        if not target_tagged:
            new_traffic.append(run_v2.TrafficTarget(type_=at, revision=revision_name, percent=100))
        service.traffic = new_traffic
        client.update_service(service=service).result()

    def _probe(self, url: str) -> List[int]:
        n = max(1, self.cfg.probe_count)
        if self._prober is not None:
            return self._prober(url, n)
        import httpx
        codes = []
        for _ in range(n):
            try:
                codes.append(httpx.get(url, timeout=5).status_code)
            except Exception:
                codes.append(0)
        return codes

    def _refresh_revs(self, service) -> None:
        self._cur_rev = serving_revision(service)
        self._healthy_rev = tag_to_revision(service, HEALTHY_TAG) or self._healthy_rev
        self._bad_rev = tag_to_revision(service, BAD_TAG) or self._bad_rev
        self._feature_bug_rev = tag_to_revision(service, FEATURE_BUG_TAG) or self._feature_bug_rev
        self._fixed_rev = tag_to_revision(service, FIXED_TAG) or self._fixed_rev
        if self._feature_bug_rev and self._cur_rev == self._feature_bug_rev:
            self._scenario = "feature_bug"
        elif self._bad_rev and self._cur_rev == self._bad_rev:
            self._scenario = "http500"
        elif self._fixed_rev and self._cur_rev == self._fixed_rev:
            self._scenario = "fixed"
        else:
            self._scenario = "healthy"

    def observe(self, service: str) -> Observation:
        svc = self._get_service()
        self._refresh_revs(svc)
        url = getattr(svc, "uri", "")
        codes = self._probe(url) if url else []
        er = error_rate_from_statuses(codes)
        now = _now_iso()
        self._last_error_rate = er
        self.history.append((now, er))
        self.history = self.history[-60:]
        # 不調リビジョン（bad / feature-bug）が配信中ならインシデント扱い（開始時刻を失っても）
        serving_faulty = self._cur_rev in (self._bad_rev, self._feature_bug_rev) and (
            bool(self._bad_rev) or bool(self._feature_bug_rev)
        )
        if serving_faulty and not self._incident_started_at:
            self._incident_started_at = now
        secs = seconds_between(self._incident_started_at, now) if self._incident_started_at else None
        logs: List[str] = []
        if er > self.cfg.error_rate_threshold:
            fetch = self._log_fetcher or (lambda: fetch_recent_error_logs(self.cfg, self.service))
            logs = fetch()
        # feature-bug 配信中は不調ソースを添付（self_heal の入力になる）
        faulty = BUGGY_FEATURE_SOURCE if self._scenario == "feature_bug" else None
        return Observation(
            service=service, error_rate=er, request_count=len(codes),
            current_revision=self._cur_rev, last_healthy_revision=self._healthy_rev,
            last_deploy_at=self._incident_started_at, seconds_since_last_deploy=secs,
            recent_error_logs=logs, faulty_source=faulty, observed_at=now,
        )

    def inject_fault(self) -> None:
        svc = self._get_service()
        self._refresh_revs(svc)
        bad = tag_to_revision(svc, BAD_TAG)
        if not bad:
            raise ValueError("'bad' タグのリビジョンが見つかりません（不調リビジョン未デプロイ？）")
        self._route_100(bad)
        self._incident_started_at = _now_iso()
        self._cur_rev = bad
        self._scenario = "http500"

    def inject_feature_bug(self) -> None:
        """新機能＋仕込みバグ版（'feature-bug' タグ）へトラフィックを振替（self_heal シナリオ）。"""
        svc = self._get_service()
        self._refresh_revs(svc)
        fb = tag_to_revision(svc, FEATURE_BUG_TAG)
        if not fb:
            raise ValueError(
                "'feature-bug' タグのリビジョンが見つかりません（新機能＋バグ版が未デプロイ）。"
                " リハーサル時に deploy スクリプトで feature-bug/fixed 版をデプロイしてください。"
            )
        self._route_100(fb)
        self._incident_started_at = _now_iso()
        self._cur_rev = fb
        self._scenario = "feature_bug"

    def apply_code_fix(self, fix: CodeFix) -> None:
        """承認された修正をデプロイ相当で反映。

        既定（新IAM不要・デモ安定）: 事前ビルド済み 'fixed' タグのリビジョンへ振替。
        cfg.self_heal_live=True かつ deploy_fn 注入時のみ、ライブビルド（gcloud run deploy --source）を行う。
        """
        if self.cfg.self_heal_live and self._deploy_fn is not None:
            rev = self._deploy_fn(fix)              # ライブビルド（要・追加IAM）。既定では未使用。
            self._route_100(rev)
            self._cur_rev = rev
        else:
            svc = self._get_service()
            self._refresh_revs(svc)
            fixed = tag_to_revision(svc, FIXED_TAG)
            if not fixed:
                raise ValueError(
                    "'fixed' タグのリビジョンが見つかりません（事前ビルド済み修正版が未デプロイ）。"
                )
            self._route_100(fixed)
            self._cur_rev = fixed
        self._incident_started_at = None
        self._scenario = "fixed"

    def apply_rollback(self, cfg, service, revision) -> None:
        self._route_100(revision)
        self._incident_started_at = None
        self._cur_rev = revision
        self._scenario = "healthy"

    def reset(self) -> None:
        try:
            svc = self._get_service()
            self._refresh_revs(svc)
            if self._healthy_rev:
                self._route_100(self._healthy_rev)
                self._cur_rev = self._healthy_rev
        except Exception:
            pass
        self._incident_started_at = None
        self._scenario = "healthy"
        self.history.clear()

    def snapshot(self) -> dict:
        return {
            "service": self.service, "current_revision": self._cur_rev,
            "healthy_revision": self._healthy_rev, "bad_revision": self._bad_rev,
            "feature_bug_revision": self._feature_bug_rev, "fixed_revision": self._fixed_rev,
            "scenario": self._scenario,
            "injected": bool(self._incident_started_at), "error_rate": self._last_error_rate,
            "faulty_source": BUGGY_FEATURE_SOURCE if self._scenario == "feature_bug" else None,
            "applied_source": None,
            "history": [{"t": t, "error_rate": e} for t, e in self.history],
        }
