"""decide — Diagnosis から Decision を決める純粋関数。

確信度ゲート / allowlist（自己除外）/ アクション対応表 / 戻り先チェック。
GCP 副作用なし。dry_run フラグは config から引き継ぐ（実行可否は act 側で最終判断）。
"""
from __future__ import annotations

from agent.config import Config
from agent.config import config as default_config
from agent.models import ActionType, Category, Decision, Diagnosis, Observation

# 自動対応可能（= 取り消し可能なロールバックで対処できる）カテゴリ
AUTO_ROLLBACK_CATEGORIES = {Category.bad_deploy, Category.crash_loop}

# AI がコードを修正して直す（ロールバックでは新機能を失う）カテゴリ
SELF_HEAL_CATEGORIES = {Category.feature_bug}

# crash_loop を「直近デプロイ起因」とみなす猶予（秒）
RECENT_DEPLOY_SECONDS = 600


def _recommends_rollback(diagnosis: Diagnosis, obs: Observation) -> bool:
    cat = diagnosis.category
    if cat == Category.bad_deploy:
        return True
    if cat == Category.crash_loop:
        s = obs.seconds_since_last_deploy
        return s is not None and 0 <= s <= RECENT_DEPLOY_SECONDS
    return False


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

    if not _recommends_rollback(diagnosis, observation):
        return Decision(
            action=ActionType.escalate,
            target_service=service,
            reason=f"カテゴリ {diagnosis.category.value} は自動ロールバック対象外。人に通知。",
            requires_human=True,
            dry_run=dry_run,
        )

    # ロールバック推奨。自動実行の確信度ゲート＋allowlist＋戻り先チェック。
    blockers = []
    if not cfg.is_target_allowed(service):
        blockers.append(f"{service} は操作 allowlist 外（または agent 自身）")
    if diagnosis.confidence < cfg.auto_act_threshold:
        blockers.append(f"確信度 {diagnosis.confidence:.2f} < 閾値 {cfg.auto_act_threshold:.2f}")
    if diagnosis.category not in AUTO_ROLLBACK_CATEGORIES:
        blockers.append(f"カテゴリ {diagnosis.category.value} は自動対応集合外")
    if not observation.last_healthy_revision:
        blockers.append("戻り先 last_healthy_revision が不明")

    if blockers:
        return Decision(
            action=ActionType.escalate,
            target_service=service,
            reason="ロールバック推奨だが自動実行ゲート不通過: " + " / ".join(blockers),
            requires_human=True,
            dry_run=dry_run,
        )

    return Decision(
        action=ActionType.rollback,
        target_service=service,
        target_revision=observation.last_healthy_revision,
        reason=(
            f"{diagnosis.category.value}（確信度 {diagnosis.confidence:.2f}）→ "
            f"{observation.last_healthy_revision} へ自動ロールバック。"
        ),
        requires_human=False,
        dry_run=dry_run,
    )
