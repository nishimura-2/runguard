"""diagnose — Observation を Gemini に渡して Diagnosis（構造化出力）を得る。

純粋関数（GCP 副作用なし）。LLM はインターフェイス経由で注入し、テストでモック可能。
診断の肝: 「エラー急増が新リビジョンのデプロイ直後に始まったか」を最重視させる。
"""
from __future__ import annotations

from typing import Optional

from agent.gemini_client import LLMClient
from agent.models import Diagnosis, Observation

SYSTEM_INSTRUCTION = (
    "You are RunGuard, an on-call SRE for Cloud Run services. "
    "Given metrics and recent error logs for a single service, diagnose the most likely "
    "root cause and output it as JSON matching the provided schema. "
    "Most important signal: if an error spike began shortly AFTER a new revision was deployed "
    "(small seconds_since_last_deploy with high error_rate), strongly prefer category "
    "'bad_deploy' with recommended_action 'rollback'. "
    "Use 'out_of_memory' when memory_ratio is high / logs show OOM (recommend 'escalate'). "
    "Use 'dependency_5xx' when errors look like an external dependency failing (recommend "
    "'escalate', since rollback would not help). "
    "Use 'crash_loop' when logs show repeated startup crashes; recommend 'rollback' if it "
    "started right after a deploy, else 'escalate'. "
    "Use 'traffic_spike' when request_count is unusually high without a recent deploy. "
    "Use 'unknown' with low confidence when evidence is insufficient. "
    "confidence is 0..1 and must reflect genuine certainty. "
    "Put the most relevant raw log lines into evidence_log_lines."
)


def _deploy_phrase(obs: Observation) -> str:
    s = obs.seconds_since_last_deploy
    if s is None:
        return "直近のデプロイ情報なし（last_deploy_at unknown）。"
    if s < 0:
        return f"last_deploy_at は未来（時刻同期に注意）: {obs.last_deploy_at}"
    mins = s // 60
    return (
        f"最後のデプロイは約 {mins} 分前 ({s} 秒前 / last_deploy_at={obs.last_deploy_at})。"
        f" current_revision={obs.current_revision} / last_healthy_revision={obs.last_healthy_revision}"
    )


def build_prompt(obs: Observation, playbook_context: str = "") -> str:
    logs = "\n".join(f"  - {line}" for line in obs.recent_error_logs) or "  (なし)"
    parts = [
        f"service: {obs.service}",
        f"window_minutes: {obs.window_minutes}",
        f"error_rate (5xx): {obs.error_rate:.3f}",
        f"request_count: {obs.request_count}",
        f"p95_latency_ms: {obs.p95_latency_ms:.0f}",
        f"memory_ratio: {obs.memory_ratio:.2f}",
        f"instances: {obs.instances}",
        f"deploy timing: {_deploy_phrase(obs)}",
        "recent_error_logs:",
        logs,
    ]
    if playbook_context.strip():
        parts.append("")
        parts.append("過去に効いた対応（プレイブック / learn の蓄積）:")
        parts.append(playbook_context.strip())
    parts.append("")
    parts.append("上記を診断し、スキーマに沿った JSON を返してください。")
    return "\n".join(parts)


def diagnose(
    observation: Observation,
    llm: LLMClient,
    playbook_context: str = "",
) -> Diagnosis:
    prompt = build_prompt(observation, playbook_context)
    result = llm.generate_structured(
        prompt=prompt,
        schema=Diagnosis,
        system_instruction=SYSTEM_INSTRUCTION,
    )
    # 念のため型を保証（モック/実クライアントどちらでも Diagnosis を返す）
    if not isinstance(result, Diagnosis):
        result = Diagnosis.model_validate(result)
    return result
