"""real_env の hermetic テスト（run_v2/httpx 不要・クライアント等を注入）。"""
import unittest
from dataclasses import replace

from agent.config import Config
from agent.models import CodeFix
from agent.real_env import (
    RealEnvironment,
    error_rate_from_statuses,
    serving_revision,
    tag_to_revision,
)


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_service(serving="svc-healthy-1", tags=None):
    tags = tags or {"healthy": "svc-healthy-1", "bad": "svc-bad-2"}
    traffic = [Obj(tag=t, revision=r, percent=(100 if r == serving else 0)) for t, r in tags.items()]
    statuses = [Obj(tag=t, revision=r, percent=(100 if r == serving else 0)) for t, r in tags.items()]
    return Obj(traffic=traffic, traffic_statuses=statuses,
               latest_ready_revision=serving, uri="https://svc.example")


FULL_TAGS = {"healthy": "svc-healthy-1", "bad": "svc-bad-2",
             "feature-bug": "svc-fb-3", "fixed": "svc-fixed-4"}


class FakeClient:
    def __init__(self, svc):
        self.svc = svc

    def get_service(self, name):
        return self.svc


CFG = replace(Config(), project_id="p", region="r", target_services=("sample-service",),
              error_rate_threshold=0.1, probe_count=4)


class TestPureHelpers(unittest.TestCase):
    def test_error_rate(self):
        self.assertEqual(error_rate_from_statuses([200, 200, 500, 500]), 0.5)
        self.assertEqual(error_rate_from_statuses([200, 200]), 0.0)
        self.assertEqual(error_rate_from_statuses([]), 0.0)

    def test_tag_resolution(self):
        svc = fake_service()
        self.assertEqual(tag_to_revision(svc, "bad"), "svc-bad-2")
        self.assertEqual(tag_to_revision(svc, "healthy"), "svc-healthy-1")
        self.assertEqual(tag_to_revision(svc, "nope"), "")

    def test_serving_revision(self):
        self.assertEqual(serving_revision(fake_service(serving="svc-bad-2")), "svc-bad-2")


class TestObserve(unittest.TestCase):
    def _env(self, serving, codes, route_sink=None):
        return RealEnvironment(
            CFG, services_client=FakeClient(fake_service(serving=serving)),
            prober=lambda u, n: list(codes), log_fetcher=lambda: ["synthetic 500"],
            route_fn=(route_sink.append if route_sink is not None else (lambda r: None)),
        )

    def test_observe_healthy(self):
        env = self._env("svc-healthy-1", [200, 200, 200, 200])
        obs = env.observe("sample-service")
        self.assertEqual(obs.error_rate, 0.0)
        self.assertEqual(obs.current_revision, "svc-healthy-1")
        self.assertEqual(obs.last_healthy_revision, "svc-healthy-1")
        self.assertEqual(obs.recent_error_logs, [])  # 健全時はログ取得しない

    def test_observe_bad_serving_marks_incident(self):
        env = self._env("svc-bad-2", [500, 500, 500, 500])
        obs = env.observe("sample-service")
        self.assertEqual(obs.error_rate, 1.0)
        self.assertIsNotNone(obs.seconds_since_last_deploy)        # bad 配信 → インシデント開始を記録
        self.assertEqual(obs.recent_error_logs, ["synthetic 500"])  # 異常時はログ取得

    def test_inject_and_rollback_route_traffic(self):
        routed = []
        env = self._env("svc-healthy-1", [200] * 4, route_sink=routed)
        env.inject_fault()
        self.assertEqual(routed[-1], "svc-bad-2")           # 本物の振替（bad へ）
        self.assertTrue(env.snapshot()["injected"])
        env.apply_rollback(CFG, "sample-service", "svc-healthy-1")
        self.assertEqual(routed[-1], "svc-healthy-1")       # ロールバック（healthy へ）
        self.assertFalse(env.snapshot()["injected"])


class TestFeatureBugSelfHeal(unittest.TestCase):
    def _env(self, serving, codes, route_sink=None):
        return RealEnvironment(
            CFG, services_client=FakeClient(fake_service(serving=serving, tags=FULL_TAGS)),
            prober=lambda u, n: list(codes),
            log_fetcher=lambda: ["Traceback (most recent call last):", "ZeroDivisionError: division by zero"],
            route_fn=(route_sink.append if route_sink is not None else (lambda r: None)),
        )

    def test_inject_feature_bug_routes_and_attaches_source(self):
        routed = []
        env = self._env("svc-healthy-1", [200] * 4, route_sink=routed)
        env.inject_feature_bug()
        self.assertEqual(routed[-1], "svc-fb-3")            # 新機能＋バグ版へ振替
        snap = env.snapshot()
        self.assertEqual(snap["scenario"], "feature_bug")
        self.assertTrue(snap["injected"])
        self.assertIsNotNone(snap["faulty_source"])

    def test_observe_feature_bug_sets_faulty_source(self):
        env = self._env("svc-fb-3", [500] * 4)
        obs = env.observe("sample-service")
        self.assertEqual(obs.error_rate, 1.0)
        self.assertIsNotNone(obs.faulty_source)
        self.assertIsNotNone(obs.seconds_since_last_deploy)

    def test_apply_code_fix_prebuilt_routes_to_fixed(self):
        routed = []
        env = self._env("svc-fb-3", [500] * 4, route_sink=routed)
        env.observe("sample-service")                       # リビジョン解決
        env.apply_code_fix(CodeFix(summary="s", fixed_source="x"))
        self.assertEqual(routed[-1], "svc-fixed-4")         # 事前ビルド済み修正版へ振替
        self.assertEqual(env.snapshot()["scenario"], "fixed")
        self.assertFalse(env.snapshot()["injected"])


if __name__ == "__main__":
    unittest.main()
