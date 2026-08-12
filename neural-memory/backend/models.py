"""3CAN data model layer.

Defines nodes, edges, route requests/responses, agent coordination, and runtime
audit objects for a project-local knowledge graph.
"""
from __future__ import annotations

import datetime as dt
import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from owner_intent import (
    OWNER_INTENT_DEFAULT_VALUES,
    OWNER_INTENT_FILENAME,
    OWNER_INTENT_PRECEDENCE,
    OWNER_INTENT_PROJECTION_KEYS,
    OWNER_INTENT_SCHEMA,
)


_WINDOWS_FORBIDDEN_NODE_ID_CHARS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_NODE_ID_BASENAMES = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
_MAX_NODE_ID_LENGTH = 200
_MAX_ROUTING_CONTEXT_ID_LENGTH = 128
_ROUTING_CONTEXT_ID_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:-]{{0,{_MAX_ROUTING_CONTEXT_ID_LENGTH - 1}}}"
)
_SEMANTIC_ID_FAMILY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*")


def validate_routing_context_identifier(
    value: str,
    *,
    field_name: str,
) -> str:
    """Validate an opaque session/route correlation identifier.

    These values are used as in-memory isolation keys and are reflected in
    public route metadata.  Keep them compact, printable, and path-agnostic.
    """

    if not isinstance(value, str):
        raise ValueError(f"{field_name}_must_be_string")
    if not value or value != value.strip():
        raise ValueError(f"{field_name}_must_be_nonempty_and_trimmed")
    if len(value) > _MAX_ROUTING_CONTEXT_ID_LENGTH:
        raise ValueError(f"{field_name}_too_long")
    if not _ROUTING_CONTEXT_ID_RE.fullmatch(value):
        raise ValueError(f"{field_name}_format_invalid")
    return value


def validate_node_identifier(value: str) -> str:
    """Validate the storage key used for ``graph/nodes/<id>.json``.

    Node IDs are graph identifiers, not paths.  Keep this validation in the
    model layer and repeat the containment check in the storage layer so direct
    ``GraphEngine`` callers cannot bypass the API.
    """

    if not isinstance(value, str):
        raise ValueError("node_id_must_be_string")
    if not value or value != value.strip():
        raise ValueError("node_id_must_be_nonempty_and_trimmed")
    if len(value) > _MAX_NODE_ID_LENGTH:
        raise ValueError("node_id_too_long")
    if value in {".", ".."} or value.endswith((".", " ")):
        raise ValueError("node_id_path_segment_forbidden")
    if any(char in _WINDOWS_FORBIDDEN_NODE_ID_CHARS for char in value):
        raise ValueError("node_id_path_character_forbidden")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("node_id_control_character_forbidden")
    if value.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NODE_ID_BASENAMES:
        raise ValueError("node_id_windows_device_name_forbidden")
    return value


def semantic_id_family(node_id: str) -> str:
    """Project semantic role projected from the stable node-ID prefix."""

    match = _SEMANTIC_ID_FAMILY_RE.match(str(node_id or ""))
    return match.group(0).upper() if match else "UNKNOWN"


# ── 枚举 ──

class NodeType(str, Enum):
    knowledge = "knowledge"      # 领域知识
    process = "process"          # 流程/管线
    tool = "tool"                # 工具/技术栈
    config = "config"            # 配置/环境
    reference = "reference"      # 外部引用
    secret = "secret"            # 密钥/凭证(仅存引用名)
    session = "session"          # 会话记录
    decision = "decision"        # 架构决策
    feedback = "feedback"        # 用户反馈/规则
    skill = "skill"              # v9.1: SKILL.md 对应的程序性记忆 (trigger/precondition/success_rate)


class NodeStatus(str, Enum):
    active = "active"
    dormant = "dormant"
    archived = "archived"
    blocked = "blocked"
    deprecated = "deprecated"


class SourceProvenance(str, Enum):
    machine_verifiable = "machine_verifiable"
    user_authoritative = "user_authoritative"
    untrusted_inferred = "untrusted_inferred"


class DurableProvenance(BaseModel):
    """Audit declaration for a durable-current knowledge write.

    The fields are caller provenance claims, not authentication or signature
    proof. The graph owner validates supported machine evidence before a
    protected current write can proceed.
    """

    source_provenance: SourceProvenance = SourceProvenance.untrusted_inferred
    verification_state: str = "unverified"
    evidence_refs: list[str] = Field(default_factory=list)
    authorized_by: str = ""

    @field_validator("evidence_refs")
    @classmethod
    def _validate_evidence_refs(cls, value: list[str]) -> list[str]:
        if len(value) > 20:
            raise ValueError("durable_provenance_evidence_refs_too_many")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("durable_provenance_evidence_ref_invalid")
            clean = item.strip()
            if len(clean) > 500:
                raise ValueError("durable_provenance_evidence_ref_too_long")
            normalized.append(clean)
        return normalized

    def has_required_claim_fields(self) -> bool:
        if self.source_provenance == SourceProvenance.user_authoritative:
            return self.authorized_by.strip().casefold() == "user"
        if self.source_provenance == SourceProvenance.machine_verifiable:
            return (
                self.verification_state.strip().casefold() == "verified"
                and bool(self.evidence_refs)
            )
        return False


