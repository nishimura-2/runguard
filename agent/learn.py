"""learn — インシデント記録とプレイブック更新（永続化）。

- IncidentStore プロトコルの背後に InMemory（テスト可能）と Firestore（ライブ）を用意。
- プレイブック: 「障害署名 → 効いた対応」を蓄積し、次サイクルの診断に文脈として渡す。
- ループ保護の状態（直近アクション数・最終アクション時刻）もストアが保持する
  （Cloud Run はスケールゼロのため、ライブでは Firestore に永続化してティック跨ぎで効かせる）。
- Firestore のライブ動作確認は GCP 接続後（Phase 4 ライブ / デプロイ時）。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Protocol, Tuple

from agent.config import Config
from agent.config import config as default_config
from agent.models import Decision, Diagnosis, Incident
from agent.observe import seconds_between


class IncidentStore(Protocol):
    def save_incident(self, incident: Incident) -> None: ...
    def list_incidents(self, limit: int = 50) -> List[Incident]: ...
    def update_playbook(self, diagnosis: Diagnosis, decision: Decision, outcome: Optional[str]) -> None: ...
    def playbook_context(self) -> str: ...
    def recent_action_stats(self, service: str, now_iso: str) -> Tuple[int, Optional[int]]: ...
    def record_action(self, service: str, now_iso: str) -> None: ...
    def mark_healthy(self, service: str) -> None: ...


def _playbook_key(diagnosis: Diagnosis, decision: Decision) -> str:
    return f"{diagnosis.category.value} -> {decision.action.value}"


def _playbook_text(playbook: Dict[str, dict]) -> str:
    if not playbook:
        return ""
    lines = ["過去のインシデントで効いた対応:"]
    for key, e in playbook.items():
        lines.append(f"- {key}: {e['resolved']}/{e['total']} 回で復旧")
    return "\n".join(lines)


class InMemoryStore:
    """テスト・単一インスタンス用のストア。"""

    def __init__(self) -> None:
        self.incidents: List[Incident] = []
        self.playbook: Dict[str, dict] = {}
        self._action_count: Dict[str, int] = {}
        self._last_action_at: Dict[str, str] = {}

    def save_incident(self, incident: Incident) -> None:
        self.incidents.append(incident)

    def list_incidents(self, limit: int = 50) -> List[Incident]:
        return self.incidents[-limit:]

    def update_playbook(self, diagnosis, decision, outcome) -> None:
        key = _playbook_key(diagnosis, decision)
        e = self.playbook.setdefault(key, {"resolved": 0, "total": 0})
        e["total"] += 1
        if outcome == "resolved":
            e["resolved"] += 1

    def playbook_context(self) -> str:
        return _playbook_text(self.playbook)

    def recent_action_stats(self, service, now_iso):
        count = self._action_count.get(service, 0)
        last = self._last_action_at.get(service)
        secs = seconds_between(last, now_iso) if last else None
        return count, secs

    def record_action(self, service, now_iso) -> None:
        self._action_count[service] = self._action_count.get(service, 0) + 1
        self._last_action_at[service] = now_iso

    def mark_healthy(self, service) -> None:
        self._action_count[service] = 0
        self._last_action_at.pop(service, None)


class FirestoreStore:
    """Firestore 永続化（ライブ）。実接続の最終確認は GCP 接続後。"""

    def __init__(self, cfg: Config = default_config) -> None:
        self._cfg = cfg
        self._db = None
        self._fs = None

    def _ensure(self):
        if self._db is None:
            from google.cloud import firestore  # 遅延 import
            self._fs = firestore
            self._db = firestore.Client(
                project=self._cfg.project_id,
                database=self._cfg.firestore_database or "(default)",
            )
        return self._db

    def _col(self, name: str) -> str:
        return f"{self._cfg.collection_prefix}_{name}"

    def save_incident(self, incident: Incident) -> None:
        db = self._ensure()
        db.collection(self._col("incidents")).document(incident.id).set(incident.model_dump())

    def list_incidents(self, limit: int = 50) -> List[Incident]:
        db = self._ensure()
        docs = (
            db.collection(self._col("incidents"))
            .order_by("timestamp", direction=self._fs.Query.DESCENDING)
            .limit(limit)
            .stream()
        )
        return [Incident.model_validate(d.to_dict()) for d in docs]

    def update_playbook(self, diagnosis, decision, outcome) -> None:
        db = self._ensure()
        key = _playbook_key(diagnosis, decision).replace("/", "_")
        ref = db.collection(self._col("playbook")).document(key)
        snap = ref.get()
        e = snap.to_dict() if snap.exists else {"resolved": 0, "total": 0}
        e["total"] = e.get("total", 0) + 1
        if outcome == "resolved":
            e["resolved"] = e.get("resolved", 0) + 1
        ref.set(e)

    def playbook_context(self) -> str:
        db = self._ensure()
        playbook = {d.id: d.to_dict() for d in db.collection(self._col("playbook")).stream()}
        return _playbook_text(playbook)

    def _guard_ref(self, service: str):
        return self._ensure().collection(self._col("guards")).document(service)

    def recent_action_stats(self, service, now_iso):
        snap = self._guard_ref(service).get()
        if not snap.exists:
            return 0, None
        d = snap.to_dict()
        last = d.get("last_action_at")
        secs = seconds_between(last, now_iso) if last else None
        return int(d.get("action_count", 0)), secs

    def record_action(self, service, now_iso) -> None:
        ref = self._guard_ref(service)
        snap = ref.get()
        count = int(snap.to_dict().get("action_count", 0)) if snap.exists else 0
        ref.set({"action_count": count + 1, "last_action_at": now_iso})

    def mark_healthy(self, service) -> None:
        self._guard_ref(service).set({"action_count": 0, "last_action_at": None})


def make_store(cfg: Config = default_config) -> IncidentStore:
    """STORE_BACKEND に応じてストアを返す（既定 firestore / それ以外は in-memory）。"""
    if cfg.store_backend == "firestore" and cfg.project_id:
        return FirestoreStore(cfg)
    return InMemoryStore()
