"""observe — 監視対象サービスのメトリクス/ログ/デプロイ時刻を集めて Observation を作る。

- 純粋ヘルパ（error_rate / seconds_between）は GCP 不要でテスト可能。
- build_observation() は Cloud Monitoring / Logging / Run Admin を読む副作用関数。
  google-cloud-* は遅延 import。★ライブ動作の確認は GCP プロジェクト用意後（Phase 4）に行う★
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from agent.config import Config
from agent.config import config as default_config
from agent.models import Observation


# ---- 純粋ヘルパ（テスト対象） ----

def error_rate(server_errors: int, total_requests: int) -> float:
    if total_requests <= 0:
        return 0.0
    return max(0.0, min(1.0, server_errors / total_requests))


def seconds_between(earlier_iso: Optional[str], later_iso: Optional[str]) -> Optional[int]:
    if not earlier_iso or not later_iso:
        return None
    a = _parse_iso(earlier_iso)
    b = _parse_iso(later_iso)
    if a is None or b is None:
        return None
    return int((b - a).total_seconds())


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


# ---- 副作用（GCP 読み取り） ----

def fetch_recent_error_logs(cfg: Config, service: str, max_results: int = 8, minutes: int = 10) -> list:
    """Cloud Logging から直近の ERROR 以上のログ本文を取得（best-effort、失敗時 []）。"""
    try:
        from datetime import timedelta
        import google.cloud.logging
        from google.cloud.logging import DESCENDING
        client = google.cloud.logging.Client(project=cfg.project_id)
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
        flt = (
            'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service}" '
            'AND severity>=ERROR '
            f'AND timestamp>="{since}"'
        )
        out = []
        for e in client.list_entries(
            resource_names=[f"projects/{cfg.project_id}"],
            filter_=flt, order_by=DESCENDING, max_results=max_results,
        ):
            p = e.payload
            msg = p if isinstance(p, str) else (p.get("message") if isinstance(p, dict) else str(p))
            out.append(str(msg)[:300])
        return out
    except Exception:
        return []


def build_observation(
    service: str,
    cfg: Config = default_config,
    now_iso: Optional[str] = None,
) -> Observation:
    """Cloud Monitoring/Logging/Run から Observation を構築する（ライブ）。

    現時点では構造のみ確定。実際の Monitoring クエリ等の細部は GCP プロジェクトに対して
    Phase 4 で検証・調整する（記憶の API 名に頼らずライブで確認する方針）。
    """
    from google.cloud import monitoring_v3  # noqa: F401  遅延 import
    from google.cloud import logging_v2      # noqa: F401  遅延 import
    from google.cloud import run_v2          # noqa: F401  遅延 import

    raise NotImplementedError(
        "build_observation() のライブ実装は Phase 4（GCP 接続後）に行う。"
        " それまでは diagnose を合成 Observation で検証する。"
    )
