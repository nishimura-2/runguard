"""Gemini 呼び出しの抽象（差し替え可能）。

- LLMClient プロトコルを介して diagnose 等から使う。テストではモックを注入する。
- 実クライアントは google-genai を「遅延 import」する（未インストールでも本モジュールは import 可能）。
- 経路は config.use_vertexai で切替（Vertex 推奨 / 開発API）。
- 構造化出力は response.text を Pydantic で検証して取り出す（response.parsed の版差に依存しない）。
"""
from __future__ import annotations

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