class EdgeType(str, Enum):
    depends_on = "depends_on"
    feeds_into = "feeds_into"
    blocks = "blocks"
    informs = "informs"
    requires = "requires"
    updates = "updates"
    validates = "validates"
    triggers = "triggers"
    grouped_in = "grouped_in"
    resolves = "resolves"
    verified_by = "verified_by"
    applies_to = "applies_to"
    supersedes = "supersedes"
    regressed_from = "regressed_from"


class Priority(str, Enum):
    critical = "critical"
    high = "high"
    medium = "medium"
    low = "low"


# ── 节点内容 ──

class NodeContent(BaseModel):
    description: str = ""
    current_state: str = ""
    tech_stack: list[str] = Field(default_factory=list)
    key_files: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    api_refs: list[str] = Field(default_factory=list)    # 密钥变量名，不存明文
    tools: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    last_session: str = ""
    notes: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


# ── 节点 ──

class Node(BaseModel):
    id: str
    name: str
    cluster: str
    layer: str = "L0"                       # L0-L5 对应C³AN层级
    type: NodeType = NodeType.knowledge
    status: NodeStatus = NodeStatus.active
    content: NodeContent = Field(default_factory=NodeContent)
    activation_keywords: list[str] = Field(default_factory=list)
    priority: Priority = Priority.medium
    activation_count: int = 0               # 被路由命中的次数
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    updated_by: str = "system"
    primary_author: str = "system"          # 协议v2.1 L4: 节点主作者, writeback审计用
    contributors: list[str] = Field(default_factory=list)  # 后续修改agent列表

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        return validate_node_identifier(value)


class NodeCreate(BaseModel):
    id: str | None = None                   # 可自动生成
    name: str
    cluster: str
    layer: str = "L0"
    type: NodeType = NodeType.knowledge
    status: NodeStatus = NodeStatus.active
    content: NodeContent = Field(default_factory=NodeContent)
    activation_keywords: list[str] = Field(default_factory=list)
    priority: Priority = Priority.medium
    primary_author: str = "system"

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str | None) -> str | None:
        return validate_node_identifier(value) if value is not None else None


class NodeUpdate(BaseModel):
    name: str | None = None
    cluster: str | None = None
    layer: str | None = None
    type: NodeType | None = None
    status: NodeStatus | None = None
    content: NodeContent | None = None
    activation_keywords: list[str] | None = None
    priority: Priority | None = None
    updated_by: str = "system"
    expected_updated_at: str | None = None


# ── 边 ──

class Edge(BaseModel):
    source: str                             # from node id
    target: str                             # to node id
    type: EdgeType = EdgeType.informs
    weight: float = 1.0
    description: str = ""
    created_at: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())

    @field_validator("source", "target")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        return validate_node_identifier(value)


class EdgeCreate(BaseModel):
    source: str
    target: str
    type: EdgeType = EdgeType.informs
    weight: float = 1.0
    description: str = ""

    @field_validator("source", "target")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        return validate_node_identifier(value)


# ── 路由 ──

