"""Phase 3 hermetic 単体テスト（GCP 不要）。

decide（確信度ゲート/allowlist/対応表）・actions（dry-run/ループ保護/自己除外）・verify を検証。
config は dataclasses.replace で固定し、環境変数に依存しない。
"""
import unittest
from dataclasses import replace

from agent.actions import ActionResult, execute, guard_allows_action
from agent.config import Config
from agent.decide import decide
from agent.models import ActionType, Category, Decision, Diagnosis, Observation
from agent.verify import is_recovered, verify_outcome

CFG = replace(
    Config(),
    dry_run=True,
    auto_act_threshold=0.8,
    target_services=("sample-service",),
    agent_service="runguard-agent",
    max_actions_per_incident=1,
    cooldown_seconds=300,
    error_rate_threshold=0.1,
)


def obs(service="sample-service", healthy="sample-service-00001-healthy",
        seconds_since_deploy=90):
    return Observation(
        service=service,
        error_rate=0.9,
        request_count=500,
        last_healthy_revision=healthy,
        current_revision="sample-service-00002-bad",
        seconds_since_last_deploy=seconds_since_deploy,
    )


def diag(category=Category.bad_deploy, confidence=0.9):
    return Diagnosis(category=category, confidence=confidence,
                     recommended_action=ActionType.rollback)


class FakeBackend:
    """execute の live ディスパッチ確認用。呼ばれたメソッドを記録する。"""

    def __init__(self):
        self.calls = []

    def apply_rollback(self, cfg, service, revision):
        self.calls.append(("rollback", service, revision))

    def scale_memory(self, cfg, service):
        self.calls.append(("scale_memory", service))

    def scale_instances(self, cfg, service):
        self.calls.append(("scale_instances", service))

    def restart(self, cfg, service):
        self.calls.append(("restart", service))


class TestDecide(unittest.TestCase):
    def test_bad_deploy_high_confidence_auto_rollback(self):
        d = decide(diag(Category.bad_deploy, 0.9), obs(), CFG)
        self.assertEqual(d.action, ActionType.rollback)
        self.assertFalse(d.requires_human)
        self.assertEqual(d.target_revision, "sample-service-00001-healthy")

    def test_low_confidence_escalates(self):
        d = decide(diag(Category.bad_deploy, 0.5), obs(), CFG)
        self.assertEqual(d.action, ActionType.escalate)
        self.assertTrue(d.requires_human)
        self.assertIn("確信度", d.reason)

    def test_service_not_in_allowlist_escalates(self):
        d = decide(diag(Category.bad_deploy, 0.95), obs(service="runguard-agent"), CFG)
        self.assertEqual(d.action, ActionType.escalate)
        self.assertIn("allowlist", d.reason)

    def test_out_of_memory_scales_memory(self):
        d = decide(diag(Category.out_of_memory, 0.95), obs(), CFG)
        self.assertEqual(d.action, ActionType.scale_memory)
        self.assertFalse(d.requires_human)          # 取り消し可能 → 自律実行

    def test_out_of_memory_low_confidence_escalates(self):
        d = decide(diag(Category.out_of_memory, 0.5), obs(), CFG)
        self.assertEqual(d.action, ActionType.escalate)
        self.assertIn("確信度", d.reason)

    def test_traffic_spike_scales_instances(self):
        d = decide(diag(Category.traffic_spike, 0.9), obs(), CFG)
        self.assertEqual(d.action, ActionType.scale_instances)
        self.assertFalse(d.requires_human)

    def test_dependency_5xx_escalates(self):
        d = decide(diag(Category.dependency_5xx, 0.9), obs(), CFG)
        self.assertEqual(d.action, ActionType.escalate)   # 自動対応の手段なし → 人へ

    def test_scale_not_in_allowlist_escalates(self):
        d = decide(diag(Category.out_of_memory, 0.95), obs(service="runguard-agent"), CFG)
        self.assertEqual(d.action, ActionType.escalate)
        self.assertIn("allowlist", d.reason)

    def test_feature_bug_with_source_self_heals(self):
        o = obs()
        o.faulty_source = "def handle_price(s, q):\n    return s / q\n"
        d = decide(diag(Category.feature_bug, 0.9), o, CFG)
        self.assertEqual(d.action, ActionType.self_heal)
        self.assertTrue(d.requires_human)             # コード出荷は必ず承認ゲート
        self.assertEqual(d.target_service, "sample-service")

    def test_feature_bug_without_source_escalates(self):
        d = decide(diag(Category.feature_bug, 0.95), obs(), CFG)  # faulty_source なし
        self.assertEqual(d.action, ActionType.escalate)
        self.assertIn("ソース", d.reason)

    def test_feature_bug_low_confidence_escalates(self):
        o = obs()
        o.faulty_source = "x"
        d = decide(diag(Category.feature_bug, 0.5), o, CFG)
        self.assertEqual(d.action, ActionType.escalate)
        self.assertIn("確信度", d.reason)

    def test_crash_loop_recent_deploy_rolls_back(self):
        d = decide(diag(Category.crash_loop, 0.9), obs(seconds_since_deploy=120), CFG)
        self.assertEqual(d.action, ActionType.rollback)

    def test_crash_loop_old_deploy_restarts(self):
        d = decide(diag(Category.crash_loop, 0.9), obs(seconds_since_deploy=99999), CFG)
        self.assertEqual(d.action, ActionType.restart)
        self.assertFalse(d.requires_human)          # 取り消し可能 → 自律実行

    def test_missing_healthy_revision_escalates(self):
        d = decide(diag(Category.bad_deploy, 0.95), obs(healthy=None), CFG)
        self.assertEqual(d.action, ActionType.escalate)
        self.assertIn("last_healthy_revision", d.reason)


