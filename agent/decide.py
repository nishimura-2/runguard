"""decide — Diagnosis から Decision を決める純粋関数。

確信度ゲート / allowlist（自己除外）/ アクション対応表 / 戻り先チェック。
GCP 副作用なし。dry_run フラグは config から引き継ぐ（実行可否は act 側で最終判断）。
"""
from __future__ import annotations

from agent.config import Config
from agent.config import config as default_config
from agent.models import ActionType, Category, Decision, Diagnosis, Observation

# AI がコードを修正して直す（ロールバックでは新機能を失う）カテゴリ
SELF_HEAL_CATEGORIES = {Category.feature_bug}

# crash_loop を「直近デプロイ起因」とみなす猶予（秒）
RECENT_DEPLOY_SECONDS = 600

# 取り消し可能（= 自律実行してよい）アクション。escalate/self_heal は別扱い。
REVERSIBLE_ACTIONS = {
    ActionType.rollback, ActionType.scale_memory,
    ActionType.scale_instances, ActionType.restart,
}

_ACTION_PHRASE = {
    ActionType.rollback: "正常リビジョンへロールバック",
    ActionType.scale_memory: "メモリ上限を引き上げ",
    ActionType.scale_instances: "max-instances を増やす",
    ActionType.restart: "同一イメージで再起動（新リビジョン）",
}


def _intended_action(diagnosis: Diagnosis, obs: Observation) -> ActionType:
    """カテゴリ（と直近デプロイ）から、取り消し可能な対応アクションを引く。

    対応手段が無い（=人に委ねる）カテゴリは ActionType.escalate を返す。
    """
    cat = diagnosis.category
    if cat == Category.bad_deploy:
        return ActionType.rollback
    if cat == Category.crash_loop:
        s = obs.seconds_since_last_deploy
        recent = s is not None and 0 <= s <= RECENT_DEPLOY_SECONDS
        return ActionType.rollback if recent else ActionType.restart
    if cat == Category.out_of_memory:
        return ActionType.scale_memory
    if cat == Category.traffic_spike:
        return ActionType.scale_instances
    return ActionType.escalate    # dependency_5xx / unknown 等は人へ


def _decide_self_heal(
    diagnosis: Diagnosis, obs: Observation, cfg: Config
) -> Decision:
    """新機能のバグ → AI がバグだけ修正した新リビジョンを提案（必ず人の承認ゲート）。"""
    service = obs.service
    dry_run = cfg.dry_run
    blockers = []
    if not cfg.is_target_allowed(service):
        blockers.append(f"{service} は操作 allowlist 外（または agent 自身）")
    if diagnosis.confidence < cfg.auto_act_threshold:
        blockers.append(f"確信度 {diagnosis.confidence:.2f} < 閾値 {cfg.auto_act_threshold:.2f}")
    if not obs.faulty_source:
        blockers.append("不調リビジョンのソースを取得できずコード修正不可")
    if blockers:
        return Decision(
            action=ActionType.escalate,
            target_service=service,
            reason="コード修正(self_heal)推奨だが不通過: " + " / ".join(blockers),
            requires_human=True,
            dry_run=dry_run,
        )
    return Decision(
        action=ActionType.self_heal,
        target_service=service,
        target_revision=obs.current_revision,   # 参考: 修正対象の不調リビジョン
        reason=(
            f"{diagnosis.category.value}（確信度 {diagnosis.confidence:.2f}）: "
            "ロールバックでは新機能を失うため、AI がバグだけを修正した新リビジョンを提案。"
            "コードを出荷するため人の承認後にデプロイ。"
        ),
        requires_human=True,   # self_heal はコードを出荷する → 必ず承認ゲートを通す
        dry_run=dry_run,
    )


def decide(
    diagnosis: Diagnosis,
    observation: Observation,
    cfg: Config = default_config,
) -> Decision:
    service = observation.service
    dry_run = cfg.dry_run

    # 新機能のバグはロールバックより「コード修正(self_heal)」が適切（新機能を保持）。
    if diagnosis.category in SELF_HEAL_CATEGORIES:
        return _decide_self_heal(diagnosis, observation, cfg)

    action = _intended_action(diagnosis, observation)
    if action == ActionType.escalate:
        return Decision(
            action=ActionType.escalate,
            target_service=service,
            reason=f"カテゴリ {diagnosis.category.value} は自動対応の手段がない。人に通知。",
            requires_human=True,
            dry_run=dry_run,
        )

    # 取り消し可能なアクション推奨。自動実行の確信度ゲート＋allowlist（＋rollback は戻り先）。
    blockers = []
    if not cfg.is_target_allowed(service):
        blockers.append(f"{service} は操作 allowlist 外（または agent 自身）")
    if diagnosis.confidence < cfg.auto_act_threshold:
        blockers.append(f"確信度 {diagnosis.confidence:.2f} < 閾値 {cfg.auto_act_threshold:.2f}")
    if action == ActionType.rollback and not observation.last_healthy_revision:
        blockers.append("戻り先 last_healthy_revision が不明")

    if blockers:
        return Decision(
            action=ActionType.escalate,
            target_service=service,
            reason=f"{_ACTION_PHRASE[action]} 推奨だが自動実行ゲート不通過: " + " / ".join(blockers),
            requires_human=True,
            dry_run=dry_run,
        )

    return Decision(
        action=action,
        target_service=service,
        target_revision=observation.last_healthy_revision if action == ActionType.rollback else None,
        reason=(
            f"{diagnosis.category.value}（確信度 {diagnosis.confidence:.2f}）→ "
            f"{_ACTION_PHRASE[action]}（取り消し可能なため自動実行）。"
        ),
        requires_human=False,
        dry_run=dry_run,
    )
