"""Phase 5 hermetic テスト（FastAPI TestClient・GCP/Gemini 不要・sim モード）。

inject → tick で「障害注入 → bad_deploy 診断 → rollback → 復旧(resolved)」の一連を検証。
"""
import unittest

from fastapi.testclient import TestClient

import agent.main as m
from agent.gemini_client import RuleBasedLLM
from agent.diagnose import build_prompt
from agent.models import Category, Diagnosis, Observation


class TestRuleBasedLLM(unittest.TestCase):
    def test_offline_bad_deploy(self):
        obs = Observation(service="s", error_rate=0.9, seconds_since_last_deploy=30,
                          last_deploy_at="2026-06-25T01:00:00+00:00",
                          recent_error_logs=["x"])
        d = RuleBasedLLM().generate_structured(
            prompt=build_prompt(obs), schema=Diagnosis)
        self.assertEqual(d.category, Category.bad_deploy)
        self.assertGreaterEqual(d.confidence, 0.8)


class TestApi(unittest.TestCase):
    def setUp(self):
        self.c = TestClient(m.app)
        self.c.post("/api/reset")

    def test_initial_state_healthy(self):
        s = self.c.get("/api/state").json()
        self.assertFalse(s["backend"]["injected"])
        self.assertEqual(s["backend"]["error_rate"], 0.0)
        self.assertEqual(s["config"]["llm"], "RuleBasedLLM")  # オフライン

    def test_inject_then_tick_recovers(self):
        self.c.post("/api/inject")
        s = self.c.get("/api/state").json()
        self.assertTrue(s["backend"]["injected"])
        self.assertGreater(s["backend"]["error_rate"], 0.1)

        t = self.c.post("/api/tick").json()
        self.assertIsNotNone(t["incident"])
        self.assertEqual(t["incident"]["diagnosis"]["category"], "bad_deploy")
        self.assertEqual(t["incident"]["decision"]["action"], "rollback")
        self.assertEqual(t["incident"]["outcome"], "resolved")
        self.assertFalse(t["backend"]["injected"])  # 復旧

    def test_dashboard_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("RunGuard", r.text)


if __name__ == "__main__":
    unittest.main()
