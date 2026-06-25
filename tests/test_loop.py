"""Phase 4 hermetic 単体テスト（GCP / 実 Gemini / ADK 不要）。

loop.run_cycle を InMemoryStore とモック observe/llm/executor で検証する。
- 健康サイクル / dry-run ロールバック / ライブ復旧確認 / ループ保護（ティック跨ぎ）。
"""
import unittest
from dataclasses import replace

from agent.actions import ActionResult
from agent.config import Config
from agent.learn import InMemoryStore
from agent.loop import LoopDeps, run_cycle
from agent.models import ActionType, Category, Diagnosis, Observation

SERVICE = "sample-service"


def cfg(dry_run=True):
    return replace(
        Config(),
        dry_run=dry_run,
        auto_act_threshold=0.8,
        error_rate_threshold=0.1,
        target_services=(SERVICE,),
        agent_service="runguard-agent",
        max_actions_per_incident=1,
        cooldown_seconds=300,
        verify_wait_seconds=0,
    )


def make_obs(error_rate, seconds_since_deploy=90, healthy="sample-service-00001-healthy"):
    return Observation(
        service=SERVICE,
        error_rate=error_rate,
        request_count=500,
        last_healthy_revision=healthy,
        current_revision="sample-service-00002-bad",
        seconds_since_last_deploy=seconds_since_deploy,
        recent_error_logs=["synthetic 500"],
    )


class SequenceObserve:
    """呼ばれるたびに用意した Observation を順に返す observe モック。"""

    def __init__(self, *observations):
        self._obs = list(observations)
        self.calls = 0

    def __call__(self, service):
        self.calls += 1
        idx = min(self.calls - 1, len(self._obs) - 1)
        return self._obs[idx]


class FakeLLM:
    def __init__(self, diagnosis):
        self._d = diagnosis

    def generate_structured(self, *, prompt, schema, system_instruction=None):
        return self._d


def bad_deploy_diag(confidence=0.9):
    return Diagnosis(category=Category.bad_deploy, confidence=confidence,
                     recommended_action=ActionType.rollback)


class TestRunCycle(unittest.TestCase):
    def test_healthy_cycle_no_incident(self):
        store = InMemoryStore()
        deps = LoopDeps(observe=SequenceObserve(make_obs(0.0)),
                        llm=FakeLLM(bad_deploy_diag()), store=store, cfg=cfg())
        incident = run_cycle(SERVICE, deps)
        self.assertIsNone(incident)
        self.assertEqual(store.list_incidents(), [])

    def test_bad_deploy_dry_run(self):
        store = InMemoryStore()
        deps = LoopDeps(observe=SequenceObserve(make_obs(0.9)),
                        llm=FakeLLM(bad_deploy_diag(0.95)), store=store, cfg=cfg(dry_run=True))
        incident = run_cycle(SERVICE, deps)
        self.assertIsNotNone(incident)
        self.assertEqual(incident.decision.action, ActionType.rollback)
        self.assertFalse(incident.decision.requires_human)
        self.assertEqual(incident.outcome, "dry_run")
        self.assertEqual(len(store.list_incidents()), 1)
        self.assertIn("bad_deploy -> rollback", store.playbook_context())

    def test_live_rollback_verifies_resolved(self):
        store = InMemoryStore()
        # 1回目=障害, 2回目(verify)=復旧
        observe = SequenceObserve(make_obs(0.9), make_obs(0.0))

        def fake_executor(decision, c, **kw):
            return ActionResult(action=ActionType.rollback, executed=True, dry_run=False,
                                target_service=SERVICE, target_revision="sample-service-00001-healthy",
                                message="rolled back")

        deps = LoopDeps(observe=observe, llm=FakeLLM(bad_deploy_diag(0.95)),
                        store=store, cfg=cfg(dry_run=False), executor=fake_executor)
        incident = run_cycle(SERVICE, deps)
        self.assertEqual(incident.outcome, "resolved")
        self.assertEqual(observe.calls, 2)  # observe → (act) → verify-observe

    def test_loop_guard_blocks_second_rollback(self):
        store = InMemoryStore()
        c = cfg(dry_run=True)
        # 同じストアで連続2サイクル（どちらも障害継続）
        deps1 = LoopDeps(observe=SequenceObserve(make_obs(0.9)),
                         llm=FakeLLM(bad_deploy_diag(0.95)), store=store, cfg=c,
                         now=lambda: "2026-06-25T01:00:00+00:00")
        first = run_cycle(SERVICE, deps1)
        self.assertEqual(first.outcome, "dry_run")

        deps2 = LoopDeps(observe=SequenceObserve(make_obs(0.9)),
                         llm=FakeLLM(bad_deploy_diag(0.95)), store=store, cfg=c,
                         now=lambda: "2026-06-25T01:01:00+00:00")  # 60秒後 < cooldown 300
        second = run_cycle(SERVICE, deps2)
        self.assertEqual(second.outcome, "loop_guard")
        self.assertEqual(len(store.list_incidents()), 2)


if __name__ == "__main__":
    unittest.main()
