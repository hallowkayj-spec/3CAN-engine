"""3CAN generic seed nodes.

This script creates a small, non-project-specific graph so a first-time user can
start the engine and verify routing before bootstrapping their own repository.

Run:
  python backend/seed_nodes.py
"""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

def ensure_windows_stdio() -> None:
    if os.name != "nt":
        return
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from graph_engine import GraphEngine  # noqa: E402
from models import (  # noqa: E402
    EdgeCreate,
    EdgeType,
    NodeContent,
    NodeCreate,
    NodeType,
    Priority,
)


SEED_NODES = [
    NodeCreate(
        id="DOC-3can-quickstart",
        name="3CAN Quickstart",
        cluster="project-memory",
        layer="L0",
        type=NodeType.knowledge,
        priority=Priority.high,
        content=NodeContent(
            description="Minimal startup guidance for a new 3CAN graph.",
            current_state="Use this node to verify route/read/writeback before importing a real project.",
            tech_stack=["FastAPI", "JSON graph", "agent route"],
            notes="After installation, run project_bootstrapper.py or auto_bootstrap.py against your own repository.",
        ),
        activation_keywords=[
            "quickstart",
            "startup",
            "bootstrap",
            "first run",
            "new project",
            "verify installation",
            "fresh installation",
            "3can",
        ],
    ),
    NodeCreate(
        id="INTF-3can-route-api",
        name="Route API",
        cluster="api-contract",
        layer="L1",
        type=NodeType.tool,
        priority=Priority.high,
        content=NodeContent(
            description="POST /api/route returns relevant graph nodes for an agent task.",
            current_state="Stable core API.",
            api_refs=["POST /api/route", "GET /api/nodes/{node_id}", "POST /api/activity/log"],
            notes="Agents should route before reading long docs and should write back durable decisions only.",
        ),
        activation_keywords=["route", "api", "read_node", "writeback", "activity", "agent"],
    ),
    NodeCreate(
        id="ERR-3can-no-secret-values",
        name="Do Not Store Secret Values",
        cluster="security",
        layer="L0",
        type=NodeType.feedback,
        priority=Priority.critical,
        content=NodeContent(
            description="3CAN may store secret references, but never raw tokens, cookies, passwords, or recovery codes.",
            current_state="Permanent safety rule.",
            notes="Use environment variables or a local secret manager. Graph nodes can mention variable names such as API_KEY_REF, not values.",
        ),
        activation_keywords=["secret", "token", "password", "credential", "security", "environment variable"],
    ),
    NodeCreate(
        id="SEC-3can-secret-reference-policy",
        name="Secret Reference Policy",
        cluster="security",
        layer="L0",
        type=NodeType.secret,
        priority=Priority.critical,
        content=NodeContent(
            description="Store credential references and environment-variable names, never secret values.",
            current_state="General project policy; ErrorKnowledge remains reserved for explicit failures.",
            notes="Keep raw tokens, cookies, passwords, recovery codes, and private keys outside the graph.",
        ),
        activation_keywords=[
            "secret reference policy",
            "environment variable reference",
            "credential reference",
            "secret values",
            "password",
            "cookie",
            "api key",
        ],
    ),
    NodeCreate(
        id="DEC-3can-project-graph-isolation",
        name="Project Graph Isolation",
        cluster="project-memory",
        layer="L0",
        type=NodeType.decision,
        priority=Priority.critical,
        content=NodeContent(
            description="Each project must bind 3CAN to its own graph directory, port, and runtime state.",
            current_state="Required for running several projects side by side.",
            key_files=["graph/nodes/*.json", "graph/edges.json", "graph/token_usage.sqlite3"],
            notes="Do not reuse another project's graph, token database, runtime logs, or local absolute paths.",
        ),
        activation_keywords=["isolation", "project graph", "port", "graph dir", "new project", "sidecar"],
    ),
    NodeCreate(
        id="PROC-3can-session-bootstrap",
        name="Session Bootstrap",
        cluster="agent-runtime",
        layer="L0",
        type=NodeType.process,
        priority=Priority.high,
        content=NodeContent(
            description="Agents start by checking health, registering themselves, and routing the current task.",
            current_state="Use bootstrap/start/route wrappers before reading long history.",
            api_refs=["GET /api/stats", "POST /api/agents/checkin", "POST /api/route"],
            notes="A responding HTTP port is not enough; verify the expected graph and node count.",
        ),
        activation_keywords=[
            "bootstrap",
            "session start",
            "start agent session",
            "verify graph",
            "checkin",
            "doctor",
            "healthy",
            "agent",
        ],
    ),
    NodeCreate(
        id="PROC-3can-standing-orders",
        name="Standing Orders",
        cluster="agent-runtime",
        layer="L0",
        type=NodeType.process,
        priority=Priority.high,
        content=NodeContent(
            description="Preflight discipline for coding agents before edits, failures, interface changes, and handoff.",
            current_state="Route before edits, check ERR before repeated failures, check INTF before contracts, write back durable outcomes.",
            tools=["scripts/3can_standing_orders.py", "scripts/3can_task_ledger.py"],
            notes="High-risk actions need explicit approval gates.",
        ),
        activation_keywords=[
            "standing orders",
            "approval gate",
            "high risk actions",
            "route before change",
            "preflight before editing",
            "loop detection",
            "task ledger",
            "preflight",
            "harness",
        ],
    ),
    NodeCreate(
        id="PROC-3can-project-bootstrap",
        name="Project Bootstrapper",
        cluster="project-memory",
        layer="L1",
        type=NodeType.process,
        priority=Priority.high,
        content=NodeContent(
            description="Scan a target repository and create a small set of project-specific seed nodes.",
            current_state="Run dry-run first, then apply after reviewing generated nodes.",
            key_files=["tools/project_bootstrapper.py", "scripts/init-project.ps1", "scripts/init-project.sh"],
            notes="Use THREECAN_BASE_URL and THREECAN_PROJECT_DIR so the same release package can bind to any project.",
        ),
        activation_keywords=[
            "project bootstrap",
            "bootstrap new repository",
            "seed project",
            "scan repo",
            "dry run",
            "reviewed dry run",
            "apply seeds",
        ],
    ),
    NodeCreate(
        id="INTF-3can-writeback-api",
        name="Writeback API",
        cluster="api-contract",
        layer="L1",
        type=NodeType.tool,
        priority=Priority.high,
        content=NodeContent(
            description="APIs used by agents to record durable decisions, task activity, errors, and handoffs.",
            current_state="Core writeback contract.",
            api_refs=["POST /api/activity/log", "POST /api/session-writeback", "POST /api/nodes?force=true"],
            notes="Write back verified outcomes, not noisy intermediate reasoning.",
        ),
        activation_keywords=[
            "writeback",
            "activity writeback api",
            "record durable verified work",
            "verified outcome",
            "decision",
            "error node",
            "handoff",
            "audit",
        ],
    ),
    NodeCreate(
        id="INTF-3can-token-usage-api",
        name="Token Usage API",
        cluster="api-contract",
        layer="L1",
        type=NodeType.tool,
        priority=Priority.medium,
        content=NodeContent(
            description="Token ledger and dashboard endpoints for runtime status imports, session rollups, and 3CAN impact estimates.",
            current_state="Project-local ledger; runtime_status imports are treated as high-trust telemetry when available.",
            api_refs=[
                "GET /api/token-usage/summary",
                "GET /api/token-usage/overview",
                "GET /api/token-usage/impact",
                "POST /api/token-usage/import/codex-status",
            ],
            notes="Fresh input = input_tokens - cached_tokens. Cached input is displayed separately.",
        ),
        activation_keywords=[
            "token usage",
            "token dashboard",
            "project local token ledger",
            "audit token telemetry",
            "runtime status",
            "cache",
            "codex status",
        ],
    ),
    NodeCreate(
        id="INTF-3can-admin-health",
        name="Admin Health",
        cluster="api-contract",
        layer="L1",
        type=NodeType.tool,
        priority=Priority.high,
        content=NodeContent(
            description="Health surfaces for graph, agents, skills, writeback, task ledger, approval gate, and loop detection.",
            current_state="Use health checks before trusting a project-local engine.",
            api_refs=["GET /api/stats", "GET /api/agents", "GET /api/token-usage/health"],
            notes="New projects start with a small seed graph, so the expected min-node threshold should be project specific.",
        ),
        activation_keywords=[
            "health",
            "stats",
            "gateway",
            "doctor",
            "node count",
            "project specific node threshold",
            "http response not enough",
            "fresh graph health",
            "verify health",
            "admin",
        ],
    ),
    NodeCreate(
        id="PROC-3can-research-gate",
        name="Deep Research Gate",
        cluster="agent-runtime",
        layer="L1",
        type=NodeType.process,
        priority=Priority.medium,
        content=NodeContent(
            description="Mandatory research workflow for latest facts, technology selection, community feedback, and official documentation checks.",
            current_state="Use the 3CAN deep research skill when the task requires sourced external evidence.",
            key_files=[".agents/skills/3can-deep-research/SKILL.md", "scripts/3can_research_harness.py"],
            notes="Record a source ledger before claiming researched conclusions.",
        ),
        activation_keywords=[
            "research",
            "external research",
            "source ledger",
            "official docs",
            "official sources",
            "community",
            "technology selection",
        ],
    ),
    NodeCreate(
        id="ERR-3can-local-path-rebinding",
        name="Local Path Rebinding Required",
        cluster="portability",
        layer="L0",
        type=NodeType.feedback,
        priority=Priority.critical,
        content=NodeContent(
            description="Released 3CAN packages must not preserve maintainer-specific absolute paths.",
            current_state="Use env vars and relative project paths instead.",
            notes="Scan for maintainer-local absolute paths, private project names, runtime databases, and private logs before publishing or copying.",
        ),
        activation_keywords=[
            "absolute path",
            "release path",
            "local paths leaked",
            "rebinding",
            "privacy",
            "portability",
            "released package",
        ],
    ),
    NodeCreate(
        id="SKILL-3can-context-router",
        name="Context Router Skill",
        cluster="skills",
        layer="L1",
        type=NodeType.skill,
        priority=Priority.medium,
        content=NodeContent(
            description="Route task context through a small set of relevant nodes and skills instead of injecting all project history.",
            current_state="Use skeleton/slim route first; expand full nodes only when required.",
            api_refs=["POST /api/route"],
            notes="This is the core mechanism for reducing avoidable input context.",
        ),
        activation_keywords=["context router", "route precision", "briefing", "skill router", "token discipline"],
    ),
    NodeCreate(
        id="ERR-3can-loop-detection",
        name="Loop Detection Stop Condition",
        cluster="agent-runtime",
        layer="L1",
        type=NodeType.feedback,
        priority=Priority.high,
        content=NodeContent(
            description="Repeated identical failures should stop the agent and require diagnosis instead of more blind edits.",
            current_state="Standing-order helper can return stop_and_diagnose for repeated command/file/error hashes.",
            tools=["scripts/3can_standing_orders.py"],
            notes="Use this to preserve engineering time and avoid noisy writeback.",
        ),
        activation_keywords=["loop detection", "stop and diagnose", "repeated failure", "same error", "harness"],
    ),
    NodeCreate(
        id="ERR-20260508-github-pr-local-rest-fallback-required",
        name="GitHub PR Local REST Fallback Required",
        cluster="agent-runtime",
        layer="L1",
        type=NodeType.feedback,
        priority=Priority.high,
        content=NodeContent(
            description=(
                "GitHub PR creation can fail repeatedly when an agent tries a missing gh CLI or a connector "
                "that cannot access a private repository. Use local git push plus the 3CAN PR harness REST fallback."
            ),
            current_state="Active guardrail: after push, run scripts/3can_pr_harness.py create-pr with an approval id.",
            key_files=[
                "scripts/3can_pr_harness.py",
                ".codex/hooks.json",
                "docs/GITHUB_PR_HARNESS.md",
            ],
            notes=(
                "The harness reads GITHUB_TOKEN, GH_TOKEN, or Git Credential Manager/wincred in memory and never prints token values. "
                "Do not stop at a manual PR link unless local REST creation also fails."
            ),
        ),
        activation_keywords=[
            "github",
            "pull request",
            "gh cli",
            "gh missing",
            "connector 404",
            "wincred",
            "local rest",
            "create-pr",
            "pr fallback",
        ],
    ),
]


