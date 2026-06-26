"""Phase 2 hermetic 単体テスト（GCP / 実 Gemini 不要）。

Gemini はモック（RecordingLLM）を注入し、diagnose の配線・プロンプト内容・モデル検証を確認する。
"""
import unittest

from agent.diagnose import SYSTEM_INSTRUCTION, build_prompt, diagnose
from agent.models import ActionType, Category, Diagnosis, Observation
from agent.observe import error_rate, seconds_between


class RecordingLLM:
    """Gemini の代役。呼び出しを記録し、固定の戻り値を返す。"""

    def __init__(self, to_return):
        self.to_return = to_return
        self.calls = []

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        self.calls.append(
            {"prompt": prompt, "schema": schema, "system_instruction": system_instruction}
        )
        return self.to_return


def make_bad_deploy_observation() -> Observation:
    return Observation(
        service="sample-service",
        window_minutes=5,
        error_rate=0.92,
        request_count=500,
        p95_latency_ms=120,
        memory_ratio=0.30,
        instances=2,
        recent_error_logs=[
            '{"severity":"ERROR","message":"synthetic 500 (FAULT=http500)","status":500}',
            '{"severity":"ERROR","message":"synthetic 500 (FAULT=http500)","status":500}',
        ],
        last_deploy_at="2026-06-25T01:00:00+00:00",
        seconds_since_last_deploy=90,
        current_revision="sample-service-00002-bad",
        last_healthy_revision="sample-service-00001-healthy",
        observed_at="2026-06-25T01:01:30+00:00",
    )


class TestBuildPrompt(unittest.TestCase):
    def test_prompt_contains_key_signals(self):
        prompt = build_prompt(make_bad_deploy_observation())
        self.assertIn("error_rate", prompt)
        self.assertIn("0.920", prompt)
        self.assertIn("分前", prompt)                 # デプロイ直後シグナルが伝わる
        self.assertIn("synthetic 500", prompt)         # エラーログ行が含まれる
        self.assertIn("last_healthy_revision", prompt)

    def test_prompt_includes_playbook_context(self):
        prompt = build_prompt(
            make_bad_deploy_observation(),
            playbook_context="直後デプロイ+5xx急増 -> rollback が有効",
        )
        self.assertIn("プレイブック", prompt)
        self.assertIn("rollback が有効", prompt)


class TestDiagnose(unittest.TestCase):
    def test_diagnose_uses_schema_and_returns_result(self):
        expected = Diagnosis(
            category=Category.bad_deploy,
            confidence=0.9,
            evidence_log_lines=["synthetic 500"],
            reasoning="errors began right after deploy",
            recommended_action=ActionType.rollback,
        )
        llm = RecordingLLM(expected)
        out = diagnose(make_bad_deploy_observation(), llm)
        self.assertEqual(out, expected)
        self.assertEqual(len(llm.calls), 1)
        self.assertIs(llm.calls[0]["schema"], Diagnosis)
        self.assertEqual(llm.calls[0]["system_instruction"], SYSTEM_INSTRUCTION)

    def test_diagnose_accepts_dict_return(self):
        llm = RecordingLLM(
            {
                "category": "unknown",
                "confidence": 0.1,
                "evidence_log_lines": [],
                "reasoning": "n/a",
                "recommended_action": "escalate",
            }
        )
        out = diagnose(make_bad_deploy_observation(), llm)
        self.assertIsInstance(out, Diagnosis)
        self.assertEqual(out.category, Category.unknown)


class TestModels(unittest.TestCase):
    def test_confidence_out_of_range_rejected(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            Diagnosis(category=Category.unknown, confidence=1.5)

    def test_round_trip_from_json_text(self):
        text = (
            '{"category":"bad_deploy","confidence":0.88,'
            '"evidence_log_lines":["x"],"reasoning":"r","recommended_action":"rollback"}'
        )
        d = Diagnosis.model_validate_json(text)
        self.assertEqual(d.category, Category.bad_deploy)
        self.assertEqual(d.recommended_action, ActionType.rollback)


class TestFallbackLLM(unittest.TestCase):
    def test_falls_back_on_primary_error(self):
        from agent.gemini_client import FallbackLLM, RuleBasedLLM

        class Boom:
            def generate_structured(self, **kw):
                raise RuntimeError("gemini down")

        fb = FallbackLLM(Boom(), RuleBasedLLM())
        d = fb.generate_structured(prompt=build_prompt(make_bad_deploy_observation()), schema=Diagnosis)
        self.assertEqual(d.category, Category.bad_deploy)   # RuleBased が拾う
        self.assertIn("fallback", fb.last_used)


class TestObserveHelpers(unittest.TestCase):
    def test_error_rate(self):
        self.assertEqual(error_rate(0, 0), 0.0)
        self.assertEqual(error_rate(50, 100), 0.5)
        self.assertEqual(error_rate(200, 100), 1.0)   # clamp

    def test_seconds_between(self):
        self.assertEqual(
            seconds_between("2026-06-25T01:00:00Z", "2026-06-25T01:01:30Z"), 90
        )
        self.assertIsNone(seconds_between(None, "2026-06-25T01:01:30Z"))


if __name__ == "__main__":
    unittest.main()
