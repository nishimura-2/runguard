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

HEALTHY_ERROR_RATE = 0.0
FAULT_ERROR_RATE = 0.9


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
        self.scenario = "healthy"          # healthy / http500 / feature_bug / fixed
        self.faulty_source = None          # feature_bug 時の不調ソース
        self.applied_source = None         # self_heal で採用した修正後ソース
        self.history: List[Tuple[str, float]] = []

    def inject_fault(self) -> None:
        """悪いリビジョン(http500)へトラフィックを振替（= 障害注入 / rollback シナリオ）。"""
        self.injected = True
        self.scenario = "http500"
        self.current_rev = self.bad_rev
        self.last_deploy_at = _now_iso()
        self.faulty_source = None

    def inject_feature_bug(self) -> None:
        """新機能＋仕込みバグ版へデプロイ（= self_heal シナリオ）。ロールバックでは新機能を失う。"""
        self.injected = True
        self.scenario = "feature_bug"
        self.current_rev = self.feature_bug_rev
        self.last_deploy_at = _now_iso()
        self.faulty_source = BUGGY_FEATURE_SOURCE

    def apply_code_fix(self, fix: CodeFix) -> None:
        """AI の修正を採用し、修正版リビジョンを配信（= 自己修復のデプロイ相当）。"""
        self.applied_source = fix.fixed_source if fix else None
        self.current_rev = self.fixed_rev
        self.injected = False
        self.scenario = "fixed"
        self.faulty_source = None

    def reset(self) -> None:
        self.injected = False
        self.scenario = "healthy"
        self.current_rev = self.healthy_rev
        self.last_deploy_at = None
        self.faulty_source = None
        self.applied_source = None
        self.history.clear()

    def _error_rate(self) -> float:
        return FAULT_ERROR_RATE if self.injected else HEALTHY_ERROR_RATE

    def observe(self, service: str) -> Observation:
        now = _now_iso()
        er = self._error_rate()
        self.history.append((now, er))
        self.history = self.history[-60:]
        logs: List[str] = []
        faulty = None
        if self.injected and self.scenario == "feature_bug":
            logs = list(FEATURE_BUG_LOGS)
            faulty = BUGGY_FEATURE_SOURCE
        elif self.injected:
            logs = ['{"severity":"ERROR","message":"synthetic 500 (FAULT=http500)","status":500}']
        return Observation(
            service=service,
            error_rate=er,
            request_count=500,
            current_revision=self.current_rev,
            last_healthy_revision=self.healthy_rev,
            last_deploy_at=self.last_deploy_at,
            seconds_since_last_deploy=seconds_between(self.last_deploy_at, now),
            recent_error_logs=logs,
            faulty_source=faulty,
            observed_at=now,
        )

    def apply_rollback(self, cfg: Config, service: str, revision: str) -> None:
        """Cloud Run トラフィック振替の代わりに健全リビジョンへ戻す（in-memory）。"""
        self.current_rev = revision
        self.injected = False
        self.scenario = "healthy"
        self.faulty_source = None

    def snapshot(self) -> dict:
        return {
            "service": self.service,
            "current_revision": self.current_rev,
            "healthy_revision": self.healthy_rev,
            "bad_revision": self.bad_rev,
            "feature_bug_revision": self.feature_bug_rev,
            "fixed_revision": self.fixed_rev,
            "scenario": self.scenario,
            "injected": self.injected,
            "error_rate": self._error_rate(),
            "faulty_source": self.faulty_source,
            "applied_source": self.applied_source,
            "history": [{"t": t, "error_rate": e} for t, e in self.history],
        }
