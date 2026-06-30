"""RunGuard のドメインモデル（Pydantic v2）。

- Diagnosis は Gemini の構造化出力スキーマ（response_schema）として使うため、
  ネストした BaseModel を持たない「フラット」構造にする（python-genai の既知の制約回避）。
- Incident は永続化用なので Observation/Diagnosis/Decision をネストしてよい。
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    bad_deploy = "bad_deploy"
    feature_bug = "feature_bug"          # 新機能のバグ（ロールバックでは新機能を失う → コード修正が適切）
    out_of_memory = "out_of_memory"
    dependency_5xx = "dependency_5xx"
    crash_loop = "crash_loop"
    traffic_spike = "traffic_spike"
    unknown = "unknown"


class ActionType(str, Enum):
    rollback = "rollback"
    self_heal = "self_heal"              # AI がソースのバグだけ修正して新リビジョンをデプロイ（新機能は維持）
    escalate = "escalate"
    none = "none"


class Observation(BaseModel):
    service: str
    window_minutes: int = 5
    error_rate: float = 0.0                       # 0..1（5xx 率）
    request_count: int = 0
    p95_latency_ms: float = 0.0
    memory_ratio: float = 0.0                     # 0..1（メモリ使用率）
    instances: int = 0
    recent_error_logs: list[str] = Field(default_factory=list)
    last_deploy_at: Optional[str] = None          # ISO8601
    seconds_since_last_deploy: Optional[int] = None
    current_revision: Optional[str] = None
    last_healthy_revision: Optional[str] = None
    observed_at: Optional[str] = None             # ISO8601
    faulty_source: Optional[str] = None           # 不調リビジョンの該当ソース（取得できた場合）。self_heal の入力。


class Diagnosis(BaseModel):
    """Gemini の構造化出力（フラット構造）。"""
    category: Category = Category.unknown
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence_log_lines: list[str] = Field(default_factory=list)
    reasoning: str = ""
    recommended_action: ActionType = ActionType.escalate


class CodeFix(BaseModel):
    """self_heal で Gemini が生成するコード修正（フラット構造＝構造化出力スキーマ）。

    diff は LLM に書かせず、faulty_source と fixed_source から difflib で機械生成する
    （フォーマット崩れを避けるため）。
    """
    summary: str = ""                 # 一言サマリ（日本語）
    bug_explanation: str = ""         # 何が原因で 5xx になっていたか（日本語）
    fixed_source: str = ""            # 修正後の完全なソース（新機能は維持・バグだけ修正）
    kept_feature: bool = True         # 新機能を維持したか


class Decision(BaseModel):
    action: ActionType = ActionType.escalate
    target_service: Optional[str] = None
    target_revision: Optional[str] = None
    reason: str = ""
    requires_human: bool = False
    dry_run: bool = True


class Incident(BaseModel):
    id: str
    timestamp: str                                # ISO8601
    observation: Observation
    diagnosis: Diagnosis
    decision: Decision
    outcome: Optional[str] = None                 # resolved / not_resolved / escalated / dry_run / awaiting_approval / self_healed
    human_override: Optional[str] = None
    context_used: Optional[str] = None            # この診断が参照した過去の知見（学習→診断の可視化）
    fix: Optional[CodeFix] = None                 # self_heal で AI が提案/適用したコード修正
    fix_diff: Optional[str] = None                # faulty_source → fixed_source の unified diff（可視化用）
