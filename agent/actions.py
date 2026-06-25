"""actions — Decision を実行する（副作用 / dry-run / ループ保護）。

- rollback: Cloud Run Admin API でトラフィックを last_healthy_revision へ 100% 戻す。
- escalate: 記録（Webhook 等は任意。最低限ダッシュボード/ログ/learn に残す）。
- セーフティ: DRY_RUN / クールダウン / 1インシデント最大アクション数 / 自己除外（二重ガード）。
- ライブの Cloud Run API 呼び出しは確認済み API（run_v2）で実装。実接続の最終確認は Phase 4。
"""
from __future__ import annotations

from typing import Optional, Tuple

from pydantic import BaseModel

from agent.config import Config
from agent.config import config as default_config
from agent.models import ActionType, Decision


class ActionResult(BaseModel):
    action: ActionType
    executed: bool = False
    dry_run: bool = True
    target_service: Optional[str] = None
    target_revision: Optional[str] = None
    message: str = ""
    skipped_reason: Optional[str] = None


# ---- ループ保護（純粋・テスト可能） ----

def guard_allows_action(
    *,
    actions_taken_this_incident: int,
    seconds_since_last_action: Optional[int],
    cfg: Config = default_config,
) -> Tuple[bool, Optional[str]]:
    if actions_taken_this_incident >= cfg.max_actions_per_incident:
        return False, f"1インシデント最大アクション数 {cfg.max_actions_per_incident} に到達"
    if (
        seconds_since_last_action is not None
        and seconds_since_last_action < cfg.cooldown_seconds
    ):
        return False, f"クールダウン中（{seconds_since_last_action}s < {cfg.cooldown_seconds}s）"
    return True, None


# ---- 実行 ----

def execute(
    decision: Decision,
    cfg: Config = default_config,
    *,
    actions_taken_this_incident: int = 0,
    seconds_since_last_action: Optional[int] = None,
    rollback_fn=None,
) -> ActionResult:
    if decision.action == ActionType.escalate:
        return ActionResult(
            action=ActionType.escalate,
            executed=True,
            dry_run=cfg.dry_run,
            target_service=decision.target_service,
            message=f"escalate: {decision.reason}",
        )

    if decision.action != ActionType.rollback:
        return ActionResult(
            action=decision.action, executed=False, dry_run=cfg.dry_run,
            target_service=decision.target_service, message="no-op",
        )

    # --- rollback の各ガード ---
    if not cfg.is_target_allowed(decision.target_service or ""):
        return ActionResult(
            action=ActionType.escalate, executed=False, dry_run=cfg.dry_run,
            target_service=decision.target_service,
            message="allowlist 外のためロールバック中止（自己操作防止の二重ガード）。",
            skipped_reason="not_allowed",
        )
    if decision.requires_human:
        return ActionResult(
            action=ActionType.escalate, executed=False, dry_run=cfg.dry_run,
            target_service=decision.target_service,
            message="requires_human=True のため自動実行しない（人の承認待ち）。",
            skipped_reason="requires_human",
        )
    if not decision.target_revision:
        return ActionResult(
            action=ActionType.escalate, executed=False, dry_run=cfg.dry_run,
            target_service=decision.target_service,
            message="戻り先リビジョン不明のため中止。",
            skipped_reason="no_target_revision",
        )
    ok, why = guard_allows_action(
        actions_taken_this_incident=actions_taken_this_incident,
        seconds_since_last_action=seconds_since_last_action,
        cfg=cfg,
    )
    if not ok:
        return ActionResult(
            action=ActionType.rollback, executed=False, dry_run=cfg.dry_run,
            target_service=decision.target_service,
            target_revision=decision.target_revision,
            message=f"ループ保護によりロールバック抑止: {why}",
            skipped_reason="loop_guard",
        )

    return _rollback(decision, cfg, rollback_fn)


def _rollback(decision: Decision, cfg: Config, rollback_fn=None) -> ActionResult:
    service = decision.target_service
    revision = decision.target_revision
    if cfg.dry_run:
        return ActionResult(
            action=ActionType.rollback, executed=False, dry_run=True,
            target_service=service, target_revision=revision,
            message=f"DRY_RUN: {service} のトラフィックを {revision} へ 100% 戻す（意図のみ）。",
        )
    (rollback_fn or _apply_rollback_live)(cfg, service, revision)
    return ActionResult(
        action=ActionType.rollback, executed=True, dry_run=False,
        target_service=service, target_revision=revision,
        message=f"{service} のトラフィックを {revision} へ 100% 戻した。",
    )


def _apply_rollback_live(cfg: Config, service: str, revision: str) -> None:
    """Cloud Run Admin API でトラフィックを revision へ 100% にする（ライブ）。

    run_v2 の API は確認済み。実接続の最終確認は GCP 接続後（Phase 4 / デプロイ時）。
    """
    from google.cloud import run_v2  # 遅延 import
    client = run_v2.ServicesClient()
    name = f"projects/{cfg.project_id}/locations/{cfg.region}/services/{service}"
    svc = client.get_service(name=name)
    svc.traffic = [
        run_v2.TrafficTarget(
            type_=run_v2.TrafficTargetAllocationType.TRAFFIC_TARGET_ALLOCATION_TYPE_REVISION,
            revision=revision,
            percent=100,
        )
    ]
    client.update_service(service=svc).result()
