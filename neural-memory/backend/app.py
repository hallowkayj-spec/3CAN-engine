"""Neural Memory — FastAPI 后端。

提供节点/边CRUD、路由、WebSocket实时推送、图导出。
启动: python app.py (默认 http://localhost:9700)
"""
from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Mapping
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# 确保 models 可导入
sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_engine import GRAPH_DIR, GraphEngine
from owner_intent import OwnerIntentError, load_owner_intent
from readiness import READINESS_MODE_DEVELOPMENT, ReadinessCache, configured_readiness_mode
from error_knowledge import (
    ErrorCase,
    canonical_error_identity,
    deterministic_fingerprint,
)
from models import (
    DurableProvenance,
    EdgeCreate,
    EdgeType,
    NodeContent,
    NodeCreate,
    NodeStatus,
    NodeType,
    NodeUpdate,
    RoutingRequest,
    semantic_id_family,
    validate_routing_context_identifier,
)
from ticket_ledger import LedgerError, TicketLedger, canonical_hash
from token_usage import (
    TokenUsageStore,
    collect_codex_status_events,
    estimate_tokens_for_payload,
    sanitize_public_payload,
)

# ── WebSocket 管理 ──

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()
engine: GraphEngine | None = None
engine_route_lock = Lock()
token_usage_store: TokenUsageStore | None = None
token_usage_import_task: asyncio.Task | None = None

_LOCAL_BROWSER_ORIGIN_RE = re.compile(
    r"^https?://(?:localhost|127\.0\.0\.1|\[::1\])(?::\d{1,5})?/?$",
    re.IGNORECASE,
)


def _configured_cors_origins() -> list[str]:
    return [
        item.strip().rstrip("/")
        for item in os.environ.get("THREECAN_CORS_ORIGINS", "").split(",")
        if item.strip() and item.strip() != "*"
    ]


def _websocket_origin_allowed(origin: str | None) -> bool:
    """Mirror the HTTP CORS boundary for browser-originated WebSockets.

    Native/CLI clients may omit Origin. Browsers always send it, so an explicit
    untrusted or ``null`` origin must fail before the handshake is accepted.
    """

    if origin is None:
        return True
    normalized = origin.strip().rstrip("/")
    if not normalized or normalized.casefold() == "null":
        return False
    return bool(
        _LOCAL_BROWSER_ORIGIN_RE.fullmatch(normalized)
        or normalized in _configured_cors_origins()
    )


def _websocket_token_allowed(ws: WebSocket) -> bool:
    """Optional capability-token gate for non-browser or cookie clients."""

    expected = os.environ.get("THREECAN_WS_TOKEN", "").strip()
    if not expected:
        return True
    authorization = str(ws.headers.get("authorization") or "").strip()
    bearer = (
        authorization[7:].strip()
        if authorization.casefold().startswith("bearer ")
        else ""
    )
    cookie_token = str(ws.cookies.get("threecan_ws_token") or "").strip()
    supplied = bearer or cookie_token
    return bool(supplied and secrets.compare_digest(supplied, expected))


def _get_token_usage_store() -> TokenUsageStore:
    """Initialize the local ledger lazily, never as an import side effect."""
    global token_usage_store
    if token_usage_store is None:
        token_usage_store = TokenUsageStore(GRAPH_DIR / "token_usage.sqlite3")
    return token_usage_store


