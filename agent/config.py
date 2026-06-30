"""RunGuard 設定。環境変数を一元読込する。

- 秘密情報はコード/コマンド引数に書かない。実値は env.sh（.gitignore 済）か Secret Manager。
- このモジュールは外部依存なし（標準ライブラリのみ）。
  `python -c "import agent.config"` が依存インストールなしで通ることを保証する。
- import 時に例外を投げない（必須項目チェックは validate() を明示的に呼ぶ）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _get_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _get_list(name: str, default=()):
    raw = os.environ.get(name, "")
    items = tuple(s.strip() for s in raw.split(",") if s.strip())
    return items if items else tuple(default)


@dataclass(frozen=True)
class Config:
    # --- GCP 基本 ---
    project_id: str = field(default_factory=lambda: _get("PROJECT_ID"))
    region: str = field(default_factory=lambda: _get("REGION", "asia-northeast1"))

    # --- サービス名 ---
    agent_service: str = field(default_factory=lambda: _get("AGENT_SERVICE", "runguard-agent"))
    target_services: tuple = field(
        default_factory=lambda: _get_list("TARGET_SERVICES", ("sample-service",))
    )

    # --- バックエンド種別（sim=シミュレーション / real=本物の Cloud Run 操作） ---
    mode: str = field(default_factory=lambda: _get("RUNGUARD_MODE", "sim"))
    probe_count: int = field(default_factory=lambda: _get_int("PROBE_COUNT", 8))

    # --- Gemini ---
    use_vertexai: bool = field(default_factory=lambda: _get_bool("GOOGLE_GENAI_USE_VERTEXAI", True))
    gemini_model: str = field(default_factory=lambda: _get("GEMINI_MODEL", "gemini-3.5-flash"))
    google_cloud_project: str = field(
        default_factory=lambda: _get("GOOGLE_CLOUD_PROJECT") or _get("PROJECT_ID")
    )
    google_cloud_location: str = field(
        default_factory=lambda: _get("GOOGLE_CLOUD_LOCATION") or _get("REGION", "asia-northeast1")
    )

    # --- セーフティ ---
    dry_run: bool = field(default_factory=lambda: _get_bool("DRY_RUN", True))
    auto_act_threshold: float = field(default_factory=lambda: _get_float("AUTO_ACT_THRESHOLD", 0.8))
    # self_heal の修正版デプロイ方式: False=事前ビルド済み 'fixed' へ振替（新IAM不要・既定）/
    # True=ライブビルド(gcloud run deploy --source。Cloud Build 権限の追加付与が必要・要確認)。
    self_heal_live: bool = field(default_factory=lambda: _get_bool("SELF_HEAL_LIVE", False))

    # --- 監視ループ / しきい値 ---
    observe_window_minutes: int = field(
        default_factory=lambda: _get_int("OBSERVE_WINDOW_MINUTES", 5)
    )
    error_rate_threshold: float = field(
        default_factory=lambda: _get_float("ERROR_RATE_THRESHOLD", 0.1)
    )
    breach_windows: int = field(default_factory=lambda: _get_int("BREACH_WINDOWS", 1))
    cooldown_seconds: int = field(default_factory=lambda: _get_int("COOLDOWN_SECONDS", 300))
    max_actions_per_incident: int = field(
        default_factory=lambda: _get_int("MAX_ACTIONS_PER_INCIDENT", 1)
    )
    verify_wait_seconds: int = field(default_factory=lambda: _get_int("VERIFY_WAIT_SECONDS", 60))

    # --- 永続化（Firestore） ---
    store_backend: str = field(default_factory=lambda: _get("STORE_BACKEND", "firestore"))
    firestore_database: str = field(
        default_factory=lambda: _get("FIRESTORE_DATABASE", "(default)")
    )
    collection_prefix: str = field(
        default_factory=lambda: _get("FIRESTORE_COLLECTION_PREFIX", "runguard")
    )

    # --- /api/tick 保護（任意） ---
    scheduler_token: str = field(default_factory=lambda: _get("SCHEDULER_TOKEN"))

    # --- Elastic（任意・過去インシデントの類似検索） ---
    use_elastic: bool = field(default_factory=lambda: _get_bool("USE_ELASTIC", False))
    elastic_endpoint: str = field(default_factory=lambda: _get("ELASTIC_ENDPOINT"))
    elastic_api_key: str = field(default_factory=lambda: _get("ELASTIC_API_KEY"))
    elastic_index: str = field(default_factory=lambda: _get("ELASTIC_INDEX", "runguard-incidents"))

    def is_target_allowed(self, service: str) -> bool:
        """allowlist 判定。自分自身（agent_service）は常に除外する（自己操作防止）。"""
        if not service or service == self.agent_service:
            return False
        return service in self.target_services

    def validate(self) -> list:
        """実行時の必須項目チェック。問題点を文字列リストで返す（import 時には呼ばない）。"""
        problems = []
        if not self.project_id:
            problems.append("PROJECT_ID が未設定です。")
        if not self.target_services:
            problems.append("TARGET_SERVICES（監視対象 allowlist）が空です。")
        if self.agent_service in self.target_services:
            problems.append("AGENT_SERVICE が TARGET_SERVICES に含まれています（自己操作の危険）。")
        if not (0.0 <= self.auto_act_threshold <= 1.0):
            problems.append("AUTO_ACT_THRESHOLD は 0〜1 の範囲にしてください。")
        if self.use_vertexai and not self.google_cloud_project:
            problems.append("Vertex 経路では GOOGLE_CLOUD_PROJECT（または PROJECT_ID）が必要です。")
        if self.store_backend not in ("firestore", "gcs"):
            problems.append("STORE_BACKEND は 'firestore' か 'gcs' を指定してください。")
        if self.use_elastic and not (self.elastic_endpoint and self.elastic_api_key):
            problems.append("USE_ELASTIC=1 では ELASTIC_ENDPOINT と ELASTIC_API_KEY が必要です。")
        return problems


def load_config() -> Config:
    return Config()


# import 時に例外を投げない安全なシングルトン。
config = load_config()


if __name__ == "__main__":
    c = load_config()
    print("RunGuard config:")
    for key, value in c.__dict__.items():
        if key in ("scheduler_token", "elastic_api_key") and value:
            value = "***set***"
        print(f"  {key} = {value!r}")
    issues = c.validate()
    print("\nvalidate():", "OK" if not issues else issues)