SEED_EDGES = [
    EdgeCreate(
        source="DOC-3can-quickstart",
        target="INTF-3can-route-api",
        type=EdgeType.depends_on,
        description="quickstart uses route API",
        weight=0.8,
    ),
    EdgeCreate(
        source="INTF-3can-route-api",
        target="ERR-3can-no-secret-values",
        type=EdgeType.informs,
        description="writeback must avoid secret values",
        weight=0.8,
    ),
    EdgeCreate(
        source="INTF-3can-route-api",
        target="SEC-3can-secret-reference-policy",
        type=EdgeType.informs,
        description="ordinary writeback follows the secret-reference policy",
        weight=0.8,
    ),
    EdgeCreate(
        source="DOC-3can-quickstart",
        target="PROC-3can-session-bootstrap",
        type=EdgeType.feeds_into,
        description="quickstart leads into session bootstrap",
        weight=0.9,
    ),
    EdgeCreate(
        source="PROC-3can-session-bootstrap",
        target="PROC-3can-standing-orders",
        type=EdgeType.feeds_into,
        description="bootstrap enables standing orders",
        weight=0.8,
    ),
    EdgeCreate(
        source="PROC-3can-standing-orders",
        target="ERR-3can-loop-detection",
        type=EdgeType.informs,
        description="standing orders enforce loop detection",
        weight=0.8,
    ),
    EdgeCreate(
        source="PROC-3can-standing-orders",
        target="ERR-20260508-github-pr-local-rest-fallback-required",
        type=EdgeType.informs,
        description="standing orders enforce the local PR fallback when normal GitHub tools fail",
        weight=0.8,
    ),
    EdgeCreate(
        source="DEC-3can-project-graph-isolation",
        target="PROC-3can-project-bootstrap",
        type=EdgeType.requires,
        description="project bootstrap must write to the isolated graph",
        weight=0.9,
    ),
    EdgeCreate(
        source="DEC-3can-project-graph-isolation",
        target="ERR-3can-local-path-rebinding",
        type=EdgeType.requires,
        description="portable projects require local path rebinding",
        weight=0.9,
    ),
    EdgeCreate(
        source="INTF-3can-route-api",
        target="SKILL-3can-context-router",
        type=EdgeType.informs,
        description="route API powers context and skill selection",
        weight=0.8,
    ),
    EdgeCreate(
        source="INTF-3can-token-usage-api",
        target="SKILL-3can-context-router",
        type=EdgeType.validates,
        description="token usage shows context-routing impact",
        weight=0.7,
    ),
    EdgeCreate(
        source="PROC-3can-research-gate",
        target="INTF-3can-writeback-api",
        type=EdgeType.feeds_into,
        description="research ledgers become durable writeback",
        weight=0.7,
    ),
]


def edge_exists(engine: GraphEngine, edge: EdgeCreate) -> bool:
    return any(
        existing.source == edge.source
        and existing.target == edge.target
        and existing.type == edge.type
        for existing in engine.edges
    )


def _seed_internal_owner(*node_ids: str) -> str | None:
    """Use the trusted migration lane only for reserved error-knowledge seeds."""

    if any(
        str(node_id or "").casefold().startswith(("err-", "fix-", "evd-"))
        for node_id in node_ids
    ):
        return "error-migration"
    return None


def main() -> int:
    engine = GraphEngine()
    created = 0
    for node in SEED_NODES:
        if engine.get_node(node.id):
            continue
        engine.create_node(
            node,
            internal_owner=_seed_internal_owner(node.id),
        )
        created += 1

    edge_created = 0
    for edge in SEED_EDGES:
        if edge_exists(engine, edge):
            continue
        if engine.create_edge(
            edge,
            internal_owner=_seed_internal_owner(edge.source, edge.target),
        ):
            edge_created += 1

    print(f"Seed complete: {created} nodes, {edge_created} edges")
    return 0


if __name__ == "__main__":
    ensure_windows_stdio()
    raise SystemExit(main())
