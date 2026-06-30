"""diagnose — Observation を Gemini に渡して Diagnosis（構造化出力）を得る。

純粋関数（GCP 副作用なし）。LLM はインターフェイス経由で注入し、テストでモック可能。
診断の肝: 「エラー急増が新リビジョンのデプロイ直後に始まったか」を最重視させる。
"""
from __future__ import annotations

from typing import Optional

from agent.gemini_client import LLMClient
from agent.models import Diagnosis, Observation

SYSTEM_INSTRUCTION = (
    "あなたは Cloud Run サービスの当直 SRE エージェント『RunGuard』です。"
    "1つのサービスのメトリクスと直近のエラーログを受け取り、最も可能性の高い根本原因を診断し、"
    "指定されたスキーマの JSON で出力してください。"
    "最重要シグナル: エラー急増が新リビジョンのデプロイ直後に始まった場合"
    "（seconds_since_last_deploy が小さく error_rate が高い）は、category を 'bad_deploy'、"
    "recommended_action を 'rollback' に強く寄せること。"
    "ただし、ログにアプリのコード例外（traceback / ZeroDivisionError 等）が出ていて、"
    "不調リビジョンに新機能が含まれている場合は 'feature_bug' とし、"
    "recommended_action を 'self_heal' にすること（ロールバックは新機能まで失うため、"
    "新機能を保持したままバグだけをコード修正するのが適切）。"
    "メモリ使用率が高い／OOM のログがあれば 'out_of_memory'（推奨は 'escalate'）。"
    "外部依存先の障害に見えるなら 'dependency_5xx'（ロールバックは無意味なので 'escalate'）。"
    "起動時クラッシュの繰り返しは 'crash_loop'（直近デプロイ起因なら 'rollback'、そうでなければ 'escalate'）。"
    "デプロイが無いのにリクエストが異常に多いなら 'traffic_spike'。"
    "根拠が不十分なら低い confidence の 'unknown'。"
    "confidence は 0〜1 で本当の確信度を反映すること。"
    "evidence_log_lines には最も関連するログ行を入れること。"
    "重要: category と recommended_action はスキーマの英語コード値のままにし、"
    "reasoning フィールドは必ず日本語で簡潔に書くこと。"
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
