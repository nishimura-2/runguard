"""ADK 主動線のツールロジックを hermetic に検証（ADK ランタイム / 実 Gemini 不要）。

adk_app.observe_and_diagnose / act / propose_fix は ADK ツールの中身。ここでは ADK Runner を
使わず、エージェントのツール呼び出し列（observe → act/propose）を直接再現して、
確定パイプラインと同じ結果（rollback/scale/self_heal 提案）＋インシデント記録になることを確認する。
"""
import unittest
from dataclasses import replace

from agent.adk_app import act, build_deps, observe_and_diagnose, propose_fix
from agent.config import Config
from agent.gemini_client import RuleBasedLLM
from agent.learn import InMemoryStore
from agent.models import ActionType
from agent.sim import SimEnvironment

SERVICE = "sample-service"


def cfg():
    return replace(
        Config(), mode="sim", dry_run=False, auto_act_threshold=0.8,
        error_rate_threshold=0.1, target_services=(SERVICE,),
        agent_service="runguard-agent", verify_wait_seconds=0,
    )


def _setup():
    c = cfg()
    be = SimEnvironment(service=SERVICE)
    store = InMemoryStore()
    deps = build_deps(c, be, store, RuleBasedLLM())
    return c, be, store, deps


class TestAdkTools(unittest.TestCase):
    def test_observe_then_rollback_resolves(self):
        c, be, store, deps = _setup()
        be.inject_fault()                       # http500 → bad_deploy
        ctx = {}
        info = observe_and_diagnose(c, be, deps, ctx, SERVICE)
        self.assertEqual(info["category"], "bad_deploy")
        self.assertEqual(info["recommended_action"], "rollback")
        res = act(c, be, deps, ctx, ActionType.rollback)
        self.assertEqual(res["action"], "rollback")
        self.assertEqual(res["outcome"], "resolved")

    def test_observe_then_scale_memory_resolves(self):
        c, be, store, deps = _setup()
        be.inject_oom()
        ctx = {}
        info = observe_and_diagnose(c, be, deps, ctx, SERVICE)
        self.assertEqual(info["category"], "out_of_memory")
        self.assertEqual(info["recommended_action"], "scale_memory")
        res = act(c, be, deps, ctx, ActionType.scale_memory)
        self.assertEqual(res["outcome"], "resolved")
        self.assertEqual(be.snapshot()["memory_mib"], 512)

    def test_observe_then_scale_instances_resolves(self):
        c, be, store, deps = _setup()
        be.inject_traffic_spike()
        ctx = {}
        observe_and_diagnose(c, be, deps, ctx, SERVICE)
        res = act(c, be, deps, ctx, ActionType.scale_instances)
        self.assertEqual(res["outcome"], "resolved")
        self.assertEqual(be.snapshot()["max_instances"], 5)

    def test_feature_bug_proposes_fix(self):
        c, be, store, deps = _setup()
        be.inject_feature_bug()
        ctx = {}
        info = observe_and_diagnose(c, be, deps, ctx, SERVICE)
        self.assertEqual(info["category"], "feature_bug")
        self.assertEqual(info["recommended_action"], "self_heal")
        res = propose_fix(c, be, deps, ctx)
        self.assertEqual(res["action"], "self_heal")
        self.assertEqual(res["outcome"], "awaiting_approval")
        self.assertEqual(len(store.list_incidents()), 1)
        self.assertIsNotNone(store.list_incidents()[0].fix)

    def test_act_without_observe_errors(self):
        c, be, store, deps = _setup()
        res = act(c, be, deps, {}, ActionType.rollback)
        self.assertIn("error", res)

    def test_healthy_no_action(self):
        c, be, store, deps = _setup()
        ctx = {}
        observe_and_diagnose(c, be, deps, ctx, SERVICE)   # 健全
        res = act(c, be, deps, ctx, ActionType.rollback)
        self.assertIn("note", res)
        self.assertEqual(store.list_incidents(), [])


if __name__ == "__main__":
    unittest.main()
