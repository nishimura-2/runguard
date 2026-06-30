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
        "backend": BACKEND.snapshot(),
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
        return {"ok": True, "injected": True, "scenario": "http500"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/inject_feature")
def api_inject_feature():
    """新機能＋仕込みバグ版をデプロイ（self_heal シナリオ）。ロールバックでは新機能を失う。"""
    try:
        BACKEND.inject_feature_bug()
        return {"ok": True, "injected": True, "scenario": "feature_bug"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/approve_fix")
def api_approve_fix(x_runguard_token: str = Header(default="")):
    """承認待ちの AI 修正案を承認 → 修正版をデプロイ相当で反映 → 検証（失敗時ロールバック退避）。"""
    _check_token(x_runguard_token)
    incidents = STORE.list_incidents(20)
    pending = [i for i in incidents if i.outcome == "awaiting_approval" and i.fix]
    if not pending:
        return {"ok": False, "error": "承認待ちの修正案がありません。先に『1サイクル実行』で検知・提案してください。"}
    incident = max(pending, key=lambda i: i.timestamp)
    # 冪等性: より新しい適用結果があれば再デプロイしない（連打・二重適用の防止）。
    applied = [i for i in incidents
               if i.outcome in ("self_healed", "not_resolved_rolled_back") and i.timestamp >= incident.timestamp]
    if applied:
        return {"ok": True, "already": True,
                "incident": max(applied, key=lambda i: i.timestamp).model_dump(),
                "backend": BACKEND.snapshot()}
    from agent.loop import apply_self_heal
    healed = apply_self_heal(BACKEND.service, _deps(), BACKEND, incident)
    return {"ok": True, "incident": healed.model_dump(), "backend": BACKEND.snapshot()}


@app.post("/api/tick")
def api_tick(x_runguard_token: str = Header(default="")):
    _check_token(x_runguard_token)
    incident = run_cycle(BACKEND.service, _deps())
    return {
        "incident": incident.model_dump() if incident else None,
        "backend": BACKEND.snapshot(),
    }


@app.post("/api/reset")
def api_reset():
    BACKEND.reset()
    if hasattr(STORE, "clear"):
        STORE.clear()
    return {"ok": True}


@app.post("/api/agent")
async def api_agent(x_runguard_token: str = Header(default="")):
    """ADK の LlmAgent を実走させ、LLM 主導で観測→（必要なら）ロールバックを行う。"""
    _check_token(x_runguard_token)
    try:
        from agent.adk_app import run_agent
        result = await run_agent(
            RUN_CFG, BACKEND,
            f"{BACKEND.service} を点検し、悪いデプロイなら正常リビジョンへロールバックして。",
        )
        return {"ok": True, **result, "backend": BACKEND.snapshot()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
