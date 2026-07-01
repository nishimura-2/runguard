"""adk_app — ADK の LlmAgent を“主動線”として実走させる（必須技術 ADK）。

RunGuard の点検は単一動線＝ADK エージェントが駆動する：
  observe_service で観測・診断 → 診断カテゴリに応じて最適なツールを1つ選んで実行。
  - bad_deploy / crash_loop(直近デプロイ)         → rollback_service
  - out_of_memory                                  → scale_memory
  - traffic_spike                                  → scale_instances
  - crash_loop(直近デプロイでない)                 → restart_service
  - feature_bug（新機能のバグ）                    → propose_code_fix（人の承認後にデプロイ）

各ツールは確定パイプラインと同じ安全ゲート（allowlist / 確信度 / ループ保護 / 承認）を通し、
loop.execute_and_record で検証・インシデント記録・学習（playbook/Elastic）まで行う。
LLM(ADK) が使えない環境では main 側が run_cycle（確定パイプライン）へ自動フォールバックする。

ADK / google-genai は遅延 import（hermetic テスト・オフライン sim を壊さない）。
"""
from __future__ import annotations

from agent.config import Config
from agent.decide import decide
from agent.diagnose import diagnose
from agent.loop import _propose_self_heal, execute_and_record
from agent.models import ActionType, Decision

AGENT_INSTRUCTION = (
    "あなたは Cloud Run の当直 SRE エージェント『RunGuard』です。手順は次の通り:\n"
    "1) まず observe_service を呼び、health（5xx 率・診断カテゴリ category・確信度・"
    "recommended_action・現/正常リビジョン）を確認する。\n"
    "2) error_rate が低く正常なら、何もせず『正常』と報告して終了する。\n"
    "3) 異常なら、observe_service が返した recommended_action に従って対応ツールを"
    "ちょうど1つだけ呼ぶ:\n"
    "   - rollback         → rollback_service\n"
    "   - scale_memory     → scale_memory\n"
    "   - scale_instances  → scale_instances\n"
    "   - restart          → restart_service\n"
    "   - self_heal        → propose_code_fix（新機能のバグ。コード修正案を出し人の承認を待つ）\n"
    "   - escalate         → どのツールも呼ばず、人に委ねる旨を報告\n"
    "4) 対応後はもう一度 observe_service で復旧を確認し、日本語で簡潔に結果を報告する。\n"
    "allowlist 外のサービスや自分自身は操作しないこと。"
)


# --- ツールの中身（ADK ランタイム非依存＝単体テスト可能） -------------------------

def observe_and_diagnose(cfg: Config, backend, deps, ctx: dict, service: str) -> dict:
    """観測＋診断し、ctx に obs/diagnosis/context を格納。健康状態と推奨アクションを返す。"""
    obs = backend.observe(service)
    context = deps.store.playbook_context()
    if deps.elastic is not None:
        extra = deps.elastic.similar_incidents_context(obs)
        if extra:
            context = (context + "\n" + extra).strip()
    diagnosis = diagnose(obs, deps.llm, context)
    ctx["obs"], ctx["diagnosis"], ctx["context"] = obs, diagnosis, context
    rec = decide(diagnosis, obs, cfg)
    return {
        "error_rate": round(obs.error_rate, 3),
        "category": diagnosis.category.value,
        "confidence": round(diagnosis.confidence, 2),
        "recommended_action": rec.action.value,
        "requires_human": rec.requires_human,
        "current_revision": obs.current_revision,
        "last_healthy_revision": obs.last_healthy_revision,
        "has_faulty_source": bool(obs.faulty_source),
        "reason": rec.reason,
    }


def act(cfg: Config, backend, deps, ctx: dict, action: ActionType) -> dict:
    """ctx の観測・診断を用いて指定アクションを（ゲート付きで）実行・記録する。"""
    obs = ctx.get("obs")
    diagnosis = ctx.get("diagnosis")
    context = ctx.get("context", "")
    if obs is None or diagnosis is None:
        return {"error": "先に observe_service を呼んでください。"}
    service = obs.service
    if obs.error_rate <= cfg.error_rate_threshold:
        return {"note": "サービスは正常、対応不要。"}
    # 確定パイプラインと同じ自律実行ゲート（allowlist / 確信度）。
    if (not cfg.is_target_allowed(service)) or diagnosis.confidence < cfg.auto_act_threshold:
        decision = Decision(action=ActionType.escalate, target_service=service,
                            requires_human=True, dry_run=cfg.dry_run,
                            reason="自律実行ゲート不通過（allowlist/確信度）。人へ通知。")
    else:
        decision = Decision(
            action=action, target_service=service,
            target_revision=obs.last_healthy_revision if action == ActionType.rollback else None,
            requires_human=False, dry_run=cfg.dry_run)
    incident = execute_and_record(service, obs, diagnosis, decision, context, deps, deps.now())
    ctx["incident"] = incident
    return {"action": incident.decision.action.value, "outcome": incident.outcome,
            "current_revision": incident.observation.current_revision}


