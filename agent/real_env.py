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
from agent.models import Observation
from agent.observe import fetch_recent_error_logs, seconds_between

HEALTHY_TAG = "healthy"
BAD_TAG = "bad"


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
                 log_fetcher=None, route_fn=None):
        self.cfg = cfg
        self.service = cfg.target_services[0] if cfg.target_services else "sample-service"
        self._services = services_client
        self._prober = prober            # callable(url, n) -> List[int]
        self._log_fetcher = log_fetcher  # callable() -> List[str]
        self._route_fn = route_fn        # callable(revision_name) -> None（テスト注入用）
        self._incident_started_at: Optional[str] = None
        # snapshot 用キャッシュ（毎ポーリングで API を叩かない）
        self._cur_rev = ""
        self._healthy_rev = ""
        self._bad_rev = ""
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
        service.traffic = [run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=revision_name, percent=100)]
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
        # 不調リビジョンが配信中なら（インスタンス再生成で開始時刻を失っても）インシデント扱い
        if self._bad_rev and self._cur_rev == self._bad_rev and not self._incident_started_at:
            self._incident_started_at = now
        secs = seconds_between(self._incident_started_at, now) if self._incident_started_at else None
        logs: List[str] = []
        if er > self.cfg.error_rate_threshold:
            fetch = self._log_fetcher or (lambda: fetch_recent_error_logs(self.cfg, self.service))
            logs = fetch()
        return Observation(
            service=service, error_rate=er, request_count=len(codes),
            current_revision=self._cur_rev, last_healthy_revision=self._healthy_rev,
            last_deploy_at=self._incident_started_at, seconds_since_last_deploy=secs,
            recent_error_logs=logs, observed_at=now,
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

    def apply_rollback(self, cfg, service, revision) -> None:
        self._route_100(revision)
        self._incident_started_at = None
        self._cur_rev = revision

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
        self.history.clear()

    def snapshot(self) -> dict:
        return {
            "service": self.service, "current_revision": self._cur_rev,
            "healthy_revision": self._healthy_rev, "bad_revision": self._bad_rev,
            "injected": bool(self._incident_started_at), "error_rate": self._last_error_rate,
            "history": [{"t": t, "error_rate": e} for t, e in self.history],
        }
