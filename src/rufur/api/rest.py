"""REST adapter (`/v1/*`) plus SSE, the mounted MCP endpoint, and the static UI."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..handover import HandoverIn
from ..service import AccessDenied, MemoryService, NotFound

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


class AgentIn(BaseModel):
    name: str
    trust: str = "medium_low"
    clearance: str = "confidential"


class PoolIn(BaseModel):
    name: str
    description: str | None = None
    acl: dict[str, list[str]] | None = None


class AclIn(BaseModel):
    acl: dict[str, list[str]]


class SessionIn(BaseModel):
    pool: str | None = None
    task_id: str | None = None


class EventIn(BaseModel):
    content: str
    role: Literal["user", "assistant", "tool", "document", "web"] = "user"
    session_id: str | None = None
    pool: str | None = None
    source_type: str | None = None


class RememberIn(BaseModel):
    text: str
    type: str | None = None
    subject: str = ""
    predicate: str = ""
    value: str = ""
    source: Literal["user", "document", "agent", "tool", "web"] = "agent"
    pool: str | None = None
    sensitivity: Literal["public", "internal", "confidential", "restricted"] = "internal"
    pinned: bool = False
    session_id: str | None = None
    long_term: bool = False
    wait: bool = True  # false: return at once and structure the text in the background


class RecallIn(BaseModel):
    query: str
    token_budget: int | None = Field(default=None, ge=50, le=20000)
    pool: str | None = None
    session_id: str | None = None
    verify: bool = True
    exclude_sent: bool = False


class RecallPastIn(BaseModel):
    query: str
    since: float | None = None
    until: float | None = None
    include_history: bool = True
    token_budget: int | None = Field(default=None, ge=50, le=20000)


class BootstrapIn(BaseModel):
    project: str | None = None
    prompt: str | None = None
    session_id: str | None = None
    pool: str | None = None


class ConsolidateIn(BaseModel):
    journal: bool = False
    retry_discarded: bool = False


class SpawnIn(BaseModel):
    purpose: str
    needs: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    ttl_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    session_id: str | None = None
    seed_budget: int = Field(default=2000, ge=100, le=20000)


class PoolSearchIn(BaseModel):
    address: str
    query: str
    token_budget: int | None = None


class PoolRememberIn(BaseModel):
    address: str
    text: str
    type: str | None = None
    subject: str = ""
    predicate: str = ""
    value: str = ""


class PoolAddressIn(BaseModel):
    address: str
    summary: str | None = None


class CloseIn(BaseModel):
    summary: str | None = None


class DecisionIn(BaseModel):
    action: Literal["keep", "both", "neither"] = "keep"
    winner: str | None = None
    scopes: dict[str, str] | None = None
    reason: str = ""
    decided_by: Literal["agent", "user"] = "agent"


class SubagentStoppedIn(BaseModel):
    session_id: str | None = None


class ConfirmIn(BaseModel):
    action: Literal["confirm", "reject"] = "confirm"


class ForgetIn(BaseModel):
    memory_id: str | None = None
    memory_ids: list[str] | None = None
    query: str | None = None
    confirm: bool = False
    include_history: bool = True
    cascade_mentions: bool = False


class ResumeIn(BaseModel):
    token_budget: int | None = None


def create_app(service: MemoryService, *, mcp_app=None, mcp_session_manager=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if mcp_session_manager is not None:
            async with mcp_session_manager.run():
                yield
        else:
            yield

    app = FastAPI(title="RUFUR Knowledge Brain", version="0.1.0", lifespan=lifespan)
    app.state.service = service

    def agent(request: Request) -> str:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            name = service.agent_for_key(auth[7:].strip())
            if name is None:
                raise HTTPException(401, "invalid API key")
            return name
        if service.cfg.require_auth:
            raise HTTPException(401, "API key required")
        return request.headers.get("x-rufur-agent") or "local"

    @app.exception_handler(NotFound)
    async def _nf(_r, e):
        return JSONResponse({"detail": str(e)}, status_code=404)

    @app.exception_handler(AccessDenied)
    async def _ad(_r, e):
        return JSONResponse({"detail": str(e)}, status_code=403)

    @app.exception_handler(ValueError)
    async def _ve(_r, e):
        return JSONResponse({"detail": str(e)}, status_code=400)

    S = service
    A = Depends(agent)

    # -- system ----------------------------------------------------------
    @app.get("/v1/health")
    def health():
        return S.health()

    @app.get("/v1/whoami")
    def whoami(a: str = A):
        return {k: v for k, v in S.agent(a).items() if k != "key_hash"}

    @app.post("/v1/agents")
    def create_agent(body: AgentIn, _a: str = A):
        return S.create_agent(body.name, trust=body.trust, clearance=body.clearance)

    @app.get("/v1/agents")
    def list_agents(_a: str = A):
        return S.list_agents()

    @app.get("/v1/access")
    def access_overview(a: str = A):
        return S.access_overview(a)

    @app.post("/v1/agents/{name}/role")
    def set_agent_role(name: str, body: dict, a: str = A):
        return S.set_agent_role(a, name, str(body.get("role", "")))

    @app.post("/v1/agents/{name}/quarantine")
    def quarantine_agent(name: str, a: str = A):
        return S.quarantine_agent(a, name)

    # -- pools -----------------------------------------------------------
    @app.get("/v1/pools")
    def pools(a: str = A):
        return S.list_pools(a)

    @app.post("/v1/pools")
    def create_pool(body: PoolIn, a: str = A):
        return S.create_pool(a, body.name, description=body.description, acl=body.acl)

    @app.put("/v1/pools/{name}/acl")
    def set_acl(name: str, body: AclIn, a: str = A):
        return S.set_pool_acl(a, name, body.acl)

    # -- sessions and capture -----------------------------------------------
    @app.post("/v1/sessions")
    def session_start(body: SessionIn, a: str = A):
        return S.session_start(a, body.pool, body.task_id)

    @app.post("/v1/sessions/{session_id}/end")
    def session_end(session_id: str, background: bool = False, a: str = A):
        return S.session_end(a, session_id, background=background)

    @app.post("/v1/events")
    def log_event(body: EventIn, a: str = A):
        return S.log_event(a, body.content, role=body.role, session_id=body.session_id, pool=body.pool,
                           source_type=body.source_type)  # fmt: skip

    @app.get("/v1/events/{event_id}")
    def get_event(event_id: str, a: str = A):
        return S.event(a, event_id)

    # -- memories --------------------------------------------------------
    @app.post("/v1/memories")
    def remember(body: RememberIn, a: str = A):
        return S.remember(a, **body.model_dump())

    @app.get("/v1/memories")
    def list_memories(pool: str | None = None, status: str | None = None, type: str | None = None,
                      tier: str | None = None, limit: int = 500, a: str = A):  # fmt: skip
        return S.list_memories(a, pool=pool, status=status, type=type, tier=tier, limit=limit)

    @app.get("/v1/memories/{memory_id}")
    def inspect(memory_id: str, a: str = A):
        return S.inspect(a, memory_id)

    @app.get("/v1/memories/{memory_id}/access")
    def memory_access(memory_id: str, a: str = A):
        return S.memory_access(a, memory_id)

    @app.post("/v1/memories/{memory_id}/confirm")
    def confirm(memory_id: str, body: ConfirmIn, a: str = A):
        return S.confirm_memory(a, memory_id, body.action)

    @app.post("/v1/recall")
    def recall(body: RecallIn, a: str = A):
        return S.recall(a, body.query, token_budget=body.token_budget, pool=body.pool, session_id=body.session_id,
                        verify=body.verify, exclude_sent=body.exclude_sent)  # fmt: skip

    @app.post("/v1/recall/past")
    def recall_past(body: RecallPastIn, a: str = A):
        return S.recall_past(a, body.query, since=body.since, until=body.until, include_history=body.include_history,
                             token_budget=body.token_budget)  # fmt: skip

    @app.post("/v1/bootstrap")
    def bootstrap(body: BootstrapIn, a: str = A):
        return S.bootstrap(a, project=body.project, prompt=body.prompt, session_id=body.session_id, pool=body.pool)

    # -- consolidation, STM, subagent pools ----------------------------------------
    @app.post("/v1/consolidate")
    def consolidate(body: ConsolidateIn | None = None, a: str = A):
        body = body or ConsolidateIn()
        return S.consolidate(a, journal=body.journal, retry_discarded=body.retry_discarded)

    @app.get("/v1/consolidation/runs")
    def consolidation_runs(limit: int = 30, _a: str = A):
        return S.consolidation_runs(limit)

    @app.get("/v1/stm/stats")
    def stm_stats(_a: str = A):
        return S.stm_stats()

    @app.post("/v1/maintenance/sweep")
    def sweep(_a: str = A):
        return S.sweep()

    @app.get("/v1/doctor")
    def doctor(_a: str = A):
        return S.doctor()

    @app.post("/v1/pools/spawn")
    def pool_spawn(body: SpawnIn, a: str = A):
        return S.pool_spawn(a, body.purpose, needs=body.needs, memory_ids=body.memory_ids,
                            ttl_minutes=body.ttl_minutes, session_id=body.session_id, seed_budget=body.seed_budget)  # fmt: skip

    # Memory addresses (mem://pool/secret) travel in the body: they contain a slash and a secret.
    @app.post("/v1/pool/search")
    def pool_search(body: PoolSearchIn, a: str = A):
        return S.pool_search(a, body.address, body.query, token_budget=body.token_budget)

    @app.post("/v1/pool/remember")
    def pool_remember(body: PoolRememberIn, a: str = A):
        return S.pool_remember(a, body.address, body.text, type=body.type, subject=body.subject,
                               predicate=body.predicate, value=body.value)  # fmt: skip

    @app.post("/v1/pool/fetch")
    def pool_fetch(body: PoolSearchIn, a: str = A):
        return S.pool_fetch(a, body.address, body.query)

    @app.post("/v1/pool/info")
    def pool_info(body: PoolAddressIn, a: str = A):
        return S.pool_info(a, body.address)

    @app.post("/v1/pool/close")
    def pool_close_by_address(body: PoolAddressIn, a: str = A):
        return S.pool_close(a, body.address, summary=body.summary)

    @app.post("/v1/pools/{pool_id}/close")
    def pool_close(pool_id: str, body: CloseIn | None = None, a: str = A):
        return S.pool_close(a, pool_id, summary=(body or CloseIn()).summary)

    @app.get("/v1/decisions")
    def pending_decisions(a: str = A):
        return S.pending_decisions(a)

    @app.get("/v1/conflicts/history")
    def conflict_history(limit: int = 30, a: str = A):
        return S.conflict_history(a, limit=limit)

    @app.get("/v1/llm/log")
    def llm_log(limit: int = 200, a: str = A):
        return S.llm_log(limit=limit)

    @app.post("/v1/review/auto")
    def auto_review(a: str = A):
        return S.auto_review(a)

    @app.post("/v1/decisions/{conflict_id}")
    def resolve_conflict(conflict_id: str, body: DecisionIn, a: str = A):
        return S.resolve_conflict(a, conflict_id, action=body.action, winner=body.winner, scopes=body.scopes,
                                  reason=body.reason, decided_by=body.decided_by)  # fmt: skip

    @app.get("/v1/scratch-pools")
    def scratch_pools(a: str = A):
        return S.scratch_pools(a)

    @app.post("/v1/subagents/stopped")
    def subagent_stopped(body: SubagentStoppedIn, a: str = A):
        return S.subagent_stopped(a, body.session_id)

    @app.post("/v1/forget")
    def forget(body: ForgetIn, a: str = A):
        return S.forget(a, **body.model_dump())

    @app.get("/v1/forget/receipts")
    def forget_receipts(limit: int = 50, a: str = A):
        return S.forget_receipts(a, limit)

    @app.get("/v1/forget/receipts/{receipt_id}")
    def forget_receipt(receipt_id: str, a: str = A):
        return S.forget_receipt(a, receipt_id)

    @app.post("/v1/forget/verify")
    def verify_forgetting(a: str = A):
        return S.verify_forgetting(a)

    # -- handover --------------------------------------------------------
    @app.post("/v1/tasks/{task_id}/handover")
    def handover_create(task_id: str, body: HandoverIn, a: str = A):
        return S.handover_create(a, task_id, body)

    @app.post("/v1/tasks/{task_id}/resume")
    def handover_resume(task_id: str, body: ResumeIn | None = None, a: str = A):
        return S.handover_resume(a, task_id, (body or ResumeIn()).token_budget)

    @app.get("/v1/tasks")
    def tasks(a: str = A):
        return S.list_tasks(a)

    # -- UI reads --------------------------------------------------------
    @app.get("/v1/graph")
    def graph(pool: str | None = None, limit: int = 500, a: str = A):
        return S.graph(a, pool=pool, limit=limit)

    @app.get("/v1/quarantine")
    def quarantine(a: str = A):
        return S.quarantine(a)

    @app.get("/v1/metrics/summary")
    def metrics(_a: str = A):
        return S.metrics_summary()

    @app.get("/v1/audit")
    def audit(limit: int = 200, _a: str = A):
        return S.audit_log(limit)

    @app.get("/v1/dashboard/sessions")
    def dashboard_sessions(a: str = A):
        return S.dashboard_sessions(a)

    @app.get("/v1/dashboard/{kind}/{ident}/memories")
    def dashboard_memories(kind: Literal["session", "scratch", "unsessioned"], ident: str, a: str = A):
        return S.dashboard_memories(a, kind, ident)

    @app.get("/v1/dashboard/{kind}/{ident}/timeline")
    def dashboard_timeline(kind: Literal["session", "scratch"], ident: str, a: str = A):
        return S.dashboard_timeline(a, kind, ident)

    @app.get("/v1/dashboard/tokens")
    def dashboard_tokens(bucket: Literal["session", "hour"] = "session", _a: str = A):
        return S.dashboard_tokens(bucket)

    @app.get("/v1/dashboard/forgetting")
    def dashboard_forgetting(a: str = A):
        return S.dashboard_forgetting(a)

    @app.get("/v1/raw/catalog")
    def raw_catalog(a: str = A):
        return S.raw_catalog(a)

    @app.get("/v1/raw/tables/{table}")
    def raw_rows(table: str, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
                 column: str | None = None, value: str | None = None, desc: bool = False, a: str = A):  # fmt: skip
        return S.raw_rows(a, table, limit=limit, offset=offset, column=column, value=value, desc=desc)

    @app.post("/v1/maintenance/auto-forget")
    def auto_forget(a: str = A):
        return S.auto_forget(a)

    @app.get("/v1/stream")
    async def stream(request: Request):
        q = S.bus.subscribe()

        async def gen():
            try:
                yield "event: hello\ndata: {}\n\n"
                while not await request.is_disconnected():
                    try:
                        ev = await asyncio.wait_for(q.get(), timeout=15)
                        yield f"data: {json.dumps(ev)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                S.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    # -- MCP + UI ----------------------------------------------------------
    if mcp_app is not None:
        # Mount the MCP Starlette routes directly so `/mcp` works without a trailing-slash redirect.
        app.router.routes.extend(mcp_app.routes)

    if (STATIC_DIR / "index.html").exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> Any:
            f = STATIC_DIR / path
            if path and f.is_file() and STATIC_DIR in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(STATIC_DIR / "index.html")

    return app