def _env_bool(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


token_usage_import_state: dict[str, Any] = {
    "enabled": _env_bool("THREECAN_TOKEN_AUTO_IMPORT", True),
    "interval_sec": _env_int("THREECAN_TOKEN_AUTO_IMPORT_INTERVAL", 60, minimum=5),
    "max_files": _env_int("THREECAN_TOKEN_AUTO_IMPORT_MAX_FILES", 3, minimum=1),
    "max_events": _env_int("THREECAN_TOKEN_AUTO_IMPORT_MAX_EVENTS", 1000, minimum=1),
    "running": False,
    "last_run_at": None,
    "last_ok": None,
    "last_imported_events": 0,
    "last_skipped_duplicates": 0,
    "last_error": "",
    "latest_snapshot": None,
}


def _route_sync_locked(req: RoutingRequest):
    if engine is None:
        raise RuntimeError("3CAN engine is not initialized")
    with engine_route_lock:
        return engine.route(req)


async def _route_in_worker(req: RoutingRequest):
    return await asyncio.to_thread(_route_sync_locked, req)


def _request_with_owner_intent(
    req: RoutingRequest,
) -> tuple[RoutingRequest, str | None]:
    """Bind the local project's compact 3CAN.md projection before routing.

    Shared-authority clients may already send a project-bound projection.  A
    server-local file is only considered for an explicit matching project pair,
    so one runtime's project file never becomes machine-global policy.
    """

    projection = req.owner_intent
    assertion_origin = "client_asserted" if projection else None
    if req.project_id and req.project_namespace:
        try:
            local_projection = load_owner_intent(
                _default_project_dir(),
                project_id=req.project_id,
                project_namespace=req.project_namespace,
            )
        except OwnerIntentError as exc:
            raise HTTPException(
                422,
                detail={"error": "owner_intent_invalid", "reason": str(exc)},
            ) from exc
        if local_projection and local_projection.get("status") == "applied":
            projection = local_projection
            assertion_origin = "server_local_file"

    if not projection:
        return req, None

    payload = req.model_dump(mode="python")
    payload["owner_intent"] = projection
    if "mode" not in req.model_fields_set:
        payload["mode"] = {
            "compact": "skeleton",
            "standard": "slim",
            "full": "full",
        }.get(str(projection.get("defaults", {}).get("context") or ""), "slim")
    return RoutingRequest.model_validate(payload), assertion_origin


def _applicable_project_reality(req: RoutingRequest, result: Any) -> dict[str, Any]:
    """Project a small request-local view from existing route evidence."""

    route_meta = result.route_meta if isinstance(result.route_meta, dict) else {}
    selected_ids = [node.id for node in result.activated_nodes]
    core = route_meta.get("core_memory_graph")
    lane_nodes = (
        core.get("lane_selected_nodes", {})
        if isinstance(core, dict)
        else {}
    )
    constraint_lanes = {
        "environment_constraints",
        "project_constitution",
        "project_file_system",
        "error_warnings",
    }
    constraint_ids = list(dict.fromkeys(
        str(node_id)
        for lane, node_ids in lane_nodes.items()
        if lane in constraint_lanes and isinstance(node_ids, list)
        for node_id in node_ids
        if str(node_id) in selected_ids
    ))
    selected_current_ids = [
        node_id
        for node_id in selected_ids
        if semantic_id_family(node_id)
        in {"INTF", "PROC", "DEC", "PRJ", "DOC", "ENV"}
    ]
    experience_ids = [
        node_id
        for node_id in selected_ids
        if semantic_id_family(node_id)
        in {"ERR", "ERRCASE", "FIX", "EVD", "SES", "HO"}
    ]
    current_policy = route_meta.get("current_reality_policy")
    current_policy = current_policy if isinstance(current_policy, dict) else {}
    return {
        "selected_current_node_ids": selected_current_ids[:12],
        "constraint_node_ids": constraint_ids[:12],
        "experience_node_ids": experience_ids[:12],
        "external_verification_required": bool(
            current_policy.get("external_verification_required")
        ),
    }


async def _token_usage_auto_import_loop() -> None:
    """Keep Codex runtime token status flowing into the 3CAN ledger."""
    await asyncio.sleep(_env_int("THREECAN_TOKEN_AUTO_IMPORT_INITIAL_DELAY", 5, minimum=0))
    while True:
        token_usage_import_state["running"] = True
        token_usage_import_state["last_run_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        try:
            result = await asyncio.to_thread(
                _get_token_usage_store().import_codex_status_events,
                max_files=int(token_usage_import_state["max_files"]),
                max_events=int(token_usage_import_state["max_events"]),
            )
            token_usage_import_state.update({
                "last_ok": bool(result.get("ok")),
                "last_imported_events": int(result.get("imported_events") or 0),
                "last_skipped_duplicates": int(result.get("skipped_duplicates") or 0),
                "last_error": "",
                "latest_snapshot": result.get("latest_snapshot"),
            })
        except Exception as exc:
            token_usage_import_state.update({
                "last_ok": False,
                "last_error": str(exc)[:500],
            })
        finally:
            token_usage_import_state["running"] = False
        await asyncio.sleep(int(token_usage_import_state["interval_sec"]))


def _path_from_env(name: str, fallback: Path | None = None) -> Path | None:
    value = os.environ.get(name, "").strip()
    if value:
        return Path(value).expanduser()
    return fallback


def _default_project_dir() -> Path:
    return _path_from_env("THREECAN_PROJECT_DIR", Path.cwd()) or Path.cwd()


def _default_memory_dir() -> Path:
    fallback = Path.home() / ".claude" / "memory"
    return _path_from_env("THREECAN_MEMORY_DIR", fallback) or fallback


def _default_watch_dirs() -> list[Path]:
    raw = os.environ.get("THREECAN_WATCH_DIRS", "").strip()
    if raw:
        candidates = [
            Path(part).expanduser()
            for part in raw.split(os.pathsep)
            if part.strip()
        ]
    else:
        project_dir = _default_project_dir()
        candidates = [
            _default_memory_dir(),
            project_dir / "docs" / "specs" / "handoffs" / "active",
            project_dir / "docs" / "specs",
        ]
    return [path for path in candidates if path.exists()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine, token_usage_import_task
    engine = GraphEngine()
    print(f"[3CAN] 图引擎启动: {len(engine.nodes)} 节点, {len(engine.edges)} 边, {len(engine.agents)} agents")
    try:
        reconcile = _reconcile_error_ledger_from_graph()
        print(
            "[3CAN] Error ledger reconcile: "
            f"imported={len(reconcile['imported'])} "
            f"graph_only={len(reconcile['graph_only_review_required'])} "
            f"failures={len(reconcile['failures'])}"
        )
    except Exception as exc:
        print(f"[3CAN] Error ledger reconcile PARTIAL: {exc}")
    token_status = _get_token_usage_store().integration_status(include_events=False)
    print(f"[3CAN] Token meter: {json.dumps(token_status['hook_status'], ensure_ascii=False)}")
    # Watch paths are configured explicitly or derived from the current project.
    watch_dirs = _default_watch_dirs()
    if watch_dirs:
        engine.start_sync_watcher(watch_dirs, interval=30)
    if token_usage_import_state.get("enabled"):
        token_usage_import_task = asyncio.create_task(_token_usage_auto_import_loop())
    try:
        yield
    finally:
        if token_usage_import_task:
            token_usage_import_task.cancel()
            try:
                await token_usage_import_task
            except asyncio.CancelledError:
                pass
            token_usage_import_task = None
        try:
            engine.stop_sync_watcher()
        finally:
            engine.close()
    print("[3CAN] 关闭")


app = FastAPI(
    title="Neural Memory",
    description="C³AN-inspired 项目知识图谱引擎",
    version="0.2.0.dev0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_configured_cors_origins(),
    allow_origin_regex=_LOCAL_BROWSER_ORIGIN_RE.pattern,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件 — 前端
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

RUNTIME_IDENTITY_SCHEMA = "3can.runtime-identity/v1"
_READINESS_CACHE = ReadinessCache()


def _runtime_path_sha256(path: Path) -> str:
    """Hash a canonical local path without disclosing it through the API."""

    canonical = os.path.normcase(
        str(Path(path).expanduser().resolve(strict=False))
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _public_runtime_identity(
    *,
    engine_root: Path | None = None,
    graph_root: Path | None = None,
    startup_nonce: str | None = None,
) -> dict[str, str]:
    """Return only non-reversible runtime selectors for client verification."""

    actual_engine_root = (
        Path(engine_root)
        if engine_root is not None
        else Path(__file__).resolve().parent.parent
    )
    actual_graph_root = Path(graph_root) if graph_root is not None else GRAPH_DIR
    nonce = (
        startup_nonce
        if startup_nonce is not None
        else os.environ.get("THREECAN_STARTUP_NONCE", "")
    )
    identity = {
        "schema": RUNTIME_IDENTITY_SCHEMA,
        "engine_root_sha256": _runtime_path_sha256(actual_engine_root),
        "graph_root_sha256": _runtime_path_sha256(actual_graph_root),
    }
    if nonce:
        identity["startup_nonce_sha256"] = hashlib.sha256(
            nonce.encode("utf-8")
        ).hexdigest()
    return identity


def _readiness_snapshot(
    *,
    graph_engine: GraphEngine | None = None,
    engine_root: Path | None = None,
    graph_root: Path | None = None,
    deep: bool = False,
) -> tuple[dict[str, str], dict[str, Any], dict[str, Any]]:
    """Compute liveness-independent production readiness evidence."""

    active_engine = graph_engine if graph_engine is not None else engine
    active_engine_root = (
        Path(engine_root)
        if engine_root is not None
        else Path(__file__).resolve().parent.parent
    )
    active_graph_root = Path(graph_root) if graph_root is not None else GRAPH_DIR
    identity = _public_runtime_identity(
        engine_root=active_engine_root,
        graph_root=active_graph_root,
    )
    embedding = active_engine.embedding_status(deep=deep)
    readiness = _READINESS_CACHE.snapshot(
        active_engine,
        engine_root=active_engine_root,
        graph_root=active_graph_root,
        runtime_identity=identity,
        embedding_status=embedding,
        force_refresh=deep,
    )
    return identity, embedding, readiness


def _stats_snapshot(
    *,
    graph_engine: GraphEngine | None = None,
    engine_root: Path | None = None,
    graph_root: Path | None = None,
    deep: bool = False,
) -> dict[str, Any]:
    """Return compatibility stats with evidence-backed health fields."""

    active_engine = graph_engine if graph_engine is not None else engine
    stats = active_engine.stats().model_dump()
    identity, embedding, readiness = _readiness_snapshot(
        graph_engine=active_engine,
        engine_root=engine_root,
        graph_root=graph_root,
        deep=deep,
    )
    stats["liveness"] = {"alive": True}
    stats["readiness"] = readiness
    stats["healthy"] = readiness["production_ready"]
    stats["runtime_identity"] = identity
    stats["embedding"] = embedding
    integrity_status = "not_ready"
    if readiness["production_ready"]:
        integrity_status = "verified_production"
    elif readiness.get("development_ready"):
        integrity_status = "verified_development"
    stats["physical_integrity"] = {
        "status": integrity_status,
        "production_ready": readiness["production_ready"],
        "development_ready": readiness.get("development_ready", False),
        "source": "existing deep readiness contract",
    }
    project_reality_diagnostics = getattr(
        active_engine,
        "project_reality_diagnostics",
        None,
    )
    stats["effective_project_reality"] = (
        project_reality_diagnostics()
        if callable(project_reality_diagnostics)
        else {
            "status": "unavailable",
            "reason": "project_reality_diagnostics_not_supported",
        }
    )
    return stats


# ── 前端入口 ──

@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = FRONTEND_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Neural Memory</h1><p>Frontend not found.</p>")


# ── 图数据 (前端渲染用) ──

@app.get("/api/graph")
async def get_graph():
    return engine.export_graph()


@app.get("/api/stats")
async def get_stats(deep: bool = False):
    return _stats_snapshot(deep=deep)


@app.get("/api/health/live")
async def get_liveness():
    return {
        "schema": "3can.liveness/v1",
        "alive": True,
        "runtime_identity": _public_runtime_identity(),
    }


@app.get("/api/health/ready")
async def get_readiness(deep: bool = False):
    _, _, readiness = _readiness_snapshot(deep=deep)
    return JSONResponse(
        content=readiness,
        status_code=200 if readiness["production_ready"] else 503,
    )


@app.get("/api/embedding/status")
async def get_embedding_status(deep: bool = False):
    """Expose read-only embedding/cache diagnostics without graph mutation."""

    return engine.embedding_status(deep=deep)


# ── Token usage / cost metering ──

@app.post("/api/token-usage/events")
async def record_token_usage_event(payload: dict):
    """Record one LLM usage event.

    Prefer provider-returned usage fields. Prompt/completion text should not be
    included; metadata is sanitized before persistence.
    """
    try:
        event = _get_token_usage_store().record_event(payload)
    except ValueError as exc:
        raise HTTPException(400, sanitize_public_payload(str(exc)))
    return {"ok": True, "event": event}


@app.get("/api/token-usage/summary")
async def get_token_usage_summary(
    group_by: str | None = Query(None, description="provider|model|agent_id|session_id|task_id|usage_source|status"),
    provider: str | None = Query(None),
    model: str | None = Query(None),
    agent_id: str | None = Query(None),
    session_id: str | None = Query(None),
    task_id: str | None = Query(None),
):
    try:
        return _get_token_usage_store().summary(
            group_by=group_by,
            provider=provider,
            model=model,
            agent_id=agent_id,
            session_id=session_id,
            task_id=task_id,
        )
    except ValueError as exc:
        raise HTTPException(400, sanitize_public_payload(str(exc)))


@app.get("/api/token-usage/health")
async def get_token_usage_health():
    health = _get_token_usage_store().health()
    health["auto_importer"] = token_usage_import_state
    return sanitize_public_payload(health)


@app.get("/api/token-usage/status")
async def get_token_usage_status():
    return _get_token_usage_store().integration_status()


@app.get("/api/token-usage/overview")
async def get_token_usage_overview(
    limit: int = Query(12, ge=1, le=50),
):
    return _get_token_usage_store().overview(limit=limit)


@app.get("/api/token-usage/impact")
async def get_token_usage_impact(
    limit: int = Query(12, ge=1, le=50),
):
    return _get_token_usage_store().impact(limit=limit)


@app.get("/api/token-usage/codex-status")
async def get_codex_status_snapshot(
    max_files: int = Query(1, ge=1, le=10),
    max_events: int = Query(20, ge=1, le=5000),
):
    """Preview Codex slash-status token telemetry from local session JSONL."""
    return collect_codex_status_events(max_files=max_files, max_events=max_events)


@app.post("/api/token-usage/import/codex-status")
async def import_codex_status_usage(
    max_files: int = Query(1, ge=1, le=10),
    max_events: int = Query(5000, ge=1, le=20000),
):
    """Import Codex slash-status token telemetry into the 3CAN token ledger."""
    return _get_token_usage_store().import_codex_status_events(
        max_files=max_files,
        max_events=max_events,
    )


@app.post("/api/token-usage/estimate")
async def estimate_token_usage(payload: dict):
    return estimate_tokens_for_payload(payload)


# ── 节点 CRUD ──

@app.get("/api/nodes")
async def list_nodes(
    cluster: str | None = Query(None),
    status: str | None = Query(None),
    type: str | None = Query(None),
):
    return [n.model_dump() for n in engine.list_nodes(cluster, status, type)]


@app.get("/api/nodes/{node_id}")
async def get_node(
    node_id: str,
    agent_id: str = Query(""),
    session_instance_id: str | None = Query(None),
    route_id: str | None = Query(None),
):
    if not agent_id and (session_instance_id is not None or route_id is not None):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "route_correlation_invalid",
                "reason": "agent_id_required_with_route_correlation",
            },
        )
    node = engine.get_node(node_id)
    if not node:
        raise HTTPException(404, f"节点 {node_id} 不存在")
    # Miss Healer: 自动推断route outcome
    inferred = None
    if agent_id:
        try:
            if session_instance_id is not None:
                validate_routing_context_identifier(
                    session_instance_id,
                    field_name="session_instance_id",
                )
                if route_id is None:
                    raise ValueError("route_id_required_with_session_instance_id")
            if route_id is not None:
                validate_routing_context_identifier(
                    route_id,
                    field_name="route_id",
                )
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "route_correlation_invalid",
                    "reason": str(exc),
                },
            ) from exc
        inferred = engine.infer_outcome(
            agent_id,
            node_id,
            session_instance_id=session_instance_id,
            route_id=route_id,
        )
    resp = node.model_dump()
    if inferred:
        resp["_inferred_outcome"] = inferred
    return resp


MIN_KEYWORDS = 5
MAX_NOTES = 2000
MAX_CURRENT_STATE = 400
DUPLICATE_NODE_THRESHOLD = 0.030
DUPLICATE_NODE_SCORE_RATIO = 1.15


def _reserved_error_knowledge_id(node_id: str) -> bool:
    return str(node_id or "").casefold().startswith(
        ("err-", "errcase-", "fix-", "evd-")
    )


def _guard_error_knowledge_crud(*node_ids: str) -> None:
    reserved = [
        node_id for node_id in node_ids
        if _reserved_error_knowledge_id(node_id)
    ]
    if reserved:
        raise HTTPException(
            403,
            detail={
                "error": "error_knowledge_write_requires_lifecycle_endpoint",
                "reserved_node_ids": reserved,
            },
        )


def _validate_node_quality(req: NodeCreate) -> list[str]:
    """检查节点质量, 返回warnings列表 (不阻塞, 但记录)."""
    w = []
    if len(req.activation_keywords) < MIN_KEYWORDS:
        w.append(f"activation_keywords仅{len(req.activation_keywords)}个, 建议>={MIN_KEYWORDS} (包含中英文/同义词/反义词)")
    if not req.content.description or len(req.content.description) < 10:
        w.append("description太短, 应含3-5个可搜索关键词")
    if req.content.notes and len(req.content.notes) > MAX_NOTES:
        w.append(f"notes超过{MAX_NOTES}字符, 建议分裂到多个节点 + edge连接")
    if req.content.current_state and len(req.content.current_state) > MAX_CURRENT_STATE:
        w.append(f"current_state超过{MAX_CURRENT_STATE}字符, 应精简摘要")
    return w


@app.post("/api/nodes")
async def create_node(req: NodeCreate, strict: bool = Query(False), force: bool = Query(False)):
    """创建节点. strict=True时若质量不达标返回422, 默认只返warnings.

    S66c (Ka 硬规则 R1 先查再建): force=False 时自动 route 查重,
    top1 score ≥0.8 且非自引 → 返 409 Conflict + 建议 merge 目标.
    force=True 强制创建 (少用, 紧急时).
    """
    _guard_error_knowledge_crud(req.id)
    warnings = _validate_node_quality(req)
    if strict and warnings:
        raise HTTPException(422, {"errors": warnings, "hint": "调整后重试, 或去掉?strict=true接受warnings"})

    # R1 防冗余: 先 route 查近似 (名字+描述+keywords 一起喂)
    if not force:
        try:
            desc = ""
            if req.content is not None:
                desc = getattr(req.content, 'description', '') or ''
            kw_text = ' '.join(req.activation_keywords or [])
            probe_text = ((req.name or '') + ' ' + desc + ' ' + kw_text).strip()[:400]
            if probe_text:
                _req = RoutingRequest(task=probe_text, max_nodes=3, agent_id='create-guard')
                _resp = await _route_in_worker(_req)
                top_nodes = _resp.activated_nodes or []
                top_scores = _resp.scores or {}
                if top_nodes:
                    top_id = top_nodes[0].id
                    top_name = top_nodes[0].name
                    top_score = top_scores.get(top_id, 0)
                    # 实测 route score 分布 0.03-0.05 (RRF fused), dup 通常 top1 ≥ 0.045 且显著高于 top3
                    third_score = top_scores.get(top_nodes[-1].id, 0) if len(top_nodes) >= 3 else 0
                    ratio = (top_score / third_score) if third_score > 0 else 99
                    if (
                        top_id != req.id
                        and top_score >= DUPLICATE_NODE_THRESHOLD
                        and ratio >= DUPLICATE_NODE_SCORE_RATIO
                    ):
                        raise HTTPException(409, {
                            "error": "Similar node exists (R1 先查再建)",
                            "suggested_merge_target": {"id": top_id, "name": top_name, "score": top_score},
                            "separation_ratio": round(ratio, 2),
                            "hint": "考虑 PUT /api/nodes/{id} 更新已有节点, 或带 ?force=true 强建",
                            "top3": [{"id": n.id, "name": n.name, "score": top_scores.get(n.id, 0)} for n in top_nodes],
                        })
        except HTTPException:
            raise
        except Exception as _e:
            import sys
            import traceback
            print(f"[R1 probe failed non-fatal] {_e}\n{traceback.format_exc()}", file=sys.stderr)

    try:
        node = engine.create_node(req)
    except (PermissionError, ValueError) as exc:
        message = str(exc)
        status = (
            403
            if isinstance(exc, PermissionError)
            else (409 if "case_conflict" in message else 422)
        )
        raise HTTPException(status, detail={"error": message}) from exc
    await manager.broadcast({"event": "node_created", "node": node.model_dump()})
    result = node.model_dump()
    if warnings:
        result["_warnings"] = warnings
    return result


@app.put("/api/nodes/{node_id}")
async def update_node(node_id: str, req: NodeUpdate):
    _guard_error_knowledge_crud(node_id)
    try:
        node = engine.update_node(node_id, req)
    except (PermissionError, ValueError) as exc:
        status = 403 if isinstance(exc, PermissionError) else 422
        raise HTTPException(status, detail={"error": str(exc)}) from exc
    if not node:
        raise HTTPException(404, f"节点 {node_id} 不存在")
    await manager.broadcast({"event": "node_updated", "node": node.model_dump()})
    return node.model_dump()


@app.delete("/api/nodes/{node_id}")
async def delete_node(node_id: str):
    _guard_error_knowledge_crud(node_id)
    try:
        ok = engine.delete_node(node_id)
    except (PermissionError, ValueError) as exc:
        status = 403 if isinstance(exc, PermissionError) else 422
        raise HTTPException(status, detail={"error": str(exc)}) from exc
    if not ok:
        raise HTTPException(404, f"节点 {node_id} 不存在")
    await manager.broadcast({"event": "node_deleted", "node_id": node_id})
    return {"deleted": node_id}


# ── 边 CRUD ──

@app.get("/api/edges")
async def list_edges(node_id: str | None = Query(None)):
    return [e.model_dump() for e in engine.list_edges(node_id)]


@app.post("/api/edges")
async def create_edge(req: EdgeCreate):
    _guard_error_knowledge_crud(req.source, req.target)
    if req.source == req.target:
        raise HTTPException(400, "self_edge_not_allowed")
    if req.source not in engine.nodes:
        raise HTTPException(400, f"源节点 {req.source} 不存在")
    if req.target not in engine.nodes:
        raise HTTPException(400, f"目标节点 {req.target} 不存在")
    req_type = str(getattr(req.type, "value", req.type))
    if any(
        e.source == req.source
        and e.target == req.target
        and str(getattr(e.type, "value", e.type)) == req_type
        for e in engine.edges
    ):
        raise HTTPException(409, "duplicate_edge_exists")
    try:
        edge = engine.create_edge(req)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    await manager.broadcast({"event": "edge_created", "edge": edge.model_dump()})
    return edge.model_dump()


@app.post("/api/graph/maintenance/edges")
async def cleanup_graph_edges(payload: dict):
    apply_changes = bool(payload.get("apply", False))
    sample_limit = int(payload.get("sample_limit", 50))
    result = engine.cleanup_edges(apply=apply_changes, sample_limit=sample_limit)
    if apply_changes and result.get("changed"):
        await manager.broadcast({"event": "edges_cleaned", "result": result})
    return result


@app.delete("/api/edges")
async def delete_edge(source: str = Query(...), target: str = Query(...)):
    _guard_error_knowledge_crud(source, target)
    try:
        ok = engine.delete_edge(source, target)
    except PermissionError as exc:
        raise HTTPException(403, detail={"error": str(exc)}) from exc
    if not ok:
        raise HTTPException(404, "边不存在")
    await manager.broadcast({"event": "edge_deleted", "source": source, "target": target})
    return {"deleted": f"{source} → {target}"}


# ── 路由 ──

EDGE_EVIDENCE_TYPE_PRIORITY = {
    "requires": 90,
    "depends_on": 82,
    "validates": 78,
    "blocks": 74,
    "triggers": 70,
    "feeds_into": 58,
    "updates": 48,
    "informs": 20,
}


def _edge_type_value(edge_type: Any) -> str:
    return str(getattr(edge_type, "value", edge_type))


def _edge_evidence_sort_key(item: dict[str, Any]) -> tuple[int, float]:
    return (
        EDGE_EVIDENCE_TYPE_PRIORITY.get(str(item.get("type") or ""), 10),
        float(item.get("weight") or 0.0),
    )


def _edge_evidence_item(edge: Any, node_id: str) -> dict[str, Any]:
    is_source = edge.source == node_id
    item = {
        "other": edge.target if is_source else edge.source,
        "type": _edge_type_value(edge.type),
        "dir": "out" if is_source else "in",
        "weight": round(float(edge.weight or 0.0), 3),
    }
    description = (edge.description or "").strip()
    if description:
        item["why"] = description[:100]
    return item


def _build_edge_evidence_map(
    edges: list[Any],
    node_ids: list[str],
    max_per_node: int = 2,
) -> dict[str, list[dict[str, Any]]]:
    selected = set(node_ids)
    evidence: dict[str, list[dict[str, Any]]] = {node_id: [] for node_id in selected}
    for edge in edges:
        if edge.source in selected:
            evidence[edge.source].append(_edge_evidence_item(edge, edge.source))
        if edge.target in selected:
            evidence[edge.target].append(_edge_evidence_item(edge, edge.target))
    return {
        node_id: sorted(items, key=_edge_evidence_sort_key, reverse=True)[:max_per_node]
        for node_id, items in evidence.items()
        if items
    }


def _compact_error_case(node: Any) -> dict[str, Any] | None:
    """Expose the useful error lifecycle without returning raw evidence payloads."""
    node_id = str(getattr(node, "id", "") or "")
    content = getattr(node, "content", None)
    extra = getattr(content, "extra", {}) if content is not None else {}
    if not isinstance(extra, dict):
        extra = {}
    if not node_id.startswith("ERR-") and not any(
        key in extra for key in ("case_status", "occurrence_count", "current_resolution_id")
    ):
        return None

    evidence = extra.get("verification_evidence") or []
    if not isinstance(evidence, list):
        evidence = []
    typed_evidence = [item for item in evidence if isinstance(item, dict)]
    verified_evidence = []
    for item in typed_evidence:
        metadata = item.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        verification_status = str(
            item.get("verification_status")
            or metadata.get("verification_status")
            or ""
        ).strip()
        verifier = str(item.get("verifier") or metadata.get("verifier") or "").strip()
        reference = str(item.get("ref") or item.get("reference") or "").strip()
        integrity = item.get("digest") or metadata.get("digest")
        if (
            item.get("verified") is True
            and str(item.get("kind") or "").strip()
            and reference
            and verifier
            and integrity
            and verification_status == "signed_attestation_verified"
        ):
            verified_evidence.append(item)

    case_status = str(
        extra.get("case_status")
        or extra.get("state")
        or ("resolved" if extra.get("current_resolution_id") else "active")
    )
    try:
        occurrence_count = max(0, int(extra.get("occurrence_count") or 0))
    except (TypeError, ValueError):
        occurrence_count = 0
    result: dict[str, Any] = {
        "status": case_status[:32],
        "occurrence_count": occurrence_count,
        "verification_evidence_count": len(typed_evidence),
        "verified_evidence_count": len(verified_evidence),
        "verified": bool(verified_evidence) and case_status == "resolved",
    }
    root_cause = str(extra.get("root_cause") or extra.get("diagnosis") or "").strip()
    solution_summary = str(extra.get("solution_summary") or extra.get("solution") or "").strip()
    fixed_in = str(extra.get("fixed_in") or "").strip()
    resolution_id = str(extra.get("current_resolution_id") or "").strip()
    resolved_at = str(extra.get("resolved_at") or "").strip()
    if root_cause:
        result["root_cause"] = root_cause[:160]
    if solution_summary:
        result["solution_summary"] = solution_summary[:200]
    if fixed_in:
        result["fixed_in"] = fixed_in[:160]
    if resolution_id:
        result["resolution_id"] = resolution_id[:80]
    if resolved_at:
        result["resolved_at"] = resolved_at[:64]
    return result


def _pack_skeleton(node, edge_evidence: list[dict[str, Any]] | None = None) -> dict:
    """最小 skeleton: ~20 token/节点, 仅足够判断是否 /api/retrieve 展开."""
    item = {
        "id": node.id,
        "name": node.name,
        "kw": node.activation_keywords[:6],
        "summary": (node.content.description or "")[:80],
    }
    if edge_evidence:
        item["edge_evidence"] = edge_evidence[:1]
    error_case = _compact_error_case(node)
    if error_case:
        item["error_case"] = error_case
    return item


def _pack_slim(node, edge_evidence: list[dict[str, Any]] | None = None) -> dict:
    """slim (默认, v8 behavior): ~50 token/节点."""
    item = {
        "id": node.id,
        "name": node.name,
        "cluster": node.cluster,
        "type": node.type,
        "summary": (node.content.description or "")[:120],
        "current_state": (node.content.current_state or "")[:80],
        "key_files": node.content.key_files[:3],
    }
    if edge_evidence:
        item["edge_evidence"] = edge_evidence[:2]
    error_case = _compact_error_case(node)
    if error_case:
        item["error_case"] = error_case
    return item


def _packed_item_id(item: dict[str, Any]) -> str:
    value = item.get("id")
    return str(value) if value else ""


def _core_memory_graph(route_meta: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(route_meta, dict):
        return None
    core = route_meta.get("core_memory_graph")
    return core if isinstance(core, dict) else None


def _must_consume_node_ids(route_meta: dict[str, Any] | None) -> list[str]:
    core = _core_memory_graph(route_meta)
    if not core:
        return []
    values = core.get("must_consume_node_ids")
    if not isinstance(values, list):
        return []
    return [str(item) for item in values if item]


def _verified_solution_bundle_node_ids(
    route_meta: dict[str, Any] | None,
) -> list[str]:
    """Protect complete bundles and the ErrorCase anchor of partial bundles.

    Complete bundle membership is derived from the structured fields instead
    of trusting the redundant ``required_node_ids`` declaration. The API
    boundary validates that declaration separately before delivery.
    """

    if not isinstance(route_meta, dict):
        return []
    error_policy = route_meta.get("error_route_policy")
    if not isinstance(error_policy, dict):
        return []
    bundles = error_policy.get("verified_solution_bundles")
    if not isinstance(bundles, list):
        return []
    protected: list[str] = []
    for bundle in bundles:
        if not isinstance(bundle, dict):
            continue
        selection_status = str(bundle.get("selection_status") or "")
        if selection_status == "partial":
            case_id = str(bundle.get("case_id") or "").strip()
            if case_id:
                protected.append(case_id)
            continue
        if selection_status != "complete":
            continue
        structured_ids = [
            str(bundle.get(key) or "").strip()
            for key in ("case_id", "resolution_id", "evidence_id")
        ]
        if (
            any(not node_id for node_id in structured_ids)
            or len(set(structured_ids)) != 3
        ):
            continue
        protected.extend(structured_ids)
    return list(dict.fromkeys(protected))


def _sync_verified_solution_bundle_delivery(
    route_meta: dict[str, Any] | None,
    kept_ids: set[str],
) -> None:
    """Make ErrorKnowledge bundle metadata match the nodes actually delivered."""

    if not isinstance(route_meta, dict):
        return
    error_policy = route_meta.get("error_route_policy")
    if not isinstance(error_policy, dict):
        return
    bundles = error_policy.get("verified_solution_bundles")
    if isinstance(bundles, list):
        for bundle in bundles:
            if not isinstance(bundle, dict):
                continue
            structured_ids = [
                str(bundle.get(key) or "").strip()
                for key in ("case_id", "resolution_id", "evidence_id")
            ]
            if (
                any(not node_id for node_id in structured_ids)
                or len(set(structured_ids)) != 3
            ):
                continue
            missing = [
                node_id
                for node_id in structured_ids
                if node_id not in kept_ids
            ]
            bundle["missing_node_ids"] = missing
            bundle["selection_status"] = (
                "complete" if not missing else "partial"
            )
    for key in (
        "attached_solution_node_ids",
        "attached_evidence_node_ids",
    ):
        values = error_policy.get(key)
        if isinstance(values, list):
            error_policy[key] = [
                str(item) for item in values
                if item and str(item) in kept_ids
            ]


def _require_verified_solution_bundle_nodes(
    route_meta: dict[str, Any] | None,
    available_node_ids: set[str],
) -> set[str]:
    """Fail closed when metadata claims a complete bundle that is not present."""

    required: set[str] = set()
    error_policy = (
        route_meta.get("error_route_policy")
        if isinstance(route_meta, dict)
        else None
    )
    bundles = (
        error_policy.get("verified_solution_bundles")
        if isinstance(error_policy, dict)
        else None
    )
    if isinstance(bundles, list):
        for bundle_index, bundle in enumerate(bundles):
            if not isinstance(bundle, dict):
                continue
            selection_status = str(
                bundle.get("selection_status") or ""
            ).strip()
            if selection_status not in {"complete", "partial"}:
                continue
            structured_ids = [
                str(bundle.get(key) or "").strip()
                for key in ("case_id", "resolution_id", "evidence_id")
            ]
            declared_ids = bundle.get("required_node_ids")
            normalized_declared = (
                [str(item).strip() for item in declared_ids if str(item).strip()]
                if isinstance(declared_ids, list)
                else []
            )
            if (
                any(not node_id for node_id in structured_ids)
                or len(set(structured_ids)) != 3
                or len(normalized_declared) != 3
                or set(normalized_declared) != set(structured_ids)
            ):
                raise HTTPException(
                    500,
                    detail={
                        "error": "verified_solution_bundle_metadata_invalid",
                        "bundle_index": bundle_index,
                        "structured_node_ids": structured_ids,
                        "required_node_ids": normalized_declared,
                    },
                )
            if selection_status == "complete":
                required.update(structured_ids)
            else:
                # A partial bundle must retain its ErrorCase anchor while the
                # missing resolution/evidence remain explicitly discoverable.
                required.add(structured_ids[0])
    missing = sorted(required - {
        str(item) for item in available_node_ids if item
    })
    if missing:
        raise HTTPException(
            500,
            detail={
                "error": "verified_solution_bundle_incomplete",
                "missing_node_ids": missing,
                "required_node_ids": sorted(required),
            },
        )
    return required


def _sync_core_memory_delivery(
    route_meta: dict[str, Any] | None,
    kept_ids: set[str],
    *,
    budget_tokens: int | None,
    post_budget_tokens: int,
) -> None:
    """Make route_meta reflect the nodes actually delivered after API packing."""
    core = _core_memory_graph(route_meta)
    if not core:
        return
    must_consume = _must_consume_node_ids(route_meta)
    if not must_consume:
        return

    delivered = [node_id for node_id in must_consume if node_id in kept_ids]
    missing = [node_id for node_id in must_consume if node_id not in kept_ids]
    core["delivered_must_consume_node_ids"] = delivered
    core["missing_must_consume_node_ids"] = missing
    core["pack_status"] = "complete" if not missing else "partial"

    budget_policy = core.get("budget_policy")
    if not isinstance(budget_policy, dict):
        budget_policy = {}
    budget_policy.update({
        "mode": "protect_must_consume",
        "protected_must_consume_count": len(must_consume),
        "delivered_must_consume_count": len(delivered),
        "missing_must_consume_count": len(missing),
        "budget_tokens_requested": int(budget_tokens or 0),
        "post_budget_tokens": int(post_budget_tokens or 0),
        "hard_gate_overrode_budget": bool(
            budget_tokens and post_budget_tokens > int(budget_tokens) and not missing
        ),
    })
    core["budget_policy"] = budget_policy


def _enforce_budget(
    packed: list[dict],
    budget_tokens: int | None,
    *,
    protected_ids: set[str] | None = None,
) -> tuple[list[dict], bool]:
    """Strictly enforce budget, reducing protected items to tiny references."""
    if not budget_tokens or budget_tokens <= 0:
        return packed, False

    protected = {str(item) for item in (protected_ids or set()) if item}
    selected: dict[int, dict[str, Any]] = {}
    truncated = False

    # Reserve bounded references for protected nodes before optional content.
    for index, item in enumerate(packed):
        item_id = _packed_item_id(item)
        if item_id not in protected:
            continue
        reference = {"id": item_id, "summary_ref": True}
        candidate = [
            value for _, value in sorted([*selected.items(), (index, reference)])
        ]
        if _estimate_packed_tokens(candidate) <= int(budget_tokens):
            selected[index] = reference
            truncated = True
        else:
            truncated = True

    # Expand protected references only when the complete payload remains within
    # budget, then spend remaining room on optional nodes in route order.
    for index, item in enumerate(packed):
        if index not in selected:
            continue
        candidate_items = dict(selected)
        candidate_items[index] = item
        candidate = [
            value for _, value in sorted(candidate_items.items())
        ]
        if _estimate_packed_tokens(candidate) <= int(budget_tokens):
            selected[index] = item
        else:
            truncated = True

    for index, item in enumerate(packed):
        if index in selected or _packed_item_id(item) in protected:
            continue
        candidate_items = dict(selected)
        candidate_items[index] = item
        candidate = [
            value for _, value in sorted(candidate_items.items())
        ]
        if _estimate_packed_tokens(candidate) <= int(budget_tokens):
            selected[index] = item
        else:
            truncated = True

    kept = [value for _, value in sorted(selected.items())]
    if _estimate_packed_tokens(kept) > int(budget_tokens):
        raise RuntimeError("route budget enforcement invariant failed")
    return kept, truncated


def _estimate_packed_tokens(packed: list[dict]) -> int:
    return _estimate_json_tokens(packed)


def _estimate_json_tokens(value: Any) -> int:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return math.ceil(len(encoded) / 3.5) if encoded else 0


def _compact_route_meta_for_budget(
    route_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    """Keep only executable routing policy when the response budget is tight."""

    if not isinstance(route_meta, dict):
        return {}
    compact: dict[str, Any] = {"budget_compacted": True}
    core = _core_memory_graph(route_meta)
    if core:
        compact["core_memory_graph"] = {
            key: core[key]
            for key in (
                "must_consume_node_ids",
                "selected_must_consume_node_ids",
                "injected_node_ids",
                "delivered_must_consume_node_ids",
                "missing_must_consume_node_ids",
                "pack_status",
            )
            if key in core
        }
    error_policy = route_meta.get("error_route_policy")
    if isinstance(error_policy, dict):
        compact_error = {
            key: error_policy[key]
            for key in (
                "explicit_error_requested",
                "attached_solution_node_ids",
                "attached_evidence_node_ids",
                "verified_solution_bundles",
                "error_case_cap",
            )
            if key in error_policy
        }
        verified_ranking = error_policy.get("verified_solution_ranking")
        if isinstance(verified_ranking, dict) and verified_ranking.get("reason"):
            compact_error["verified_solution_reason"] = str(
                verified_ranking["reason"]
            )[:80]
        if compact_error:
            compact["error_route_policy"] = compact_error
    temporal_policy = route_meta.get("temporal_route_policy")
    if isinstance(temporal_policy, dict):
        compact_temporal = {
            key: temporal_policy[key]
            for key in (
                "enabled",
                "triggered_terms",
                "freshness_required",
                "validity_focus",
                "error_focus",
                "half_life_days",
                "boosted_node_count",
                "penalized_node_count",
            )
            if key in temporal_policy
        }
        if compact_temporal:
            compact["temporal_route_policy"] = compact_temporal
    current_policy = route_meta.get("current_reality_policy")
    if isinstance(current_policy, dict):
        compact_current = {
            key: current_policy[key]
            for key in (
                "enabled",
                "intent",
                "external_verification_required",
                "excluded_superseded_count",
                "excluded_project_mismatch_count",
                "demoted_sediment_count",
                "demoted_unproven_core_count",
                "boosted_durable_count",
            )
            if key in current_policy
        }
        if compact_current:
            compact["current_reality_policy"] = compact_current
    owner_defaults = route_meta.get("owner_defaults")
    if isinstance(owner_defaults, dict) and owner_defaults.get("status") == "applied":
        compact["owner_defaults"] = owner_defaults
    applicable = route_meta.get("applicable_project_reality")
    if isinstance(applicable, dict):
        compact["applicable_project_reality"] = {
            key: applicable[key]
            for key in (
                "selected_current_node_ids",
                "constraint_node_ids",
                "experience_node_ids",
                "external_verification_required",
            )
            if key in applicable
        }
    for key in ("policy_version", "route_policy_version"):
        if route_meta.get(key):
            compact[key] = route_meta[key]
    return compact


def _payload_with_nodes(
    payload: dict[str, Any],
    node_key: str,
    selected: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    candidate = dict(payload)
    candidate[node_key] = [
        item for _, item in sorted(selected.items())
    ]
    return candidate


def _enforce_route_response_budget(
    payload: dict[str, Any],
    budget_tokens: int,
    *,
    node_key: str,
    protected_ids: set[str] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Apply ``budget_tokens`` to the complete successful route response."""

    budget = int(budget_tokens or 0)
    if budget <= 0:
        return payload, False
    original = dict(payload)
    original.pop("route_token_estimate", None)
    original_nodes = [
        item for item in original.get(node_key, [])
        if isinstance(item, dict)
    ]
    available_node_ids = {
        _packed_item_id(item)
        for item in original_nodes
        if _packed_item_id(item)
    }
    protected = {str(item) for item in (protected_ids or set()) if item}
    protected.update(
        _require_verified_solution_bundle_nodes(
            original.get("route_meta"),
            available_node_ids,
        )
    )
    pre_budget_tokens = _estimate_json_tokens(original)
    estimate = {
        "pre_budget_tokens": pre_budget_tokens,
        "budget_tokens": budget,
        "response_tokens": 0,
        "post_budget_tokens": 0,
    }
    untouched = dict(original)
    untouched["route_token_estimate"] = estimate
    for _ in range(4):
        response_tokens = _estimate_json_tokens(untouched)
        estimate["response_tokens"] = response_tokens
        estimate["post_budget_tokens"] = response_tokens
        estimate["saved_by_budget_tokens"] = max(
            0, pre_budget_tokens - response_tokens
        )
    if _estimate_json_tokens(untouched) <= budget:
        return untouched, False

    base: dict[str, Any] = {}
    if "route_response_schema" in original:
        base["route_response_schema"] = original["route_response_schema"]
    if "mode" in original:
        base["mode"] = original["mode"]
    base[node_key] = []
    if "relevant_edges" in original:
        base["relevant_edges"] = []
    if "scores" in original:
        base["scores"] = {}
    for key in ("total_nodes", "total_edges"):
        if key in original:
            base[key] = original[key]
    base["budget_truncated"] = True

    if _estimate_json_tokens(base) > budget:
        minimum: dict[str, Any] = {
            node_key: [],
            "budget_truncated": True,
        }
        if "route_response_schema" in original:
            minimum["route_response_schema"] = original[
                "route_response_schema"
            ]
        if "mode" in original:
            minimum["mode"] = original["mode"]
        minimum_required = _estimate_json_tokens(minimum)
        if minimum_required > budget:
            raise HTTPException(
                413,
                detail={
                    "error": "route_budget_too_small",
                    "budget_tokens": budget,
                    "minimum_budget_tokens": minimum_required,
                },
            )
        base = minimum

    selected: dict[int, dict[str, Any]] = {}
    for index, item in enumerate(original_nodes):
        item_id = _packed_item_id(item)
        if item_id not in protected:
            continue
        selected[index] = {"id": item_id, "summary_ref": True}
    protected_minimum = _payload_with_nodes(base, node_key, selected)
    protected_minimum_tokens = _estimate_json_tokens(protected_minimum)
    if protected_minimum_tokens > budget:
        raise HTTPException(
            413,
            detail={
                "error": "route_budget_too_small_for_required_references",
                "budget_tokens": budget,
                "minimum_budget_tokens": protected_minimum_tokens,
                "required_node_ids": sorted(protected),
            },
        )

    kept_ids = {
        _packed_item_id(item)
        for item in selected.values()
        if _packed_item_id(item)
    }
    route_meta = original.get("route_meta")
    if isinstance(route_meta, dict):
        _sync_core_memory_delivery(
            route_meta,
            kept_ids,
            budget_tokens=budget,
            post_budget_tokens=_estimate_json_tokens(
                _payload_with_nodes(base, node_key, selected)
            ),
        )
        _sync_verified_solution_bundle_delivery(route_meta, kept_ids)
        compact_meta = _compact_route_meta_for_budget(route_meta)
        required_project_meta = {
            key: route_meta[key]
            for key in ("owner_defaults", "applicable_project_reality")
            if key in route_meta
        }
        if required_project_meta:
            required_candidate = _payload_with_nodes(
                base,
                node_key,
                selected,
            )
            required_candidate["route_meta"] = required_project_meta
            minimum_required = _estimate_json_tokens(required_candidate)
            if minimum_required > budget:
                raise HTTPException(
                    413,
                    detail={
                        "error": (
                            "route_budget_too_small_for_project_reality"
                        ),
                        "budget_tokens": budget,
                        "minimum_budget_tokens": minimum_required,
                    },
                )
            base["route_meta"] = required_project_meta
        if compact_meta:
            candidate = _payload_with_nodes(base, node_key, selected)
            candidate["route_meta"] = compact_meta
            if _estimate_json_tokens(candidate) <= budget:
                base["route_meta"] = compact_meta

    for index, item in enumerate(original_nodes):
        if index not in selected:
            continue
        candidate_selected = dict(selected)
        candidate_selected[index] = item
        candidate = _payload_with_nodes(base, node_key, candidate_selected)
        if _estimate_json_tokens(candidate) <= budget:
            selected = candidate_selected

    for index, item in enumerate(original_nodes):
        if index in selected or _packed_item_id(item) in protected:
            continue
        candidate_selected = dict(selected)
        candidate_selected[index] = item
        candidate = _payload_with_nodes(base, node_key, candidate_selected)
        if _estimate_json_tokens(candidate) <= budget:
            selected = candidate_selected

    working = _payload_with_nodes(base, node_key, selected)
    kept_ids = {
        _packed_item_id(item)
        for item in selected.values()
        if _packed_item_id(item)
    }
    _sync_verified_solution_bundle_delivery(
        working.get("route_meta"),
        kept_ids,
    )
    scores = original.get("scores")
    if isinstance(scores, dict) and "scores" in working:
        kept_scores: dict[str, Any] = {}
        for node_id in kept_ids:
            if node_id not in scores:
                continue
            candidate_scores = {**kept_scores, node_id: scores[node_id]}
            candidate = dict(working)
            candidate["scores"] = candidate_scores
            if _estimate_json_tokens(candidate) <= budget:
                working = candidate
                kept_scores = candidate_scores

    edges = original.get("relevant_edges")
    if isinstance(edges, list) and "relevant_edges" in working:
        kept_edges: list[dict[str, Any]] = []
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            if (
                str(edge.get("source") or "") not in kept_ids
                or str(edge.get("target") or "") not in kept_ids
            ):
                continue
            candidate_edges = [*kept_edges, edge]
            candidate = dict(working)
            candidate["relevant_edges"] = candidate_edges
            if _estimate_json_tokens(candidate) <= budget:
                working = candidate
                kept_edges = candidate_edges

    for key in (
        "confidence",
        "confidence_meta",
        "fallback_hint",
        "low_confidence_acknowledged",
    ):
        if key not in original:
            continue
        candidate = dict(working)
        candidate[key] = original[key]
        if _estimate_json_tokens(candidate) <= budget:
            working = candidate

    final_estimate = {
        "pre_budget_tokens": pre_budget_tokens,
        "budget_tokens": budget,
        "response_tokens": 0,
        "post_budget_tokens": 0,
    }
    candidate = dict(working)
    candidate["route_token_estimate"] = final_estimate
    for _ in range(4):
        response_tokens = _estimate_json_tokens(candidate)
        final_estimate["response_tokens"] = response_tokens
        final_estimate["post_budget_tokens"] = response_tokens
        final_estimate["saved_by_budget_tokens"] = max(
            0, pre_budget_tokens - response_tokens
        )
    if _estimate_json_tokens(candidate) <= budget:
        working = candidate

    if _estimate_json_tokens(working) > budget:
        raise RuntimeError("route response budget enforcement invariant failed")
    return working, True


# v9.2 Path 4: Agentic RAG fallback 信号
LOW_CONF_THRESHOLD = 0.030
BGE_LOW_CONF_THRESHOLD = 0.015
ROUTE_RESPONSE_SCHEMA = "3can.route-response/v1"
SPARSE_TOP_GAP = 1.15         # top1/top3 < 1.15 → 结果扁平, 可能没真命中


def _compute_confidence(
    scores: dict,
    ordered_node_ids: list[str] | None = None,
    *,
    low_conf_threshold: float = LOW_CONF_THRESHOLD,
) -> tuple[str, dict]:
    """返回 ('high'|'medium'|'low', meta) 基于 top1 score + top1/top3 比."""
    if not scores:
        return "low", {"reason": "empty"}
    ordered = [
        node_id
        for node_id in (ordered_node_ids or list(scores))
        if node_id in scores
    ]
    if not ordered:
        return "low", {"reason": "empty_ordered_results"}
    vals = [float(scores[node_id]) for node_id in ordered]
    top1 = vals[0]
    top3 = vals[2] if len(vals) >= 3 else vals[-1]
    gap = (top1 / top3) if top3 > 0 else float("inf")
    common = {
        "top1": top1,
        "top3": top3,
        "gap": gap,
        "top1_node_id": ordered[0],
        "low_conf_threshold": low_conf_threshold,
    }
    if top1 < low_conf_threshold:
        return "low", {**common, "reason": "top1_below_threshold"}
    if gap < SPARSE_TOP_GAP and top1 < low_conf_threshold * 2:
        return "medium", {**common, "reason": "flat_distribution"}
    return "high", common


@app.post("/api/route")
async def route_task(req: RoutingRequest, detail: bool = Query(False)):
    """路由查询. mode=skeleton|slim|full (默认 slim 向后兼容). budget_tokens 硬限总包.
    - skeleton: ~20 token/节点, Entroly CCR 风格, agent 按需调 /api/retrieve/{id} 取全量
    - slim: ~50 token/节点 (v8 behavior)
    - full: 全量 Node (~500-800 token/节点)
    - detail=true 旧参数保留, 等同 mode=full
    """
    req, owner_assertion_origin = _request_with_owner_intent(req)
    result = await _route_in_worker(req)
    if req.owner_intent:
        result.route_meta["owner_defaults"] = {
            **req.owner_intent,
            "assertion_origin": owner_assertion_origin,
        }
    if req.project_id and req.project_namespace:
        result.route_meta["applicable_project_reality"] = (
            _applicable_project_reality(req, result)
        )
    semantic_result_ids = result.route_meta.get("semantic_result_ids")
    if not isinstance(semantic_result_ids, list):
        semantic_result_ids = [node.id for node in result.activated_nodes]
    embedding_backend = str(result.route_meta.get("embedding_backend") or "")
    low_conf_threshold = (
        BGE_LOW_CONF_THRESHOLD
        if embedding_backend.startswith("sentence-transformers:BAAI/bge-m3@")
        else LOW_CONF_THRESHOLD
    )
    confidence, conf_meta = _compute_confidence(
        result.scores,
        [str(node_id) for node_id in semantic_result_ids],
        low_conf_threshold=low_conf_threshold,
    )
    fallback_hint = None
    if confidence == "low":
        fallback_hint = "route low-confidence: agent 应考虑 (1) 换 query 重试 (2) /api/skills 找匹配 skill (3) WebSearch 外部核验"
        # v9.4 基座#31 Confidence Hard Gate: low 时必须显式 confirm 才返结果
        if not (req.confirm_low_confidence or req.allow_degraded):
            raise HTTPException(
                status_code=428,  # Precondition Required
                detail={
                    "error": "low_confidence_requires_confirmation",
                    "confidence": confidence,
                    "confidence_meta": conf_meta,
                    "fallback_hint": fallback_hint,
                    "guidance": f"本次 route 低置信 (top1 score<{low_conf_threshold}). Agent 必须显式承认看到了此警告, 传 confirm_low_confidence=true (或 allow_degraded=true) 重试, 才获取结果. 这是防 agent 默默忽略低置信结果.",
                    "suggested_action": "先查 /api/skills 找匹配 skill, 或换 query 重试, 或接受降级 (设 confirm_low_confidence=true)",
                },
            )

    mode = "full" if detail else (req.mode or "slim").lower()
    protected_ids = set(_must_consume_node_ids(result.route_meta))
    protected_ids.update(
        _require_verified_solution_bundle_nodes(
            result.route_meta,
            {node.id for node in result.activated_nodes},
        )
    )
    low_confidence_acknowledged = (
        req.confirm_low_confidence or req.allow_degraded
        if confidence == "low"
        else None
    )
    if mode == "full":
        full_payload = result.model_dump(mode="json")
        full_payload.update(
            {
                "route_response_schema": ROUTE_RESPONSE_SCHEMA,
                "confidence": confidence,
                "confidence_meta": conf_meta,
                "fallback_hint": fallback_hint,
                "low_confidence_acknowledged": low_confidence_acknowledged,
            }
        )
        if not req.budget_tokens:
            return full_payload
        budgeted, _ = _enforce_route_response_budget(
            full_payload,
            req.budget_tokens,
            node_key="activated_nodes",
            protected_ids=protected_ids,
        )
        return budgeted

    packer = _pack_skeleton if mode == "skeleton" else _pack_slim
    max_evidence = 1 if mode == "skeleton" else 2
    node_ids = [n.id for n in result.activated_nodes]
    edge_evidence = _build_edge_evidence_map(result.relevant_edges, node_ids, max_evidence)
    packed = [packer(n, edge_evidence.get(n.id)) for n in result.activated_nodes]
    pre_budget_tokens = _estimate_packed_tokens(packed)
    kept_ids = {item["id"] for item in packed}
    response = {
        "route_response_schema": ROUTE_RESPONSE_SCHEMA,
        "mode": mode,
        "nodes": packed,
        "scores": {k: v for k, v in result.scores.items() if k in kept_ids},
        "total_nodes": result.total_nodes,
        "total_edges": result.total_edges,
        "budget_truncated": False,
        "confidence": confidence,  # v9.2 Path 4: 'high' | 'medium' | 'low'
        "confidence_meta": conf_meta,
        "route_meta": result.route_meta,
        "fallback_hint": fallback_hint,
        "low_confidence_acknowledged": low_confidence_acknowledged,
    }
    if req.budget_tokens:
        budgeted, _ = _enforce_route_response_budget(
            response,
            req.budget_tokens,
            node_key="nodes",
            protected_ids=protected_ids,
        )
        return budgeted
    _sync_core_memory_delivery(
        result.route_meta,
        kept_ids,
        budget_tokens=None,
        post_budget_tokens=pre_budget_tokens,
    )
    response["route_token_estimate"] = {
        "pre_budget_tokens": pre_budget_tokens,
        "post_budget_tokens": pre_budget_tokens,
        "response_tokens": pre_budget_tokens,
        "budget_tokens": None,
        "saved_by_budget_tokens": 0,
    }
    return response


@app.get("/api/retrieve/{node_id}")
async def retrieve_node(node_id: str, agent_id: str = Query("unknown")):
    """CCR 第二段: agent 从 skeleton 选定后取全量 content. 记 expand 活动用于学习."""
    node = engine.get_node(node_id)
    if not node:
        raise HTTPException(404, f"节点 {node_id} 不存在")
    engine.log_activity(agent_id, "expand", f"retrieve {node_id}", affected_nodes=[node_id])
    return node.model_dump()


@app.get("/api/route/simple")
async def route_simple(
    q: str = Query(..., description="查询文本"),
    max_nodes: int = Query(6, ge=1, le=100),
    agent_id: str = Query("unknown"),
    detail: bool = Query(False),
    mode: str = Query("slim"),
    budget_tokens: int | None = Query(None, ge=1, le=1_000_000),
    confirm_low_confidence: bool = Query(False),
    allow_degraded: bool = Query(False),
):
    """GET 旁路: 绕开 curl -d body 引号逃逸问题 (中文/嵌套引号场景).
    用法: curl 'http://localhost:9700/api/route/simple?q=xxx&max_nodes=4&mode=skeleton&budget_tokens=400'
    语义等价于 POST /api/route, 返回结构一致.
    """
    req = RoutingRequest(
        task=q,
        max_nodes=max_nodes,
        agent_id=agent_id,
        include_edges=True,
        mode=mode,
        budget_tokens=budget_tokens,
        confirm_low_confidence=confirm_low_confidence,
        allow_degraded=allow_degraded,
    )
    return await route_task(req, detail=detail)


# Route outcome信号 — Layer 2 outcome-gated learning
@app.post("/api/route/outcome")
async def route_outcome(payload: dict):
    """记录route outcome信号。不是click就学，是verified usage才学。
    signal: +1.0=agent确认使用, -1.0=跳过后grep了别的, +0.3=部分使用
    query自动uppercase做key，3次positive后mapping进active。
    """
    query = str(payload.get("query") or "").strip()
    node_id = str(payload.get("node_id") or "").strip()
    raw_signal = payload.get("signal", 1.0)
    try:
        result = engine.record_route_feedback(query, [(node_id, raw_signal)])
        signal = float(raw_signal)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, detail={"error": str(exc)}) from exc
    return {
        "recorded": result["recorded"],
        "query": query,
        "node_id": node_id,
        "signal": signal,
    }


@app.post("/api/route/feedback")
async def route_feedback(payload: dict):
    """Record an explicit route correction through the canonical outcome owner.

    Body: {query, correct_node_ids: [...], agent_id, wrong_node_ids?: [...]}
    """
    raw_query = payload.get("query")
    if not isinstance(raw_query, str) or not raw_query.strip():
        raise HTTPException(400, "query和correct_node_ids必填")
    query = raw_query.strip()
    correct = payload.get("correct_node_ids", [])
    wrong = payload.get("wrong_node_ids", [])
    agent_id = payload.get("agent_id", "unknown")
    if not query or not correct:
        raise HTTPException(400, "query和correct_node_ids必填")
    if not isinstance(correct, list) or not isinstance(wrong, list):
        raise HTTPException(400, "correct_node_ids和wrong_node_ids必须是list")
    try:
        result = engine.record_route_feedback(
            query,
            [
                *((str(node_id), 1.0) for node_id in correct),
                *((str(node_id), -1.0) for node_id in wrong),
            ],
            promote_keywords=True,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, detail={"error": str(exc)}) from exc

    return {
        "recorded": result["recorded"],
        "agent_id": agent_id,
        "learning_owner": "graph_engine.route_feedback",
        "boosted": result["promoted"],
    }


# ── Route Ticket (SQLite transactional ledger) ──
_ROUTE_TICKETS_FILE = GRAPH_DIR / "route_tickets.json"  # read-only legacy import
_ROUTE_TICKET_RECEIPTS_FILE = GRAPH_DIR / "route_ticket_receipts.jsonl"


def _configured_ticket_ledger_path() -> Path:
    """Resolve the ledger without allowing production to escape its graph root."""

    graph_root = Path(GRAPH_DIR).expanduser().resolve(strict=False)
    configured = os.environ.get("THREECAN_TICKET_LEDGER_PATH", "").strip()
    if not configured:
        return graph_root / "route_ticket_ledger.sqlite3"

    candidate = Path(configured).expanduser()
    if not candidate.is_absolute():
        candidate = graph_root / candidate
    resolved = candidate.resolve(strict=False)

    # Only an explicit development mode may retain the legacy external-ledger
    # override. Production is the default, and unknown/typo modes fail closed.
    if configured_readiness_mode() != READINESS_MODE_DEVELOPMENT:
        try:
            resolved.relative_to(graph_root)
        except ValueError as exc:
            raise RuntimeError(
                "ticket_ledger_path_outside_graph_dir_in_production"
            ) from exc
    return resolved


_TICKET_LEDGER_PATH = _configured_ticket_ledger_path()
_TICKET_TTL_SEC = 900
_TICKET_POLICY_VERSION = os.environ.get(
    "THREECAN_TICKET_POLICY_VERSION",
    "3can.ticket-policy/v2",
)
_TICKETED_ERROR_CAPABILITY_SCHEMA = (
    "3can.ticketed-error-occurrence-capability/v1"
)
_TICKETED_ERROR_REQUEST_SCHEMA = "3can.ticketed-error-occurrence/v1"
_TICKETED_ERROR_RECEIPT_SCHEMA = (
    "3can.ticketed-error-occurrence-receipt/v1"
)
_UNTICKETED_ERROR_OCCURRENCE_ENV = (
    "THREECAN_ALLOW_UNTICKETED_ERROR_OCCURRENCES"
)
_COMPLETION_SENSITIVE_KEY_RE = re.compile(
    r"(?:^|[_-])(?:authorization|proxy_authorization|api_?key|access_?token|"
    r"refresh_?token|client_?secret|password|passwd|pwd|cookie|set_cookie|"
    r"private_?key|credential|secret)(?:$|[_-])",
    re.IGNORECASE,
)
_COMPLETION_SECRET_TEXT_RE = re.compile(
    r"(?i)\b(authorization|proxy-authorization|api[-_ ]?key|access[-_ ]?token|"
    r"refresh[-_ ]?token|client[-_ ]?secret|password|passwd|pwd|cookie|"
    r"set-cookie|private[-_ ]?key|credential|secret)"
    r"(\s*(?:=|:)\s*|\s+)(?:(?:Bearer|Basic)\s+[^\s,;]+|"
    r"\"[^\"]*\"|'[^']*'|[^\s,;]+)",
)
_COMPLETION_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|password|"
    r"passwd|client[-_]?secret|secret))(?:\s+|=)(?:\"[^\"]*\"|'[^']*'|\S+)",
)
_COMPLETION_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"AKIA[A-Z0-9]{16}|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]{12,}\."
    r"[A-Za-z0-9_-]{8,})(?![A-Za-z0-9])"
)
_COMPLETION_BEARER_RE = re.compile(
    r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}"
)
_COMPLETION_HIGH_ENTROPY_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z0-9_+/=-]{64,}(?![A-Za-z0-9])"
)
_COMPLETION_USER_HOME_RE = re.compile(
    r"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+"
    r"|(?:/home|/users)/[^/\s\"']+"
    r"|\\\\[^\\/\r\n]+[\\/][^\\/\r\n]+[\\/][^\\/\r\n]+)"
)
_COMPLETION_PEM_RE = re.compile(
    r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----",
    re.DOTALL,
)
_TICKET_LEDGER_INSTANCE: tuple[str, TicketLedger] | None = None
_TICKETED_ERROR_CRASH_HOOK: Any = None


def _sanitize_completion_audit_request(value: Any, depth: int = 0) -> Any:
    """Redact the server-owned completion journal copy before persistence."""

    if depth > 6:
        return "<depth-limit>"
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (raw_key, nested) in enumerate(value.items()):
            if index >= 80:
                result["_truncated_items"] = True
                break
            key = re.sub(r"\s+", " ", str(raw_key)).strip()[:120]
            if _COMPLETION_SENSITIVE_KEY_RE.search(key):
                result[key] = "<redacted>"
            else:
                result[key] = _sanitize_completion_audit_request(
                    nested,
                    depth + 1,
                )
        return result
    if isinstance(value, (list, tuple)):
        return [
            _sanitize_completion_audit_request(item, depth + 1)
            for item in list(value)[:80]
        ]
    if isinstance(value, str):
        text = _COMPLETION_PEM_RE.sub("<redacted-private-key>", value)
        text = _COMPLETION_SECRET_TEXT_RE.sub(
            lambda match: f"{match.group(1)}{match.group(2)}<redacted>",
            text,
        )
        text = _COMPLETION_CLI_SECRET_RE.sub(r"\1 <redacted>", text)
        text = _COMPLETION_TOKEN_RE.sub("<redacted-token>", text)
        text = _COMPLETION_BEARER_RE.sub("Bearer <redacted>", text)
        text = _COMPLETION_HIGH_ENTROPY_RE.sub("<redacted-token>", text)
        text = _COMPLETION_USER_HOME_RE.sub("<user-home>", text)
        return re.sub(r"\s+", " ", text).strip()[:2000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:500]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _ticket_ledger() -> TicketLedger:
    global _TICKET_LEDGER_INSTANCE
    path = str(Path(_TICKET_LEDGER_PATH).resolve())
    if _TICKET_LEDGER_INSTANCE is None or _TICKET_LEDGER_INSTANCE[0] != path:
        _TICKET_LEDGER_INSTANCE = (
            path,
            TicketLedger(
                path,
                legacy_tickets_path=_ROUTE_TICKETS_FILE,
                legacy_receipts_path=_ROUTE_TICKET_RECEIPTS_FILE,
                busy_timeout_ms=int(
                    os.environ.get("THREECAN_TICKET_BUSY_TIMEOUT_MS", "5000")
                ),
                completion_owner_ttl_sec=int(
                    os.environ.get("THREECAN_COMPLETION_OWNER_TTL_SEC", "30")
                ),
                completion_grace_sec=int(
                    os.environ.get("THREECAN_COMPLETION_GRACE_SEC", "3600")
                ),
            ),
        )
    return _TICKET_LEDGER_INSTANCE[1]


def _normalize_lease_values(values: Any) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        values = [values] if values else []
    return sorted({
        re.sub(r"\s+", " ", str(value or "").strip()).casefold()
        for value in values
        if str(value or "").strip()
    })


def _identity_value(payload: Mapping[str, Any], name: str, env_name: str) -> str:
    value = str(payload.get(name) or os.environ.get(env_name) or "unspecified").strip()
    return re.sub(r"\s+", " ", value)[:200]


def _execution_identity_context(payload: Mapping[str, Any]) -> dict[str, str]:
    context: dict[str, str] = {}
    for field, env_name in (
        ("project_id", "THREECAN_PROJECT_ID"),
        ("project_namespace", "THREECAN_PROJECT_NAMESPACE"),
        ("workspace_id", "THREECAN_WORKSPACE_ID"),
        ("workorder_id", "THREECAN_WORKORDER_ID"),
    ):
        value = str(payload.get(field) or os.environ.get(env_name) or "").strip()
        if not value:
            continue
        try:
            context[field] = validate_routing_context_identifier(
                value,
                field_name=field,
            )
        except ValueError as exc:
            raise HTTPException(
                400,
                detail={"error": str(exc)},
            ) from exc
    return context


def _specified_identity(value: Any) -> str:
    normalized = str(value or "").strip()
    return "" if normalized.casefold() == "unspecified" else normalized


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _target_allowed_roots() -> list[Path]:
    configured = [
        Path(item.strip()).expanduser()
        for item in os.environ.get("THREECAN_TARGET_ROOTS", "").split(os.pathsep)
        if item.strip()
    ]
    roots = [_default_project_dir(), *configured]
    resolved: list[Path] = []
    for root in roots:
        candidate = root.resolve(strict=False)
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def _target_root_for(path: Path, roots: list[Path]) -> Path | None:
    return next(
        (
            root
            for root in roots
            if path == root or root in path.parents
        ),
        None,
    )


def _git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    if result.returncode:
        raise ValueError("git_identity_unavailable")
    return result.stdout.strip()


def _repository_key(remote: str) -> str:
    value = str(remote or "").strip()
    scp_match = re.fullmatch(r"(?:[^@]+@)?([^:]+):(.+)", value)
    if scp_match and "://" not in value:
        host, path = scp_match.groups()
    else:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            raise ValueError("git_remote_invalid")
        host, path = parsed.hostname, parsed.path
    path = path.strip("/")
    if path.casefold().endswith(".git"):
        path = path[:-4]
    if not host or not path:
        raise ValueError("git_remote_invalid")
    return f"{host.casefold()}/{path.casefold()}"


def _canonical_physical_path(value: str | Path) -> str:
    normalized = str(value).replace("\\", "/")
    wsl_drive = re.fullmatch(r"/mnt/([A-Za-z])(?:/(.*))?", normalized)
    if wsl_drive:
        drive, tail = wsl_drive.groups()
        normalized = f"{drive}:/{tail or ''}"
    if re.match(r"^[A-Za-z]:/", normalized):
        normalized = normalized.casefold()
    return normalized.rstrip("/") or "/"


def _local_path_sha256(path: Path) -> str:
    value = _canonical_physical_path(path.resolve())
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _wire_target_path(value: str) -> Path:
    """Accept the canonical Windows spelling emitted by Windows or WSL clients."""

    normalized = _canonical_physical_path(value)
    return Path(normalized)


def _verified_project_target_root(
    target: Path,
    *,
    project_id: str,
    project_namespace: str,
    workspace_id: str,
) -> Path | None:
    """Verify an out-of-allowlist target against Git plus its tracked capsule."""

    if not project_id or not workspace_id:
        return None
    probe = target if target.is_dir() else target.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        root = Path(_git_value(probe, "rev-parse", "--show-toplevel")).resolve()
        if target != root and root not in target.parents:
            raise ValueError("target_not_in_worktree")
        capsule_path = root / ".agents" / "project.json"
        capsule = json.loads(capsule_path.read_text(encoding="utf-8"))
        if not isinstance(capsule, dict):
            raise ValueError("project_capsule_invalid")
        capsule_project = validate_routing_context_identifier(
            str(capsule.get("project_id") or "").strip(),
            field_name="project_id",
        )
        capsule_namespace = validate_routing_context_identifier(
            str(capsule.get("project_namespace") or "").strip(),
            field_name="project_namespace",
        )
        if capsule_project.casefold() != project_id.casefold():
            raise ValueError("project_id_mismatch")
        if (
            project_namespace
            and capsule_namespace.casefold() != project_namespace.casefold()
        ):
            raise ValueError("project_namespace_mismatch")
        configured_root = root / str(capsule.get("project_root") or ".")
        if configured_root.resolve() != root:
            raise ValueError("project_root_mismatch")
        expected_repository = str(capsule.get("git_repository") or "").strip().casefold()
        actual_repository = _repository_key(
            _git_value(root, "remote", "get-url", "origin")
        )
        if not expected_repository or expected_repository != actual_repository:
            raise ValueError("git_repository_mismatch")
        common_dir = Path(
            _git_value(
                root,
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            )
        )
        if not common_dir.is_absolute():
            common_dir = root / common_dir
        expected_workspace = (
            f"git-{_local_path_sha256(common_dir)[:12]}-"
            f"{_local_path_sha256(root)[:12]}"
        )
        if expected_workspace.casefold() != workspace_id.casefold():
            raise ValueError("workspace_id_mismatch")
        return root
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError) as exc:
        raise HTTPException(
            403,
            detail={"error": "project_target_identity_unverified"},
        ) from exc


def _target_state_manifest(
    target_files: Any,
    *,
    project_id: str = "",
    project_namespace: str = "",
    workspace_id: str = "",
) -> list[dict[str, Any]]:
    """Snapshot target existence and content before a mutating tool runs."""

    if not isinstance(target_files, (list, tuple, set)):
        target_files = [target_files] if target_files else []
    raw_paths = sorted(
        {
            str(value).strip()
            for value in target_files
            if str(value or "").strip()
        },
        key=str.casefold,
    )
    max_files = _env_int("THREECAN_TARGET_MAX_FILES", 64, minimum=1)
    max_total_bytes = _env_int(
        "THREECAN_TARGET_MAX_TOTAL_BYTES",
        256 * 1024 * 1024,
        minimum=1,
    )
    if len(raw_paths) > max_files:
        raise HTTPException(
            413,
            detail={
                "error": "target_file_count_exceeded",
                "max_files": max_files,
            },
        )
    roots = _target_allowed_roots()
    manifest: list[dict[str, Any]] = []
    total_bytes = 0
    identity_bound = any((project_id, project_namespace, workspace_id))
    if identity_bound and not all((project_id, project_namespace, workspace_id)):
        raise HTTPException(
            400,
            detail={"error": "project_execution_identity_incomplete"},
        )
    verified_project_root: Path | None = None
    for raw in raw_paths:
        candidate = _wire_target_path(raw).expanduser()
        if not candidate.is_absolute():
            if identity_bound:
                raise HTTPException(
                    400,
                    detail={"error": "bound_target_path_must_be_absolute"},
                )
            candidate = _default_project_dir() / candidate
        resolved = candidate.resolve(strict=False)
        if identity_bound:
            root = _verified_project_target_root(
                resolved,
                project_id=project_id,
                project_namespace=project_namespace,
                workspace_id=workspace_id,
            )
        else:
            root = _target_root_for(resolved, roots)
            if root is None:
                raise HTTPException(
                    400,
                    detail={"error": "target_path_outside_allowed_roots"},
                )
        if verified_project_root is not None and root != verified_project_root:
            raise HTTPException(
                400,
                detail={"error": "target_files_span_multiple_worktrees"},
            )
        verified_project_root = root
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise HTTPException(
                400,
                detail={"error": "target_path_outside_allowed_roots"},
            ) from exc
        root_key = hashlib.sha256(
            str(root).replace("\\", "/").casefold().encode("utf-8")
        ).hexdigest()[:16]
        path_key = f"{root_key}:{relative.as_posix().casefold()}"
        if not resolved.exists():
            manifest.append({"path": path_key, "kind": "missing"})
            continue
        file_stat = resolved.stat()
        if stat.S_ISREG(file_stat.st_mode):
            total_bytes += int(file_stat.st_size)
            if total_bytes > max_total_bytes:
                raise HTTPException(
                    413,
                    detail={
                        "error": "target_total_bytes_exceeded",
                        "max_total_bytes": max_total_bytes,
                    },
                )
            manifest.append({
                "path": path_key,
                "kind": "file",
                "size": int(file_stat.st_size),
                "sha256": _sha256_file(resolved),
            })
            continue
        if stat.S_ISDIR(file_stat.st_mode):
            raise HTTPException(
                400,
                detail={"error": "target_directory_requires_explicit_files"},
            )
        raise HTTPException(
            400,
            detail={"error": "target_path_not_regular_file"},
        )
    return manifest


def _target_digest(
    target_files: Any,
    *,
    project_id: str = "",
    project_namespace: str = "",
    workspace_id: str = "",
) -> str:
    return canonical_hash(
        _target_state_manifest(
            target_files,
            project_id=project_id,
            project_namespace=project_namespace,
            workspace_id=workspace_id,
        )
    )


def _client_target_digest(payload: Mapping[str, Any]) -> str:
    candidate = str(payload.get("target_state_digest") or "").strip().casefold()
    if not candidate:
        return ""
    if not re.fullmatch(r"[0-9a-f]{64}", candidate):
        raise HTTPException(
            400,
            detail={"error": "target_state_digest_invalid"},
        )
    return candidate


def _ticket_scope_digest(
    *,
    project_id: str,
    project_namespace: str,
    workspace_id: str,
    workorder_id: str,
    task_type: str,
    scope_keywords: Any,
    target_digest: str,
) -> str:
    return canonical_hash({
        "project_id": project_id.casefold(),
        "project_namespace": project_namespace.casefold(),
        "workspace_id": workspace_id.casefold(),
        "workorder_id": workorder_id.casefold(),
        "task_type": re.sub(r"\s+", " ", task_type.strip()).casefold(),
        "scope_keywords": _normalize_lease_values(scope_keywords),
        "target_digest": target_digest,
    })


def _stable_ticket_lease_key(
    *,
    agent_id: str,
    task_description: str,
    target_files: Any,
    scope_keywords: Any,
    task_type: str,
    project_id: str = "unspecified",
    project_namespace: str = "unspecified",
    workspace_id: str = "unspecified",
    workorder_id: str = "unspecified",
    policy_version: str = _TICKET_POLICY_VERSION,
    target_digest: str | None = None,
) -> str:
    effective_target_digest = target_digest or _target_digest(target_files)
    scope_digest = _ticket_scope_digest(
        project_id=project_id,
        project_namespace=project_namespace,
        workspace_id=workspace_id,
        workorder_id=workorder_id,
        task_type=task_type,
        scope_keywords=scope_keywords,
        target_digest=effective_target_digest,
    )
    return canonical_hash({
        "agent_id": re.sub(r"\s+", " ", agent_id.strip()).casefold(),
        "task_description": re.sub(
            r"\s+", " ", task_description.strip()
        ).casefold(),
        "scope_digest": scope_digest,
        "policy_version": policy_version,
    })


def _load_ticket_receipts_unlocked(
    ticket_id: str | None = None,
) -> list[dict[str, Any]]:
    """Compatibility reader backed by indexed SQLite events, not JSONL scans."""
    return _ticket_ledger().events(ticket_id)


def _prune_expired_tickets() -> None:
    _ticket_ledger().active_count()


def _ledger_http_status(error: LedgerError) -> int:
    if error.code in {
        "ticket_agent_mismatch",
        "ticket_target_digest_mismatch",
        "ticket_scope_digest_mismatch",
        "ticket_error_not_allowed",
        "ticket_consumption_activity_mismatch",
        "ticket_consumption_binding_mismatch",
        "ticket_consumption_not_exclusive",
    }:
        return 403
    if error.code in {"ticket_not_found", "ticket_not_active"}:
        return 404
    return 409


def _canonical_error_case_payload(node: Any) -> tuple[dict[str, Any], ErrorCase] | None:
    content = getattr(node, "content", None)
    extra = getattr(content, "extra", {}) if content is not None else {}
    if not isinstance(extra, dict):
        return None
    nested = extra.get("error_case")
    payload = nested if isinstance(nested, dict) else extra
    if payload.get("schema_version") != "3can.error-case/v1":
        return None
    if str(payload.get("case_id") or "") != str(getattr(node, "id", "")):
        return None
    try:
        case = ErrorCase.from_dict(payload)
    except (KeyError, TypeError, ValueError):
        return None
    if not case.fingerprint or case.occurrence_count < 2:
        return None
    expected_fingerprint = _ek2_fingerprint(payload)
    expected_case_id = (
        f"ERR-case-{expected_fingerprint.split(':', 1)[1][:24]}"
    )
    if case.fingerprint.casefold() != expected_fingerprint.casefold():
        return None
    if case.case_id != expected_case_id:
        return None
    return dict(payload), case


@app.post("/api/route/ticket")
async def issue_route_ticket(payload: dict):
    """Issue a short-lived ticket after agent routes relevant ERR/INTF/API_USAGE.

    Body:
      agent_id: str
      task_description: str (required) — what the agent is about to do
      target_files: list[str] — files the agent plans to touch
      scope_keywords: list[str] — keywords scoping this work
      task_type: str — hint (Edit|Write|Bash|...)
    Returns the full ticket including err_warnings + intf_anchors + api_usage_hints
    the agent MUST read before proceeding.
    """
    agent_id = str(payload.get("agent_id") or "").strip()
    task_desc = (payload.get("task_description") or "").strip()
    target_files = payload.get("target_files") or []
    scope_keywords = payload.get("scope_keywords") or []
    task_type = (payload.get("task_type") or "").strip()

    if not agent_id:
        raise HTTPException(400, detail={"error": "agent_id_required"})
    if not task_desc:
        raise HTTPException(400, detail={"error": "task_description_required"})

    execution_context = _execution_identity_context(payload)
    project_id = execution_context.get("project_id", "unspecified")
    project_namespace = execution_context.get("project_namespace", "")
    workspace_id = execution_context.get("workspace_id", "unspecified")
    workorder_id = execution_context.get("workorder_id", "unspecified")
    policy_version = str(
        payload.get("policy_version") or _TICKET_POLICY_VERSION
    ).strip()[:100]
    client_target_digest = _client_target_digest(payload)
    target_digest = _target_digest(
        target_files,
        project_id=project_id if project_id != "unspecified" else "",
        project_namespace=project_namespace,
        workspace_id=workspace_id if workspace_id != "unspecified" else "",
    )
    if client_target_digest and not secrets.compare_digest(
        client_target_digest,
        target_digest,
    ):
        raise HTTPException(
            409,
            detail={"error": "target_state_digest_mismatch"},
        )
    target_digest_source = "server_verified_snapshot_v1"
    scope_digest = _ticket_scope_digest(
        project_id=project_id,
        project_namespace=(project_namespace or "unspecified"),
        workspace_id=workspace_id,
        workorder_id=workorder_id,
        task_type=task_type,
        scope_keywords=scope_keywords,
        target_digest=target_digest,
    )
    lease_key = _stable_ticket_lease_key(
        agent_id=agent_id,
        task_description=task_desc,
        target_files=target_files,
        scope_keywords=scope_keywords,
        task_type=task_type,
        project_id=project_id,
        project_namespace=(project_namespace or "unspecified"),
        workspace_id=workspace_id,
        workorder_id=workorder_id,
        policy_version=policy_version,
        target_digest=target_digest,
    )
    reusable = _ticket_ledger().find_active_by_lease(lease_key)
    if reusable:
        reusable["reused"] = True
        return reusable

    # Route related nodes for this task. Uses slim mode since we only need IDs +
    # names + summaries to scope the ticket; agent can /api/retrieve/{id} for full.
    route_query = task_desc
    if scope_keywords:
        route_query += " " + " ".join(scope_keywords[:8])
    req = RoutingRequest(
        task=route_query,
        max_nodes=8,
        agent_id=agent_id,
        mode="slim",
        confirm_low_confidence=True,
        allow_degraded=True,
        project_id=(project_id if project_id != "unspecified" else None),
        project_namespace=project_namespace or None,
        workspace_id=(workspace_id if workspace_id != "unspecified" else None),
        workorder_id=(workorder_id if workorder_id != "unspecified" else None),
    )
    result = await _route_in_worker(req)
    route_meta = (
        result.route_meta
        if isinstance(getattr(result, "route_meta", None), dict)
        else {}
    )
    error_route_policy = (
        route_meta.get("error_route_policy")
        if isinstance(route_meta.get("error_route_policy"), dict)
        else {}
    )
    exact_error_ranking = (
        error_route_policy.get("exact_error_case_ranking")
        if isinstance(
            error_route_policy.get("exact_error_case_ranking"),
            dict,
        )
        else {}
    )
    exact_match_kinds = (
        exact_error_ranking.get("match_kinds")
        if isinstance(exact_error_ranking.get("match_kinds"), dict)
        else {}
    )
    related = []
    required_error_disposition_ids: list[str] = []
    for n in result.activated_nodes[:8]:
        ntype = n.type.value if hasattr(n.type, "value") else str(n.type)
        related_item = {
            "id": n.id,
            "name": n.name,
            "type": ntype,
            "summary": (n.content.description or "")[:160],
        }
        error_case = _compact_error_case(n)
        if error_case:
            related_item["error_case"] = error_case
        canonical_error = _canonical_error_case_payload(n)
        match_kind = str(exact_match_kinds.get(str(n.id)) or "").strip()
        if canonical_error and match_kind:
            related_item["error_match_kind"] = match_kind
            if canonical_error[1].blocking:
                related_item["disposition_required"] = True
                required_error_disposition_ids.append(str(n.id))
        related.append(related_item)

    err_hits = [r for r in related if r["id"].startswith("ERR-")][:3]
    intf_hits = [r for r in related if r["id"].startswith("INTF-")][:3]
    api_usage_hits = [r for r in related
                      if r["id"].startswith("DOC-") and "api" in r["name"].lower()][:2]
    allowed_error_ids = sorted({
        str(node.id)
        for node in result.activated_nodes
        if str(node.id).startswith("ERR-")
        and _canonical_error_case_payload(node) is not None
    })

    ticket_id = f"rt_{uuid.uuid4().hex[:16]}"
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    ticket = {
        "ticket_id": ticket_id,
        "agent_id": agent_id,
        "issued_at": now_iso,
        "ttl_sec": _TICKET_TTL_SEC,
        "lease_key": lease_key,
        "state": "issued",
        "reused": False,
        "project_id": project_id,
        "project_namespace": project_namespace or "unspecified",
        "workspace_id": workspace_id,
        "workorder_id": workorder_id,
        "target_digest": target_digest,
        "target_digest_source": target_digest_source,
        "target_set_digest": canonical_hash(_normalize_lease_values(target_files)),
        "scope_digest": scope_digest,
        "policy_version": policy_version,
        "allowed_error_ids": allowed_error_ids,
        # Only exact, unresolved ErrorCases require an explicit completion
        # disposition. Similar routed ErrorCases remain advisory.
        "required_error_disposition_ids": sorted(
            set(required_error_disposition_ids)
        ),
        "error_disposition_policy": {
            "schema_version": "3can.error-disposition-policy/v1",
            "allowed": ["resolved", "still_open", "not_applicable"],
            "scope": "exact_unresolved_error_cases_only",
        },
        "task_description": task_desc,
        "task_type": task_type,
        "scope": {
            "target_files": target_files,
            "scope_keywords": scope_keywords,
            "related_node_ids": [r["id"] for r in related],
        },
        "err_warnings": err_hits,
        "intf_anchors": intf_hits,
        "api_usage_hints": api_usage_hits,
        "consumed_by_tools": [],
    }
    stored_ticket, reused = _ticket_ledger().issue(ticket)
    if reused:
        stored_ticket["reused"] = True
        return stored_ticket

    engine.log_activity(
        agent_id=agent_id,
        action="ticket_issued",
        detail=f"task={task_desc[:80]} n_err={len(err_hits)} n_intf={len(intf_hits)}",
        affected_nodes=[r["id"] for r in related[:5]],
        meta={
            "ticket_id": ticket_id,
            "task_type": task_type,
            "target_digest": target_digest,
            "scope_digest": scope_digest,
            "policy_version": policy_version,
        },
    )
    return stored_ticket


@app.get("/api/route/ticket/{ticket_id}")
async def get_route_ticket(ticket_id: str):
    """Gate reads this to validate. 404 if expired/unknown."""
    t = _ticket_ledger().get(ticket_id, active_only=True)
    if not t:
        raise HTTPException(404, detail={"error": "ticket_not_found_or_expired",
                                         "ticket_id": ticket_id})
    return t


@app.post("/api/route/ticket/{ticket_id}/consume")
async def consume_route_ticket(ticket_id: str, payload: dict):
    """Gate calls this after validating + letting a tool through. Records tool name
    + input summary on the ticket for audit. All immutable audit bindings are
    required: agent_id, tool_name, tool_input_summary, target_digest, scope_digest."""
    supplied_agent = str(payload.get("agent_id") or "").strip()
    tool_name = str(payload.get("tool_name") or "").strip()
    tool_input_summary = str(payload.get("tool_input_summary") or "").strip()
    target_digest = str(payload.get("target_digest") or "").strip()
    scope_digest = str(payload.get("scope_digest") or "").strip()
    if not supplied_agent:
        raise HTTPException(400, detail={"error": "agent_id_required"})
    if not tool_name:
        raise HTTPException(400, detail={"error": "tool_name_required"})
    if not tool_input_summary:
        raise HTTPException(400, detail={"error": "tool_input_summary_required"})
    if not target_digest:
        raise HTTPException(400, detail={"error": "target_digest_required"})
    if not scope_digest:
        raise HTTPException(400, detail={"error": "scope_digest_required"})
    consumed = {
        "tool_name": tool_name[:40],
        "tool_input_summary": tool_input_summary[:200],
        "consumed_at": _utc_now().isoformat(),
    }
    try:
        current_ticket = _ticket_ledger().get(ticket_id, active_only=True)
        if (
            current_ticket
            and current_ticket.get("target_digest_source")
            == "server_verified_snapshot_v1"
        ):
            current_digest = _target_digest(
                (current_ticket.get("scope") or {}).get("target_files") or [],
                project_id=_specified_identity(current_ticket.get("project_id")),
                project_namespace=_specified_identity(
                    current_ticket.get("project_namespace")
                ),
                workspace_id=_specified_identity(
                    current_ticket.get("workspace_id")
                ),
            )
            if current_digest != str(current_ticket.get("target_digest") or ""):
                raise LedgerError("ticket_target_state_changed")
        existing_consumes = (
            current_ticket.get("consumed_by_tools")
            if isinstance(current_ticket, dict)
            and isinstance(current_ticket.get("consumed_by_tools"), list)
            else []
        )
        if existing_consumes:
            if len(existing_consumes) != 1:
                raise LedgerError("ticket_already_consumed")
            prior = existing_consumes[0]
            if (
                str(current_ticket.get("agent_id") or "") != supplied_agent
                or str(current_ticket.get("target_digest") or "")
                != target_digest
                or str(current_ticket.get("scope_digest") or "")
                != scope_digest
                or str(prior.get("tool_name") or "") != tool_name[:40]
                or str(prior.get("tool_input_summary") or "")
                != tool_input_summary[:200]
            ):
                raise LedgerError("ticket_already_consumed")
            activity_hash = str(prior.get("activity_hash") or "").casefold()
            if not activity_hash:
                for candidate in reversed(
                    list(getattr(engine, "activity_log", []) or [])
                ):
                    meta = getattr(candidate, "meta", {}) or {}
                    if (
                        str(getattr(candidate, "action", "") or "")
                        == "ticket_consumed"
                        and str(getattr(candidate, "agent_id", "") or "")
                        == supplied_agent
                        and str(meta.get("ticket_id") or "") == ticket_id
                        and int(meta.get("consume_count") or 0) == 1
                    ):
                        activity_hash = str(candidate.self_hash).casefold()
                        break
                if activity_hash:
                    current_ticket = (
                        _ticket_ledger().attach_consume_activity_hash(
                            ticket_id,
                            agent_id=supplied_agent,
                            tool_name=tool_name[:40],
                            tool_input_summary=tool_input_summary[:200],
                            activity_hash=activity_hash,
                        )
                    )
            if not activity_hash:
                repaired = engine.log_activity(
                    agent_id=supplied_agent,
                    action="ticket_consumed",
                    detail=(
                        f"tool={prior['tool_name']} "
                        f"summary={str(prior['tool_input_summary'])[:120]}"
                    ),
                    affected_nodes=list(
                        (current_ticket.get("scope") or {}).get(
                            "related_node_ids"
                        )
                        or []
                    )[:5],
                    meta={"ticket_id": ticket_id, "consume_count": 1},
                )
                activity_hash = str(repaired.self_hash).casefold()
                current_ticket = _ticket_ledger().attach_consume_activity_hash(
                    ticket_id,
                    agent_id=supplied_agent,
                    tool_name=tool_name[:40],
                    tool_input_summary=tool_input_summary[:200],
                    activity_hash=activity_hash,
                )
            if not re.fullmatch(r"[0-9a-f]{64}", activity_hash):
                raise LedgerError("consume_activity_receipt_missing")
            return {
                "ok": True,
                "idempotent": True,
                "ticket_id": ticket_id,
                "consume_count": 1,
                "completion_deadline": current_ticket["completion_deadline"],
                "activity_hash": activity_hash,
            }
        t = _ticket_ledger().consume(
            ticket_id,
            agent_id=supplied_agent,
            target_digest=target_digest,
            scope_digest=scope_digest,
            consumed=consumed,
        )
    except LedgerError as exc:
        raise HTTPException(
            _ledger_http_status(exc),
            detail={"error": exc.code, "ticket_id": ticket_id},
        ) from exc
    entry = engine.log_activity(
        agent_id=str(t.get("agent_id") or "unknown"),
        action="ticket_consumed",
        detail=f"tool={consumed['tool_name']} summary={consumed['tool_input_summary'][:120]}",
        affected_nodes=list((t.get("scope") or {}).get("related_node_ids") or [])[:5],
        meta={"ticket_id": ticket_id, "consume_count": len(t["consumed_by_tools"])},
    )
    _ticket_ledger().attach_consume_activity_hash(
        ticket_id,
        agent_id=str(t.get("agent_id") or "unknown"),
        tool_name=consumed["tool_name"],
        tool_input_summary=consumed["tool_input_summary"],
        activity_hash=entry.self_hash,
    )
    return {"ok": True, "idempotent": False, "ticket_id": ticket_id,
            "consume_count": len(t["consumed_by_tools"]),
            "completion_deadline": t["completion_deadline"],
            "activity_hash": entry.self_hash}


def _ek2_identity(payload: Mapping[str, Any]) -> dict[str, str]:
    return {
        field: re.sub(
            r"\s+", " ", str(payload.get(field) or "").strip()
        ).casefold()
        for field in ("project_id", "operation", "component", "error_type")
    }


def _ek2_fingerprint(payload: Mapping[str, Any]) -> str:
    return deterministic_fingerprint(
        project_id=str(payload.get("project_id") or ""),
        operation=str(payload.get("operation") or ""),
        component=str(payload.get("component") or ""),
        error_type=str(payload.get("error_type") or ""),
    )


def _error_case_projection_payload(case: Mapping[str, Any]) -> dict[str, Any]:
    state = str(case.get("state") or "observed")
    first_seen = str(case.get("first_seen_at") or _utc_now().isoformat())
    last_seen = str(case.get("last_seen_at") or first_seen)
    promoted_at = str(case.get("promoted_at") or last_seen)
    resolution = case.get("resolution")
    history = [resolution] if isinstance(resolution, dict) else []
    return {
        "schema_version": "3can.error-case/v1",
        "case_id": str(case["case_id"]),
        "fingerprint": str(case["fingerprint"]),
        "fingerprint_version": "ek2",
        "project_id": str(case["project_id"]),
        "operation": str(case["operation"]),
        "component": str(case["component"]),
        "error_type": str(case["error_type"]),
        "error": str(case["error"]),
        "root_cause": str(case["root_cause"]),
        "applicability": {
            "project_id": str(case["project_id"]),
            "operation": str(case["operation"]),
            "component": str(case["component"]),
            "error_type": str(case["error_type"]),
            "fingerprint_version": "ek2",
        },
        "state": state,
        "blocking": int(case["occurrence_count"]) >= 2
        and state not in {"resolved", "superseded"},
        "occurrence_count": int(case["occurrence_count"]),
        "first_seen_at": first_seen,
        "last_seen_at": last_seen,
        "promoted_at": promoted_at,
        "state_changed_at": last_seen,
        "diagnosis": (
            str(case["root_cause"]) if str(case["root_cause"]).strip() else None
        ),
        "diagnosed_by": None,
        "diagnosed_at": None,
        "mitigation": None,
        "mitigated_by": None,
        "mitigated_at": None,
        "active_resolution": resolution if isinstance(resolution, dict) else None,
        "resolution_history": history,
        "regression_count": 1 if state == "regressed" else 0,
        "superseded_by": None,
        "metadata": {
            "component": str(case["component"]),
            "error_type": str(case["error_type"]),
            "ledger_authoritative": True,
        },
    }


def _project_error_case(case: Mapping[str, Any]) -> dict[str, Any]:
    canonical = _error_case_projection_payload(case)
    error_id = str(case["case_id"])
    content = NodeContent(
        description=str(case["error"])[:500],
        current_state=(
            f"{case['state']}; occurrence_count={case['occurrence_count']}"
        ),
        blockers=(
            ["exact promoted ErrorCase blocks blind retry"]
            if canonical["blocking"] else []
        ),
        notes=str(case["root_cause"])[:1000],
        extra={
            "error_case": canonical,
            "error_knowledge_schema_version": "3can.error-knowledge/v2",
            "fingerprint": case["fingerprint"],
            "case_status": case["state"],
            "occurrence_count": case["occurrence_count"],
            "project_id": case["project_id"],
            "operation": case["operation"],
            "component": case["component"],
            "error_type": case["error_type"],
            "ledger_authoritative": True,
        },
    )
    existing = engine.get_node(error_id)
    if existing:
        node = engine.update_node(
            error_id,
            NodeUpdate(
                content=content,
                status=NodeStatus.active,
                activation_keywords=list(dict.fromkeys([
                    *existing.activation_keywords,
                    str(case["component"]),
                    str(case["error_type"]),
                    str(case["fingerprint"]),
                ]))[:20],
                updated_by="error-ledger",
            ),
            internal_owner="error-ledger",
        )
    else:
        node = engine.create_node(
            NodeCreate(
                id=error_id,
                name=f"ErrorCase: {case['component']} / {case['error_type']}",
                cluster="ErrorKnowledge",
                layer="L1",
                type=NodeType.feedback,
                status=NodeStatus.active,
                content=content,
                activation_keywords=[
                    str(case["component"]),
                    str(case["error_type"]),
                    str(case["fingerprint"]),
                ],
                primary_author="error-ledger",
            ),
            internal_owner="error-ledger",
        )
    _ticket_ledger().mark_error_projection(
        str(case["fingerprint"]),
        state="projected",
    )
    return node.model_dump() if hasattr(node, "model_dump") else {"id": error_id}


async def _record_error_occurrence_core(payload: dict):
    try:
        payload_bytes = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            400,
            detail={"error": "occurrence_payload_not_json"},
        ) from exc
    if payload_bytes > 64 * 1024:
        raise HTTPException(
            413,
            detail={"error": "occurrence_payload_too_large"},
        )
    required = (
        "occurrence_id",
        "fingerprint",
        "project_id",
        "operation",
        "component",
        "error_type",
        "error",
        "root_cause",
        "occurred_at",
    )
    missing = [
        field for field in required
        if not str(payload.get(field) or "").strip()
    ]
    if missing:
        raise HTTPException(
            400,
            detail={"error": "occurrence_fields_missing", "fields": missing},
        )
    occurrence_id = str(payload["occurrence_id"]).strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{1,200}", occurrence_id):
        raise HTTPException(
            400,
            detail={"error": "occurrence_id_invalid"},
        )
    expected = _ek2_fingerprint(payload)
    if str(payload["fingerprint"]).strip().casefold() != expected:
        raise HTTPException(
            409,
            detail={
                "error": "ek2_fingerprint_mismatch",
                "expected_fingerprint": expected,
            },
        )
    identity = canonical_error_identity(
        project_id=str(payload["project_id"]),
        operation=str(payload["operation"]),
        component=str(payload["component"]),
        error_type=str(payload["error_type"]),
    )
    secret_assignment = re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|cookie)"
        r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
    )
    bearer_secret = re.compile(
        r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"
    )
    user_home = re.compile(
        r"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/]+"
        r"|(?:/home|/users)/[^/\s\"']+"
        r"|\\\\[^\\/\r\n]+[\\/][^\\/\r\n]+[\\/][^\\/\r\n]+)"
    )

    def _safe_text(value: Any, limit: int) -> str:
        text = str(value or "")
        text = secret_assignment.sub(
            lambda match: f"{match.group(1)}=<redacted>",
            text,
        )
        text = bearer_secret.sub("Bearer <redacted>", text)
        text = user_home.sub("<user-home>", text)
        return re.sub(r"\s+", " ", text).strip()[:limit]

    def _safe_context(value: Any, depth: int = 0) -> Any:
        if depth >= 4:
            return "<depth-limit>"
        if isinstance(value, dict):
            return {
                _safe_text(key, 80): _safe_context(item, depth + 1)
                for key, item in list(value.items())[:20]
            }
        if isinstance(value, list):
            return [_safe_context(item, depth + 1) for item in value[:20]]
        if isinstance(value, str):
            return _safe_text(value, 1000)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return _safe_text(value, 1000)

    occurrence = {
        "occurrence_id": occurrence_id,
        "fingerprint": expected,
        **identity,
        "error": _safe_text(payload["error"], 2000),
        "root_cause": _safe_text(payload["root_cause"], 1000),
        "occurred_at": _safe_text(payload["occurred_at"], 80),
        "agent_id": _safe_text(payload.get("agent_id") or "unknown", 200),
        "context": _safe_context(
            payload.get("context")
            if isinstance(payload.get("context"), dict)
            else {}
        ),
    }
    if len(
        json.dumps(
            occurrence["context"],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ) > 16 * 1024:
        raise HTTPException(
            413,
            detail={"error": "occurrence_context_too_large"},
        )
    try:
        result = _ticket_ledger().record_error_occurrence(occurrence)
    except LedgerError as exc:
        raise HTTPException(
            409,
            detail={"error": exc.code},
        ) from exc
    case = result["case"]
    status = "RECORDED"
    projection_error = None
    if case["promoted"]:
        status = "PROMOTED"
        try:
            _project_error_case(case)
            case = _ticket_ledger().error_case(
                fingerprint=str(case["fingerprint"])
            )
        except Exception as exc:
            projection_error = f"{type(exc).__name__}: {exc}"[:500]
            _ticket_ledger().mark_error_projection(
                str(case["fingerprint"]),
                state="partial",
                error=projection_error,
            )
            case = _ticket_ledger().error_case(
                fingerprint=str(case["fingerprint"])
            )
            status = "PARTIAL"
    return {
        "ok": True,
        "status": status,
        "idempotent": result["idempotent"],
        "case": case,
        "projection_error": projection_error,
    }


def _unticketed_error_occurrence_enabled() -> bool:
    explicit = os.environ.get(_UNTICKETED_ERROR_OCCURRENCE_ENV, "")
    return (
        configured_readiness_mode() == READINESS_MODE_DEVELOPMENT
        and explicit.strip().casefold() in {"1", "true", "yes", "on"}
    )


@app.post("/api/errors/occurrences")
async def record_error_occurrence(payload: dict):
    if not _unticketed_error_occurrence_enabled():
        raise HTTPException(
            403,
            detail={
                "error": "unticketed_error_occurrence_disabled",
                "required_endpoint": "/api/errors/occurrences/ticketed",
            },
        )
    return await _record_error_occurrence_core(payload)


def _ticketed_error_crash_point(stage: str) -> None:
    hook = _TICKETED_ERROR_CRASH_HOOK
    if callable(hook):
        hook(stage)


def _ticketed_event_digest(event: Mapping[str, Any]) -> str:
    core = json.loads(json.dumps(dict(event), ensure_ascii=False))
    core.pop("captured_at", None)
    core.pop("event_digest", None)
    candidate = core.get("candidate_writeback")
    if isinstance(candidate, dict):
        occurrence = candidate.get("payload")
        if isinstance(occurrence, dict):
            occurrence.pop("occurred_at", None)
    return f"sha256:{canonical_hash(core)}"


def _ticketed_error_target_path(ticket: Mapping[str, Any]) -> Path:
    scope = ticket.get("scope")
    target_files = (
        scope.get("target_files") if isinstance(scope, dict) else None
    )
    if not isinstance(target_files, list) or len(target_files) != 1:
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_exact_target_required"},
        )
    candidate = _wire_target_path(str(target_files[0])).expanduser()
    if not candidate.is_absolute():
        candidate = _default_project_dir() / candidate
    resolved = candidate.resolve(strict=False)
    project_id = _specified_identity(ticket.get("project_id"))
    project_namespace = _specified_identity(ticket.get("project_namespace"))
    workspace_id = _specified_identity(ticket.get("workspace_id"))
    identity_bound = all((project_id, project_namespace, workspace_id))
    if identity_bound:
        _verified_project_target_root(
            resolved,
            project_id=project_id,
            project_namespace=project_namespace,
            workspace_id=workspace_id,
        )
    elif _target_root_for(resolved, _target_allowed_roots()) is None:
        raise HTTPException(
            403,
            detail={"error": "ticketed_error_target_outside_allowed_roots"},
        )
    if not resolved.is_file():
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_target_not_file"},
        )
    if resolved.stat().st_size > 256 * 1024:
        raise HTTPException(
            413,
            detail={"error": "ticketed_error_target_too_large"},
        )
    return resolved


def _ticketed_consume_summary(
    occurrence_id: str,
    event_idempotency_key: str,
) -> str:
    digest = event_idempotency_key.rsplit(":", 1)[-1]
    return f"deliver {occurrence_id} event={digest}"


def _ticketed_error_spool_binding(
    request: Mapping[str, Any],
    ticket: Mapping[str, Any],
    occurrence: Mapping[str, Any],
) -> tuple[Path, dict[str, Any]]:
    target_digest = str(request.get("target_digest") or "").casefold()
    scope_digest = str(request.get("scope_digest") or "").casefold()
    if target_digest != str(ticket.get("target_digest") or "").casefold():
        raise HTTPException(
            403,
            detail={"error": "ticket_target_digest_mismatch"},
        )
    if scope_digest != str(ticket.get("scope_digest") or "").casefold():
        raise HTTPException(
            403,
            detail={"error": "ticket_scope_digest_mismatch"},
        )
    scope = ticket.get("scope") if isinstance(ticket.get("scope"), dict) else {}
    current_target_digest = _target_digest(
        scope.get("target_files") or [],
        project_id=_specified_identity(ticket.get("project_id")),
        project_namespace=_specified_identity(ticket.get("project_namespace")),
        workspace_id=_specified_identity(ticket.get("workspace_id")),
    )
    if current_target_digest != target_digest:
        raise HTTPException(
            409,
            detail={"error": "ticket_target_state_changed"},
        )
    path = _ticketed_error_target_path(ticket)
    try:
        event = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_spool_invalid"},
        ) from exc
    if not isinstance(event, dict):
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_spool_not_object"},
        )
    idempotency_key = str(request.get("event_idempotency_key") or "")
    event_digest = str(request.get("event_digest") or "").casefold()
    idempotency_digest = idempotency_key.rsplit(":", 1)[-1]
    if path.name != f"codex-error-event-{idempotency_digest}.json":
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_spool_filename_mismatch"},
        )
    if (
        event.get("schema_version") != "3can.codex-error-hook-spool/v1"
        or event.get("kind") != "codex_error_hook_event"
        or event.get("idempotency_key") != idempotency_key
        or event.get("event_digest") != event_digest
        or _ticketed_event_digest(event) != event_digest
    ):
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_event_binding_mismatch"},
        )
    source = event.get("source")
    source_ids = (
        {
            field: str(source.get(field) or "")
            for field in (
                "session_id",
                "turn_id",
                "tool_use_id",
                "tool_use_id_source",
            )
        }
        if isinstance(source, dict)
        else {}
    )
    stable_identity = {
        **source_ids,
        "tool_name": str(source.get("tool_name") or "")
        if isinstance(source, dict)
        else "",
    }
    expected_idempotency_key = (
        f"codex-posttooluse:{canonical_hash(stable_identity)}"
    )
    occurrence_context = occurrence.get("context")
    if (
        not isinstance(source, dict)
        or source.get("surface") != "codex"
        or source.get("hook_event_name") != "PostToolUse"
        or not all(source_ids.values())
        or not stable_identity["tool_name"]
        or expected_idempotency_key != idempotency_key
        or str(source.get("workspace_ref") or "")
        != str(occurrence.get("project_id") or "")
        or not isinstance(occurrence_context, dict)
        or any(
            str(occurrence_context.get(field) or "") != value
            for field, value in stable_identity.items()
        )
    ):
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_source_binding_mismatch"},
        )
    if str(occurrence.get("occurrence_id") or "") != (
        f"OCC-CODEX-{idempotency_digest[:32]}"
    ):
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_occurrence_identity_mismatch"},
        )
    privacy = event.get("privacy")
    if (
        not isinstance(privacy, dict)
        or privacy.get("redaction_version") != "3can-hook-redaction/v1"
        or privacy.get("raw_tool_input_stored") is not False
        or privacy.get("raw_tool_response_stored") is not False
        or not isinstance(privacy.get("command_truncated"), bool)
        or not isinstance(privacy.get("response_truncated"), bool)
    ):
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_privacy_contract_invalid"},
        )
    if event.get("classification") != {
        "outcome": "failure",
        "review_required": False,
        "signals": (event.get("classification") or {}).get("signals", []),
    }:
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_not_explicit_failure"},
        )
    if event.get("delivery") != {
        "requires_route_ticket": True,
        "status": "pending_ticketed_worker",
        "writeback_attempted": False,
        "writeback_eligible": True,
    }:
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_delivery_state_invalid"},
        )
    candidate = event.get("candidate_writeback")
    candidate_occurrence = (
        candidate.get("payload") if isinstance(candidate, dict) else None
    )
    if (
        not isinstance(candidate, dict)
        or candidate.get("endpoint") != "/api/errors/occurrences/ticketed"
        or candidate.get("method") != "POST"
        or not isinstance(candidate_occurrence, dict)
        or canonical_hash(candidate_occurrence) != canonical_hash(occurrence)
    ):
        raise HTTPException(
            409,
            detail={"error": "ticketed_error_occurrence_binding_mismatch"},
        )
    return path, event


def _ticketed_consume_activity(
    *,
    activity_hash: str,
    ticket_id: str,
    agent_id: str,
) -> Any:
    if not re.fullmatch(r"[0-9a-f]{64}", activity_hash):
        raise HTTPException(
            400,
            detail={"error": "consume_activity_hash_invalid"},
        )
    for entry in reversed(list(getattr(engine, "activity_log", []) or [])):
        if str(getattr(entry, "self_hash", "") or "").casefold() != activity_hash:
            continue
        meta = getattr(entry, "meta", {}) or {}
        if (
            str(getattr(entry, "action", "") or "") != "ticket_consumed"
            or str(getattr(entry, "agent_id", "") or "") != agent_id
            or str(meta.get("ticket_id") or "") != ticket_id
        ):
            raise HTTPException(
                409,
                detail={"error": "consume_activity_binding_mismatch"},
            )
        return entry
    raise HTTPException(
        409,
        detail={"error": "consume_activity_not_found"},
    )


def _find_ticketed_error_activity(
    event_idempotency_key: str,
    occurrence_id: str,
) -> Any | None:
    for entry in reversed(list(getattr(engine, "activity_log", []) or [])):
        meta = getattr(entry, "meta", {}) or {}
        if (
            str(getattr(entry, "action", "") or "")
            == "error_occurrence_delivered"
            and str(meta.get("event_idempotency_key") or "")
            == event_idempotency_key
            and str(meta.get("occurrence_id") or "") == occurrence_id
        ):
            return entry
    return None


def _ticketed_error_replay_response(
    receipt: Mapping[str, Any],
    *,
    authorization_ticket_id: str,
    authorization_consume_activity_hash: str,
    authorization_completion_request_hash: str,
) -> dict[str, Any]:
    return {
        **dict(receipt),
        "idempotent": True,
        "authorization_ticket_id": authorization_ticket_id,
        "authorization_ticket_state": "completed",
        "authorization_consume_activity_hash": (
            authorization_consume_activity_hash
        ),
        "authorization_completion_request_hash": (
            authorization_completion_request_hash
        ),
        "replayed_from_ticket_id": str(receipt.get("ticket_id") or ""),
    }


@app.get("/api/errors/occurrences/ticketed/capabilities")
async def ticketed_error_occurrence_capabilities():
    return {
        "ok": True,
        "schema_version": _TICKETED_ERROR_CAPABILITY_SCHEMA,
        "request_schema_version": _TICKETED_ERROR_REQUEST_SCHEMA,
        "receipt_schema_version": _TICKETED_ERROR_RECEIPT_SCHEMA,
        "requires_route_ticket": True,
        "idempotency_scope": "event_idempotency_key",
    }


@app.post("/api/errors/occurrences/ticketed")
async def record_ticketed_error_occurrence(payload: dict):
    if payload.get("schema_version") != _TICKETED_ERROR_REQUEST_SCHEMA:
        raise HTTPException(
            400,
            detail={"error": "ticketed_error_request_schema_invalid"},
        )
    ticket_id = str(payload.get("ticket_id") or "").strip()
    agent_id = str(payload.get("agent_id") or "").strip()
    target_digest = str(payload.get("target_digest") or "").strip().casefold()
    scope_digest = str(payload.get("scope_digest") or "").strip().casefold()
    consume_activity_hash = str(
        payload.get("consume_activity_hash") or ""
    ).strip().casefold()
    event_idempotency_key = str(
        payload.get("event_idempotency_key") or ""
    ).strip()
    event_digest = str(payload.get("event_digest") or "").strip().casefold()
    occurrence = payload.get("occurrence")
    if not ticket_id or not agent_id:
        raise HTTPException(
            400,
            detail={"error": "ticketed_error_ticket_and_agent_required"},
        )
    if not re.fullmatch(r"[0-9a-f]{64}", target_digest):
        raise HTTPException(400, detail={"error": "target_digest_invalid"})
    if not re.fullmatch(r"[0-9a-f]{64}", scope_digest):
        raise HTTPException(400, detail={"error": "scope_digest_invalid"})
    if not re.fullmatch(
        r"codex-posttooluse:[0-9a-f]{64}",
        event_idempotency_key,
    ):
        raise HTTPException(
            400,
            detail={"error": "event_idempotency_key_invalid"},
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", event_digest):
        raise HTTPException(400, detail={"error": "event_digest_invalid"})
    if not isinstance(occurrence, dict):
        raise HTTPException(400, detail={"error": "occurrence_payload_required"})
    if str(occurrence.get("agent_id") or "") != agent_id:
        raise HTTPException(403, detail={"error": "occurrence_agent_mismatch"})

    ledger = _ticket_ledger()
    ticket = ledger.get(ticket_id, active_only=False)
    if not ticket:
        raise HTTPException(404, detail={"error": "ticket_not_found"})
    if str(ticket.get("agent_id") or "") != agent_id:
        raise HTTPException(403, detail={"error": "ticket_agent_mismatch"})
    _ticketed_error_spool_binding(payload, ticket, occurrence)
    _ticketed_consume_activity(
        activity_hash=consume_activity_hash,
        ticket_id=ticket_id,
        agent_id=agent_id,
    )

    occurrence_id = str(occurrence.get("occurrence_id") or "")
    occurrence_fingerprint = str(occurrence.get("fingerprint") or "").casefold()
    expected_summary = _ticketed_consume_summary(
        occurrence_id,
        event_idempotency_key,
    )
    canonical_request = {
        "schema_version": _TICKETED_ERROR_REQUEST_SCHEMA,
        "ticket_id": ticket_id,
        "agent_id": agent_id,
        "target_digest": target_digest,
        "scope_digest": scope_digest,
        "consume_activity_hash": consume_activity_hash,
        "event_idempotency_key": event_idempotency_key,
        "event_digest": event_digest,
        "occurrence": occurrence,
    }
    request_hash = canonical_hash(canonical_request)
    owner_token = uuid.uuid4().hex
    dispositions = {
        str(error_id): "still_open"
        for error_id in ticket.get("required_error_disposition_ids", [])
        if str(error_id).strip()
    }
    try:
        authorization = ledger.begin_completion(
            ticket_id,
            agent_id=agent_id,
            request_hash=request_hash,
            request=_sanitize_completion_audit_request(canonical_request),
            requested_error_ids=[],
            error_dispositions=dispositions,
            owner_token=owner_token,
        )
    except LedgerError as exc:
        raise HTTPException(
            _ledger_http_status(exc),
            detail={"error": exc.code, "ticket_id": ticket_id},
        ) from exc
    if authorization["mode"] == "replay":
        response = authorization["response"]
        journal = ledger.ticketed_error_delivery(event_idempotency_key)
        if journal and not journal.get("receipt"):
            ledger.complete_ticketed_error_delivery(
                event_idempotency_key,
                receipt=response,
            )
        return response

    owner_token = str(authorization["owner_token"])
    try:
        delivery = ledger.begin_ticketed_error_delivery(
            ticket_id=ticket_id,
            agent_id=agent_id,
            target_digest=target_digest,
            scope_digest=scope_digest,
            completion_request_hash=request_hash,
            expected_tool_name="3can-error-occurrence-writeback",
            expected_tool_input_summary=expected_summary,
            event_idempotency_key=event_idempotency_key,
            event_digest=event_digest,
            occurrence_id=occurrence_id,
            occurrence_fingerprint=occurrence_fingerprint,
            occurrence_payload_hash=canonical_hash(occurrence),
            consume_activity_hash=consume_activity_hash,
        )
        if delivery["mode"] == "replay":
            response = _ticketed_error_replay_response(
                delivery["receipt"],
                authorization_ticket_id=ticket_id,
                authorization_consume_activity_hash=consume_activity_hash,
                authorization_completion_request_hash=request_hash,
            )
            return ledger.complete(
                ticket_id,
                request_hash=request_hash,
                owner_token=owner_token,
                response=response,
            )

        context = dict(delivery.get("context") or {})
        stage = str(delivery.get("stage") or "authorized")
        if stage == "authorized":
            occurrence_result = await _record_error_occurrence_core(occurrence)
            _ticketed_error_crash_point("after_occurrence_recorded")
            context.update({
                "occurrence_status": occurrence_result["status"],
                "occurrence_idempotent": occurrence_result["idempotent"],
                "case_id": (occurrence_result.get("case") or {}).get("case_id"),
            })
            delivery = ledger.advance_ticketed_error_delivery(
                event_idempotency_key,
                stage="occurrence_recorded",
                context=context,
            )
            stage = delivery["stage"]

        if stage == "occurrence_recorded":
            stored_occurrence = ledger.error_occurrence(occurrence_id)
            if not stored_occurrence:
                raise RuntimeError("ticketed occurrence missing after ledger write")
            if (
                str(stored_occurrence.get("fingerprint") or "").casefold()
                != occurrence_fingerprint
            ):
                raise RuntimeError("ticketed occurrence fingerprint conflict")
            case = ledger.error_case(fingerprint=occurrence_fingerprint)
            if case and case.get("promoted"):
                _project_error_case(case)
                context["occurrence_status"] = "PROMOTED"
                context["case_id"] = case.get("case_id")
            elif context.get("occurrence_status") == "PARTIAL":
                context["occurrence_status"] = "RECORDED"
            _ticketed_error_crash_point("after_projection")
            delivery = ledger.advance_ticketed_error_delivery(
                event_idempotency_key,
                stage="projected",
                context=context,
            )
            stage = delivery["stage"]

        if stage == "projected":
            activity = _find_ticketed_error_activity(
                event_idempotency_key,
                occurrence_id,
            )
            if activity is None:
                affected = [str(context.get("case_id") or "")]
                activity = engine.log_activity(
                    agent_id=agent_id,
                    action="error_occurrence_delivered",
                    detail=f"occurrence={occurrence_id}",
                    affected_nodes=[value for value in affected if value],
                    meta={
                        "ticket_id": delivery["original_ticket_id"],
                        "event_idempotency_key": event_idempotency_key,
                        "event_digest": event_digest,
                        "occurrence_id": occurrence_id,
                        "occurrence_fingerprint": occurrence_fingerprint,
                        "completion_request_hash": request_hash,
                    },
                )
            _ticketed_error_crash_point("after_activity_logged")
            context["activity_self_hash"] = activity.self_hash
            delivery = ledger.advance_ticketed_error_delivery(
                event_idempotency_key,
                stage="activity_logged",
                context=context,
            )

        context = dict(delivery.get("context") or {})
        activity_self_hash = str(context.get("activity_self_hash") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", activity_self_hash):
            raise RuntimeError("ticketed activity self hash missing")
        original_ticket_id = str(delivery["original_ticket_id"])
        original_consume_hash = str(
            context.get("original_consume_activity_hash") or ""
        )
        original_completion_hash = str(
            context.get("original_completion_request_hash") or ""
        )
        if not re.fullmatch(r"[0-9a-f]{64}", original_completion_hash):
            raise RuntimeError("ticketed completion request hash missing")
        receipt = {
            "ok": True,
            "schema_version": _TICKETED_ERROR_RECEIPT_SCHEMA,
            "status": str(context.get("occurrence_status") or "RECORDED"),
            "idempotent": bool(context.get("occurrence_idempotent")),
            "ticket_id": original_ticket_id,
            "ticket_state": "completed",
            "authorization_ticket_id": ticket_id,
            "authorization_ticket_state": "completed",
            "event_idempotency_key": event_idempotency_key,
            "event_digest": event_digest,
            "occurrence_id": occurrence_id,
            "durable_id": occurrence_id,
            "occurrence_fingerprint": occurrence_fingerprint,
            "consume_activity_hash": original_consume_hash,
            "authorization_consume_activity_hash": consume_activity_hash,
            "activity_self_hash": activity_self_hash,
            "completion_request_hash": original_completion_hash,
            "authorization_completion_request_hash": request_hash,
        }
        response = ledger.complete(
            ticket_id,
            request_hash=request_hash,
            owner_token=owner_token,
            response=receipt,
        )
        _ticketed_error_crash_point("after_ticket_completed")
        ledger.complete_ticketed_error_delivery(
            event_idempotency_key,
            receipt=receipt,
        )
        return response
    except LedgerError as exc:
        ledger.release_completion(
            ticket_id,
            request_hash=request_hash,
            owner_token=owner_token,
            error=exc.code,
        )
        ledger.release_ticketed_error_delivery(
            event_idempotency_key,
            error=exc.code,
        )
        raise HTTPException(
            _ledger_http_status(exc),
            detail={"error": exc.code, "ticket_id": ticket_id},
        ) from exc
    except HTTPException:
        ledger.release_completion(
            ticket_id,
            request_hash=request_hash,
            owner_token=owner_token,
            error="ticketed_error_http_failure",
        )
        ledger.release_ticketed_error_delivery(
            event_idempotency_key,
            error="ticketed_error_http_failure",
        )
        raise
    except Exception as exc:
        ledger.release_completion(
            ticket_id,
            request_hash=request_hash,
            owner_token=owner_token,
            error=f"{type(exc).__name__}: {exc}",
        )
        ledger.release_ticketed_error_delivery(
            event_idempotency_key,
            error=f"{type(exc).__name__}: {exc}",
        )
        journal = ledger.ticketed_error_delivery(event_idempotency_key)
        raise HTTPException(
            500,
            detail={
                "error": "ticketed_error_delivery_recoverable",
                "ticket_id": ticket_id,
                "event_idempotency_key": event_idempotency_key,
                "journal_stage": (
                    journal.get("stage") if journal else "not_reserved"
                ),
            },
        ) from exc


@app.get("/api/errors/cases")
async def get_error_case(
    fingerprint: str | None = Query(None),
    case_id: str | None = Query(None),
):
    try:
        case = _ticket_ledger().error_case(
            fingerprint=fingerprint,
            case_id=case_id,
        )
    except LedgerError as exc:
        raise HTTPException(400, detail={"error": exc.code}) from exc
    if not case:
        raise HTTPException(404, detail={"error": "error_case_not_found"})
    return case


@app.get("/api/errors/occurrences/{occurrence_id}")
async def get_error_occurrence(occurrence_id: str):
    occurrence = _ticket_ledger().error_occurrence(occurrence_id)
    if not occurrence:
        raise HTTPException(404, detail={"error": "error_occurrence_not_found"})
    return occurrence


def _reconcile_error_ledger_from_graph() -> dict[str, Any]:
    imported: list[str] = []
    graph_only_review_required: list[str] = []
    untrusted_state_ignored: list[str] = []
    failures: list[dict[str, str]] = []
    for node in list(getattr(engine, "nodes", {}).values()):
        node_id = str(getattr(node, "id", "") or "")
        if not node_id.startswith("ERR-"):
            continue
        extra = dict(getattr(node.content, "extra", {}) or {})
        canonical = _canonical_error_case_payload(node)
        if canonical is not None:
            payload, _case = canonical
        elif isinstance(extra.get("error_case"), dict):
            graph_only_review_required.append(node_id)
            continue
        elif extra.get("error_knowledge_schema_version") or extra.get(
            "legacy_error_migration_version"
        ):
            fingerprint = str(extra.get("fingerprint") or "").strip()
            identity = {
                "project_id": str(extra.get("project_id") or ""),
                "operation": str(extra.get("operation") or ""),
                "component": str(extra.get("component") or ""),
                "error_type": str(extra.get("error_type") or ""),
            }
            if not fingerprint or not all(identity.values()):
                graph_only_review_required.append(node_id)
                continue
            expected_fingerprint = _ek2_fingerprint(identity)
            expected_case_id = (
                f"ERR-case-{expected_fingerprint.split(':', 1)[1][:24]}"
            )
            if (
                fingerprint.casefold() != expected_fingerprint.casefold()
                or node_id != expected_case_id
            ):
                graph_only_review_required.append(node_id)
                continue
            now = str(node.updated_at or _utc_now().isoformat())
            payload = {
                "case_id": node_id,
                "fingerprint": fingerprint,
                **identity,
                "error": str(extra.get("error") or node.content.description),
                "root_cause": str(
                    extra.get("root_cause") or extra.get("diagnosis") or ""
                ),
                "state": str(
                    extra.get("case_status") or extra.get("state") or "observed"
                ),
                "occurrence_count": int(extra.get("occurrence_count") or 2),
                "first_seen_at": str(extra.get("first_seen_at") or node.created_at),
                "last_seen_at": str(extra.get("last_seen_at") or now),
                "promoted_at": str(extra.get("promoted_at") or now),
                "active_resolution": extra.get("active_resolution"),
            }
        else:
            continue
        try:
            requested_state = str(payload.get("state") or "observed")
            if (
                requested_state != "observed"
                or payload.get("active_resolution")
                or extra.get("current_resolution_id")
                or extra.get("resolution_ids")
            ):
                untrusted_state_ignored.append(node_id)
            _ticket_ledger().reconcile_graph_error_case(payload)
            imported.append(node_id)
        except Exception as exc:
            failures.append({"node_id": node_id, "error": str(exc)[:300]})
    return {
        "imported": imported,
        "graph_only_review_required": graph_only_review_required,
        "untrusted_state_ignored": untrusted_state_ignored,
        "failures": failures,
        "status": "PARTIAL" if failures else "OK",
    }


@app.post("/api/errors/reconcile")
async def reconcile_error_ledger():
    return _reconcile_error_ledger_from_graph()


@app.post("/api/activity/log")
async def log_activity_endpoint(payload: dict):
    """PostToolUse writeback — record what agent just did.

    Body: {agent_id, action, detail, affected_nodes?, meta?, ticket_id?}
    Called by 3can-post-tool-capture.js after Edit/Write/MultiEdit/NotebookEdit
    and mutating Bash. Writes to activity_log (hash chain) + optional ticket
    back-reference (which ticket authorized this action).
    """
    agent_id = (payload.get("agent_id") or "unknown").strip()
    action = (payload.get("action") or "").strip()
    detail = (payload.get("detail") or "")[:400]
    affected = payload.get("affected_nodes") or []
    meta = payload.get("meta") or {}
    ticket_id = payload.get("ticket_id")
    if ticket_id:
        meta["ticket_id"] = ticket_id

    if not action:
        raise HTTPException(400, detail={"error": "action_required"})

    entry = engine.log_activity(
        agent_id=agent_id,
        action=action,
        detail=detail,
        affected_nodes=affected if isinstance(affected, list) else [],
        meta=meta,
    )
    return {
        "ok": True,
        "timestamp": entry.timestamp,
        "agent_id": entry.agent_id,
        "self_hash": entry.self_hash,
    }


def _normalize_resolved_error_ids(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value:
        node_id = str(raw or "").strip()
        if node_id and node_id not in seen:
            seen.add(node_id)
            result.append(node_id)
    return result[:20]


def _normalize_verification_evidence(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise HTTPException(
            400,
            detail={"error": "verification_evidence_must_be_typed_list"},
        )
    evidence: list[dict[str, Any]] = []
    for index, raw in enumerate(value[:20]):
        if not isinstance(raw, dict):
            raise HTTPException(
                400,
                detail={
                    "error": "verification_evidence_item_must_be_object",
                    "index": index,
                },
            )
        kind = str(raw.get("kind") or "").strip().casefold()
        reference = str(raw.get("ref") or raw.get("reference") or "").strip()
        verifier = str(raw.get("verifier") or "").strip()
        verified = raw.get("verified")
        digest = str(raw.get("digest") or "").strip().casefold()
        self_hash = str(raw.get("self_hash") or "").strip().casefold()
        if kind not in {
            "test_result",
            "build_result",
            "static_analysis",
            "migration_result",
        }:
            raise HTTPException(
                400,
                detail={
                    "error": "evidence_kind_not_allowed",
                    "index": index,
                },
            )
        if not reference or not verifier:
            raise HTTPException(
                400,
                detail={
                    "error": "evidence_kind_ref_verifier_required",
                    "index": index,
                },
            )
        if type(verified) is not bool:
            raise HTTPException(
                400,
                detail={"error": "evidence_verified_must_be_bool", "index": index},
            )
        if not digest and not self_hash:
            raise HTTPException(
                400,
                detail={
                    "error": "evidence_digest_or_self_hash_required",
                    "index": index,
                },
            )
        if digest:
            digest_hex = digest.removeprefix("sha256:")
            if not re.fullmatch(r"[0-9a-f]{64}", digest_hex):
                raise HTTPException(
                    400,
                    detail={"error": "evidence_digest_invalid", "index": index},
                )
            digest = f"sha256:{digest_hex}"
        if self_hash and not re.fullmatch(r"[0-9a-f]{64}", self_hash):
            raise HTTPException(
                400,
                detail={"error": "evidence_self_hash_invalid", "index": index},
            )
        evidence.append({
            "kind": kind[:80],
            "ref": reference[:1000],
            "summary": str(raw.get("summary") or reference).strip()[:500],
            "verified": verified,
            "verifier": verifier[:200],
            "digest": digest or None,
            "self_hash": self_hash or None,
        })
    return evidence


def _activity_by_self_hash(self_hash: str) -> Any | None:
    for entry in reversed(list(getattr(engine, "activity_log", []) or [])):
        if str(getattr(entry, "self_hash", "") or "").casefold() == self_hash:
            return entry
    return None


def _evidence_roots() -> list[Path]:
    raw = os.environ.get("THREECAN_EVIDENCE_ROOTS", "").strip()
    values = [item.strip() for item in raw.split(os.pathsep) if item.strip()]
    return [Path(item).resolve() for item in values]


def _artifact_evidence_path(reference: str) -> Path | None:
    roots = _evidence_roots()
    if not roots:
        return None
    candidate = Path(reference)
    if not candidate.is_absolute():
        candidate = roots[0] / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    if not resolved.is_file():
        return None
    max_bytes = _env_int(
        "THREECAN_EVIDENCE_MAX_BYTES",
        4 * 1024 * 1024,
        minimum=1,
    )
    try:
        if resolved.stat().st_size > max_bytes:
            return None
    except OSError:
        return None
    for root in roots:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    return None


def _sha256_artifact(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_evidence_receipts(
    evidence: list[dict[str, Any]],
    *,
    ticket: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    verified_receipts: list[dict[str, Any]] = []
    reasons: list[str] = []
    now = _utc_now().isoformat()
    for index, item in enumerate(evidence):
        actual_verified = False
        verification_status = "claim_not_verified"
        if item["verified"] and item.get("digest"):
            path = _artifact_evidence_path(item["ref"])
            if path is not None:
                actual = _sha256_artifact(path)
                expected = str(item["digest"]).removeprefix("sha256:")
                if actual != expected:
                    verification_status = "artifact_digest_mismatch"
                else:
                    try:
                        attestation = json.loads(path.read_text(encoding="utf-8"))
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        attestation = None
                    signing_key = os.environ.get(
                        "THREECAN_EVIDENCE_HMAC_KEY",
                        "",
                    )
                    if not isinstance(attestation, dict):
                        verification_status = "attestation_json_required"
                    elif (
                        attestation.get("schema_version")
                        != "3can.verification-attestation/v1"
                    ):
                        verification_status = "attestation_schema_invalid"
                    elif str(attestation.get("kind") or "").casefold() != item["kind"]:
                        verification_status = "attestation_kind_mismatch"
                    elif str(attestation.get("verifier") or "") != item["verifier"]:
                        verification_status = "attestation_verifier_mismatch"
                    elif str(attestation.get("ticket_id") or "") != str(
                        ticket.get("ticket_id") or ""
                    ):
                        verification_status = "attestation_ticket_mismatch"
                    elif str(attestation.get("target_digest") or "") != str(
                        ticket.get("target_digest") or ""
                    ):
                        verification_status = "attestation_target_mismatch"
                    elif str(attestation.get("scope_digest") or "") != str(
                        ticket.get("scope_digest") or ""
                    ):
                        verification_status = "attestation_scope_mismatch"
                    elif (
                        str(attestation.get("outcome") or "").casefold()
                        not in {"pass", "passed", "success"}
                        or attestation.get("exit_code") != 0
                        or not str(attestation.get("command") or "").strip()
                    ):
                        verification_status = "attestation_result_not_passing"
                    elif len(signing_key) < 32:
                        verification_status = "attestation_signing_key_unavailable"
                    else:
                        signature = str(
                            attestation.get("signature") or ""
                        ).casefold().removeprefix("hmac-sha256:")
                        signed_payload = {
                            key: value
                            for key, value in attestation.items()
                            if key != "signature"
                        }
                        expected_signature = hmac.new(
                            signing_key.encode("utf-8"),
                            json.dumps(
                                signed_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                                separators=(",", ":"),
                            ).encode("utf-8"),
                            hashlib.sha256,
                        ).hexdigest()
                        actual_verified = bool(
                            re.fullmatch(r"[0-9a-f]{64}", signature)
                            and hmac.compare_digest(
                                signature,
                                expected_signature,
                            )
                        )
                        verification_status = (
                            "signed_attestation_verified"
                            if actual_verified
                            else "attestation_signature_invalid"
                        )
            else:
                verification_status = "artifact_not_found_or_outside_allowed_roots"
        elif item["verified"] and item.get("self_hash"):
            activity_known = _activity_by_self_hash(item["self_hash"]) is not None
            verification_status = (
                "activity_self_hash_untrusted_for_resolution"
                if activity_known
                else "activity_self_hash_not_found"
            )
        receipt = {
            **item,
            "verified": bool(actual_verified),
            "verified_at": now if actual_verified else None,
            "verification_status": verification_status,
        }
        verified_receipts.append(receipt)
        if not actual_verified:
            reasons.append(f"evidence[{index}]:{verification_status}")
    return verified_receipts, bool(evidence) and not reasons, reasons


def _error_resolution_ids(
    error_id: str,
    *,
    solution_summary: str,
    verification_evidence: list[dict[str, Any]],
    fixed_in: str,
) -> tuple[str, str]:
    material = json.dumps(
        {
            "error_id": error_id,
            "solution_summary": solution_summary,
            "verification_evidence": verification_evidence,
            "fixed_in": fixed_in,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"FIX-{digest[:16]}", f"EVD-{digest[16:32]}"


def _validate_error_resolution_targets(
    error_ids: list[str],
    *,
    ticket_id: str,
) -> list[tuple[Any, dict[str, Any], ErrorCase]]:
    targets: list[tuple[Any, dict[str, Any], ErrorCase]] = []
    for error_id in error_ids:
        node = engine.get_node(error_id)
        if not node:
            raise HTTPException(
                404,
                detail={"error": "resolved_error_node_not_found", "node_id": error_id},
            )
        canonical = _canonical_error_case_payload(node)
        if canonical is None:
            raise HTTPException(
                409,
                detail={
                    "error": "resolved_error_not_canonical_error_case",
                    "node_id": error_id,
                },
            )
        payload, case = canonical
        prior_ticket = str(
            (node.content.extra or {}).get("resolution_ticket_id") or ""
        )
        if case.state.value == "resolved" and prior_ticket != ticket_id:
            raise HTTPException(
                409,
                detail={"error": "error_case_already_resolved", "node_id": error_id},
            )
        _ticket_ledger().reconcile_graph_error_case(payload)
        targets.append((node, payload, case))
    return targets


def _canonical_resolution_evidence(
    receipts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        {
            "kind": item["kind"],
            "reference": item["ref"],
            "summary": item["summary"],
            "verified": item["verified"],
            "verified_at": item["verified_at"],
            "digest": item.get("digest"),
            "metadata": {
                "self_hash": item.get("self_hash"),
                "verifier": item["verifier"],
                "verification_status": item["verification_status"],
            },
        }
        for item in receipts
    ]


def _build_resolution_blueprints(
    targets: list[tuple[Any, dict[str, Any], ErrorCase]],
    *,
    agent_id: str,
    ticket_id: str,
    root_cause: str,
    solution_summary: str,
    verification_evidence: list[dict[str, Any]],
    fixed_in: str,
    resolved_at: str,
) -> list[dict[str, Any]]:
    blueprints: list[dict[str, Any]] = []
    canonical_evidence = _canonical_resolution_evidence(verification_evidence)
    for error_node, case_payload, _case in targets:
        error_id = str(error_node.id)
        effective_root = (
            root_cause
            or str(case_payload.get("root_cause") or "").strip()
            or str((error_node.content.extra or {}).get("diagnosis") or "").strip()
        )
        fix_id, evidence_id = _error_resolution_ids(
            error_id,
            solution_summary=solution_summary,
            verification_evidence=verification_evidence,
            fixed_in=fixed_in,
        )
        record = {
            "resolution_id": fix_id,
            "solution_summary": solution_summary,
            "evidence": canonical_evidence,
            "resolved_by": agent_id,
            "resolved_at": resolved_at,
        }
        resolved_case = dict(case_payload)
        history = [
            item for item in resolved_case.get("resolution_history") or []
            if isinstance(item, dict) and item.get("resolution_id") != fix_id
        ]
        history.append(record)
        resolved_case.update({
            "root_cause": effective_root,
            "state": "resolved",
            "blocking": False,
            "state_changed_at": resolved_at,
            "active_resolution": record,
            "resolution_history": history,
        })
        blueprints.append({
            "error_node": error_node,
            "error_id": error_id,
            "fix_id": fix_id,
            "evidence_id": evidence_id,
            "previous_resolution_id": str(
                (error_node.content.extra or {}).get("current_resolution_id") or ""
            ).strip(),
            "effective_root_cause": effective_root,
            "canonical_evidence": canonical_evidence,
            "verified_evidence": verification_evidence,
            "resolved_case": resolved_case,
            "agent_id": agent_id,
            "ticket_id": ticket_id,
            "solution_summary": solution_summary,
            "fixed_in": fixed_in,
            "resolved_at": resolved_at,
        })
    return blueprints


def _upsert_evidence_nodes(blueprints: list[dict[str, Any]]) -> None:
    for item in blueprints:
        content = NodeContent(
            description=f"Verified evidence for {item['fix_id']}",
            current_state="verified",
            extra={
                "schema_version": "3can.resolution-evidence/v1",
                "kind": "resolution_evidence",
                "resolution_id": item["fix_id"],
                "error_id": item["error_id"],
                "evidence": item["canonical_evidence"],
                "verified_at": item["resolved_at"],
                "verified_by": item["agent_id"],
                "ticket_id": item["ticket_id"],
            },
        )
        existing = engine.get_node(item["evidence_id"])
        if existing:
            engine.update_node(
                item["evidence_id"],
                NodeUpdate(content=content, status=NodeStatus.active,
                           updated_by=item["agent_id"]),
                internal_owner="error-ledger",
            )
        else:
            engine.create_node(
                NodeCreate(
                    id=item["evidence_id"],
                    name=f"Evidence for {item['fix_id']}",
                    cluster="ErrorKnowledge",
                    layer="L2",
                    type=NodeType.reference,
                    status=NodeStatus.active,
                    content=content,
                    activation_keywords=[
                        item["fix_id"], item["error_id"], "verification evidence"
                    ],
                    priority=item["error_node"].priority,
                    primary_author=item["agent_id"],
                ),
                internal_owner="error-ledger",
            )


def _upsert_solution_nodes(blueprints: list[dict[str, Any]]) -> None:
    for item in blueprints:
        content = NodeContent(
            description=item["solution_summary"][:500],
            current_state="verified resolution",
            key_files=[item["fixed_in"]] if item["fixed_in"] else [],
            notes=item["effective_root_cause"][:1000],
            extra={
                "schema_version": "3can.error-resolution/v1",
                "kind": "error_resolution",
                "error_id": item["error_id"],
                "root_cause": item["effective_root_cause"],
                "solution_summary": item["solution_summary"],
                "evidence_id": item["evidence_id"],
                "fixed_in": item["fixed_in"],
                "resolved_at": item["resolved_at"],
                "resolved_by": item["agent_id"],
                "ticket_id": item["ticket_id"],
            },
        )
        existing = engine.get_node(item["fix_id"])
        if existing:
            engine.update_node(
                item["fix_id"],
                NodeUpdate(content=content, status=NodeStatus.active,
                           updated_by=item["agent_id"]),
                internal_owner="error-ledger",
            )
        else:
            engine.create_node(
                NodeCreate(
                    id=item["fix_id"],
                    name=f"Resolution for {item['error_id']}",
                    cluster="ErrorKnowledge",
                    layer="L2",
                    type=NodeType.knowledge,
                    status=NodeStatus.active,
                    content=content,
                    activation_keywords=[
                        item["error_id"], "error resolution", "fix", "solution"
                    ],
                    priority=item["error_node"].priority,
                    primary_author=item["agent_id"],
                ),
                internal_owner="error-ledger",
            )


def _upsert_resolution_edges(blueprints: list[dict[str, Any]]) -> None:
    for item in blueprints:
        engine.create_edge(
            EdgeCreate(
                source=item["fix_id"],
                target=item["error_id"],
                type=EdgeType.resolves,
                weight=1.0,
                description="verified resolution for canonical ErrorCase",
            ),
            internal_owner="error-ledger",
        )
        engine.create_edge(
            EdgeCreate(
                source=item["fix_id"],
                target=item["evidence_id"],
                type=EdgeType.verified_by,
                weight=1.0,
                description="server-verified resolution evidence",
            ),
            internal_owner="error-ledger",
        )
        previous = item["previous_resolution_id"]
        if previous and previous != item["fix_id"] and engine.get_node(previous):
            engine.create_edge(
                EdgeCreate(
                    source=item["fix_id"],
                    target=previous,
                    type=EdgeType.supersedes,
                    weight=1.0,
                    description="newer verified resolution",
                ),
                internal_owner="error-ledger",
            )


def _update_resolved_error_nodes(
    blueprints: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in blueprints:
        node = item["error_node"]
        content = node.content.model_dump()
        extra = dict(content.get("extra") or {})
        extra.update({
            "error_case": item["resolved_case"],
            "case_status": "resolved",
            "state": "resolved",
            "root_cause": item["effective_root_cause"],
            "solution_summary": item["solution_summary"],
            "verification_evidence": item["verified_evidence"],
            "fixed_in": item["fixed_in"],
            "resolved_at": item["resolved_at"],
            "resolved_by": item["agent_id"],
            "resolution_ticket_id": item["ticket_id"],
            "current_resolution_id": item["fix_id"],
        })
        content.update({
            "current_state": f"resolved: {item['solution_summary']}"[:500],
            "blockers": [],
            "extra": extra,
        })
        engine.update_node(
            item["error_id"],
            NodeUpdate(
                content=NodeContent(**content),
                status=NodeStatus.active,
                updated_by=item["agent_id"],
            ),
            internal_owner="error-ledger",
        )
        results.append({
            "error_id": item["error_id"],
            "resolution_id": item["fix_id"],
            "evidence_id": item["evidence_id"],
            "case_status": "resolved",
        })
    return results


def _mark_errors_review_required(
    targets: list[tuple[Any, dict[str, Any], ErrorCase]],
    *,
    agent_id: str,
    ticket_id: str,
    evidence: list[dict[str, Any]],
    reasons: list[str],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for node, case_payload, _case in targets:
        content = node.content.model_dump()
        extra = dict(content.get("extra") or {})
        canonical = dict(case_payload)
        metadata = dict(canonical.get("metadata") or {})
        metadata["resolution_review"] = {
            "status": "review_required",
            "ticket_id": ticket_id,
            "requested_by": agent_id,
            "evidence": evidence,
            "reasons": reasons,
            "updated_at": _utc_now().isoformat(),
        }
        canonical["metadata"] = metadata
        extra.update({
            "error_case": canonical,
            "case_status": "review_required",
            "verification_evidence": evidence,
            "resolution_review_reasons": reasons,
            "resolution_ticket_id": ticket_id,
        })
        content.update({
            "current_state": "review_required: resolution evidence was not verified",
            "extra": extra,
        })
        engine.update_node(
            node.id,
            NodeUpdate(content=NodeContent(**content), status=NodeStatus.active,
                       updated_by=agent_id),
            internal_owner="error-ledger",
        )
        results.append({
            "error_id": node.id,
            "resolution_id": None,
            "evidence_id": None,
            "case_status": "review_required",
            "review_reasons": reasons,
        })
    return results


def _find_completion_activity(ticket_id: str, request_hash: str) -> Any | None:
    for entry in reversed(list(getattr(engine, "activity_log", []) or [])):
        meta = getattr(entry, "meta", {}) or {}
        if (
            str(meta.get("ticket_id") or "") == ticket_id
            and str(meta.get("completion_request_hash") or "") == request_hash
            and str(getattr(entry, "action", "") or "") == "done"
        ):
            return entry
    return None


_ERROR_DISPOSITIONS = frozenset(
    {"resolved", "still_open", "not_applicable"}
)


def _normalize_error_dispositions(value: Any) -> list[dict[str, str]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise HTTPException(
            400,
            detail={"error": "error_dispositions_must_be_list"},
        )
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise HTTPException(
                400,
                detail={
                    "error": "error_disposition_must_be_object",
                    "index": index,
                },
            )
        error_id = str(item.get("error_id") or "").strip()
        disposition = str(item.get("disposition") or "").strip().casefold()
        reason = str(item.get("reason") or "").strip()[:1000]
        if not error_id:
            raise HTTPException(
                400,
                detail={
                    "error": "error_disposition_error_id_required",
                    "index": index,
                },
            )
        if error_id in seen:
            raise HTTPException(
                400,
                detail={
                    "error": "error_disposition_duplicate",
                    "error_id": error_id,
                },
            )
        if disposition not in _ERROR_DISPOSITIONS:
            raise HTTPException(
                400,
                detail={
                    "error": "error_disposition_invalid",
                    "error_id": error_id,
                    "allowed": sorted(_ERROR_DISPOSITIONS),
                },
            )
        if disposition in {"still_open", "not_applicable"} and not reason:
            raise HTTPException(
                400,
                detail={
                    "error": "error_disposition_reason_required",
                    "error_id": error_id,
                    "disposition": disposition,
                },
            )
        normalized.append(
            {
                "error_id": error_id,
                "disposition": disposition,
                "reason": reason,
            }
        )
        seen.add(error_id)
    return normalized


@app.post("/api/activity/done")
async def complete_activity_endpoint(payload: dict):
    """Complete exactly once through CAS plus a recoverable graph journal."""
    agent_id = str(payload.get("agent_id") or "").strip()
    ticket_id = str(payload.get("ticket_id") or "").strip()
    detail = str(payload.get("detail") or "")[:400]
    affected = payload.get("affected_nodes") or []
    meta = payload.get("meta") or {}
    if not agent_id:
        raise HTTPException(400, detail={"error": "agent_id_required"})
    if not ticket_id:
        raise HTTPException(400, detail={"error": "ticket_id_required"})
    if not isinstance(meta, dict):
        raise HTTPException(400, detail={"error": "meta_must_be_object"})

    error_ids = _normalize_resolved_error_ids(payload.get("resolved_errors"))
    error_dispositions = _normalize_error_dispositions(
        payload.get("error_dispositions")
    )
    evidence = (
        _normalize_verification_evidence(payload.get("verification_evidence"))
        if error_ids else []
    )
    root_cause = str(payload.get("root_cause") or "").strip()[:2000]
    solution_summary = str(payload.get("solution_summary") or "").strip()[:2000]
    fixed_in = str(payload.get("fixed_in") or "").strip()[:500]
    if error_ids:
        if not solution_summary:
            raise HTTPException(
                400,
                detail={"error": "solution_summary_required_for_error_resolution"},
            )
        if not evidence:
            raise HTTPException(
                400,
                detail={"error": "verification_evidence_required_for_error_resolution"},
            )
    affected_ids = [
        str(value) for value in affected
        if str(value or "").strip()
    ] if isinstance(affected, list) else []
    affected_ids = list(dict.fromkeys(affected_ids))
    canonical_request = {
        "schema_version": "3can.ticket-completion/v3",
        "agent_id": agent_id,
        "ticket_id": ticket_id,
        "detail": detail,
        "affected_nodes": affected_ids,
        "meta": meta,
        "resolved_errors": error_ids,
        "error_dispositions": error_dispositions,
        "root_cause": root_cause,
        "solution_summary": solution_summary,
        "verification_evidence": evidence,
        "fixed_in": fixed_in,
    }
    try:
        request_hash = canonical_hash(canonical_request)
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, detail={"error": "completion_payload_not_json"}) from exc

    ledger = _ticket_ledger()
    owner_token = uuid.uuid4().hex
    try:
        authorization = ledger.begin_completion(
            ticket_id,
            agent_id=agent_id,
            request_hash=request_hash,
            request=_sanitize_completion_audit_request(canonical_request),
            requested_error_ids=error_ids,
            error_dispositions={
                item["error_id"]: item["disposition"]
                for item in error_dispositions
            },
            owner_token=owner_token,
        )
    except LedgerError as exc:
        raise HTTPException(
            _ledger_http_status(exc),
            detail={"error": exc.code, "ticket_id": ticket_id},
        ) from exc
    if authorization["mode"] == "replay":
        return authorization["response"]

    journal_context = dict(authorization.get("context") or {})
    resolution_results: list[dict[str, Any]] = []
    resolved_at = _utc_now().isoformat()
    try:
        targets = _validate_error_resolution_targets(
            error_ids,
            ticket_id=ticket_id,
        )
        verified_evidence, evidence_ok, review_reasons = (
            _verify_evidence_receipts(
                evidence,
                ticket=authorization["ticket"],
            )
        )
        if targets and evidence_ok:
            blueprints = _build_resolution_blueprints(
                targets,
                agent_id=agent_id,
                ticket_id=ticket_id,
                root_cause=root_cause,
                solution_summary=solution_summary,
                verification_evidence=verified_evidence,
                fixed_in=fixed_in,
                resolved_at=resolved_at,
            )
            journal_context["resolution_refs"] = [
                {
                    "error_id": item["error_id"],
                    "resolution_id": item["fix_id"],
                    "evidence_id": item["evidence_id"],
                }
                for item in blueprints
            ]
            _upsert_evidence_nodes(blueprints)
            ledger.advance_completion(
                ticket_id, request_hash=request_hash, owner_token=owner_token,
                stage="evidence_upserted", context=journal_context,
            )
            _upsert_solution_nodes(blueprints)
            ledger.advance_completion(
                ticket_id, request_hash=request_hash, owner_token=owner_token,
                stage="solution_upserted", context=journal_context,
            )
            _upsert_resolution_edges(blueprints)
            ledger.advance_completion(
                ticket_id, request_hash=request_hash, owner_token=owner_token,
                stage="edges_upserted", context=journal_context,
            )
            resolution_results = _update_resolved_error_nodes(blueprints)
            ledger.resolve_error_cases(
                [
                    {
                        "case_id": result["error_id"],
                        "resolution_id": result["resolution_id"],
                        "resolved_at": resolved_at,
                        "resolved_by": agent_id,
                        "solution_summary": solution_summary,
                        "evidence": verified_evidence,
                    }
                    for result in resolution_results
                ]
            )
            ledger.advance_completion(
                ticket_id, request_hash=request_hash, owner_token=owner_token,
                stage="error_updated", context=journal_context,
            )
        elif targets:
            resolution_results = _mark_errors_review_required(
                targets,
                agent_id=agent_id,
                ticket_id=ticket_id,
                evidence=verified_evidence,
                reasons=review_reasons,
            )
            journal_context["review_reasons"] = review_reasons
            ledger.advance_completion(
                ticket_id, request_hash=request_hash, owner_token=owner_token,
                stage="review_required", context=journal_context,
            )

        for result in resolution_results:
            affected_ids.extend([
                value for value in (
                    result["error_id"],
                    result.get("resolution_id"),
                    result.get("evidence_id"),
                )
                if value
            ])
        affected_ids = list(dict.fromkeys(affected_ids))
        entry = _find_completion_activity(ticket_id, request_hash)
        resolution_outcome = (
            "resolved"
            if targets and evidence_ok
            else (
                "review_required"
                if targets
                else (
                    "disposition_recorded"
                    if error_dispositions
                    else "completed"
                )
            )
        )
        if entry is None:
            activity_meta = {
                **meta,
                "ticket_id": ticket_id,
                "completion_request_hash": request_hash,
                "resolved_error_ids": error_ids,
                "error_dispositions": error_dispositions,
                "resolution_outcome": resolution_outcome,
            }
            entry = engine.log_activity(
                agent_id=agent_id,
                action="done",
                detail=detail,
                affected_nodes=affected_ids,
                meta=activity_meta,
            )
        journal_context["activity_self_hash"] = entry.self_hash
        ledger.advance_completion(
            ticket_id, request_hash=request_hash, owner_token=owner_token,
            stage="activity_logged", context=journal_context,
        )
        response = {
            "ok": True,
            "timestamp": entry.timestamp,
            "agent_id": entry.agent_id,
            "self_hash": entry.self_hash,
            "ticket_id": ticket_id,
            "ticket_state": "completed",
            "completion_request_hash": request_hash,
            "resolution_outcome": resolution_outcome,
            "resolved_errors": resolution_results,
            "error_dispositions": error_dispositions,
        }
        response = ledger.complete(
            ticket_id,
            request_hash=request_hash,
            owner_token=owner_token,
            response=response,
        )
    except HTTPException as exc:
        ledger.release_completion(
            ticket_id, request_hash=request_hash, owner_token=owner_token,
            error=str(exc.detail),
        )
        raise
    except Exception as exc:
        ledger.release_completion(
            ticket_id, request_hash=request_hash, owner_token=owner_token,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise HTTPException(
            500,
            detail={
                "error": "completion_failed_recoverable",
                "ticket_id": ticket_id,
                "request_hash": request_hash,
                "journal_stage": (
                    (ledger.journal(ticket_id) or {}).get("stage")
                ),
            },
        ) from exc

    for result in resolution_results:
        await manager.broadcast({
            "event": (
                "error_resolved"
                if result["case_status"] == "resolved"
                else "error_review_required"
            ),
            "error_id": result["error_id"],
            "resolution_id": result.get("resolution_id"),
        })
    return response


# Handoff主动交接机制 — 新session快速感知待办
@app.post("/api/handoff/create")
async def handoff_create(payload: dict):
    """Agent session结束前主动create handoff节点, 目标agent新开session时能感知.

    Body: {from_agent, to_agent ("*"广播), context_node_ids, task_continuation, unresolved}
    """
    from_agent = payload.get("from_agent", "unknown")
    to_agent = payload.get("to_agent", "*")
    context_ids = payload.get("context_node_ids", [])
    task_cont = payload.get("task_continuation", "")
    unresolved = payload.get("unresolved", [])
    execution_context = _execution_identity_context(payload)

    if not isinstance(context_ids, list) or any(
        not isinstance(node_id, str) for node_id in context_ids
    ):
        raise HTTPException(
            400,
            detail={"error": "handoff_context_node_ids_invalid"},
        )
    existing_context_ids = [
        node_id for node_id in context_ids if node_id in engine.nodes
    ]
    reserved_context_ids = [
        node_id
        for node_id in existing_context_ids
        if _reserved_error_knowledge_id(node_id)
    ]
    if reserved_context_ids:
        raise HTTPException(
            403,
            detail={
                "error": "handoff_reserved_context_not_allowed",
                "node_ids": reserved_context_ids,
            },
        )

    now = _utc_now()
    ts = now.strftime("%Y%m%d-%H%M%S")
    node_id = f"HO-{ts}-{from_agent.replace('-', '_')}"

    from models import NodeCreate, NodeContent
    content = NodeContent(
        description=f"{from_agent} → {to_agent} 主动交接",
        current_state=f"pending (to_agent={to_agent})",
        notes=f"任务延续: {task_cont}\n未解决: {'; '.join(unresolved)}\n上下文节点: {', '.join(context_ids)}",
        extra={
            "from_agent": from_agent,
            "to_agent": to_agent,
            "context_node_ids": context_ids,
            "acknowledged_by": {},
            "unresolved": unresolved,
            **execution_context,
        },
    )
    req = NodeCreate(
        id=node_id, name=f"Handoff {from_agent}→{to_agent}",
        cluster="项目交接", layer="L1", type="session", status="active",
        content=content,
        activation_keywords=["handoff", "交接", from_agent, to_agent, "pending", "待办"],
        priority="high",
    )
    node = engine.create_node(req)

    # 建立context edges
    for cid in existing_context_ids:
        engine.create_edge(
            EdgeCreate(
                source=node_id,
                target=cid,
                type=EdgeType.informs,
                weight=0.5,
                description="handoff上下文",
            )
        )

    await manager.broadcast({"event": "handoff_created", "node": node.model_dump()})
    return {"handoff_id": node_id, "to_agent": to_agent}


@app.get("/api/handoff/pending")
async def handoff_pending(agent_id: str = Query(...)):
    """返回指向本agent或广播的未ack handoff. 新session冷启动时调用."""
    pending = []
    for n in engine.nodes.values():
        if not n.id.startswith("HO-"):
            continue
        if n.status.value != "active":
            continue
        to = n.content.extra.get("to_agent", "")
        if to != agent_id and to != "*":
            continue
        ack = n.content.extra.get("acknowledged_by", {})
        if ack.get(agent_id):
            continue
        pending.append({
            "id": n.id,
            "from_agent": n.content.extra.get("from_agent"),
            "task_continuation": n.content.notes.split("未解决:")[0].replace("任务延续:", "").strip() if n.content.notes else "",
            "unresolved": n.content.extra.get("unresolved", []),
            "context_node_ids": n.content.extra.get("context_node_ids", []),
            "created_at": n.created_at,
        })
    return {"agent_id": agent_id, "count": len(pending), "handoffs": pending}


@app.post("/api/handoff/{handoff_id}/ack")
async def handoff_ack(handoff_id: str, payload: dict):
    """Agent确认已读handoff."""
    raw_agent_id = payload.get("agent_id")
    if not isinstance(raw_agent_id, str) or not raw_agent_id.strip():
        raise HTTPException(400, detail={"error": "handoff_agent_id_required"})
    agent_id = raw_agent_id.strip()
    node = engine.nodes.get(handoff_id)
    if not node:
        raise HTTPException(404, "handoff不存在")
    extra = node.content.extra if isinstance(node.content.extra, dict) else {}
    node_type = str(getattr(node.type, "value", node.type)).strip().casefold()
    if (
        not handoff_id.startswith("HO-")
        or node_type != "session"
        or not isinstance(extra.get("from_agent"), str)
        or not isinstance(extra.get("to_agent"), str)
        or not isinstance(extra.get("acknowledged_by"), dict)
    ):
        raise HTTPException(400, detail={"error": "handoff_identity_invalid"})
    ack = dict(extra["acknowledged_by"])
    ack[agent_id] = _utc_now().isoformat()
    node.content.extra["acknowledged_by"] = ack
    node.updated_at = _utc_now().isoformat()
    engine._save_node(node)
    return {"acknowledged": handoff_id, "agent_id": agent_id}


# ── 自动回写 (v9.4 基座#32: Writeback Rate Limit) ──

# 内存状态: (agent_id, node_id) → list of timestamps (近 60s 内)
_WB_COUNTER: dict[tuple[str, str], list[float]] = {}
_WB_WINDOW_SEC = 60
_WB_MAX_PER_WINDOW = 5


def _writeback_rate_limit_violators(agent_id: str, node_ids: list[str]) -> list[str]:
    """Read the current window without consuming quota."""
    import time
    now = time.time()
    cutoff = now - _WB_WINDOW_SEC
    violators = []
    for nid in dict.fromkeys(node_ids):
        key = (agent_id, nid)
        hist = [t for t in _WB_COUNTER.get(key, []) if t >= cutoff]
        if len(hist) >= _WB_MAX_PER_WINDOW:
            violators.append(nid)
    return violators


def _record_writeback_rate_limit(agent_id: str, node_ids: list[str]) -> None:
    """Consume one quota unit only for nodes changed by a successful batch."""
    import time
    now = time.time()
    cutoff = now - _WB_WINDOW_SEC
    for nid in dict.fromkeys(node_ids):
        key = (agent_id, nid)
        hist = [t for t in _WB_COUNTER.get(key, []) if t >= cutoff]
        hist.append(now)
        _WB_COUNTER[key] = hist


@app.post("/api/writeback")
async def session_writeback(payload: dict):
    """Session结束时批量回写节点变更。支持 agent_id 追踪。
    v9.4 基座#32: 同 agent+同 node 60s 内 ≤5 次, 超限 429 防刷."""
    if isinstance(payload, list):
        changes = payload
        agent_id = "unknown"
    elif isinstance(payload, dict):
        changes = payload.get("changes", [])
        raw_agent_id = payload.get("agent_id", "unknown")
        if not isinstance(raw_agent_id, str):
            raise HTTPException(400, detail={"error": "writeback_agent_id_invalid"})
        agent_id = raw_agent_id.strip() or "unknown"
    else:
        raise HTTPException(400, detail={"error": "writeback_payload_invalid"})
    if not isinstance(changes, list):
        raise HTTPException(
            400,
            detail={"error": "writeback_changes_must_be_list"},
        )

    # v9.4 #32 Rate limit 检查
    target_nodes = [
        node_id
        for change in changes
        if isinstance(change, dict)
        and isinstance((node_id := change.get("node_id")), str)
        and node_id
    ]
    if target_nodes:
        violators = _writeback_rate_limit_violators(agent_id, target_nodes)
        if violators:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "writeback_rate_limit",
                    "agent_id": agent_id,
                    "violators": violators,
                    "window_sec": _WB_WINDOW_SEC,
                    "max_per_window": _WB_MAX_PER_WINDOW,
                    "guidance": f"Agent {agent_id} 在 {_WB_WINDOW_SEC}s 窗口内对节点 {violators} writeback 次数已超 {_WB_MAX_PER_WINDOW}. 防刷. 等 60s 再写, 或合并多次变更为一次.",
                },
            )

    execution_context = _execution_identity_context(
        payload if isinstance(payload, dict) else {}
    )
    provenance_payload = payload if isinstance(payload, dict) else {}
    try:
        provenance = DurableProvenance(
            source_provenance=provenance_payload.get("source_provenance", "untrusted_inferred"),
            verification_state=provenance_payload.get("verification_state", "unverified"),
            evidence_refs=provenance_payload.get("evidence_refs", []),
            authorized_by=provenance_payload.get("authorized_by", ""),
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "writeback_provenance_invalid", "message": str(exc)},
        ) from exc
    try:
        updated = engine.session_writeback(
            changes,
            agent_id=agent_id,
            execution_context=execution_context,
            provenance=provenance,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "writeback_change_invalid",
                "reason": str(exc),
            },
        ) from exc
    if updated:
        _record_writeback_rate_limit(agent_id, updated)
    await manager.broadcast({"event": "writeback", "updated": updated, "agent_id": agent_id})
    return {"updated": updated, "count": len(updated), "agent_id": agent_id}


# ── 用户偏好沉淀 ──

@app.post("/api/preference")
async def learn_preference(payload: dict):
    """沉淀用户偏好。"""
    key = payload.get("key", "general")
    value = payload.get("value", "")
    context = payload.get("context", "")
    node = engine.learn_preference(key, value, context)
    await manager.broadcast({"event": "preference_learned", "key": key})
    return {"status": "ok", "preferences": node.content.extra.get("preferences", {})}


# ── Skill 管理 (v9.1) ──

@app.get("/api/skills")
async def list_skills(active_only: bool = Query(True)):
    """列出所有 type=skill 节点 + 使用统计."""
    out = []
    for n in engine.nodes.values():
        # NodeType(str, Enum): n.type.value == "skill", 也接受字符串直写
        ntype = getattr(n.type, "value", n.type)
        if ntype != "skill":
            continue
        nstatus = getattr(n.status, "value", n.status)
        if active_only and nstatus != "active":
            continue
        extra = (n.content.extra or {}) if n.content else {}
        sc = extra.get("success_count", 0) or 0
        fc = extra.get("fail_count", 0) or 0
        total = sc + fc
        rate = (sc / total) if total else None
        out.append({
            "id": n.id,
            "name": n.name,
            "source": extra.get("skill_source"),
            "success_count": sc,
            "fail_count": fc,
            "success_rate": rate,
            "avg_duration_s": extra.get("avg_duration_s"),
            "last_invoked_at": extra.get("last_invoked_at"),
            "description": (n.content.description or "")[:120],
        })
    return {"total": len(out), "skills": sorted(out, key=lambda x: x["id"])}


@app.post("/api/skills/invoke")
async def record_skill_invocation(payload: dict):
    """记 skill 调用结果. Body: {skill_id, agent_id, outcome: 'success'|'fail', duration_s?, notes?}.
    更新 content.extra 统计 + 活动日志.
    """
    import datetime as _dt
    skill_id = payload.get("skill_id")
    if not skill_id:
        raise HTTPException(400, "missing skill_id")
    agent_id = payload.get("agent_id", "unknown")
    outcome = payload.get("outcome", "success")
    duration_s = payload.get("duration_s")
    node = engine.get_node(skill_id)
    if not node:
        raise HTTPException(404, f"skill {skill_id} 不存在")
    ntype = getattr(node.type, "value", node.type)
    if ntype != "skill":
        raise HTTPException(400, f"{skill_id} 不是 skill 节点 (type={ntype})")

    extra = dict(node.content.extra or {})
    sc = int(extra.get("success_count", 0) or 0)
    fc = int(extra.get("fail_count", 0) or 0)
    avg = extra.get("avg_duration_s")

    if outcome == "success":
        sc += 1
    else:
        fc += 1
    # 运行平均 duration
    if duration_s is not None:
        n_calls = sc + fc
        if avg is None or n_calls <= 1:
            avg = float(duration_s)
        else:
            avg = (avg * (n_calls - 1) + float(duration_s)) / n_calls

    extra.update({
        "success_count": sc,
        "fail_count": fc,
        "avg_duration_s": round(avg, 3) if avg is not None else None,
        "last_invoked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "last_outcome": outcome,
        "last_agent": agent_id,
    })
    node.content.extra = extra
    node.activation_count += 1
    engine._save_node(node)

    engine.log_activity(
        agent_id, "skill_invoked",
        f"skill={skill_id} outcome={outcome} dur={duration_s}",
        affected_nodes=[skill_id],
    )

    total = sc + fc
    rate = sc / total if total else None
    return {
        "skill_id": skill_id,
        "success_count": sc,
        "fail_count": fc,
        "success_rate": rate,
        "avg_duration_s": extra["avg_duration_s"],
    }


# ── Audit (v9.3) ──

@app.get("/api/audit/verify")
async def audit_verify_chain():
    """校验 activity_log 的 hash chain 完整性. 开源后给第三方审计用."""
    return engine.verify_activity_chain()


# ── 热重载 ──

@app.post("/api/reload")
async def reload_graph(background: bool = True):
    """热重载. background=True (默认) 在线程池执行不阻塞event loop. 即使触发全量rebuild, 其他API正常服务."""
    if background:
        asyncio.create_task(asyncio.to_thread(engine.reload))
        await manager.broadcast({"event": "graph_reload_started"})
        return {"status": "started", "background": True, "nodes_before": len(engine.nodes), "hint": "查询 /api/stats 直到node数变化"}
    else:
        await asyncio.to_thread(engine.reload)
        await manager.broadcast({"event": "graph_reloaded"})
        return {"nodes": len(engine.nodes), "edges": len(engine.edges)}


# ── Feature 1: 同步层 ──

@app.post("/api/sync/start")
async def start_sync(payload: dict = {}):
    """启动文件变更监听。"""
    memory_dir = Path(payload.get("memory_dir") or _default_memory_dir())
    interval = payload.get("interval", 30)
    engine.start_sync_watcher([memory_dir], interval=interval)
    return {"status": "watching", "dirs": [str(memory_dir)], "interval": interval}


@app.post("/api/sync/stop")
async def stop_sync():
    engine.stop_sync_watcher()
    return {"status": "stopped"}


@app.post("/api/sync/rescan")
async def rescan_memory(payload: dict = {}):
    """手动触发memory目录全量rescan。"""
    memory_dir = Path(payload.get("memory_dir") or _default_memory_dir())
    result = engine.rescan_memory_dir(memory_dir)
    return result


# ── 节点生命周期管理 ──

@app.post("/api/lifecycle/sweep")
async def lifecycle_sweep(payload: dict = {}):
    """执行节点生命周期扫描: active→stale→archived。永不删除。
    v7.3 async: CPU重操作卸载到thread, 不阻塞event loop。
    """
    import asyncio
    stale_days = int(payload.get("stale_days", 30))
    archive_days = int(payload.get("archive_days", 60))
    background = bool(payload.get("background", False))
    if background:
        asyncio.create_task(asyncio.to_thread(engine.lifecycle_sweep, stale_days, archive_days))
        return {"status": "started", "background": True}
    result = await asyncio.to_thread(engine.lifecycle_sweep, stale_days, archive_days)
    return result


@app.get("/api/lifecycle/stats")
async def lifecycle_stats():
    """节点生命周期统计。"""
    return engine.get_lifecycle_stats()


# ── R12-R16 节点瘦身体系 ──

@app.get("/api/health/scan")
async def health_scan():
    """只读体检: 孤节点/零激活/cosine合并候选/prefix倾斜/health_score。不改数据。"""
    import asyncio
    return await asyncio.to_thread(engine.health_scan)


@app.get("/api/health/outcome-stats")
async def outcome_stats():
    """Layer 4 sidecar: outcome学习统计。
    返回click_log状态、shadow/active/demoted计数、token效率趋势。
    """
    log = engine._click_log
    total_queries = len(log)
    total_signals = sum(len(v) for v in log.values())
    # shadow: signal < 3, active: signal >= 3, demoted: signal <= -2
    shadow = active = demoted = 0
    for query, nodes in log.items():
        for nid, sig in nodes.items():
            if sig >= 3.0:
                active += 1
            elif sig <= -2.0:
                demoted += 1
            else:
                shadow += 1

    code_index_size = sum(len(v) for v in engine._code_index.values())

    # Miss Healer stats
    pending_kw = engine._pending_keywords
    pending_nodes = len(pending_kw)
    pending_tokens = sum(len(v) for v in pending_kw.values())
    near_promote = sum(1 for nid_kws in pending_kw.values()
                       for count in nid_kws.values() if count >= 2)

    return {
        "click_log_queries": total_queries,
        "click_log_signals": total_signals,
        "mapping_shadow": shadow,
        "mapping_active": active,
        "mapping_demoted": demoted,
        "code_index_entries": code_index_size,
        "code_index_codes": len(engine._code_index),
        "miss_healer_pending_nodes": pending_nodes,
        "miss_healer_pending_tokens": pending_tokens,
        "miss_healer_near_promote": near_promote,
    }


@app.post("/api/nodes/merge")
async def merge_nodes(payload: dict):
    """合并两个节点。keep_id保留, remove_id转dormant并入keep。
    approver必须显式传入(建议Ka或admin)。
    """
    keep_id = payload.get("keep_id")
    remove_id = payload.get("remove_id")
    approver = payload.get("approver", "unknown")
    if not keep_id or not remove_id:
        raise HTTPException(422, "need keep_id and remove_id")
    _guard_error_knowledge_crud(keep_id, remove_id)
    result = engine.merge_nodes(keep_id, remove_id, approver=approver)
    if "error" in result:
        error = str(result["error"])
        status = 404 if "不存在" in error else (403 if "forbidden" in error else 400)
        raise HTTPException(status, detail={"error": error, "guidance": result.get("guidance")})
    await manager.broadcast({"event": "nodes_merged", "result": result})
    return result


@app.post("/api/nodes/batch-dormant")
async def batch_dormant(payload: dict):
    """批量转dormant。node_ids=[...], reason=str。Ka审批后使用。"""
    node_ids = payload.get("node_ids", [])
    reason = payload.get("reason", "manual")
    if not node_ids:
        raise HTTPException(422, "need node_ids list")
    result = engine.batch_dormant(node_ids, reason=reason)
    await manager.broadcast({"event": "batch_dormant", "result": result})
    return result


# ── Auto-Dream 远程触发 ──

@app.post("/api/dream/run")
async def run_dream(payload: dict = {}):
    """远程触发Auto-Dream记忆整理管线。
    v7.3 async: LLM+文件扫描全走thread, 不阻塞其他agent route。
    {"scope_hours": 24, "dry_run": true, "background": false}
    """
    import asyncio
    scope_hours = int(payload.get("scope_hours", 24))
    dry_run = payload.get("dry_run", True)
    background = bool(payload.get("background", False))

    try:
        import sys
        sys.path.insert(0, str(_default_project_dir()))
        from tools.kairos.auto_dream import run_dream as _run_dream

        if background:
            asyncio.create_task(asyncio.to_thread(_run_dream, scope_hours=scope_hours, dry_run=dry_run, verbose=False))
            return {"ok": True, "status": "started", "background": True}
        report = await asyncio.to_thread(_run_dream, scope_hours=scope_hours, dry_run=dry_run, verbose=False)
        return {
            "ok": True,
            "scope_hours": report.scope_hours,
            "candidates": report.gather_candidates,
            "kept": report.consolidate_kept,
            "pruned": report.prune_archived,
            "memory_md_lines": report.memory_md_before_lines,
            "new_entries": report.new_entries[:10],
            "prune_entries": report.prune_entries[:10],
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Feature 3: 节点自动发现 ──

@app.post("/api/discover")
async def discover_interfaces(payload: dict = {}):
    """扫描代码仓库，返回建议创建的INTF节点列表。
    v7.3 async: 文件遍历卸载到thread。
    """
    import asyncio
    from pathlib import Path
    code_dir = Path(payload.get("code_dir") or _default_project_dir())
    patterns = payload.get("patterns", ["tools/**/*.py", "scripts/**/*.py"])
    suggestions = await asyncio.to_thread(engine.discover_interfaces, code_dir, patterns)
    return {"suggestions": suggestions, "count": len(suggestions)}


# ═══════════════════════════════════════════════
# Multi-Agent Coordination Layer
# ═══════════════════════════════════════════════

@app.post("/api/agents/checkin")
async def agent_checkin(payload: dict):
    """Agent签到/注册。任何Agent启动时调用一次。

    {"agent_id":"opus3", "name":"Opus3 3CAN引擎", "role":"3CAN开发",
     "current_task":"Multi-Agent Coordination", "capabilities":["code","memory"]}
    """
    agent_id = payload.get("agent_id")
    if not agent_id:
        raise HTTPException(400, "agent_id is required")
    agent = engine.agent_checkin(
        agent_id=agent_id,
        name=payload.get("name", ""),
        role=payload.get("role", ""),
        current_task=payload.get("current_task", ""),
        session_id=payload.get("session_id", ""),
        capabilities=payload.get("capabilities"),
        meta=payload.get("meta"),
    )
    await manager.broadcast({"event": "agent_checkin", "agent": agent.model_dump()})
    return agent.model_dump()


def _agents_with_heartbeat_presence(
    registered_agents: list[Any],
    *,
    status_filter: str | None,
    heartbeat_ttl_sec: int,
    now: dt.datetime | None = None,
) -> list[Any]:
    """Project persisted registrations into a heartbeat-aware API view."""
    ttl_sec = max(1, int(heartbeat_ttl_sec))
    current_time = now or dt.datetime.now(dt.timezone.utc)
    projected = []
    for registered in registered_agents:
        agent = registered.model_copy(deep=True)
        registered_status = agent.status.value
        heartbeat_age_sec: int | None = None
        heartbeat_stale = True
        try:
            checked_in = dt.datetime.fromisoformat(
                str(agent.last_checkin).replace("Z", "+00:00")
            )
            if checked_in.tzinfo is None:
                checked_in = checked_in.replace(tzinfo=dt.timezone.utc)
            heartbeat_age_sec = max(
                0,
                int(
                    (
                        current_time
                        - checked_in.astimezone(dt.timezone.utc)
                    ).total_seconds()
                ),
            )
            heartbeat_stale = heartbeat_age_sec > ttl_sec
        except (TypeError, ValueError):
            heartbeat_stale = True

        if heartbeat_stale and agent.status.value != "offline":
            agent.status = type(agent.status).offline
        agent.meta = dict(agent.meta)
        agent.meta["heartbeat_presence"] = {
            "stale": heartbeat_stale,
            "age_sec": heartbeat_age_sec,
            "ttl_sec": ttl_sec,
            "registered_status": registered_status,
        }
        projected.append(agent)

    if status_filter:
        normalized_filter = str(status_filter).strip().casefold()
        if normalized_filter == "stale":
            projected = [
                agent
                for agent in projected
                if bool(agent.meta["heartbeat_presence"]["stale"])
            ]
        else:
            projected = [
                agent
                for agent in projected
                if agent.status.value == normalized_filter
            ]
    return projected


@app.get("/api/agents")
async def list_agents(
    status: str | None = Query(None, description="online|busy|idle|offline|stale"),
    heartbeat_ttl_sec: int = Query(300, ge=30, le=86400),
):
    """列出Agent登记；超过心跳TTL的条目投影为offline。"""
    agents = _agents_with_heartbeat_presence(
        engine.list_agents(),
        status_filter=status,
        heartbeat_ttl_sec=heartbeat_ttl_sec,
    )
    return [a.model_dump() for a in agents]


@app.put("/api/agents/{agent_id}/task")
async def update_agent_task(agent_id: str, payload: dict):
    """更新Agent当前任务。

    {"current_task":"正在写knowledge_purifier.py", "status":"busy"}
    """
    agent = engine.agent_update_task(
        agent_id=agent_id,
        current_task=payload.get("current_task", ""),
        status=payload.get("status", "busy"),
    )
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not registered. Call /api/agents/checkin first.")
    await manager.broadcast({"event": "agent_task_update", "agent": agent.model_dump()})
    return agent.model_dump()


_ROLE_FILTERS: dict[str, dict] = {
    # role → {prefix_boost: [...], cluster_boost: [...], include_global_hot: bool}
    "brain":    {"prefix_boost": [], "cluster_boost": [], "include_global_hot": True},
    "frontend": {"prefix_boost": ["MOD-frontend", "INTF-frontend", "DOC-frontend", "INTF-advisor"], "cluster_boost": ["接口契约"], "include_global_hot": False},
    "backend":  {"prefix_boost": ["MOD-backend", "INTF-", "MOD-advisor", "MOD-wu"], "cluster_boost": ["接口契约", "项目模块"], "include_global_hot": False},
    "video":    {"prefix_boost": ["MOD-video", "FEE-video", "PRO-video", "MEM-video"], "cluster_boost": [], "include_global_hot": False},
    "3can":     {"prefix_boost": ["ARCH-", "MEM-agent", "PRO-3can", "AGT-", "MEM-MEMORY"], "cluster_boost": ["架构设计", "Agent协作"], "include_global_hot": False},
    "ops":      {"prefix_boost": ["SEC-", "MOD-autodl"], "cluster_boost": ["密钥配置"], "include_global_hot": False},
    "data":     {"prefix_boost": ["MOD-kb", "MOD-distill", "MOD-data", "MEM-data"], "cluster_boost": [], "include_global_hot": False},
    "review":   {"prefix_boost": ["ERR-", "FEE-", "DEC-"], "cluster_boost": ["错误与教训", "反馈与规则", "战略决策"], "include_global_hot": True},
}


def _compress_node(node) -> dict:
    """Briefing返回时压缩节点: 只返id/name/cluster/current_state[:150]/keywords/activation_count, 去掉notes全文减少token."""
    c = node.content
    return {
        "id": node.id,
        "name": node.name,
        "cluster": node.cluster,
        "current_state": (c.current_state or "")[:150],
        "description": (c.description or "")[:100],
        "activation_keywords": node.activation_keywords[:5],
        "activation_count": node.activation_count,
        "updated_at": node.updated_at,
    }


@app.get("/api/briefing")
async def agent_briefing(
    agent_id: str = Query("unknown"),
    max_nodes: int = Query(6, ge=1, le=100),
    role: str | None = Query(None),
    compress: bool = Query(True),
    include_error_history: bool = Query(False),
    project_id: str | None = Query(None),
    project_namespace: str | None = Query(None),
):
    """新Agent冷启动友好: 按role过滤相关节点 + 该agent历史 + pending handoffs。

    role参数: brain/frontend/backend/video/3can/ops/data/review. None=brain默认行为。
    compress=True (默认) 只返关键字段, 省token。
    """
    if bool(project_id) != bool(project_namespace):
        raise HTTPException(400, detail={"error": "project_identity_pair_required"})
    for field_name, value in (
        ("project_id", project_id),
        ("project_namespace", project_namespace),
    ):
        if value is not None:
            try:
                validate_routing_context_identifier(value, field_name=field_name)
            except ValueError as exc:
                raise HTTPException(400, detail={"error": str(exc)}) from exc
    owner_defaults = None
    if project_id and project_namespace:
        try:
            candidate = load_owner_intent(
                _default_project_dir(),
                project_id=project_id,
                project_namespace=project_namespace,
            )
        except OwnerIntentError as exc:
            raise HTTPException(
                422,
                detail={"error": "owner_intent_invalid", "reason": str(exc)},
            ) from exc
        if candidate and candidate.get("status") == "applied":
            owner_defaults = {
                **candidate,
                "assertion_origin": "server_local_file",
            }

    rf = _ROLE_FILTERS.get(role or "brain", _ROLE_FILTERS["brain"])
    error_history_enabled = bool(
        include_error_history and (role or "brain") == "review"
    )
    error_candidates = [
        candidate
        for candidate in engine.nodes.values()
        if engine._is_error_case_node(candidate.id, candidate)
        and candidate.status.value == "active"
    ]
    error_candidates.sort(
        key=lambda candidate: (
            candidate.activation_count,
            candidate.updated_at,
        ),
        reverse=True,
    )
    if project_id and project_namespace:
        applicability_order = {
            "exact_project": 0,
            "explicit_shared": 1,
            "unscoped_unknown": 2,
        }
        error_candidates = [
            candidate
            for candidate in error_candidates
            if engine._project_applicability(
                candidate,
                project_id=project_id,
                project_namespace=project_namespace,
            ) != "mismatch"
        ]
        error_candidates.sort(
            key=lambda candidate: applicability_order.get(
                engine._project_applicability(
                    candidate,
                    project_id=project_id,
                    project_namespace=project_namespace,
                ),
                3,
            )
        )
    allowed_error_ids = (
        {node.id for node in error_candidates[:3]}
        if error_history_enabled
        else set()
    )

    def _briefing_node_visible(node: Any) -> bool:
        if project_id and project_namespace and engine._project_applicability(
            node,
            project_id=project_id,
            project_namespace=project_namespace,
        ) == "mismatch":
            return False
        if not engine._is_error_artifact_node(node.id, node):
            return True
        return node.id in allowed_error_ids

    # role-filtered nodes
    role_nodes: list = []
    if rf["prefix_boost"] or rf["cluster_boost"]:
        for n in engine.nodes.values():
            if n.status.value != "active":
                continue
            if not _briefing_node_visible(n):
                continue
            hit = False
            for p in rf["prefix_boost"]:
                if n.id.startswith(p):
                    hit = True
                    break
            if not hit and n.cluster in rf["cluster_boost"]:
                hit = True
            if hit:
                role_nodes.append(n)
        role_nodes.sort(key=lambda n: (n.activation_count, n.updated_at), reverse=True)
        role_nodes = role_nodes[:max_nodes]

    # global hot (only for brain/review or if role filter empty)
    hot_nodes: list = []
    if rf["include_global_hot"] or not role_nodes:
        hot_nodes = sorted(
            [
                n
                for n in engine.nodes.values()
                if not n.id.startswith("INTF-")
                and n.status.value == "active"
                and _briefing_node_visible(n)
            ],
            key=lambda n: (n.activation_count, n.updated_at), reverse=True,
        )[:max_nodes]

    # agent历史活动 + 过往节点
    agent_activity = []
    if not (project_id and project_namespace):
        agent_activity = [
            entry
            for entry in engine.get_activity(agent_id=agent_id, limit=10)
            if error_history_enabled
            or not any(
                engine._reserved_error_knowledge_id(str(node_id))
                for node_id in (entry.affected_nodes or [])
            )
        ][:5]
    agent_nodes = [
        n for n in engine.nodes.values()
        if n.content.extra.get("agent") == agent_id
        and _briefing_node_visible(n)
    ][:3]

    # pending handoffs指向本agent
    pending_handoffs = [
        n for n in engine.nodes.values()
        if n.id.startswith("HO-") and n.status.value == "active"
        and _briefing_node_visible(n)
        and (n.content.extra.get("to_agent") == agent_id or n.content.extra.get("to_agent") == "*")
        and not n.content.extra.get("acknowledged_by", {}).get(agent_id)
    ][:5]

    fmt = _compress_node if compress else (lambda n: n.model_dump())

    return {
        "agent_id": agent_id,
        "role": role or "brain",
        "project_scope": {
            "project_id": project_id,
            "project_namespace": project_namespace,
        },
        "owner_defaults": owner_defaults,
        "error_history_included": error_history_enabled,
        "role_nodes": [fmt(n) for n in role_nodes],
        "hot_nodes": [fmt(n) for n in hot_nodes] if (rf["include_global_hot"] or not role_nodes) else [],
        "agent_history": [e.model_dump() for e in agent_activity],
        "agent_related_nodes": [fmt(n) for n in agent_nodes],
        "pending_handoffs": [fmt(n) for n in pending_handoffs],
        "total_active": sum(
            1 for n in engine.nodes.values()
            if n.status.value == "active" and _briefing_node_visible(n)
        ),
        "total_dormant": sum(
            1 for n in engine.nodes.values()
            if n.status.value == "dormant" and _briefing_node_visible(n)
        ),
    }


@app.get("/api/activity")
async def get_activity(
    agent_id: str | None = Query(None),
    action: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
):
    """查询活动日志。支持按agent_id和action过滤。"""
    entries = engine.get_activity(agent_id=agent_id, action=action, limit=limit)
    return [e.model_dump() for e in entries]


# ── WebSocket ──

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    if not _websocket_origin_allowed(ws.headers.get("origin")):
        await ws.close(code=1008, reason="websocket_origin_not_allowed")
        return
    if not _websocket_token_allowed(ws):
        await ws.close(code=1008, reason="websocket_token_required")
        return
    await manager.connect(ws)
    try:
        while True:
            data = await ws.receive_text()
            # 客户端可以发送 ping 或 route 请求
            try:
                msg = json.loads(data)
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                elif msg.get("type") == "route":
                    req = RoutingRequest(**msg.get("payload", {}))
                    rendered = await route_task(req, detail=False)
                    await ws.send_json({"type": "route_result", "data": rendered})
            except Exception as e:
                await ws.send_json({"type": "error", "message": str(e)})
    except WebSocketDisconnect:
        manager.disconnect(ws)


# ── 启动 ──

if __name__ == "__main__":
    import uvicorn
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9700)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Default 127.0.0.1 (localhost-only, safe). Pass --host 0.0.0.0 "
                             "to expose on LAN — only do this on trusted networks, the API has no auth.")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        print("=" * 72)
        print(f"[3CAN SECURITY WARNING] backend listening on {args.host}:{args.port}")
        print("  This API has NO authentication. Anyone on reachable network can")
        print("  read/write your knowledge graph. Use 127.0.0.1 unless you know why.")
        print("=" * 72, flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
