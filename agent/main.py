"""main — Cloud Run エントリ。ダッシュボード配信 / /api/* / 監視ループ（/api/tick）。

既定はオフラインのシミュレーションモード（GCP 不要で 2 分デモが完結）。
GCP 接続時は observe=build_observation・rollback=Cloud Run Admin に差し替える（loop は共通）。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from agent.actions import execute
from agent.config import config as cfg
from agent.elastic_store import make_elastic_store
from agent.gemini_client import select_llm
from agent.learn import InMemoryStore
from agent.loop import LoopDeps, run_cycle
from agent.sim import SimEnvironment

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

app = FastAPI(title="RunGuard")

_service = cfg.target_services[0] if cfg.target_services else "sample-service"
LLM = select_llm(cfg)
ELASTIC = make_elastic_store(cfg)        # USE_ELASTIC 時のみ。類似検索＋インシデント蓄積。

# バックエンド選択: real=本物の Cloud Run 操作 / sim=シミュレーション（既定）
if cfg.mode == "real":
    import time as _time
    from agent.learn import make_store
    from agent.real_env import RealEnvironment
    BACKEND = RealEnvironment(cfg)
    STORE = make_store(cfg)                # Firestore 永続化（project_id があれば）
    RUN_CFG = cfg                          # 本物のロールバックを行うため DRY_RUN は env 指定（デプロイで 0）
    SLEEP = _time.sleep                    # verify 前に伝播待ち
else:
    BACKEND = SimEnvironment(service=_service)
    STORE = InMemoryStore()
    RUN_CFG = replace(cfg, dry_run=False)  # sim は副作用なしなので dry_run 無効でOK
    SLEEP = lambda s: None


def _deps() -> LoopDeps:
    return LoopDeps(
        observe=BACKEND.observe,
        llm=LLM,
        store=STORE,
        cfg=RUN_CFG,
        executor=lambda d, c, **kw: execute(d, c, rollback_fn=BACKEND.apply_rollback, **kw),
        sleep=SLEEP,
        elastic=ELASTIC,
    )


def _check_token(token: str) -> None:
    if cfg.scheduler_token and token != cfg.scheduler_token:
        raise HTTPException(status_code=403, detail="invalid scheduler token")


@app.get("/")
def dashboard():
    return FileResponse(DASHBOARD_DIR / "index.html")


@app.get("/app.js")
def app_js():
    return FileResponse(DASHBOARD_DIR / "app.js", media_type="application/javascript")


@app.get("/api/state")
def api_state():
    return {
        "sim": BACKEND.snapshot(),
        "incidents": [i.model_dump() for i in reversed(STORE.list_incidents(20))],
        "playbook": STORE.playbook_context(),
        "config": {
            "mode": cfg.mode,
            "auto_act_threshold": RUN_CFG.auto_act_threshold,
            "error_rate_threshold": RUN_CFG.error_rate_threshold,
            "llm": getattr(LLM, "last_used", None) or type(LLM).__name__,
            "elastic": ELASTIC is not None,
        },
    }


@app.post("/api/inject")
def api_inject():
    try:
        BACKEND.inject_fault()
        return {"ok": True, "injected": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/tick")
def api_tick(x_runguard_token: str = Header(default="")):
    _check_token(x_runguard_token)
    incident = run_cycle(BACKEND.service, _deps())
    return {
        "incident": incident.model_dump() if incident else None,
        "sim": BACKEND.snapshot(),
    }


@app.post("/api/reset")
def api_reset():
    BACKEND.reset()
    if hasattr(STORE, "clear"):
        STORE.clear()
    return {"ok": True}
