"""sim — GCP 不要のシミュレーションバックエンド（ローカルデモ/開発用）。

ダッシュボードの「障害注入」を in-memory のフォールト状態で再現し、observe は合成メトリクスを返す。
ロールバックはトラフィック振替の代わりに状態フラグを戻す（副作用は in-memory のみで安全）。
本物の GCP 接続時は observe=build_observation / rollback=Cloud Run Admin に差し替える（loop は共通）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Tuple

from agent.config import Config
from agent.models import CodeFix, Observation
from agent.observe import seconds_between
from agent.selfheal import BUGGY_FEATURE_SOURCE, FEATURE_BUG_LOGS

# シナリオ別の合成メトリクス（error_rate / memory_ratio / request_count）。
_SCENARIO = {
    "healthy":       {"er": 0.0, "mem": 0.30, "req": 500,  "logs": []},
    "http500":       {"er": 0.9, "mem": 0.30, "req": 500,
                      "logs": ['{"severity":"ERROR","message":"synthetic 500 (FAULT=http500)","status":500}']},
    "feature_bug":   {"er": 0.9, "mem": 0.30, "req": 500,  "logs": list(FEATURE_BUG_LOGS)},
    "oom":           {"er": 0.2, "mem": 0.95, "req": 600,
                      "logs": ['{"severity":"ERROR","message":"Memory limit exceeded (OOM): container killed","status":500}']},
    "traffic_spike": {"er": 0.5, "mem": 0.60, "req": 5000,
                      "logs": ['{"severity":"WARNING","message":"instances saturated; 429 Too Many Requests"}']},
    "fixed":         {"er": 0.0, "mem": 0.30, "req": 500,  "logs": []},
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimEnvironment:
    def __init__(self, service: str = "sample-service"):
        self.service = service
        self.healthy_rev = f"{service}-00001-healthy"
        self.bad_rev = f"{service}-00002-bad"
        self.feature_bug_rev = f"{service}-00003-feature-bug"
        self.fixed_rev = f"{service}-00004-fixed"
        self.current_rev = self.healthy_rev
        self.last_deploy_at = None
        self.injected = False
        self.scenario = "healthy"          # healthy/http500/feature_bug/oom/traffic_spike/fixed
        self.faulty_source = None          # feature_bug 時の不調ソース
        self.applied_source = None         # self_heal で採用した修正後ソース
        self.memory_mib = 256              # ① scale_memory で増える
        self.max_instances = 1             # ① scale_instances で増える
        self._rev_seq = 4                  # 復旧アクションで新リビジョン名を採番
        self.history: List[Tuple[str, float]] = []

    # --- 障害注入 ---
    def inject_fault(self) -> None:
        """悪いリビジョン(http500)へ振替（= rollback シナリオ）。直近デプロイ起因。"""
        self._enter("http500", rev=self.bad_rev, recent_deploy=True)
        self.faulty_source = None

    def inject_feature_bug(self) -> None:
        """新機能＋仕込みバグ版へデプロイ（= self_heal シナリオ）。"""
        self._enter("feature_bug", rev=self.feature_bug_rev, recent_deploy=True)
        self.faulty_source = BUGGY_FEATURE_SOURCE

    def inject_oom(self) -> None:
        """メモリ逼迫（OOM）。デプロイ起因ではない → scale_memory が適切。"""
        self._enter("oom", rev=self.current_rev, recent_deploy=False)

    def inject_traffic_spike(self) -> None:
        """アクセス急増でインスタンス飽和。デプロイ起因ではない → scale_instances が適切。"""
        self._enter("traffic_spike", rev=self.current_rev, recent_deploy=False)

    def _enter(self, scenario: str, *, rev: str, recent_deploy: bool) -> None:
        self.injected = True
        self.scenario = scenario
        self.current_rev = rev
        self.last_deploy_at = _now_iso() if recent_deploy else None
        self.faulty_source = None

    # --- 復旧アクション（すべて in-memory で「効く」＝ verify が resolved になる） ---
    def apply_rollback(self, cfg: Config, service: str, revision: str) -> None:
        self.current_rev = revision
        self._recover()

    def apply_code_fix(self, fix: CodeFix) -> None:
        """AI の修正を採用し修正版リビジョンを配信（= 自己修復のデプロイ相当）。"""
        self.applied_source = fix.fixed_source if fix else None
        self.current_rev = self.fixed_rev
        self._recover()

    def scale_memory(self, cfg: Config, service: str) -> None:
        self.memory_mib *= 2                       # 例: 256 -> 512 MiB
        self.current_rev = self._next_rev("mem")
        self._recover()

    def scale_instances(self, cfg: Config, service: str) -> None:
        self.max_instances += 4                    # 例: 1 -> 5
        self.current_rev = self._next_rev("scale")
        self._recover()

    def restart(self, cfg: Config, service: str) -> None:
        self.current_rev = self._next_rev("restart")
        self._recover()

    def _next_rev(self, kind: str) -> str:
        self._rev_seq += 1
        return f"{self.service}-{self._rev_seq:05d}-{kind}"

    def _recover(self) -> None:
        self.injected = False
        self.scenario = "healthy"
        self.faulty_source = None

    def reset(self) -> None:
        self.injected = False
        self.scenario = "healthy"
        self.current_rev = self.healthy_rev
        self.last_deploy_at = None
        self.faulty_source = None
        self.applied_source = None
        self.memory_mib = 256
        self.max_instances = 1
        self.history.clear()

    # --- 観測 ---
    def _metrics(self) -> dict:
        return _SCENARIO.get(self.scenario, _SCENARIO["healthy"])

    def _error_rate(self) -> float:
        return self._metrics()["er"]

    def observe(self, service: str) -> Observation:
        now = _now_iso()
        m = self._metrics()
        er = m["er"]
        self.history.append((now, er))
        self.history = self.history[-60:]
        faulty = BUGGY_FEATURE_SOURCE if self.scenario == "feature_bug" else None
        return Observation(
            service=service,
            error_rate=er,
            request_count=m["req"],
            memory_ratio=m["mem"],
            instances=self.max_instances,
            current_revision=self.current_rev,
            last_healthy_revision=self.healthy_rev,
            last_deploy_at=self.last_deploy_at,
            seconds_since_last_deploy=seconds_between(self.last_deploy_at, now),
            recent_error_logs=list(m["logs"]),
            faulty_source=faulty,
            observed_at=now,
        )

    def snapshot(self) -> dict:
        m = self._metrics()
        return {
            "service": self.service,
            "current_revision": self.current_rev,
            "healthy_revision": self.healthy_rev,
            "bad_revision": self.bad_rev,
            "feature_bug_revision": self.feature_bug_rev,
            "fixed_revision": self.fixed_rev,
            "scenario": self.scenario,
            "injected": self.injected,
            "error_rate": m["er"],
            "memory_ratio": m["mem"],
            "memory_mib": self.memory_mib,
            "max_instances": self.max_instances,
            "faulty_source": self.faulty_source,
            "applied_source": self.applied_source,
            "history": [{"t": t, "error_rate": e} for t, e in self.history],
        }
