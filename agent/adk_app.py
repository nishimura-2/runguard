"""adk_app — ADK の LlmAgent に観測/ロールバックのツールを配線し、実走させる（必須技術 ADK）。

- build_agent(cfg, backend): backend（Sim/Real）に束ねたツールを持つ LlmAgent を返す。
- run_agent(cfg, backend, message): ADK の Runner で in-process 実行し、最終応答とツール呼び出し履歴を返す（async）。
- ADK / google-genai は遅延 import（hermetic テスト・sim デプロイを壊さない）。
- 監視サイクルの確定実行は loop.run_cycle（/api/tick）。本モジュールは LLM 主導の対話的オーケストレーション。
"""
from __future__ import annotations

from agent.config import Config

AGENT_INSTRUCTION = (
    "あなたは Cloud Run の当直 SRE エージェント『RunGuard』です。"
    "まず observe_service でサービスの健康状態（5xx 率・現/正常リビジョン・デプロイ経過秒・エラーログ）を確認し、"
    "エラー急増が新リビジョンのデプロイ直後に始まっていれば『悪いデプロイ』と判断して、"
    "rollback_service で正常リビジョン（観測結果の last_healthy_revision）へロールバックしてください。"
    "ロールバック後はもう一度 observe_service で復旧を確認すること。"
    "allowlist 外のサービスや自分自身は操作しないこと。最終回答は日本語で簡潔に。"
)


def build_agent(cfg: Config, backend):
    """backend（Sim/Real）に束ねたツールを持つ LlmAgent を構築する（ADK は遅延 import）。"""
    from google.adk.agents import LlmAgent

    from agent.actions import execute
    from agent.models import ActionType, Decision

    def observe_service(service: str) -> dict:
        """指定した Cloud Run サービスの現在の健康状態を観測して返す。

        Args:
            service: 監視対象のサービス名。
        """
        return backend.observe(service).model_dump()

    def rollback_service(service: str, to_revision: str) -> dict:
        """サービスのトラフィックを正常リビジョンへ 100% 戻す（ロールバック）。allowlist 外や自分自身は実行されない。

        Args:
            service: 対象の Cloud Run サービス名。
            to_revision: 戻し先の正常リビジョン名（観測結果の last_healthy_revision）。
        """
        decision = Decision(action=ActionType.rollback, target_service=service,
                            target_revision=to_revision, requires_human=False, dry_run=cfg.dry_run)
        return execute(decision, cfg, rollback_fn=backend.apply_rollback).model_dump()

    return LlmAgent(
        name="runguard",
        model=cfg.gemini_model,
        description="Cloud Run 当直 SRE（観測→診断→必要ならロールバック）。",
        instruction=AGENT_INSTRUCTION,
        tools=[observe_service, rollback_service],
    )


async def run_agent(cfg: Config, backend, message: str, attempts: int = 2) -> dict:
    """ADK の Runner で LlmAgent を1回実行し、最終応答とツール呼び出し履歴を返す。

    Vertex の一時的な 429（レート上限）には短いバックオフで1回リトライする。
    """
    import asyncio
    import uuid

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    last_err = None
    for attempt in range(max(1, attempts)):
        try:
            agent_obj = build_agent(cfg, backend)
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
            return {"final": final, "steps": steps}
        except Exception as e:
            last_err = e
            transient = "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e)
            if attempt + 1 < attempts and transient:
                await asyncio.sleep(20)
                continue
            raise last_err
