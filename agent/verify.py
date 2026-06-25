"""verify — アクション後にエラー率がベースラインへ戻ったかを確認する。

純粋判定（is_recovered / verify_outcome）はテスト可能。
ライブ再観測 build_post_action_observation は observe.build_observation を使う（Phase 4 で実接続確認）。
"""
from __future__ import annotations

from agent.config import Config
from agent.config import config as default_config
from agent.models import Observation


def is_recovered(post_action: Observation, cfg: Config = default_config) -> bool:
    return post_action.error_rate <= cfg.error_rate_threshold


def verify_outcome(post_action: Observation, cfg: Config = default_config) -> str:
    """resolved / not_resolved を返す。"""
    return "resolved" if is_recovered(post_action, cfg) else "not_resolved"


def build_post_action_observation(service: str, cfg: Config = default_config) -> Observation:
    """アクション後の再観測（ライブ）。Phase 4 で実接続確認。"""
    from agent.observe import build_observation
    return build_observation(service, cfg)
