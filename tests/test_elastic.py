"""Elastic 統合の hermetic テスト（elasticsearch パッケージ不要・モック注入）。

ElasticStore（index/類似検索/失敗時の握りつぶし）と、loop への文脈注入＋蓄積の配線を検証。
"""
import unittest
from dataclasses import replace

from agent.config import Config
from agent.elastic_store import ElasticStore, signature_text
from agent.learn import InMemoryStore
from agent.loop import LoopDeps, run_cycle
from agent.models import ActionType, Category, Decision, Diagnosis, Incident, Observation


def _obs(error_rate=0.9, secs=60):
    return Observation(
        service="sample-service", error_rate=error_rate, seconds_since_last_deploy=secs,
        last_healthy_revision="sample-service-00001-healthy",
        current_revision="sample-service-00002-bad", recent_error_logs=["synthetic 500"],
    )


def _incident():
    return Incident(
        id="abc123", timestamp="2026-06-25T01:00:00+00:00", observation=_obs(),
        diagnosis=Diagnosis(category=Category.bad_deploy, confidence=0.9,
                            recommended_action=ActionType.rollback),
        decision=Decision(action=ActionType.rollback, target_service="sample-service",
                          target_revision="sample-service-00001-healthy"),
        outcome="resolved",
    )


class FakeIndices:
    def __init__(self):
        self.created = []
        self._exists = False

    def exists(self, index):
        return self._exists

    def create(self, index, mappings):
        self.created.append((index, mappings))
        self._exists = True


class FakeES:
    def __init__(self, hits=None):
        self.indices = FakeIndices()
        self.indexed = []
        self._hits = hits or []
        self.last_query = None

    def index(self, index, id, document):
        self.indexed.append((index, id, document))

    def search(self, index, size, query):
        self.last_query = query
        return {"hits": {"hits": [{"_source": h} for h in self._hits]}}


CFG = replace(Config(), elastic_index="runguard-incidents")


class TestSignature(unittest.TestCase):
    def test_signature_contains_signals(self):
        s = signature_text(_obs(), "bad_deploy", "rollback", "resolved")
        self.assertIn("category=bad_deploy", s)
        self.assertIn("deploy=post-deploy", s)
        self.assertIn("error_rate=0.90", s)


class TestElasticStore(unittest.TestCase):
    def test_index_incident(self):
        es = FakeES()
        ok = ElasticStore(CFG, client=es).index_incident(_incident())
        self.assertTrue(ok)
        self.assertEqual(len(es.indexed), 1)
        _, doc_id, doc = es.indexed[0]
        self.assertEqual(doc_id, "abc123")
        self.assertEqual(doc["category"], "bad_deploy")
        self.assertIn("category=bad_deploy", doc["signature"])

    def test_similar_context_uses_semantic_query(self):
        es = FakeES(hits=[{"category": "bad_deploy", "action": "rollback",
                           "outcome": "resolved", "timestamp": "2026-06-24T00:00:00Z"}])
        ctx = ElasticStore(CFG, client=es).similar_incidents_context(_obs(), k=3)
        self.assertIn("category=bad_deploy", ctx)
        self.assertIn("semantic", es.last_query)        # semantic_text 経路

    def test_index_failure_is_swallowed(self):
        class Boom(FakeES):
            def index(self, **kw):
                raise RuntimeError("elastic down")

        self.assertFalse(ElasticStore(CFG, client=Boom()).index_incident(_incident()))


class FakeElastic:
    def __init__(self):
        self.indexed = []

    def similar_incidents_context(self, obs, k=3):
        return "類似の過去インシデント（Elastic semantic 検索）:\n- category=bad_deploy action=rollback outcome=resolved (t)"

    def index_incident(self, incident):
        self.indexed.append(incident)
        return True


class CapturingLLM:
    def __init__(self, diagnosis):
        self._d = diagnosis
        self.prompt = None

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        self.prompt = prompt
        return self._d


class TestLoopWithElastic(unittest.TestCase):
    def test_context_injected_and_incident_indexed(self):
        cfg = replace(Config(), dry_run=True, auto_act_threshold=0.8, error_rate_threshold=0.1,
                      target_services=("sample-service",), agent_service="runguard-agent",
                      max_actions_per_incident=1, cooldown_seconds=300, verify_wait_seconds=0)
        llm = CapturingLLM(Diagnosis(category=Category.bad_deploy, confidence=0.95,
                                     recommended_action=ActionType.rollback))
        fake_elastic = FakeElastic()
        deps = LoopDeps(observe=lambda s: _obs(0.9), llm=llm, store=InMemoryStore(),
                        cfg=cfg, elastic=fake_elastic)
        inc = run_cycle("sample-service", deps)
        self.assertIsNotNone(inc)
        self.assertIn("類似の過去インシデント", llm.prompt)   # Elastic 文脈が diagnose に注入
        self.assertEqual(len(fake_elastic.indexed), 1)      # インシデントが Elastic に蓄積


if __name__ == "__main__":
    unittest.main()
