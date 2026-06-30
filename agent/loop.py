"""loop — observe→diagnose→decide→act→verify→learn を束ねる確定的サイクル。

依存（observe / llm / store / executor / sleep / clock）は注入可能にして hermetic にテストする。
/api/tick から run_cycle(service, deps) を1回呼ぶ想定（Cloud Scheduler 駆動）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from agent.actions import LIVE_ACTIONS
from agent.actions import execute as default_execute
from agent.config import Config
from agent.config import config as default_config
from agent.decide import decide
from agent.diagnose import diagnose
from agent.gemini_client import LLMClient
from agent.learn import IncidentStore
from agent.models import ActionType, Diagnosis, Incident, Observation
from agent.selfheal import build_fix_and_diff
from agent.verify import verify_outcome


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class LoopDeps:
    observe: Callable[[str], Observation]
    llm: LLMClient
    store: IncidentStore
    cfg: Config = default_config
    executor: Callable = default_execute
    sleep: Callable[[int], None] = lambda s: None       # verify 待ち（テストでは no-op）
    now: Callable[[], str] = _now_iso
    new_id: Callable[[], str] = _new_id
    elastic: Optional[object] = None        # ElasticStore 互換（任意）。類似検索＋蓄積。
    # self_heal のコード修正生成（obs, diagnosis）->(CodeFix, diff)。None なら Gemini/フォールバックを使う。
    fix_generator: Optional[Callable] = None


def run_cycle(service: str, deps: LoopDeps) -> Optional[Incident]:
    cfg = deps.cfg
    obs = deps.observe(service)

    # 健康ならインシデントなし。ループ保護状態をリセット。
    if obs.error_rate <= cfg.error_rate_threshold:
        deps.store.mark_healthy(service)
        return None

    context = deps.store.playbook_context()
    if deps.elastic is not None:
        extra = deps.elastic.similar_incidents_context(obs)
        if extra:
            context = (context + "\n" + extra).strip()
    diagnosis = diagnose(obs, deps.llm, context)
    decision = decide(diagnosis, obs, cfg)

    now = deps.now()

    # self_heal（AI コード修正）は提案→人の承認→デプロイの2段。ここでは提案のみ。
    if decision.action == ActionType.self_heal:
        return _propose_self_heal(service, obs, diagnosis, decision, context, deps, now)

    actions_taken, secs_since = deps.store.recent_action_stats(service, now)
    result = deps.executor(
        decision,
        cfg,
        actions_taken_this_incident=actions_taken,
        seconds_since_last_action=secs_since,
    )

    # 取り消し可能アクション（rollback / scale_memory / scale_instances / restart）が
    # 抑止されず実行された → 記録し、ライブなら再観測で復旧を検証。
    acted = result.action in LIVE_ACTIONS and result.skipped_reason is None
    if acted:
        deps.store.record_action(service, now)
        if result.executed and not result.dry_run:
            deps.sleep(cfg.verify_wait_seconds)
            post = deps.observe(service)
            outcome = verify_outcome(post, cfg)
        else:
            outcome = "dry_run"
    elif result.action == ActionType.escalate:
        outcome = "escalated"
    else:
        outcome = result.skipped_reason or "no_action"

    incident = Incident(
        id=deps.new_id(),
        timestamp=now,
        observation=obs,
        diagnosis=diagnosis,
        decision=decision,
        outcome=outcome,
        context_used=context or "",
    )
    deps.store.save_incident(incident)
    deps.store.update_playbook(diagnosis, decision, outcome)
    if deps.elastic is not None:
        deps.elastic.index_incident(incident)
    return incident


def _propose_self_heal(
    service: str,
    obs: Observation,
    diagnosis: Diagnosis,
    decision,
    context: str,
    deps: LoopDeps,
    now: str,
) -> Optional[Incident]:
    """不調リビジョンのソースを AI に修正させ、コード差分つきで『承認待ち』として記録する。

    実デプロイはしない（人の承認ゲート）。承認は apply_self_heal で行う。
    同じ障害エピソードで毎ティック再生成しないよう、提案済みなら何もしない。
    """
    actions_taken, _ = deps.store.recent_action_stats(service, now)
    if actions_taken >= 1:
        return None  # 既に修正案を提示済み（承認待ち）。UI はストアの提案を表示し続ける。

    gen = deps.fix_generator or (
        lambda o, d: build_fix_and_diff(o.faulty_source or "", o.recent_error_logs, deps.llm)
    )
    fix, diff = gen(obs, diagnosis)
    deps.store.record_action(service, now)  # 提案済みマーク（再生成・連打抑止）

    incident = Incident(
        id=deps.new_id(),
        timestamp=now,
        observation=obs,
        diagnosis=diagnosis,
        decision=decision,
        outcome="awaiting_approval",
        context_used=context or "",
        fix=fix,
        fix_diff=diff,
    )
    deps.store.save_incident(incident)
    # 提案はまだ「効いた対応」ではないので playbook には計上しない（承認・適用時に記録）。
    if deps.elastic is not None:
        deps.elastic.index_incident(incident)
    return incident


def apply_self_heal(service: str, deps: LoopDeps, backend, incident: Incident) -> Incident:
    """承認された修正を適用→（再）観測で検証。直らなければ正常版へロールバック退避。"""
    cfg = deps.cfg
    now = deps.now()
    backend.apply_code_fix(incident.fix)  # SIM: 修正版を採用 / REAL: 修正版リビジョンへ
    deps.sleep(cfg.verify_wait_seconds)
    post = deps.observe(service)
    if verify_outcome(post, cfg) == "resolved":
        outcome = "self_healed"
        deps.store.mark_healthy(service)
    else:
        if post.last_healthy_revision:
            backend.apply_rollback(cfg, service, post.last_healthy_revision)
        outcome = "not_resolved_rolled_back"

    healed = Incident(
        id=deps.new_id(),
        timestamp=now,
        observation=post,
        diagnosis=incident.diagnosis,
        decision=incident.decision,
        outcome=outcome,
        context_used=incident.context_used or "",
        fix=incident.fix,
        fix_diff=incident.fix_diff,
    )
    deps.store.save_incident(healed)
    deps.store.update_playbook(
        incident.diagnosis, incident.decision,
        "resolved" if outcome == "self_healed" else outcome,
    )
    if deps.elastic is not None:
        deps.elastic.index_incident(healed)
    return healed