def propose_fix(cfg: Config, backend, deps, ctx: dict) -> dict:
    """ctx の観測・診断を用いて self_heal のコード修正案を提示（人の承認待ちで記録）。"""
    obs = ctx.get("obs")
    diagnosis = ctx.get("diagnosis")
    context = ctx.get("context", "")
    if obs is None or diagnosis is None:
        return {"error": "先に observe_service を呼んでください。"}
    decision = decide(diagnosis, obs, cfg)
    if decision.action != ActionType.self_heal:
        incident = execute_and_record(obs.service, obs, diagnosis, decision, context, deps, deps.now())
        ctx["incident"] = incident
        return {"action": incident.decision.action.value, "outcome": incident.outcome}
    incident = _propose_self_heal(obs.service, obs, diagnosis, decision, context, deps, deps.now())
    if incident is None:
        return {"action": "self_heal", "outcome": "awaiting_approval", "note": "既に修正案を提示済み。"}
    ctx["incident"] = incident
    return {"action": "self_heal", "outcome": incident.outcome,
            "summary": incident.fix.summary if incident.fix else "",
            "kept_feature": incident.fix.kept_feature if incident.fix else None}


def build_agent(cfg: Config, backend, deps, ctx: dict):
    """フル機能ツールを束ねた ADK LlmAgent を構築する（ADK は遅延 import）。

    各ツールは上の観測/実行/提案ヘルパの薄いラッパ。ctx は1回の実行で観測/診断/記録を共有する。
    """
    from google.adk.agents import LlmAgent

    def observe_service(service: str) -> dict:
        """指定サービスを観測・診断し、健康状態と推奨アクションを返す。

        Args:
            service: 監視対象の Cloud Run サービス名。
        """
        return observe_and_diagnose(cfg, backend, deps, ctx, service)

    def rollback_service(service: str) -> dict:
        """悪いデプロイ等を、正常リビジョンへロールバックする（取り消し可能）。

        Args:
            service: 対象の Cloud Run サービス名。
        """
        return act(cfg, backend, deps, ctx, ActionType.rollback)

    def scale_memory(service: str) -> dict:
        """メモリ不足(OOM)に対し、メモリ上限を引き上げる（取り消し可能）。

        Args:
            service: 対象の Cloud Run サービス名。
        """
        return act(cfg, backend, deps, ctx, ActionType.scale_memory)

    def scale_instances(service: str) -> dict:
        """アクセス急増に対し、max-instances を増やす（取り消し可能）。

        Args:
            service: 対象の Cloud Run サービス名。
        """
        return act(cfg, backend, deps, ctx, ActionType.scale_instances)

    def restart_service(service: str) -> dict:
        """クラッシュ（非デプロイ起因）に対し、同一イメージで再起動する（取り消し可能）。

        Args:
            service: 対象の Cloud Run サービス名。
        """
        return act(cfg, backend, deps, ctx, ActionType.restart)

    def propose_code_fix(service: str) -> dict:
        """新機能のバグに対し、AI がバグだけ修正したコードを提案する（人の承認後にデプロイ）。

        Args:
            service: 対象の Cloud Run サービス名。
        """
        return propose_fix(cfg, backend, deps, ctx)

    return LlmAgent(
        name="runguard",
        model=cfg.gemini_model,
        description="Cloud Run 当直 SRE（観測→診断→rollback/scale/restart/コード修正提案）。",
        instruction=AGENT_INSTRUCTION,
        tools=[observe_service, rollback_service, scale_memory, scale_instances,
               restart_service, propose_code_fix],
    )


def build_deps(cfg, backend, store, llm, elastic=None, sleep=lambda s: None):
    """ADK ツールが使う LoopDeps（executor は backend 経由の execute）を組む。"""
    from agent.actions import execute
    from agent.loop import LoopDeps
    return LoopDeps(
        observe=backend.observe,
        llm=llm,
        store=store,
        cfg=cfg,
        executor=lambda d, c, **kw: execute(d, c, backend=backend, **kw),
        sleep=sleep,
        elastic=elastic,
    )


async def run_agent_cycle(cfg: Config, backend, store, llm, elastic=None,
                          sleep=lambda s: None, attempts: int = 2) -> dict:
    """ADK の LlmAgent を1回実行し、ツール呼び出し履歴・最終応答・記録インシデントを返す。

    Vertex の一時的な 429 には短いバックオフで1回リトライ。失敗時は例外を投げる
    （main 側で run_cycle へフォールバック）。
    """
    import asyncio
    import uuid

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    deps = build_deps(cfg, backend, store, llm, elastic, sleep)
    message = (f"{backend.service} を点検し、診断に応じた最適な対応を実行してください"
               "（feature_bug の場合はコード修正を提案）。")

    last_err = None
    for attempt in range(max(1, attempts)):
        ctx: dict = {}
        try:
            agent_obj = build_agent(cfg, backend, deps, ctx)
            session_service = InMemorySessionService()
            runner = Runner(agent=agent_obj, app_name="runguard", session_service=session_service)
            sid = uuid.uuid4().hex
            await session_service.create_session(app_name="runguard", user_id="dashboard", session_id=sid)
            new_message = types.Content(role="user", parts=[types.Part(text=message)])

            steps, final = [], None
            async for event in runner.run_async(user_id="dashboard", session_id=sid, new_message=new_message):
                for fc in (event.get_function_calls() or []):
                    steps.append({"tool": fc.name, "args": {k: str(v) for k, v in dict(fc.args).items()}})
                if event.is_final_response() and event.content and event.content.parts:
                    final = event.content.parts[0].text
            incident = ctx.get("incident")
            return {"final": final, "steps": steps,
                    "incident": incident.model_dump() if incident else None}
        except Exception as e:
            last_err = e
            transient = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if attempt + 1 < attempts and transient:
                await asyncio.sleep(20)
                continue
            raise last_err