class RoutingRequest(BaseModel):
    task: str                               # 任务描述（中文）
    max_nodes: int = Field(default=10, ge=1, le=100)
    include_edges: bool = True
    agent_id: str = "unknown"               # 哪个Agent发起的路由
    session_instance_id: str | None = None   # 并行 session 隔离键；缺失时走 legacy 兼容
    route_id: str | None = None              # 可由客户端绑定；缺失时由引擎生成
    project_id: str | None = Field(
        default=None,
        description="Supply with project_namespace, or omit both.",
    )
    project_namespace: str | None = Field(
        default=None,
        description="Supply with project_id, or omit both.",
    )
    workspace_id: str | None = None           # path-free physical writer binding
    workorder_id: str | None = None            # existing task context; never fabricated
    owner_intent: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Compact project-bound projection of 3CAN.md defaults. A caller "
            "projection is an audit assertion unless the server replaces it "
            "from its local file; never the Markdown body, authentication, or "
            "an objective-truth override."
        ),
    )
    # v9.0 Wave 1 — Entroly CCR 风格两段式
    mode: str = "slim"                      # skeleton / slim / full
    budget_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    # v9.4 基座#31 Confidence Hard Gate
    confirm_low_confidence: bool = False    # low confidence 时 agent 必须显式传 true 才返结果
    allow_degraded: bool = False            # 别名 (更友好命名), 等价于上一条

    @field_validator(
        "session_instance_id",
        "route_id",
        "project_id",
        "project_namespace",
        "workspace_id",
        "workorder_id",
    )
    @classmethod
    def _validate_routing_context_id(
        cls,
        value: str | None,
        info: ValidationInfo,
    ) -> str | None:
        if value is None:
            return None
        return validate_routing_context_identifier(
            value,
            field_name=info.field_name,
        )

    @field_validator("owner_intent")
    @classmethod
    def _validate_owner_intent_projection(
        cls,
        value: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("owner_intent_must_be_object")
        if set(value) != OWNER_INTENT_PROJECTION_KEYS:
            raise ValueError("owner_intent_projection_keys_invalid")
        if value.get("schema") != OWNER_INTENT_SCHEMA:
            raise ValueError("owner_intent_schema_invalid")
        if (
            value.get("status") != "applied"
            or value.get("source") != OWNER_INTENT_FILENAME
        ):
            raise ValueError("owner_intent_projection_not_applied")
        digest = str(value.get("digest") or "")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("owner_intent_digest_invalid")
        project_id = validate_routing_context_identifier(
            value.get("project_id"),
            field_name="owner_intent.project_id",
        )
        namespace = validate_routing_context_identifier(
            value.get("project_namespace"),
            field_name="owner_intent.project_namespace",
        )
        defaults = value.get("defaults")
        if not isinstance(defaults, dict) or set(defaults) != set(
            OWNER_INTENT_DEFAULT_VALUES
        ):
            raise ValueError("owner_intent_defaults_invalid")
        normalized_defaults: dict[str, str] = {}
        for key, allowed in OWNER_INTENT_DEFAULT_VALUES.items():
            item = defaults.get(key)
            if not isinstance(item, str) or item not in allowed:
                raise ValueError(f"owner_intent_default_invalid:{key}")
            normalized_defaults[key] = item
        if (
            value.get("precedence") != OWNER_INTENT_PRECEDENCE
            or value.get("hard_gates_unchanged") is not True
        ):
            raise ValueError("owner_intent_boundary_invalid")
        return {
            **value,
            "project_id": project_id,
            "project_namespace": namespace,
            "defaults": normalized_defaults,
        }

    @model_validator(mode="after")
    def _validate_project_identity_pair(self) -> RoutingRequest:
        if bool(self.project_id) != bool(self.project_namespace):
            raise ValueError("project_identity_pair_required")
        if self.owner_intent:
            if not self.project_id or not self.project_namespace:
                raise ValueError("owner_intent_project_identity_required")
            if (
                str(self.owner_intent["project_id"]).casefold()
                != self.project_id.casefold()
                or str(self.owner_intent["project_namespace"]).casefold()
                != self.project_namespace.casefold()
            ):
                raise ValueError("owner_intent_project_identity_mismatch")
        return self


class RoutingResponse(BaseModel):
    activated_nodes: list[Node]
    relevant_edges: list[Edge]
    scores: dict[str, float]                # node_id → relevance score
    total_nodes: int
    total_edges: int
    route_meta: dict[str, Any] = Field(default_factory=dict)


# ── 图统计 ──

class GraphStats(BaseModel):
    total_nodes: int = 0
    total_edges: int = 0
    active_nodes: int = 0
    blocked_nodes: int = 0
    clusters: dict[str, int] = Field(default_factory=dict)
    node_types: dict[str, int] = Field(default_factory=dict)
    most_connected: list[dict[str, Any]] = Field(default_factory=list)
    last_updated: str = ""


# ── Multi-Agent Coordination ──

class AgentStatus(str, Enum):
    online = "online"
    busy = "busy"
    idle = "idle"
    offline = "offline"


class AgentInfo(BaseModel):
    """注册的Agent信息。"""
    agent_id: str                                # e.g. "opus3", "opus2-video", "codex-thread-abc123"
    name: str = ""                               # 人类可读名称
    role: str = ""                               # e.g. "3CAN引擎开发", "视频AI工程", "战略审计"
    status: AgentStatus = AgentStatus.online
    current_task: str = ""                       # 当前正在做什么
    session_id: str = ""                         # Claude session ID (如果有)
    capabilities: list[str] = Field(default_factory=list)  # ["code","video","strategy"]
    last_checkin: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    checkin_count: int = 0
    total_routes: int = 0                        # 路由查询次数
    total_writebacks: int = 0                    # 回写次数
    meta: dict[str, Any] = Field(default_factory=dict)


class ActivityEntry(BaseModel):
    """活动日志条目。v9.3: 加 hash chain (prev_hash + self_hash) 做 append-only audit trail.
    用途: 开源保护 (证明贡献时间戳不可篡改) + 多 agent 并发审计."""
    timestamp: str = Field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).isoformat())
    agent_id: str = "unknown"
    action: str = ""                             # "route" | "writeback" | "checkin" | "preference" | "node_create" | ...
    detail: str = ""                             # 简短描述
    affected_nodes: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    # v9.3 Hash chain
    prev_hash: str = "0" * 64                    # 前一条 entry 的 self_hash; 链首为 64 个 "0"
    self_hash: str = ""                          # sha256(timestamp + agent_id + action + detail + affected_nodes + meta + prev_hash)
