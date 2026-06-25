"""elastic_store — Elasticsearch で過去インシデントを類似検索（SPEC の Elastic 枠）。

- index_incident: learn の蓄積。インシデントの「ログ署名」を semantic_text で索引。
- similar_incidents_context: diagnose 前に類似の過去インシデントを文脈化して渡す。
- elasticsearch 9.x。接続は endpoint + API キー（env、リポジトリに置かない）。
- elasticsearch は遅延 import。テストではクライアントを注入してモック可能。
- best-effort: Elastic 障害が core ループを止めないよう、失敗は握って継続する。
- ライブ検証は Elastic Cloud トライアル接続後（メール登録で取得可・GCP 不要）。
"""
from __future__ import annotations

from typing import List

from agent.config import Config
from agent.config import config as default_config
from agent.models import Incident, Observation


def signature_text(obs: Observation, category: str = "", action: str = "", outcome: str = "") -> str:
    """semantic 検索のキーになる「ログ署名」テキスト。"""
    logs = " | ".join(obs.recent_error_logs[:5])
    recent = (
        "post-deploy"
        if (obs.seconds_since_last_deploy is not None and obs.seconds_since_last_deploy <= 600)
        else "no-recent-deploy"
    )
    return (
        f"service={obs.service} error_rate={obs.error_rate:.2f} memory_ratio={obs.memory_ratio:.2f} "
        f"deploy={recent} category={category} action={action} outcome={outcome} logs: {logs}"
    )


class ElasticStore:
    def __init__(self, cfg: Config = default_config, client=None):
        self._cfg = cfg
        self._client = client
        self._index = cfg.elastic_index
        self._ensured = False
        self._semantic = True

    def _es(self):
        if self._client is None:
            from elasticsearch import Elasticsearch  # 遅延 import
            self._client = Elasticsearch(
                hosts=[self._cfg.elastic_endpoint], api_key=self._cfg.elastic_api_key
            )
        return self._client

    _MAPPING_KEYWORDS = {
        "service": {"type": "keyword"},
        "category": {"type": "keyword"},
        "action": {"type": "keyword"},
        "outcome": {"type": "keyword"},
        "timestamp": {"type": "date"},
    }

    def _ensure_index(self) -> None:
        if self._ensured:
            return
        es = self._es()
        if es.indices.exists(index=self._index):
            self._ensured = True
            return
        try:
            es.indices.create(index=self._index, mappings={
                "properties": {"signature": {"type": "semantic_text"}, **self._MAPPING_KEYWORDS}
            })
            self._semantic = True
        except Exception:
            # semantic_text が使えない場合（inference 未設定等）は text にフォールバック
            es.indices.create(index=self._index, mappings={
                "properties": {"signature": {"type": "text"}, **self._MAPPING_KEYWORDS}
            })
            self._semantic = False
        self._ensured = True

    def index_incident(self, incident: Incident) -> bool:
        try:
            self._ensure_index()
            self._es().index(index=self._index, id=incident.id, document={
                "signature": signature_text(
                    incident.observation,
                    incident.diagnosis.category.value,
                    incident.decision.action.value,
                    incident.outcome or "",
                ),
                "service": incident.observation.service,
                "category": incident.diagnosis.category.value,
                "action": incident.decision.action.value,
                "outcome": incident.outcome or "",
                "timestamp": incident.timestamp,
            })
            return True
        except Exception:
            return False  # best-effort

    def similar_incidents(self, observation: Observation, k: int = 3) -> List[dict]:
        self._ensure_index()
        q = signature_text(observation)
        query = (
            {"semantic": {"field": "signature", "query": q}}
            if self._semantic
            else {"match": {"signature": q}}
        )
        resp = self._es().search(index=self._index, size=k, query=query)
        body = getattr(resp, "body", resp)
        return [h.get("_source", {}) for h in body.get("hits", {}).get("hits", [])]

    def similar_incidents_context(self, observation: Observation, k: int = 3) -> str:
        try:
            hits = self.similar_incidents(observation, k)
        except Exception:
            return ""
        if not hits:
            return ""
        lines = ["類似の過去インシデント（Elastic semantic 検索）:"]
        for h in hits:
            lines.append(
                f"- category={h.get('category')} action={h.get('action')} "
                f"outcome={h.get('outcome')} ({h.get('timestamp')})"
            )
        return "\n".join(lines)


def make_elastic_store(cfg: Config = default_config):
    """USE_ELASTIC かつ接続情報があれば ElasticStore、無ければ None。"""
    if cfg.use_elastic and cfg.elastic_endpoint and cfg.elastic_api_key:
        return ElasticStore(cfg)
    return None
