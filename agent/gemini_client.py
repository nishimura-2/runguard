"""Gemini 呼び出しの抽象（差し替え可能）。

- LLMClient プロトコルを介して diagnose 等から使う。テストではモックを注入する。
- 実クライアントは google-genai を「遅延 import」する（未インストールでも本モジュールは import 可能）。
- 経路は config.use_vertexai で切替（Vertex 推奨 / 開発API）。
- 構造化出力は response.text を Pydantic で検証して取り出す（response.parsed の版差に依存しない）。
"""
from __future__ import annotations

import re
from typing import Optional, Protocol, Type

from pydantic import BaseModel

from agent.config import Config
from agent.config import config as default_config


class LLMClient(Protocol):
    def generate_structured(
        self,
        *,
        prompt: str,
        schema: Type[BaseModel],
        system_instruction: Optional[str] = None,
    ) -> BaseModel:
        ...


class GeminiClient:
    """google-genai 経由の実装。"""

    def __init__(self, cfg: Config = default_config):
        self._cfg = cfg
        self._client = None

    def _ensure_client(self):
        if self._client is None:
            from google import genai  # 遅延 import
            cfg = self._cfg
            if cfg.use_vertexai:
                self._client = genai.Client(
                    vertexai=True,
                    project=cfg.google_cloud_project,
                    location=cfg.google_cloud_location,
                )
            else:
                # GEMINI_API_KEY / GOOGLE_API_KEY を env から読む
                self._client = genai.Client()
        return self._client

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        from google.genai import types  # 遅延 import
        client = self._ensure_client()
        resp = client.models.generate_content(
            model=self._cfg.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=schema,
                system_instruction=system_instruction,
            ),
        )
        parsed = getattr(resp, "parsed", None)
        if isinstance(parsed, schema):
            return parsed
        return schema.model_validate_json(resp.text)


def make_llm_client(cfg: Config = default_config) -> LLMClient:
    return GeminiClient(cfg)


def _extract_float(text: str, pattern: str):
    m = re.search(pattern, text)
    return float(m.group(1)) if m else None


def _extract_int(text: str, pattern: str):
    m = re.search(pattern, text)
    return int(m.group(1)) if m else None


class RuleBasedLLM:
    """オフライン/フォールバック用の規則ベース診断（Gemini 不要）。

    diagnose.build_prompt が出力するテキストから主要数値を読み、決め打ちで Diagnosis を返す。
    GCP/Gemini 未接続のローカルデモや、API エラー時のフォールバックに使う。
    """

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        er = _extract_float(prompt, r"error_rate \(5xx\):\s*([0-9.]+)") or 0.0
        mem = _extract_float(prompt, r"memory_ratio:\s*([0-9.]+)") or 0.0
        secs = _extract_int(prompt, r"\((\d+) 秒前")
        recent_deploy = secs is not None and secs <= 600
        exception_in_logs = any(
            kw in prompt for kw in ("Traceback", "ZeroDivisionError", "Exception", "Error:")
        )
        # アプリのコード例外(traceback)で 5xx → 新機能のバグ。ロールバックより self_heal。
        if er >= 0.3 and exception_in_logs:
            return schema(category="feature_bug", confidence=0.9,
                          reasoning="アプリのコード例外(traceback)で 5xx。新機能のバグと判断（規則ベース）。",
                          recommended_action="self_heal", evidence_log_lines=[])
        if er >= 0.3 and recent_deploy:
            return schema(category="bad_deploy", confidence=0.9,
                          reasoning="エラー急増がデプロイ直後（規則ベース）。",
                          recommended_action="rollback", evidence_log_lines=[])
        if mem >= 0.85:
            return schema(category="out_of_memory", confidence=0.8,
                          reasoning="メモリ使用率が高い（規則ベース）。メモリ上限引上げで復旧可能。",
                          recommended_action="scale_memory")
        req = _extract_int(prompt, r"request_count:\s*(\d+)") or 0
        if er >= 0.3 and req >= 2000 and not recent_deploy:
            return schema(category="traffic_spike", confidence=0.8,
                          reasoning="デプロイ無し＋リクエスト急増（規則ベース）。インスタンス増で復旧可能。",
                          recommended_action="scale_instances")
        if er >= 0.3:
            return schema(category="dependency_5xx", confidence=0.6,
                          reasoning="デプロイ相関のない 5xx（規則ベース）。",
                          recommended_action="escalate")
        return schema(category="unknown", confidence=0.2,
                      reasoning="判断材料不足（規則ベース）。",
                      recommended_action="escalate")


class FallbackLLM:
    """primary を試し、失敗したら fallback に切替（実 Gemini 障害でもデモ/ループを止めない）。"""

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback
        self.last_used = type(primary).__name__

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        try:
            out = self._primary.generate_structured(
                prompt=prompt, schema=schema, system_instruction=system_instruction)
            self.last_used = type(self._primary).__name__
            return out
        except Exception:
            self.last_used = type(self._fallback).__name__ + "(fallback)"
            return self._fallback.generate_structured(
                prompt=prompt, schema=schema, system_instruction=system_instruction)


def select_llm(cfg: Config = default_config) -> LLMClient:
    """Vertex/開発API が使える設定なら Gemini（失敗時 RuleBased に退避）、無ければ RuleBased。"""
    import os
    configured = (
        (cfg.use_vertexai and bool(cfg.google_cloud_project))
        or (not cfg.use_vertexai and bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")))
    )
    return FallbackLLM(GeminiClient(cfg), RuleBasedLLM()) if configured else RuleBasedLLM()
