"""loop — observe→diagnose→decide→act→verify→learn を束ねる確定的サイクル。

依存（observe / llm / store / executor / sleep / clock）は注入可能にして hermetic にテストする。
/api/tick から run_cycle(service, deps) を1回呼ぶ想定（Cloud Scheduler 駆動）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

from agent.actions import execute as default_execute
from agent.config import Config
from agent.config import config as default_config
from agent.decide import decide
from agent.diagnose import diagnose
from agent.gemini_client import LLMClient
from agent.learn import IncidentStore
from agent.models import ActionType, Incident, Observation
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
    actions_taken, secs_since = deps.store.recent_action_stats(service, now)
    result = deps.executor(
        decision,
        cfg,
        actions_taken_this_incident=actions_taken,
        seconds_since_last_action=secs_since,
    )

    rolled_back = result.action == ActionType.rollback and result.skipped_reason is None
    if rolled_back:
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
    )
    deps.store.save_incident(incident)
    deps.store.update_playbook(diagnosis, decision, outcome)
    if deps.elastic is not None:
        deps.elastic.index_incident(incident)
    return incident
