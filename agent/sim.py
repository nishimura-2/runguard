"""sim — GCP 不要のシミュレーションバックエンド（ローカルデモ/開発用）。

ダッシュボードの「障害注入」を in-memory のフォールト状態で再現し、observe は合成メトリクスを返す。
ロールバックはトラフィック振替の代わりに状態フラグを戻す（副作用は in-memory のみで安全）。
本物の GCP 接続時は observe=build_observation / rollback=Cloud Run Admin に差し替える（loop は共通）。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Tuple

from agent.config import Config
from agent.models import Observation
from agent.observe import seconds_between

HEALTHY_ERROR_RATE = 0.0
FAULT_ERROR_RATE = 0.9


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SimEnvironment:
    def __init__(self, service: str = "sample-service"):
        self.service = service
        self.healthy_rev = f"{service}-00001-healthy"
        self.bad_rev = f"{service}-00002-bad"
        self.current_rev = self.healthy_rev
        self.last_deploy_at = None
        self.injected = False
        self.history: List[Tuple[str, float]] = []

    def inject_fault(self) -> None:
        """悪いリビジョンへトラフィックを振替（= 障害注入）。"""
        self.injected = True
        self.current_rev = self.bad_rev
        self.last_deploy_at = _now_iso()

    def reset(self) -> None:
        self.injected = False
        self.current_rev = self.healthy_rev
        self.last_deploy_at = None
        self.history.clear()

    def _error_rate(self) -> float:
        return FAULT_ERROR_RATE if self.injected else HEALTHY_ERROR_RATE

    def observe(self, service: str) -> Observation:
        now = _now_iso()
        er = self._error_rate()
        self.history.append((now, er))
        self.history = self.history[-60:]
        logs = (
            ['{"severity":"ERROR","message":"synthetic 500 (FAULT=http500)","status":500}']
            if self.injected else []
        )
        return Observation(
            service=service,
            error_rate=er,
            request_count=500,
            current_revision=self.current_rev,
            last_healthy_revision=self.healthy_rev,
            last_deploy_at=self.last_deploy_at,
            seconds_since_last_deploy=seconds_between(self.last_deploy_at, now),
            recent_error_logs=logs,
            observed_at=now,
        )

    def apply_rollback(self, cfg: Config, service: str, revision: str) -> None:
        """Cloud Run トラフィック振替の代わりに健全リビジョンへ戻す（in-memory）。"""
        self.current_rev = revision
        self.injected = False

    def snapshot(self) -> dict:
        return {
            "service": self.service,
            "current_revision": self.current_rev,
            "healthy_revision": self.healthy_rev,
            "bad_revision": self.bad_rev,
            "injected": self.injected,
            "error_rate": self._error_rate(),
            "history": [{"t": t, "error_rate": e} for t, e in self.history],
        }