class TestGuard(unittest.TestCase):
    def test_max_actions_blocks(self):
        ok, why = guard_allows_action(
            actions_taken_this_incident=1, seconds_since_last_action=None, cfg=CFG)
        self.assertFalse(ok)
        self.assertIn("最大アクション数", why)

    def test_cooldown_blocks(self):
        ok, why = guard_allows_action(
            actions_taken_this_incident=0, seconds_since_last_action=100, cfg=CFG)
        self.assertFalse(ok)
        self.assertIn("クールダウン", why)

    def test_allows_when_clear(self):
        ok, why = guard_allows_action(
            actions_taken_this_incident=0, seconds_since_last_action=None, cfg=CFG)
        self.assertTrue(ok)
        self.assertIsNone(why)


class TestExecute(unittest.TestCase):
    def _auto_rollback_decision(self):
        return decide(diag(Category.bad_deploy, 0.9), obs(), CFG)

    def test_dry_run_rollback_does_not_execute(self):
        res = execute(self._auto_rollback_decision(), CFG)
        self.assertEqual(res.action, ActionType.rollback)
        self.assertFalse(res.executed)
        self.assertTrue(res.dry_run)
        self.assertIn("DRY_RUN", res.message)

    def test_requires_human_skips(self):
        dec = Decision(action=ActionType.rollback, target_service="sample-service",
                       target_revision="r1", requires_human=True, dry_run=True)
        res = execute(dec, CFG)
        self.assertFalse(res.executed)
        self.assertEqual(res.skipped_reason, "requires_human")

    def test_self_service_blocked(self):
        dec = Decision(action=ActionType.rollback, target_service="runguard-agent",
                       target_revision="r1", requires_human=False, dry_run=True)
        res = execute(dec, CFG)
        self.assertFalse(res.executed)
        self.assertEqual(res.skipped_reason, "not_allowed")

    def test_loop_guard_blocks(self):
        res = execute(self._auto_rollback_decision(), CFG,
                      actions_taken_this_incident=1)
        self.assertFalse(res.executed)
        self.assertEqual(res.skipped_reason, "loop_guard")

    def test_escalate_recorded(self):
        dec = Decision(action=ActionType.escalate, target_service="sample-service",
                       reason="dependency down", requires_human=True, dry_run=True)
        res = execute(dec, CFG)
        self.assertTrue(res.executed)
        self.assertEqual(res.action, ActionType.escalate)

    def test_scale_memory_executes_live(self):
        be = FakeBackend()
        dec = Decision(action=ActionType.scale_memory, target_service="sample-service",
                       requires_human=False, dry_run=False)
        res = execute(dec, replace(CFG, dry_run=False), backend=be)
        self.assertTrue(res.executed)
        self.assertEqual(res.action, ActionType.scale_memory)
        self.assertIn(("scale_memory", "sample-service"), be.calls)

    def test_scale_instances_executes_live(self):
        be = FakeBackend()
        dec = Decision(action=ActionType.scale_instances, target_service="sample-service",
                       requires_human=False, dry_run=False)
        res = execute(dec, replace(CFG, dry_run=False), backend=be)
        self.assertTrue(res.executed)
        self.assertIn(("scale_instances", "sample-service"), be.calls)

    def test_restart_executes_live(self):
        be = FakeBackend()
        dec = Decision(action=ActionType.restart, target_service="sample-service",
                       requires_human=False, dry_run=False)
        res = execute(dec, replace(CFG, dry_run=False), backend=be)
        self.assertTrue(res.executed)
        self.assertIn(("restart", "sample-service"), be.calls)

    def test_scale_dry_run_does_not_call_backend(self):
        be = FakeBackend()
        dec = Decision(action=ActionType.scale_memory, target_service="sample-service",
                       requires_human=False, dry_run=True)
        res = execute(dec, CFG, backend=be)         # CFG は dry_run=True
        self.assertFalse(res.executed)
        self.assertIn("DRY_RUN", res.message)
        self.assertEqual(be.calls, [])

    def test_scale_not_allowed_skips(self):
        be = FakeBackend()
        dec = Decision(action=ActionType.scale_memory, target_service="runguard-agent",
                       requires_human=False, dry_run=False)
        res = execute(dec, replace(CFG, dry_run=False), backend=be)
        self.assertEqual(res.skipped_reason, "not_allowed")
        self.assertEqual(be.calls, [])


class TestVerify(unittest.TestCase):
    def test_recovered(self):
        self.assertTrue(is_recovered(Observation(service="s", error_rate=0.02), CFG))
        self.assertEqual(verify_outcome(Observation(service="s", error_rate=0.02), CFG),
                         "resolved")

    def test_not_recovered(self):
        self.assertFalse(is_recovered(Observation(service="s", error_rate=0.8), CFG))
        self.assertEqual(verify_outcome(Observation(service="s", error_rate=0.8), CFG),
                         "not_resolved")


if __name__ == "__main__":
    unittest.main()
