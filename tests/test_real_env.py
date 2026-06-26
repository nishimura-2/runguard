"""real_env の hermetic テスト（run_v2/httpx 不要・クライアント等を注入）。"""
import unittest
from dataclasses import replace

from agent.config import Config
from agent.real_env import (
    RealEnvironment,
    error_rate_from_statuses,
    serving_revision,
    tag_to_revision,
)


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def fake_service(serving="svc-healthy-1"):
    tags = {"healthy": "svc-healthy-1", "bad": "svc-bad-2"}
    traffic = [Obj(tag=t, revision=r, percent=(100 if r == serving else 0)) for t, r in tags.items()]
    statuses = [Obj(tag=t, revision=r, percent=(100 if r == serving else 0)) for t, r in tags.items()]
    return Obj(traffic=traffic, traffic_statuses=statuses,
               latest_ready_revision=serving, uri="https://svc.example")


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


if __name__ == "__main__":
    unittest.main()
