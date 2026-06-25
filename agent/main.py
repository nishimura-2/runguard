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
from agent.gemini_client import select_llm
from agent.learn import InMemoryStore
from agent.loop import LoopDeps, run_cycle
from agent.sim import SimEnvironment

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

app = FastAPI(title="RunGuard")

_service = cfg.target_services[0] if cfg.target_services else "sample-service"
SIM = SimEnvironment(service=_service)
STORE = InMemoryStore()
LLM = select_llm(cfg)
# sim は実 GCP 副作用がないため dry_run を無効化（安全。allowlist/確信度/ループ保護は有効のまま）。
SIM_CFG = replace(cfg, dry_run=False)


def _deps() -> LoopDeps:
    return LoopDeps(
        observe=SIM.observe,
        llm=LLM,
        store=STORE,
        cfg=SIM_CFG,
        executor=lambda d, c, **kw: execute(d, c, rollback_fn=SIM.apply_rollback, **kw),
        sleep=lambda s: None,
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
        "sim": SIM.snapshot(),
        "incidents": [i.model_dump() for i in reversed(STORE.list_incidents(20))],
        "playbook": STORE.playbook_context(),
        "config": {
            "auto_act_threshold": SIM_CFG.auto_act_threshold,
            "error_rate_threshold": SIM_CFG.error_rate_threshold,
            "llm": type(LLM).__name__,
        },
    }


@app.post("/api/inject")
def api_inject():
    SIM.inject_fault()
    return {"ok": True, "injected": True}


@app.post("/api/tick")
def api_tick(x_runguard_token: str = Header(default="")):
    _check_token(x_runguard_token)
    incident = run_cycle(SIM.service, _deps())
    return {
        "incident": incident.model_dump() if incident else None,
        "sim": SIM.snapshot(),
    }


@app.post("/api/reset")
def api_reset():
    SIM.reset()
    STORE.clear()
    return {"ok": True}
