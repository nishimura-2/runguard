"""eval 用モック（GCP/Gemini なしで hermetic に評価するため）。"""
from __future__ import annotations

from agent.models import Diagnosis, Observation


class ScriptedLLM:
    """与えた Diagnosis をそのまま返す（プロンプトは無視）。decide 層の評価に使う。"""

    def __init__(self, diagnosis: Diagnosis):
        self._d = diagnosis

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        return self._d


def build_observation(d: dict) -> Observation:
    return Observation(**d)
