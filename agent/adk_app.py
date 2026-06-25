"""adk_app — ADK の LlmAgent に各ステップを FunctionTool として配線する。

- 監視サイクルの確定的実行は loop.run_cycle（/api/tick から）が担う。
- 本モジュールは「ADK エージェントとしての対話的オーケストレーション」を提供し、
  ハッカソン必須要件（ADK + Gemini）を満たす。
- ADK / google-genai は遅延 import（未インストールでも本モジュールの import は通る）。
  ADK + Gemini のライブ実行確認は GCP / Vertex 接続後に行う。
"""
from __future__ import annotations

from agent.config import Config
from agent.config import config as default_config

AGENT_INSTRUCTION = (
    "You are RunGuard, an on-call SRE agent for Cloud Run services. "
    "Use the tools to observe a target service, diagnose anomalies (treat an error spike "
    "shortly after a new deployment as a bad deployment), decide a SAFE action under the "
    "confidence gate and allowlist (rollback is reversible and preferred), act, verify "
    "recovery, then record the incident. Never act on services outside the allowlist or on "
    "yourself (runguard-agent)."
)


def build_agent(cfg: Config = default_config):
    """ADK の LlmAgent を構築して返す（ADK は遅延 import）。"""
    from google.adk.agents import LlmAgent

    return LlmAgent(
        name="runguard",
        model=cfg.gemini_model,
        description="Cloud Run on-call SRE agent (observe→diagnose→decide→act→verify→learn).",
        instruction=AGENT_INSTRUCTION,
        tools=_build_tools(cfg),
    )


def _build_tools(cfg: Config):
    """各ステップを ADK ツール関数として公開する（素の関数は ADK が FunctionTool 化）。"""
    from agent.actions import execute as _execute
    from agent.decide import decide as _decide
    from agent.diagnose import diagnose as _diagnose
    from agent.gemini_client import make_llm_client
    from agent.models import Decision, Diagnosis, Observation
    from agent.observe import build_observation
    from agent.verify import build_post_action_observation

    _llm = make_llm_client(cfg)

    def observe_service(service: str) -> dict:
        """指定 Cloud Run サービスのメトリクス/ログ/デプロイ時刻を観測する。"""
        return build_observation(service, cfg).model_dump()

    def diagnose_observation(observation: dict) -> dict:
        """観測から根本原因を診断する（デプロイ直後の急増=悪いデプロイを最重視）。"""
        return _diagnose(Observation.model_validate(observation), _llm).model_dump()

    def decide_action(diagnosis: dict, observation: dict) -> dict:
        """診断から安全なアクションを決める（確信度ゲート/allowlist）。"""
        return _decide(
            Diagnosis.model_validate(diagnosis),
            Observation.model_validate(observation),
            cfg,
        ).model_dump()

    def act(decision: dict) -> dict:
        """決定を実行する（ロールバック等。DRY_RUN/ループ保護/自己除外つき）。"""
        return _execute(Decision.model_validate(decision), cfg).model_dump()

    def verify_recovery(service: str) -> dict:
        """アクション後に再観測してエラー率が戻ったか確認する。"""
        return build_post_action_observation(service, cfg).model_dump()

    return [observe_service, diagnose_observation, decide_action, act, verify_recovery]
