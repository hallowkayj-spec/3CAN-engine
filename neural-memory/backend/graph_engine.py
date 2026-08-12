"""Neural Memory — 图引擎核心 v4 (3CAN MVP)。

v4 新增:
- 同步层: memory/目录file watcher，检测变更→自动更新节点
- 节点自动发现: 扫代码仓库tools/→自动生成INTF节点
- session自动回写: diff检测+writeback

v3 保留:
- BGE-M3 (1024d) + hybrid路由(embedding + keyword)
- session_writeback() + learn_preference()

数据存储在 graph/nodes/*.json、graph/edges.json、graph/embeddings.npz
"""
from __future__ import annotations

import ast
import datetime as dt
import hashlib
import io
import json
import logging
import math
import re
import threading
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from error_knowledge import deterministic_fingerprint, is_error_intent
from graph_runtime_lock import GraphRuntimeLease, acquire_graph_runtime_lock
from models import (
    ActivityEntry, AgentInfo, AgentStatus,
    DurableProvenance, Edge, EdgeCreate, GraphStats, Node, NodeContent, NodeCreate,
    NodeStatus, NodeUpdate, RoutingRequest, RoutingResponse,
    semantic_id_family, validate_node_identifier,
)

import os as _os
_default_graph = Path(__file__).resolve().parent.parent / "graph"
GRAPH_DIR = Path(_os.environ.get("THREECAN_GRAPH_DIR") or _default_graph)
NODES_DIR = GRAPH_DIR / "nodes"
EDGES_FILE = GRAPH_DIR / "edges.json"
EMBEDDINGS_FILE = GRAPH_DIR / "embeddings.npz"
EMBEDDINGS_META_FILE = GRAPH_DIR / "embeddings.meta.json"
AGENTS_FILE = GRAPH_DIR / "agents.json"
ACTIVITY_FILE = GRAPH_DIR / "activity_log.json"

MAX_ACTIVITY_LOG = 500  # 只保留最近500条活动
_ROUTE_BUFFER_TTL_SECONDS = 120.0
_ROUTE_BUFFER_MAX_ENTRIES = 2048
_SESSION_WRITEBACK_FIELDS = frozenset(
    {
        "blockers",
        "current_state",
        "description",
        "last_session",
        "notes",
        "status",
        "tech_stack",
    }
)
_DURABLE_CURRENT_FIELDS = frozenset(
    {"blockers", "current_state", "description", "status", "tech_stack"}
)
_AUTHORITY_PROTECTED_FAMILIES = frozenset({"INTF", "PROC", "DEC", "PRJ"})

def _replace_with_windows_retry(source: Path, target: Path) -> None:
    """Retry transient Windows sharing violations, then fail closed."""

    for attempt in range(4):
        try:
            _os.replace(source, target)
            return
        except OSError as exc:
            if getattr(exc, "winerror", None) not in {5, 32, 33} or attempt >= 3:
                raise
            time.sleep(0.05 * (2**attempt))


def _fsync_directory(directory: Path) -> bool:
    """Best-effort directory durability after replace; unsupported on Windows."""

    flags = getattr(_os, "O_RDONLY", 0)
    if hasattr(_os, "O_DIRECTORY"):
        flags |= _os.O_DIRECTORY
    try:
        descriptor = _os.open(str(directory), flags)
    except (AttributeError, OSError):
        return False
    try:
        _os.fsync(descriptor)
        return True
    except OSError:
        return False
    finally:
        try:
            _os.close(descriptor)
        except OSError:
            pass


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Durably replace one JSON file without exposing a partial target."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    temporary = target.with_name(
        f".{target.name}.{_os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open(
            "x",
            encoding="utf-8",
            newline="\n",
        ) as handle:
            handle.write(rendered)
            handle.flush()
            _os.fsync(handle.fileno())
        _replace_with_windows_retry(temporary, target)
        _fsync_directory(target.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


# ── Embedding模型（懒加载，进程级单例） ──
_embed_model = None
_EMBEDDING_DIM = 1024
_EMBEDDING_CACHE_KEYS = frozenset({"ids", "embeddings", "backend_id"})
_EMBEDDING_UNIT_NORM_ATOL = 1e-3
_EMBEDDING_NONZERO_NORM_MIN = 1e-12
_HASHING_BACKEND_ALIASES = frozenset({"hashing", "hash", "local", "fast"})
_BGE_M3_BACKEND_ALIASES = frozenset(
    {"bge", "bge-m3", "bge_m3", "sentence-transformers"}
)
_AUTO_BACKEND_ALIASES = frozenset({"auto", "prefer-bge", "semantic-auto"})
_BGE_M3_MODEL_NAME = "BAAI/bge-m3"
_DEFAULT_BGE_M3_MAX_SEQUENCE_LENGTH = 768
_MIN_BGE_M3_MAX_SEQUENCE_LENGTH = 64
_MAX_BGE_M3_MAX_SEQUENCE_LENGTH = 8192
_CN_RERANKER_MODEL_NAME = "BAAI/bge-reranker-v2-m3"
_cn_reranker_shared_model: Any | None = None
_cn_reranker_shared_lock = threading.Lock()


def _load_cn_reranker_singleflight() -> Any:
    """Load and warm the large cross-encoder once per engine process."""

    global _cn_reranker_shared_model
    cached = _cn_reranker_shared_model
    if cached is not None:
        return cached

    with _cn_reranker_shared_lock:
        cached = _cn_reranker_shared_model
        if cached is not None:
            return cached

        from sentence_transformers import CrossEncoder

        candidate = CrossEncoder(_CN_RERANKER_MODEL_NAME, max_length=512)
        candidate.predict([["3CAN reranker warmup", "memory route warmup"]])
        _cn_reranker_shared_model = candidate
        return candidate


def _decode_embedding_cache_strings(values: np.ndarray, field: str) -> list[str]:
    if values.ndim != 1 or values.dtype.kind not in {"U", "S"}:
        raise ValueError(f"embedding_cache_{field}_must_be_1d_strings")
    decoded: list[str] = []
    for raw in values.tolist():
        value = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
        if not value:
            raise ValueError(f"embedding_cache_{field}_contains_empty_value")
        decoded.append(value)
    return decoded


def _parse_embedding_cache(data: Any) -> tuple[list[str], np.ndarray, str]:
    keys = set(data.files)
    if not {"ids", "embeddings"}.issubset(keys):
        raise ValueError("embedding_cache_missing_required_arrays")
    if keys - _EMBEDDING_CACHE_KEYS:
        raise ValueError("embedding_cache_has_unexpected_arrays")

    ids = _decode_embedding_cache_strings(data["ids"], "ids")
    embeddings = data["embeddings"]
    if embeddings.ndim != 2 or embeddings.dtype.kind != "f":
        raise ValueError("embedding_cache_embeddings_must_be_2d_floats")
    if embeddings.shape[0] != len(ids):
        raise ValueError("embedding_cache_row_count_mismatch")
    if embeddings.shape[1] != _EMBEDDING_DIM:
        raise ValueError("embedding_cache_dimension_mismatch")
    if not np.isfinite(embeddings).all():
        raise ValueError("embedding_cache_embeddings_must_be_finite")

    backend_id = ""
    if "backend_id" in keys:
        backend_values = _decode_embedding_cache_strings(
            data["backend_id"], "backend_id"
        )
        if len(backend_values) != 1:
            raise ValueError("embedding_cache_backend_id_must_have_one_value")
        backend_id = backend_values[0]

    return ids, embeddings.copy(), backend_id


def _read_embedding_cache(path: Path) -> tuple[list[str], np.ndarray, str]:
    """Read a non-executable, schema-checked embedding cache."""
    with np.load(str(path), allow_pickle=False) as data:
        return _parse_embedding_cache(data)


def _read_embedding_cache_payload(
    payload: bytes,
) -> tuple[list[str], np.ndarray, str]:
    """Parse the exact bytes whose digest is reported by deep readiness."""

    with np.load(io.BytesIO(payload), allow_pickle=False) as data:
        return _parse_embedding_cache(data)


def _parse_embedding_cache_meta(payload: Any) -> tuple[str, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "backend_id",
        "source_manifest",
    }:
        raise ValueError("embedding_cache_meta_schema_invalid")
    if payload.get("schema_version") != 1:
        raise ValueError("embedding_cache_meta_version_unsupported")
    backend_id = payload.get("backend_id")
    source_manifest = payload.get("source_manifest")
    if not isinstance(backend_id, str) or not backend_id:
        raise ValueError("embedding_cache_meta_backend_id_invalid")
    if (
        not isinstance(source_manifest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", source_manifest)
    ):
        raise ValueError("embedding_cache_meta_source_manifest_invalid")
    return backend_id, source_manifest


def _read_embedding_cache_meta(path: Path) -> tuple[str, str]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    return _parse_embedding_cache_meta(payload)


def _read_embedding_cache_meta_payload(payload: bytes) -> tuple[str, str]:
    decoded = json.loads(payload.decode("utf-8"))
    return _parse_embedding_cache_meta(decoded)


def _atomic_write_embedding_cache(
    path: Path,
    ids: list[str],
    embeddings: np.ndarray,
    backend_id: str,
) -> None:
    """Durably replace the non-executable cache with backend identity."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    matrix = np.asarray(embeddings)
    if matrix.ndim != 2 or matrix.dtype.kind != "f":
        raise ValueError("embedding_cache_embeddings_must_be_2d_floats")
    if matrix.shape[0] != len(ids):
        raise ValueError("embedding_cache_row_count_mismatch")
    if matrix.shape[1] != _EMBEDDING_DIM:
        raise ValueError("embedding_cache_dimension_mismatch")
    if not np.isfinite(matrix).all():
        raise ValueError("embedding_cache_embeddings_must_be_finite")
    if not backend_id:
        raise ValueError("embedding_cache_backend_id_required")

    temporary = target.with_name(
        f".{target.name}.{_os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            np.savez(
                handle,
                ids=np.asarray(ids, dtype=str),
                embeddings=matrix,
                backend_id=np.asarray([backend_id]),
            )
            handle.flush()
            _os.fsync(handle.fileno())
        _replace_with_windows_retry(temporary, target)
        _fsync_directory(target.parent)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


class _HashingEmbeddingModel:
    """Dependency-light fallback when sentence-transformers is unavailable.

    It preserves 3CAN route/startup availability after reboot or fresh WSL setup.
    BGE-M3 remains the preferred path when installed; this fallback is for
    continuity, not benchmark-grade semantic retrieval.
    """

    backend_id = "hashing-blake2b-char-ngram-v1"

    def __init__(self, n_features: int = _EMBEDDING_DIM) -> None:
        self.n_features = n_features

    def encode(self, texts: list[str], normalize_embeddings: bool = True, show_progress_bar: bool = False) -> np.ndarray:
        matrix = np.zeros((len(texts), self.n_features), dtype=np.float32)
        for row_idx, text in enumerate(texts):
            value = f" {text.lower()} "
            for n in range(2, 6):
                if len(value) < n:
                    continue
                for pos in range(0, len(value) - n + 1):
                    gram = value[pos:pos + n].encode("utf-8", errors="ignore")
                    digest = hashlib.blake2b(gram, digest_size=8).digest()
                    col = int.from_bytes(digest, "little") % self.n_features
                    matrix[row_idx, col] += 1.0
            norm = float(np.linalg.norm(matrix[row_idx]))
            if normalize_embeddings and norm > 0:
                matrix[row_idx] /= norm
        return matrix


def _requested_embedding_backend() -> str:
    raw = (_os.environ.get("THREECAN_EMBEDDING_BACKEND") or "hashing").strip().lower()
    if raw in _HASHING_BACKEND_ALIASES:
        return "hashing"
    if raw in _BGE_M3_BACKEND_ALIASES:
        return "bge-m3"
    if raw in _AUTO_BACKEND_ALIASES:
        return "auto"
    raise ValueError(f"unsupported_embedding_backend:{raw}")


def _embedding_fallback_policy(requested_backend: str | None = None) -> str:
    requested = requested_backend or _requested_embedding_backend()
    if requested == "hashing":
        return "not_applicable"
    if requested == "bge-m3":
        return "error"

    raw = (_os.environ.get("THREECAN_EMBEDDING_FALLBACK") or "hashing").strip().lower()
    if raw in {"hashing", "hash", "local", "continue"}:
        return "hashing"
    if raw in {"error", "fail", "strict", "off", "none"}:
        return "error"
    raise ValueError(f"unsupported_embedding_fallback:{raw}")


def _embedding_batch_size() -> int:
    raw = (_os.environ.get("THREECAN_EMBEDDING_BATCH_SIZE") or "8").strip()
    try:
        configured = int(raw)
    except ValueError:
        raise ValueError(f"unsupported_embedding_batch_size:{raw}") from None
    if configured < 1 or configured > 64:
        raise ValueError(f"unsupported_embedding_batch_size:{raw}")
    return configured


def _embedding_max_sequence_length() -> int:
    raw = (
        _os.environ.get("THREECAN_EMBEDDING_MAX_SEQUENCE_LENGTH")
        or str(_DEFAULT_BGE_M3_MAX_SEQUENCE_LENGTH)
    ).strip()
    try:
        configured = int(raw)
    except ValueError:
        raise ValueError(f"unsupported_embedding_max_sequence_length:{raw}") from None
    if not (
        _MIN_BGE_M3_MAX_SEQUENCE_LENGTH
        <= configured
        <= _MAX_BGE_M3_MAX_SEQUENCE_LENGTH
    ):
        raise ValueError(f"unsupported_embedding_max_sequence_length:{raw}")
    return configured


def _expected_bge_m3_model_revision() -> str:
    revision = (
        _os.environ.get("THREECAN_EMBEDDING_MODEL_REVISION") or ""
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", revision):
        raise RuntimeError("embedding_model_revision_pin_missing:bge-m3")
    return revision


def _local_bge_m3_model_path() -> str:
    """Resolve BGE-M3 from the local HF cache without network access.

    sentence-transformers 2.7 does not accept ``local_files_only`` in its
    constructor. Resolving the snapshot first keeps the constructor compatible
    while preserving the cache-only startup contract on every supported
    sentence-transformers version.
    """

    expected_revision = _expected_bge_m3_model_revision()
    configured = (
        _os.environ.get("THREECAN_EMBEDDING_MODEL_PATH") or ""
    ).strip()
    if configured:
        try:
            snapshot = Path(configured).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise RuntimeError(
                "embedding_model_snapshot_unavailable:bge-m3"
            ) from exc
        if not snapshot.is_dir() or snapshot.name.casefold() != expected_revision:
            raise RuntimeError("embedding_model_snapshot_mismatch:bge-m3")
        return str(snapshot)

    from huggingface_hub import snapshot_download

    resolved = str(
        snapshot_download(
            repo_id=_BGE_M3_MODEL_NAME,
            revision=expected_revision,
            local_files_only=True,
        )
        or ""
    ).strip()
    if not resolved:
        raise RuntimeError("embedding_model_snapshot_unavailable:bge-m3")
    return resolved


def _bge_m3_first_module(model: Any) -> Any:
    accessor = getattr(model, "_first_module", None)
    if not callable(accessor):
        raise RuntimeError("embedding_model_first_module_unavailable:bge-m3")
    first_module = accessor()
    if first_module is None:
        raise RuntimeError("embedding_model_first_module_unavailable:bge-m3")
    return first_module


def _configure_bge_m3_max_sequence_length(
    model: Any,
    configured: int,
) -> int:
    """Set and verify the tokenizer-facing first-module sequence limit."""

    first_module = _bge_m3_first_module(model)
    if not hasattr(first_module, "max_seq_length"):
        raise RuntimeError(
            "embedding_model_max_sequence_length_unavailable:bge-m3"
        )
    try:
        first_module.max_seq_length = configured
        effective = int(first_module.max_seq_length)
    except Exception as exc:
        raise RuntimeError(
            "embedding_model_max_sequence_length_unavailable:bge-m3"
        ) from exc
    if effective != configured:
        raise RuntimeError(
            "embedding_model_max_sequence_length_mismatch:bge-m3"
        )

    # sentence-transformers >=3 exposes a forwarding public property; 2.7
    # does not. When present, require it to agree with the tokenizer module.
    try:
        public_effective = int(getattr(model, "max_seq_length", effective))
    except Exception as exc:
        raise RuntimeError(
            "embedding_model_max_sequence_length_unavailable:bge-m3"
        ) from exc
    if public_effective != effective:
        raise RuntimeError(
            "embedding_model_max_sequence_length_mismatch:bge-m3"
        )
    return effective


def _bge_m3_model_revision(model: Any) -> str:
    first_module = _bge_m3_first_module(model)
    auto_model = getattr(first_module, "auto_model", None)
    config = getattr(auto_model, "config", None)
    revision = str(getattr(config, "_commit_hash", "") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{7,64}", revision):
        raise RuntimeError("embedding_model_revision_unavailable:bge-m3")
    return revision


def _bge_m3_backend_id(model_revision: str, max_sequence_length: int) -> str:
    return (
        f"sentence-transformers:{_BGE_M3_MODEL_NAME}@{model_revision}"
        f":maxseq={max_sequence_length}"
    )


def _bge_m3_failure_reason_code(exc: Exception) -> str:
    """Return a stable public code while keeping raw paths in private logs."""

    stable = str(exc).strip()
    if re.fullmatch(r"embedding_model_[a-z0-9_]+:bge-m3", stable):
        return stable
    return f"bge_model_load_failed:{type(exc).__name__}"


def _annotate_embedding_model(
    model: Any,
    *,
    requested_backend: str,
    fallback_policy: str,
    degraded: bool,
    reason_code: str = "",
    batch_size: int = 8,
    max_sequence_length: int = _DEFAULT_BGE_M3_MAX_SEQUENCE_LENGTH,
    model_revision: str = "",
    attempted_model_revision: str = "",
) -> Any:
    setattr(model, "_3can_requested_backend", requested_backend)
    setattr(model, "_3can_fallback_policy", fallback_policy)
    setattr(model, "_3can_degraded", degraded)
    setattr(model, "_3can_reason_code", reason_code[:120])
    setattr(model, "_3can_batch_size", batch_size)
    setattr(model, "_3can_max_sequence_length", max_sequence_length)
    setattr(model, "_3can_model_revision", model_revision)
    setattr(model, "_3can_attempted_model_revision", attempted_model_revision)
    return model


def _get_model():
    global _embed_model
    if _embed_model is None:
        requested_backend = _requested_embedding_backend()
        fallback_policy = _embedding_fallback_policy(requested_backend)
        batch_size = _embedding_batch_size()
        max_sequence_length = _embedding_max_sequence_length()
        if requested_backend == "hashing":
            _embed_model = _annotate_embedding_model(
                _HashingEmbeddingModel(),
                requested_backend=requested_backend,
                fallback_policy=fallback_policy,
                degraded=False,
                batch_size=batch_size,
                max_sequence_length=max_sequence_length,
                model_revision="algorithm-v1",
            )
            print(f"[NeuralMemory] Embedding model: {_embed_model.backend_id}")
            return _embed_model
        try:
            from sentence_transformers import SentenceTransformer

            bge_model = SentenceTransformer(_local_bge_m3_model_path())
            effective_max_sequence_length = (
                _configure_bge_m3_max_sequence_length(
                    bge_model,
                    max_sequence_length,
                )
            )
            model_revision = _bge_m3_model_revision(bge_model)
            if model_revision != _expected_bge_m3_model_revision():
                raise RuntimeError("embedding_model_revision_mismatch:bge-m3")
            _embed_model = _annotate_embedding_model(
                bge_model,
                requested_backend=requested_backend,
                fallback_policy=fallback_policy,
                degraded=False,
                batch_size=batch_size,
                max_sequence_length=effective_max_sequence_length,
                model_revision=model_revision,
            )
            setattr(
                _embed_model,
                "_3can_backend_id",
                _bge_m3_backend_id(
                    model_revision,
                    effective_max_sequence_length,
                ),
            )
            print(
                "[NeuralMemory] Embedding model: BAAI/bge-m3 "
                f"({model_revision[:12]}, "
                f"max_seq={effective_max_sequence_length})"
            )
        except Exception as exc:
            if fallback_policy == "error":
                raise RuntimeError("embedding_backend_load_failed:bge-m3") from exc
            print(
                f"[WARN] sentence-transformers unavailable ({exc}); "
                "using local hashing embeddings"
            )
            _embed_model = _annotate_embedding_model(
                _HashingEmbeddingModel(),
                requested_backend=requested_backend,
                fallback_policy=fallback_policy,
                degraded=True,
                reason_code=_bge_m3_failure_reason_code(exc),
                batch_size=batch_size,
                max_sequence_length=max_sequence_length,
                model_revision="algorithm-v1",
            )
    return _embed_model


def _encode(
    texts: list[str],
    *,
    allow_auto_fallback: bool = False,
) -> np.ndarray:
    """编码文本列表为embedding矩阵。"""
    global _embed_model
    model = _get_model()
    try:
        encode_options: dict[str, Any] = {
            "normalize_embeddings": True,
            "show_progress_bar": False,
        }
        if not isinstance(model, _HashingEmbeddingModel):
            encode_options["batch_size"] = int(
                getattr(model, "_3can_batch_size", 8)
            )
        return model.encode(texts, **encode_options)
    except Exception as exc:
        if (
            getattr(model, "_3can_requested_backend", "") == "auto"
            and getattr(model, "_3can_fallback_policy", "") == "hashing"
            and not isinstance(model, _HashingEmbeddingModel)
            and allow_auto_fallback
        ):
            print(
                f"[WARN] BGE-M3 encode failed ({exc}); "
                "using local hashing embeddings"
            )
            attempted_model_revision = str(
                getattr(model, "_3can_model_revision", "")
            )
            _embed_model = _annotate_embedding_model(
                _HashingEmbeddingModel(),
                requested_backend="auto",
                fallback_policy="hashing",
                degraded=True,
                reason_code=f"bge_encode_failed:{type(exc).__name__}",
                batch_size=int(getattr(model, "_3can_batch_size", 8)),
                max_sequence_length=int(
                    getattr(
                        model,
                        "_3can_max_sequence_length",
                        _DEFAULT_BGE_M3_MAX_SEQUENCE_LENGTH,
                    )
                ),
                model_revision="algorithm-v1",
                attempted_model_revision=attempted_model_revision,
            )
            return _embed_model.encode(
                texts,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
        setattr(model, "_3can_degraded", True)
        setattr(model, "_3can_reason_code", f"bge_encode_failed:{type(exc).__name__}")
        raise RuntimeError("embedding_encode_failed:bge-m3") from exc


class GraphEngine:
    """图引擎 v2 — embedding路由 + 自动回写。"""

    _ERROR_KNOWLEDGE_INTERNAL_OWNERS = frozenset(
        {"error-ledger", "error-migration"}
    )
    _DURABLE_CURRENT_INTERNAL_OWNERS = frozenset(
        {"durable-seed"}
    )
    _GRAPH_TRAVERSAL_ANCHOR_LIMIT = 10
    _RERANKER_POOL_LIMIT = 15
    _RERANKER_DEFAULT_CAP_BY_MODE = {
        "skeleton": 8,
        "slim": 10,
        "full": 15,
    }
    _EDGE_ROUTE_BASE = 0.003
    _EDGE_ROUTE_PER_EDGE_CAP = 0.012
    _EDGE_ROUTE_PER_NODE_CAP = 0.018
    _EDGE_ROUTE_RANK_EPSILON = 0.000001
    _EDGE_ROUTE_TYPE_FACTOR = {
        "requires": 3.5,
        "depends_on": 3.0,
        "validates": 2.6,
        "blocks": 2.3,
        "triggers": 2.1,
        "feeds_into": 1.6,
        "updates": 1.1,
        "informs": 0.35,
    }
    _EDGE_ROUTE_REVERSE_FACTOR = {
        "requires": 0.35,
        "depends_on": 0.40,
        "validates": 0.50,
        "blocks": 0.30,
        "triggers": 0.35,
        "feeds_into": 0.50,
        "updates": 0.80,
        "informs": 1.00,
    }
    _TEMPORAL_ROUTE_TRIGGER = re.compile(
        r"\blatest\b|\brecent\b|\bcurrent\b|\btoday\b|\byesterday\b|\bnow\b|\bcontinue\b|"
        r"\bfresh\b|\bfreshness\b|\btemporal\b|\btime\b|\bstale\b|\bexpired\b|\bexpiry\b|"
        r"\bvalidity\b|\bvalid_until\b|\bvalid_from\b|\bttl\b|"
        r"\u6700\u65b0|\u6700\u8fd1|\u5f53\u524d|\u73b0\u5728|\u4eca\u5929|\u6628\u5929|"
        r"\u672c\u5468|\u8fd1\u671f|\u7ee7\u7eed|\u73b0\u72b6|\u65f6\u95f4|\u65f6\u5e8f|"
        r"\u65f6\u6548|\u8fc7\u671f|\u5931\u6548|\u6709\u6548\u671f",
        re.IGNORECASE,
    )
    _TEMPORAL_FRESHNESS_TRIGGER = re.compile(
        r"\blatest\b|\brecent\b|\bcurrent\b|\btoday\b|\bnow\b|\bcontinue\b|\bfresh\b|"
        r"\u6700\u65b0|\u6700\u8fd1|\u5f53\u524d|\u73b0\u5728|\u4eca\u5929|\u8fd1\u671f|"
        r"\u7ee7\u7eed|\u73b0\u72b6",
        re.IGNORECASE,
    )
    _TEMPORAL_VALIDITY_TRIGGER = re.compile(
        r"\bstale\b|\bexpired\b|\bexpiry\b|\bvalidity\b|\bvalid_until\b|\bvalid_from\b|\bttl\b|"
        r"\u65f6\u6548|\u8fc7\u671f|\u5931\u6548|\u6709\u6548\u671f",
        re.IGNORECASE,
    )
    _TEMPORAL_ERROR_TRIGGER = re.compile(
        r"\berr\b|\berror\b|\bfail(?:ed|ure)?\b|\bbug\b|\bincident\b|\bregression\b|\bticket\b|"
        r"\bstale\b|\bexpired\b|\bexpiry\b|\bttl\b|"
        r"\u9519\u8bef|\u5931\u8d25|\u6545\u969c|\u8fc7\u671f|\u5931\u6548",
        re.IGNORECASE,
    )
    _OPERATIONAL_ERROR_STRONG_TRIGGER = re.compile(
        r"\berr-[a-z0-9]"
        r"|\b(?:api|route|endpoint|request|response|http)\s*"
        r"(?:returns?|returned|status(?:\s+code)?|code)?\s*[:=]?\s*5\d\d\b"
        r"|\bstuck(?:\s+(?:at|in|on))?\b"
        r"|\b(?:can(?:not|'t)|unable\s+to)\s+"
        r"(?:start|connect|load|run|route|retrieve|recall|import|build|install|"
        r"resolve|reach|access|execute|open|read|write|save|deploy|push|pull|"
        r"authenticate)\b|\bunreachable\b|\bunavailable\b"
        r"|\brecall\s+(?:miss|failure)\b"
        r"|\b(?:dependency|module|package)\s+(?:is\s+)?missing\b"
        r"|\bmissing\s+(?:dependency|module|package)\b"
        r"|召回不到|无法召回|未命中(?:目标|节点|结果)"
        r"|卡在|卡住|不可达|无法(?:启动|连接|访问|加载|运行|路由)"
        r"|依赖(?:缺失|丢失)|缺少.{0,32}(?:依赖|模块|组件|包)",
        re.IGNORECASE,
    )
    _OPERATIONAL_ERROR_REMEDY_TRIGGER = re.compile(
        r"\b(?:fix|repair|recover|restore|troubleshoot|retry|resolve)\b"
        r"|\broot\s+cause\b|修复|恢复|排查|根因|重试|纠偏|解决",
        re.IGNORECASE,
    )
    _OPERATIONAL_ERROR_CORRELATED_SYMPTOM = re.compile(
        r"\bmismatch\b|\bkeeps?\s+rebuild(?:ing)?\b|\brebuilds?\s+every\b"
        r"|\bdrift\b|\bdisconnect(?:ed|ion)?\b|\binterrupted?\b"
        r"|不一致|反复|重复|漂移|断流|中断"
        r"|每次.{0,32}(?:重建|失效|中断)",
        re.IGNORECASE,
    )
    _TEMPORAL_DEFAULT_HALF_LIFE_DAYS = 45.0
    _TEMPORAL_FRESH_HALF_LIFE_DAYS = 14.0
    _TEMPORAL_ROUTE_MAX_BOOST = 0.006
    _CANONICAL_ERROR_CASE_CAP_BOOST = 0.002
    _TEMPORAL_ROUTE_MAX_PENALTY = 0.006
    _CURRENT_REALITY_TRIGGER = re.compile(
        r"\bcurrent\b|\bcanonical\b|\bowner\b|\bactive now\b|\bverified capability\b|"
        r"\bsource[- ]of[- ]truth\b|\bcurrent path\b|\bcurrent contract\b|"
        r"\bstill\b|"
        r"\bhow (?:do|should) (?:we|i) (?:call|use)\b|"
        r"当前|目前|现行|现在|是否仍|规范路径|权威来源|真相来源|业务真相|"
        r"当前负责人|当前契约|已验证能力|"
        r"怎么(?:调用|使用)|如何(?:调用|使用)",
        re.IGNORECASE,
    )
    _HISTORY_ROUTE_TRIGGER = re.compile(
        r"\bhistory\b|\bhistorical\b|\bprevious(?:ly)?\b|\bformerly\b|"
        r"\bhandoff\b|\bcontinuation\b|\barchaeology\b|"
        r"历史|以前|过去|曾经|交接|续接|考古|旧契约|由什么替代",
        re.IGNORECASE,
    )
    _DURABLE_EVIDENCE_TRIGGER = re.compile(
        r"\bevidence\b|\bsource\b|\bsource pointer\b|\bboundary\b|"
        r"\bknown[- ]good\b|\bverified (?:path|recovery|outcome)\b|"
        r"证据|来源|源文件|边界|已验证(?:路径|恢复|结果)|成熟链路",
        re.IGNORECASE,
    )
    _EXTERNAL_TRUTH_TRIGGER = re.compile(
        r"\bbranch\b|\bworktree\b|\bpull request\b|\bPR\b|\bCI\b|\bworkorder\b|"
        r"\bagent\b|分支|工作树|拉取请求|当前任务|当前代理",
        re.IGNORECASE,
    )
    _CURRENT_SEDIMENT_FAMILIES = frozenset({"SES", "HO"})
    _CURRENT_DURABLE_FAMILIES = frozenset(
        {"INTF", "PROC", "DEC", "PRJ", "DOC", "ENV", "MEM", "MOD", "PRO", "RUL", "SEC"}
    )
    _CURRENT_DURABLE_BOOST = 0.004
    _CURRENT_SEDIMENT_PENALTY = 0.008
    _CURRENT_UNPROVEN_CORE_PENALTY = 0.010
    _PROJECT_SCOPED_CORE_LANES = frozenset(
        {"environment_constraints", "project_constitution", "project_file_system"}
    )

    def __init__(self) -> None:
        self._graph_runtime_lease: GraphRuntimeLease | None = (
            acquire_graph_runtime_lock(GRAPH_DIR, owner_kind="3can-engine")
        )
        self.nodes: dict[str, Node] = {}
        self.edges: list[Edge] = []
        self.agents: dict[str, AgentInfo] = {}      # agent_id → AgentInfo
        self.activity_log: list[ActivityEntry] = []  # 活动日志 (最近500条)
        self._node_embeddings: dict[str, np.ndarray] = {}  # node_id → 384-dim
        self._node_id_order: list[str] = []  # 保持顺序对齐矩阵
        self._emb_matrix: np.ndarray | None = None
        self._code_index: dict[str, list[str]] = {}  # v8 Layer1: 短代码→节点ID反向索引
        self._kw_df: dict[str, int] = {}             # v9.0 Wave 2: kw document frequency (小写 kw → # 节点)
        self._kw_N: int = 0                          # v9.0 Wave 2: 总活跃节点数 (IDF 分子)
        self._click_log: dict[str, dict[str, int]] = {}  # v8 Layer2: query→{node_id: signal}
        self._route_buffer: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._route_buffer_lock = threading.Lock()
        self._pending_keywords: dict[str, dict[str, int]] = {}  # v8.3 Miss Healer: node_id → {token: confirm_count}
        self._PENDING_KW_FILE = GRAPH_DIR / "pending_keywords.json"
        self._embedding_cache_state = "uninitialized"
        self._embedding_cache_backend_id = ""
        self._embedding_cache_source_manifest = ""
        self._ensure_dirs()
        self._load()
        self._load_or_build_embeddings()
        self._build_code_index()
        self._build_kw_df()
        self._load_click_log()
        self._load_pending_keywords()
        self._cn_reranker_loading = False
        self._cn_reranker_warmup_started = False
        self._cn_reranker_warmup_error_code = ""
        self._cn_reranker_warmup_thread: threading.Thread | None = None
        self._start_reranker_warmup()

    def close(self) -> None:
        """Release exclusive graph ownership during a graceful shutdown."""

        warmup_thread = getattr(self, "_cn_reranker_warmup_thread", None)
        if (
            warmup_thread is not None
            and warmup_thread.is_alive()
            and warmup_thread is not threading.current_thread()
        ):
            raw_wait = (
                _os.environ.get("THREECAN_RERANKER_SHUTDOWN_WAIT_SECONDS")
                or "5"
            ).strip()
            try:
                wait_seconds = max(0.0, min(float(raw_wait), 60.0))
            except ValueError:
                wait_seconds = 5.0
            warmup_thread.join(timeout=wait_seconds)
            if warmup_thread.is_alive():
                logging.getLogger("3can").warning(
                    "reranker warmup still running after %.1fs shutdown wait",
                    wait_seconds,
                )

        lease = getattr(self, "_graph_runtime_lease", None)
        if lease is not None:
            lease.release()
            self._graph_runtime_lease = None

    # ── 初始化 ──

    def _ensure_dirs(self) -> None:
        NODES_DIR.mkdir(parents=True, exist_ok=True)
        if not EDGES_FILE.exists():
            _atomic_write_json(EDGES_FILE, [])

    def _load(self) -> None:
        self.nodes.clear()
        self.edges.clear()
        self.agents.clear()
        self.activity_log.clear()
        loaded_casefold_ids: dict[str, str] = {}
        for f in NODES_DIR.glob("*.json"):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                node = Node(**data)
                if f.stem != node.id:
                    raise ValueError(
                        "node_payload_id_does_not_match_filename:"
                        f"{f.stem!r}!={node.id!r}"
                    )
                expected_path = self._node_path(node.id)
                if expected_path != f.resolve():
                    raise ValueError("node_path_outside_storage_root")
                folded = node.id.casefold()
                prior = loaded_casefold_ids.get(folded)
                if prior is not None and prior != node.id:
                    raise ValueError(
                        f"node_id_case_collision:{prior!r}:{node.id!r}"
                    )
                loaded_casefold_ids[folded] = node.id
                self.nodes[node.id] = node
            except Exception as e:
                print(f"[WARN] 加载节点 {f.name} 失败: {e}")
        if EDGES_FILE.exists():
            try:
                raw = json.loads(EDGES_FILE.read_text(encoding="utf-8"))
                self.edges = [Edge(**e) for e in raw]
            except Exception as e:
                print(f"[WARN] 加载边失败: {e}")
        # 加载Agent注册表
        if AGENTS_FILE.exists():
            try:
                raw = json.loads(AGENTS_FILE.read_text(encoding="utf-8"))
                for a in raw:
                    agent = AgentInfo(**a)
                    self.agents[agent.agent_id] = agent
                print(f"[3CAN] Loaded {len(self.agents)} registered agents")
            except Exception as e:
                print(f"[WARN] 加载Agent注册表失败: {e}")
        # 加载活动日志
        if ACTIVITY_FILE.exists():
            try:
                raw = json.loads(ACTIVITY_FILE.read_text(encoding="utf-8"))
                self.activity_log = [ActivityEntry(**e) for e in raw[-MAX_ACTIVITY_LOG:]]
            except Exception as e:
                print(f"[WARN] 加载活动日志失败: {e}")

    def _load_or_build_embeddings(self) -> None:
        """加载或构建节点embedding。"""
        self._node_id_order = sorted(self.nodes.keys())
        expected_backend = self._embedding_backend_id()
        expected_source_manifest = self._embedding_source_manifest()

        if EMBEDDINGS_FILE.exists():
            try:
                saved_ids, saved_embs, saved_backend = _read_embedding_cache(
                    EMBEDDINGS_FILE
                )
                meta_backend, saved_source_manifest = _read_embedding_cache_meta(
                    EMBEDDINGS_META_FILE
                )
                # 检查是否和当前节点一致
                if (
                    saved_ids == self._node_id_order
                    and len(saved_embs) == len(self._node_id_order)
                    and saved_backend == expected_backend
                    and meta_backend == saved_backend
                    and saved_source_manifest == expected_source_manifest
                ):
                    self._emb_matrix = saved_embs
                    for i, nid in enumerate(self._node_id_order):
                        self._node_embeddings[nid] = saved_embs[i]
                    self._embedding_cache_state = "loaded"
                    self._embedding_cache_backend_id = saved_backend
                    self._embedding_cache_source_manifest = saved_source_manifest
                    print(f"[NeuralMemory] Loaded {len(saved_ids)} cached embeddings ({saved_backend})")
                    return
                if saved_ids != self._node_id_order:
                    self._embedding_cache_state = "node_set_mismatch"
                elif saved_backend != expected_backend or meta_backend != saved_backend:
                    self._embedding_cache_state = "backend_mismatch"
                    print(f"[NeuralMemory] Embedding cache backend mismatch: {saved_backend or 'legacy'} -> {expected_backend}")
                elif saved_source_manifest != expected_source_manifest:
                    self._embedding_cache_state = "source_mismatch"
                else:
                    self._embedding_cache_state = "invalid"
            except Exception as e:
                self._embedding_cache_state = "invalid"
                print(f"[WARN] Embedding缓存加载失败: {e}")
        else:
            self._embedding_cache_state = "missing"

        # 需要重建
        self._rebuild_embeddings()

    def _rebuild_embeddings(self) -> None:
        """重建全部节点embedding。"""
        if not self.nodes:
            self._emb_matrix = np.zeros((0, _EMBEDDING_DIM), dtype=np.float32)
            self._persist_embedding_cache("rebuilt")
            return

        self._node_id_order = sorted(self.nodes.keys())
        texts = []
        for nid in self._node_id_order:
            texts.append(self._node_to_text(self.nodes[nid]))

        print(f"[NeuralMemory] Building embeddings for {len(texts)} nodes...")
        self._emb_matrix = _encode(texts, allow_auto_fallback=True)

        for i, nid in enumerate(self._node_id_order):
            self._node_embeddings[nid] = self._emb_matrix[i]

        self._persist_embedding_cache("rebuilt")
        print(f"[NeuralMemory] Embeddings cached ({self._emb_matrix.shape})")

    def _embedding_backend_id(self) -> str:
        model = _get_model()
        return str(getattr(model, "_3can_backend_id", getattr(model, "backend_id", model.__class__.__name__)))

    def _embedding_source_manifest(self) -> str:
        digest = hashlib.sha256(b"3can-embedding-text-v1\0")
        for node_id in self._node_id_order:
            node = self.nodes.get(node_id)
            if node is None:
                raise ValueError(f"embedding_source_node_missing:{node_id}")
            node_id_bytes = node_id.encode("utf-8")
            text_bytes = self._node_to_text(node).encode("utf-8")
            digest.update(len(node_id_bytes).to_bytes(4, "big"))
            digest.update(node_id_bytes)
            digest.update(len(text_bytes).to_bytes(8, "big"))
            digest.update(text_bytes)
        return digest.hexdigest()

    def _persist_embedding_cache(self, state: str) -> None:
        self._embedding_cache_state = "dirty"
        matrix = self._emb_matrix
        if matrix is None:
            matrix = np.zeros((0, _EMBEDDING_DIM), dtype=np.float32)
        backend_id = self._embedding_backend_id()
        source_manifest = self._embedding_source_manifest()
        _atomic_write_embedding_cache(
            EMBEDDINGS_FILE,
            self._node_id_order,
            matrix,
            backend_id,
        )
        _atomic_write_json(
            EMBEDDINGS_META_FILE,
            {
                "schema_version": 1,
                "backend_id": backend_id,
                "source_manifest": source_manifest,
            },
        )
        self._embedding_cache_state = state
        self._embedding_cache_backend_id = backend_id
        self._embedding_cache_source_manifest = source_manifest

    def embedding_status(self, *, deep: bool = False) -> dict[str, Any]:
        """Return retrieval diagnostics without scanning the graph by default.

        ``deep=True`` explicitly opts into the O(N) source-manifest comparison.
        The default response uses ``None`` for values that cannot be proven
        without that scan, rather than claiming that the cache matches.
        """

        model = _embed_model
        matrix = self._emb_matrix
        matrix_rows = (
            int(matrix.shape[0])
            if matrix is not None and matrix.ndim == 2
            else 0
        )
        matrix_dim = (
            int(matrix.shape[1])
            if matrix is not None and matrix.ndim == 2
            else 0
        )
        if model is None:
            active_backend = self._embedding_cache_backend_id or "uninitialized"
            requested_backend = "uninitialized"
            fallback_policy = "unknown"
            degraded = False
            reason_code = "embedding_model_uninitialized"
        else:
            active_backend = str(
                getattr(
                    model,
                    "_3can_backend_id",
                    getattr(model, "backend_id", model.__class__.__name__),
                )
            )
            requested_backend = str(
                getattr(model, "_3can_requested_backend", "unknown")
            )
            fallback_policy = str(
                getattr(model, "_3can_fallback_policy", "unknown")
            )
            degraded = bool(getattr(model, "_3can_degraded", False))
            raw_reason_code = str(getattr(model, "_3can_reason_code", ""))
            if raw_reason_code in {"", "embedding_model_uninitialized"} or re.fullmatch(
                r"(?:embedding_model_[a-z0-9_]+:bge-m3|(?:bge_model_load_failed|bge_encode_failed):[A-Za-z_][A-Za-z0-9_]{0,63})",
                raw_reason_code,
            ):
                reason_code = raw_reason_code
            else:
                reason_code = "embedding_status_reason_redacted"
        if requested_backend not in {"hashing", "bge-m3", "auto", "unknown", "uninitialized"}:
            requested_backend = "unknown"
        if fallback_policy not in {"hashing", "error", "not_applicable", "unknown"}:
            fallback_policy = "unknown"
        if not (
            active_backend in {"hashing-blake2b-char-ngram-v1", "uninitialized"}
            or re.fullmatch(
                r"sentence-transformers:BAAI/bge-m3@[0-9a-f]{7,64}:maxseq=[0-9]{2,4}",
                active_backend,
            )
        ):
            active_backend = "embedding_backend_redacted"
        model_revision = (
            str(getattr(model, "_3can_model_revision", ""))
            if model is not None
            else ""
        )
        if not (
            model_revision in {"", "algorithm-v1"}
            or re.fullmatch(r"[0-9a-f]{7,64}", model_revision)
        ):
            model_revision = "redacted"
        attempted_model_revision = (
            str(getattr(model, "_3can_attempted_model_revision", ""))
            if model is not None
            else ""
        )
        if not (
            attempted_model_revision == ""
            or re.fullmatch(r"[0-9a-f]{7,64}", attempted_model_revision)
        ):
            attempted_model_revision = "redacted"
        cache_exists = EMBEDDINGS_FILE.exists()
        cache_meta_exists = EMBEDDINGS_META_FILE.exists()
        cache_structurally_ready = bool(
            cache_exists
            and cache_meta_exists
            and self._embedding_cache_state in {"loaded", "rebuilt", "updated"}
            and matrix_rows == len(self.nodes) == len(self._node_id_order)
            and matrix_dim == _EMBEDDING_DIM
            and self._embedding_cache_backend_id == active_backend
        )
        cache_sha256 = ""
        meta_sha256 = ""
        disk_source_manifest = ""
        disk_rows = 0
        disk_dimension = 0
        all_rows_finite: bool | None = None
        all_rows_nonzero: bool | None = None
        all_rows_unit_norm: bool | None = None
        cache_ids_match: bool | None = None
        cache_backend_match: bool | None = None
        meta_backend_match: bool | None = None
        deep_cache_check = "not_requested"
        deep_cache_error_code = ""
        if deep:
            try:
                cache_payload = EMBEDDINGS_FILE.read_bytes()
                meta_payload = EMBEDDINGS_META_FILE.read_bytes()
                cache_sha256 = hashlib.sha256(cache_payload).hexdigest()
                meta_sha256 = hashlib.sha256(meta_payload).hexdigest()
                disk_ids, disk_matrix, disk_backend = (
                    _read_embedding_cache_payload(cache_payload)
                )
                meta_backend, disk_source_manifest = (
                    _read_embedding_cache_meta_payload(meta_payload)
                )
                disk_rows = int(disk_matrix.shape[0])
                disk_dimension = int(disk_matrix.shape[1])
                all_rows_finite = bool(np.isfinite(disk_matrix).all())
                norms = np.linalg.norm(
                    disk_matrix.astype(np.float64, copy=False),
                    axis=1,
                )
                all_rows_nonzero = bool(
                    disk_rows > 0
                    and np.all(norms > _EMBEDDING_NONZERO_NORM_MIN)
                )
                all_rows_unit_norm = bool(
                    disk_rows > 0
                    and np.allclose(
                        norms,
                        1.0,
                        rtol=0.0,
                        atol=_EMBEDDING_UNIT_NORM_ATOL,
                    )
                )
                cache_ids_match = disk_ids == self._node_id_order
                cache_backend_match = disk_backend == active_backend
                meta_backend_match = meta_backend == active_backend
                deep_cache_check = "verified"
            except Exception:
                deep_cache_check = "failed"
                deep_cache_error_code = "embedding_cache_deep_validation_failed"
                logging.getLogger("3can").warning(
                    "deep on-disk embedding-cache verification failed",
                    exc_info=True,
                )
        current_source_manifest: str | None = None
        source_manifest_match: bool | None = None
        source_manifest_check = "not_requested"
        if deep:
            try:
                current_source_manifest = self._embedding_source_manifest()
                if deep_cache_check == "verified" and disk_source_manifest:
                    source_manifest_match = (
                        disk_source_manifest == current_source_manifest
                    )
                    source_manifest_check = "verified"
                else:
                    source_manifest_check = "failed"
            except Exception:
                logging.getLogger("3can").warning(
                    "deep embedding source-manifest verification failed",
                    exc_info=True,
                )
                source_manifest_check = "failed"
        if deep:
            deep_structure_valid = bool(
                deep_cache_check == "verified"
                and disk_rows == matrix_rows == len(self.nodes)
                and disk_dimension == matrix_dim == _EMBEDDING_DIM
                and cache_ids_match is True
                and cache_backend_match is True
                and meta_backend_match is True
                and disk_source_manifest == self._embedding_cache_source_manifest
                and all_rows_finite is True
                and all_rows_nonzero is True
                and all_rows_unit_norm is True
            )
            cache_structurally_ready = bool(
                cache_structurally_ready and deep_structure_valid
            )
        if not cache_structurally_ready:
            cache_synchronized: bool | None = False
        elif source_manifest_match is None:
            cache_synchronized = None
        else:
            cache_synchronized = source_manifest_match
        raw_reranker_mode = (
            _os.environ.get("THREECAN_RERANKER_MODE") or "adaptive"
        ).strip().lower()
        raw_reranker_warmup = (
            _os.environ.get("THREECAN_RERANKER_WARMUP") or "background"
        ).strip().lower()
        disabled_values = {"0", "false", "off", "none", "disabled", "disable"}
        public_reranker_mode = (
            "off" if raw_reranker_mode in disabled_values else "adaptive"
        )
        public_reranker_warmup = (
            "off" if raw_reranker_warmup in disabled_values else "background"
        )
        public_cache_source_manifest = (
            self._embedding_cache_source_manifest
            if re.fullmatch(r"[0-9a-f]{64}", self._embedding_cache_source_manifest)
            else ""
        )
        return {
            "requested_backend": requested_backend,
            "active_backend": active_backend,
            "active_backend_id": active_backend,
            "semantic_dense_active": active_backend.startswith(
                "sentence-transformers:"
            ),
            "degraded": degraded,
            "fallback_policy": fallback_policy,
            "reason_code": reason_code,
            "dimension": disk_dimension if deep else matrix_dim,
            "configured_dimension": _EMBEDDING_DIM,
            "batch_size": int(getattr(model, "_3can_batch_size", 0))
            if model is not None
            else 0,
            "max_sequence_length": int(
                getattr(model, "_3can_max_sequence_length", 0)
            )
            if model is not None
            else 0,
            "model_revision": model_revision,
            "attempted_model_revision": attempted_model_revision,
            "node_count": len(self.nodes),
            "row_count": disk_rows if deep else matrix_rows,
            "matrix_rows": matrix_rows,
            "matrix_dimension": matrix_dim,
            "cache_exists": cache_exists,
            "cache_meta_exists": cache_meta_exists,
            "cache_state": self._embedding_cache_state,
            "cache_backend_id": (
                self._embedding_cache_backend_id
                if self._embedding_cache_backend_id == active_backend
                else "embedding_backend_redacted"
                if self._embedding_cache_backend_id
                else ""
            ),
            "cache_source_manifest": public_cache_source_manifest,
            "source_manifest_sha256": (
                disk_source_manifest if deep else public_cache_source_manifest
            ),
            "cache_sha256": cache_sha256,
            "meta_sha256": meta_sha256,
            "cache_structurally_ready": cache_structurally_ready,
            "deep_cache_check": deep_cache_check,
            "deep_cache_error_code": deep_cache_error_code,
            "all_rows_finite": all_rows_finite,
            "all_rows_nonzero": all_rows_nonzero,
            "all_rows_unit_norm": all_rows_unit_norm,
            "unit_norm_tolerance": _EMBEDDING_UNIT_NORM_ATOL,
            "cache_ids_match": cache_ids_match,
            "cache_backend_match": cache_backend_match,
            "meta_backend_match": meta_backend_match,
            "current_source_manifest": current_source_manifest,
            "source_manifest_check": source_manifest_check,
            "source_manifest_match": source_manifest_match,
            "cache_synchronized": cache_synchronized,
            "reranker": {
                "mode": public_reranker_mode,
                "warmup": public_reranker_warmup,
                "pool_limit": self._RERANKER_POOL_LIMIT,
                **self._reranker_warmup_meta(),
            },
        }

    def _node_to_text(self, node: Node) -> str:
        """将节点转为用于 embedding 的富文本 (v9.0 baseline 等权).

        v9.1 P0 回退: layer-weighted 版本对 short-code 类目降 20%, 自审计不过.
        社区标准做法是 hybrid (dense+sparse) + reranker, 不靠 embedding text 的位置把戏.
        """
        parts = [
            node.name,
            node.content.description,
            node.content.current_state,
            " ".join(node.activation_keywords),
            " ".join(node.content.key_files[:5]),
            " ".join(node.content.api_refs[:5]),
            " ".join(node.content.tech_stack),
            " ".join(node.content.decisions[:5]),
            node.content.notes[:1000] if node.content.notes else "",
            node.cluster, node.id, node.type,
        ]
        return " ".join(p for p in parts if p)

    def _update_single_embedding(self, node_id: str) -> None:
        """更新单个节点的embedding（增量）。保持_node_id_order和sorted()一致以便cache比对."""
        node = self.nodes.get(node_id)
        if not node:
            return
        self._embedding_cache_state = "dirty"
        text = self._node_to_text(node)
        emb = _encode([text])[0]
        self._node_embeddings[node_id] = emb

        # 更新矩阵: 保持sorted order, 新节点按字典序插入对应位置
        if node_id in self._node_id_order:
            idx = self._node_id_order.index(node_id)
            self._emb_matrix[idx] = emb
        else:
            # 二分找到sorted顺序里的插入位置
            import bisect
            idx = bisect.bisect_left(self._node_id_order, node_id)
            self._node_id_order.insert(idx, node_id)
            if self._emb_matrix is None or self._emb_matrix.shape[0] == 0:
                self._emb_matrix = emb.reshape(1, -1)
            else:
                self._emb_matrix = np.insert(self._emb_matrix, idx, emb, axis=0)

        self._persist_embedding_cache("updated")

    def _node_path(self, node_id: str) -> Path:
        validated = validate_node_identifier(node_id)
        root = NODES_DIR.resolve()
        path = (root / f"{validated}.json").resolve()
        if path.parent != root:
            raise ValueError("node_path_outside_storage_root")
        return path

    def _casefold_conflict(self, node_id: str) -> str | None:
        folded = node_id.casefold()
        return next(
            (
                existing
                for existing in self.nodes
                if existing.casefold() == folded and existing != node_id
            ),
            None,
        )

    @staticmethod
    def _reserved_error_knowledge_id(node_id: str) -> bool:
        return str(node_id or "").casefold().startswith(
            ("err-", "errcase-", "fix-", "evd-")
        )

    def _assert_error_knowledge_mutation_owner(
        self,
        *node_ids: str,
        internal_owner: str | None = None,
    ) -> None:
        reserved = [
            node_id
            for node_id in node_ids
            if self._reserved_error_knowledge_id(node_id)
        ]
        if (
            reserved
            and internal_owner not in self._ERROR_KNOWLEDGE_INTERNAL_OWNERS
        ):
            raise PermissionError(
                "error_knowledge_write_requires_internal_owner:"
                + ",".join(reserved)
            )

    def _durable_provenance_permits_current(
        self,
        provenance: DurableProvenance,
    ) -> bool:
        if not provenance.has_required_claim_fields():
            return False
        source = str(
            getattr(provenance.source_provenance, "value", provenance.source_provenance)
        ).casefold()
        if source == "user_authoritative":
            return True
        if source == "machine_verifiable":
            # Existing EVD bundles verify an ErrorKnowledge resolution, not the
            # target node/field/value of an arbitrary durable-current claim.
            # Until a canonical owner emits that binding, fail closed instead
            # of treating an unrelated valid receipt as authority.
            return False
        return False

    def _assert_durable_current_mutation_owner(
        self,
        *node_ids: str,
        internal_owner: str | None = None,
        provenance_payload: dict[str, Any] | None = None,
    ) -> None:
        protected = [
            node_id
            for node_id in node_ids
            if semantic_id_family(node_id) in _AUTHORITY_PROTECTED_FAMILIES
        ]
        if not protected or internal_owner in self._DURABLE_CURRENT_INTERNAL_OWNERS:
            return
        payload = provenance_payload if isinstance(provenance_payload, dict) else {}
        project_id = str(payload.get("project_id") or "").strip().casefold()
        project_namespace = str(
            payload.get("project_namespace") or ""
        ).strip().casefold()
        try:
            provenance = DurableProvenance.model_validate(
                payload.get("durable_provenance")
            )
        except (TypeError, ValueError) as exc:
            raise PermissionError(
                "durable_current_provenance_required:" + ",".join(protected)
            ) from exc
        if (
            not self._durable_provenance_permits_current(provenance)
            or not project_id
            or not project_namespace
        ):
            raise PermissionError(
                "durable_current_provenance_required:" + ",".join(protected)
            )
        for node_id in protected:
            existing = self.nodes.get(node_id)
            if existing is None:
                continue
            existing_project, existing_namespace = self._node_project_values(existing)
            if (
                (existing_project and existing_project != project_id)
                or (existing_namespace and existing_namespace != project_namespace)
            ):
                raise PermissionError(
                    "durable_current_project_identity_mismatch:" + node_id
                )

    def _save_node(self, node: Node) -> None:
        conflict = self._casefold_conflict(node.id)
        if conflict is not None:
            raise ValueError(f"node_id_case_conflict:{conflict}")
        path = self._node_path(node.id)
        _atomic_write_json(path, node.model_dump())

    def _save_edges(self) -> None:
        _atomic_write_json(
            EDGES_FILE,
            [edge.model_dump() for edge in self.edges],
        )

    def reload(self) -> None:
        self._load()
        self._load_or_build_embeddings()  # 智能加载: 节点集合未变则用cache, 秒级完成

    # ── 节点 CRUD ──

    def create_node(
        self,
        req: NodeCreate,
        *,
        internal_owner: str | None = None,
    ) -> Node:
        node_id = validate_node_identifier(req.id or self._gen_id(req.cluster))
        self._assert_error_knowledge_mutation_owner(
            node_id,
            internal_owner=internal_owner,
        )
        self._assert_durable_current_mutation_owner(
            node_id,
            internal_owner=internal_owner,
            provenance_payload=req.content.extra,
        )
        conflict = self._casefold_conflict(node_id)
        if node_id in self.nodes or conflict is not None:
            raise ValueError(
                f"node_id_case_conflict:{conflict or node_id}"
            )
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        author = getattr(req, "primary_author", "system") or "system"
        node = Node(
            id=node_id, name=req.name, cluster=req.cluster, layer=req.layer,
            type=req.type, status=req.status, content=req.content,
            activation_keywords=req.activation_keywords, priority=req.priority,
            created_at=now, updated_at=now,
            updated_by=author, primary_author=author,
        )
        self._embedding_cache_state = "dirty"
        self._save_node(node)
        self.nodes[node.id] = node
        self._update_single_embedding(node.id)
        return node

    def get_node(self, node_id: str) -> Node | None:
        return self.nodes.get(node_id)

    def list_nodes(self, cluster=None, status=None, node_type=None) -> list[Node]:
        result = list(self.nodes.values())
        if cluster:
            result = [n for n in result if n.cluster == cluster]
        if status:
            result = [n for n in result if n.status == status]
        if node_type:
            result = [n for n in result if n.type == node_type]
        return sorted(result, key=lambda n: n.updated_at, reverse=True)

    def update_node(
        self,
        node_id: str,
        req: NodeUpdate,
        *,
        internal_owner: str | None = None,
    ) -> Node | None:
        validate_node_identifier(node_id)
        self._assert_error_knowledge_mutation_owner(
            node_id,
            internal_owner=internal_owner,
        )
        self._assert_durable_current_mutation_owner(
            node_id,
            internal_owner=internal_owner,
            provenance_payload=(req.content.extra if req.content is not None else None),
        )
        node = self.nodes.get(node_id)
        if not node:
            return None
        self._embedding_cache_state = "dirty"
        update_data = req.model_dump(exclude_none=True, exclude_unset=True)
        update_data["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
        for k, v in update_data.items():
            if k == "content" and isinstance(v, dict):
                existing = node.content.model_dump()
                if isinstance(v.get("extra"), dict):
                    merged_extra = dict(existing.get("extra") or {})
                    merged_extra.update(v["extra"])
                    v = {**v, "extra": merged_extra}
                existing.update(v)
                node.content = NodeContent(**existing)
            else:
                setattr(node, k, v)
        self._save_node(node)
        self._update_single_embedding(node_id)
        return node

    def delete_node(
        self,
        node_id: str,
        *,
        internal_owner: str | None = None,
    ) -> bool:
        self._assert_error_knowledge_mutation_owner(
            node_id,
            internal_owner=internal_owner,
        )
        self._assert_durable_current_mutation_owner(
            node_id,
            internal_owner=internal_owner,
        )
        path = self._node_path(node_id)
        if node_id not in self.nodes:
            return False
        if any(edge.source == node_id or edge.target == node_id for edge in self.edges if self._edge_type_value(edge.type) == "supersedes"):
            raise PermissionError("supersedes_endpoint_delete_forbidden")
        self._embedding_cache_state = "dirty"
        path.unlink(missing_ok=True)
        del self.nodes[node_id]
        self.edges = [e for e in self.edges if e.source != node_id and e.target != node_id]
        self._save_edges()
        # 增量删除embedding (不全量rebuild)
        self._node_embeddings.pop(node_id, None)
        if node_id in self._node_id_order:
            idx = self._node_id_order.index(node_id)
            self._node_id_order.pop(idx)
            if self._emb_matrix is not None and self._emb_matrix.shape[0] > idx:
                self._emb_matrix = np.delete(self._emb_matrix, idx, axis=0)
            self._persist_embedding_cache("updated")
        return True

    # ── 边 CRUD ──

    def _edge_type_value(self, edge_type: Any) -> str:
        return str(getattr(edge_type, "value", edge_type))

    def _edge_key(self, edge: Edge) -> tuple[str, str, str]:
        return (edge.source, edge.target, self._edge_type_value(edge.type))

    def validate_supersession(self, source_id: str, target_id: str) -> None:
        """Validate a public durable-current replacement before edge creation."""

        source = self.nodes.get(source_id)
        target = self.nodes.get(target_id)
        if source is None or target is None:
            raise ValueError("supersedes_node_not_found")
        source_family = semantic_id_family(source_id)
        target_family = semantic_id_family(target_id)
        if source_family != target_family:
            raise ValueError("supersedes_semantic_family_mismatch")
        if source.status != NodeStatus.active:
            raise ValueError("supersedes_source_must_be_active")
        source_project, source_namespace = self._node_project_values(source)
        target_project, target_namespace = self._node_project_values(target)
        if any((source_project, source_namespace, target_project, target_namespace)):
            if not all((source_project, source_namespace, target_project, target_namespace)):
                raise ValueError("supersedes_project_identity_incomplete")
            if (
                source_project != target_project
                or source_namespace != target_namespace
            ):
                raise ValueError("supersedes_project_identity_mismatch")
        if source_family in _AUTHORITY_PROTECTED_FAMILIES:
            if not source_project or not source_namespace:
                raise ValueError("supersedes_source_project_identity_required")
            provenance_payload = source.content.extra.get("durable_provenance")
            try:
                provenance = DurableProvenance.model_validate(provenance_payload)
            except (TypeError, ValueError) as exc:
                raise ValueError("supersedes_source_provenance_required") from exc
            if not self._durable_provenance_permits_current(provenance):
                raise ValueError("supersedes_source_provenance_required")
        if target_id in self._superseded_node_ids():
            raise ValueError("supersedes_target_already_superseded")
        if source_id in self._superseded_node_ids():
            raise ValueError("supersedes_source_already_superseded")

    def create_edge(
        self,
        req: EdgeCreate,
        *,
        internal_owner: str | None = None,
    ) -> Edge:
        self._assert_error_knowledge_mutation_owner(
            req.source,
            req.target,
            internal_owner=internal_owner,
        )
        if req.source == req.target:
            raise ValueError("self_edge_not_allowed")
        if self._edge_type_value(req.type) == "supersedes":
            error_lineage = (
                internal_owner in self._ERROR_KNOWLEDGE_INTERNAL_OWNERS
                and self._reserved_error_knowledge_id(req.source)
                and self._reserved_error_knowledge_id(req.target)
            )
            if not error_lineage:
                self.validate_supersession(req.source, req.target)
        requested_key = (req.source, req.target, self._edge_type_value(req.type))
        for existing in self.edges:
            if self._edge_key(existing) == requested_key:
                return existing
        edge = Edge(source=req.source, target=req.target, type=req.type,
                    weight=req.weight, description=req.description)
        self.edges.append(edge)
        self._save_edges()
        return edge

    def list_edges(self, node_id=None) -> list[Edge]:
        if node_id:
            return [e for e in self.edges if e.source == node_id or e.target == node_id]
        return self.edges

    def delete_edge(
        self,
        source: str,
        target: str,
    ) -> bool:
        if any(
            edge.source == source
            and edge.target == target
            and self._edge_type_value(edge.type) == "supersedes"
            for edge in self.edges
        ):
            raise PermissionError("supersedes_delete_forbidden")
        before = len(self.edges)
        self.edges = [e for e in self.edges if not (e.source == source and e.target == target)]
        if len(self.edges) < before:
            self._save_edges()
            return True
        return False

    def cleanup_edges(self, apply: bool = False, sample_limit: int = 50) -> dict:
        original_count = len(self.edges)
        kept: list[Edge] = []
        seen: set[tuple[str, str, str]] = set()
        removed_self: list[dict] = []
        removed_duplicate: list[dict] = []

        for edge in self.edges:
            if edge.source == edge.target:
                removed_self.append(edge.model_dump())
                continue

            key = self._edge_key(edge)
            if key in seen:
                removed_duplicate.append(edge.model_dump())
                continue

            seen.add(key)
            kept.append(edge)

        changed = len(kept) != original_count
        if apply and changed:
            self.edges = kept
            self._save_edges()

        return {
            "applied": bool(apply),
            "changed": changed,
            "before": original_count,
            "after": len(kept),
            "removed_self_edges": len(removed_self),
            "removed_duplicate_edges": len(removed_duplicate),
            "self_edge_samples": removed_self[:sample_limit],
            "duplicate_edge_samples": removed_duplicate[:sample_limit],
        }

    # ── 路由算法 v5：two-pass layered + activation heat ──
    #
    # 主脑§8建议: 先搜活跃节点(~122个)，不够再扩到dormant
    # 三层策略: Round 1 活跃池 → Round 2 全量池(如果R1不够) → Fallback
    # INTF分层: 默认降权，precision模式全权
    # 中间状态接入: activation_count做热度boost，新agent也能命中高频节点

    _INTF_DEMOTION = -0.08  # v8.3: 减半, 且仅对零激活+零keyword命中的INTF生效
    _ACTIVATION_HEAT_WEIGHT = 0.005  # v7.3: 降低heat权重防磁铁节点. 每次+0.005, 上限0.05
    _DORMANT_PENALTY = -0.12        # dormant节点额外降权
    _cooccurrence: dict[str, set[str]] | None = None  # keyword共现表 (懒加载)
    _CORE_MEMORY_REGISTRY_NODE_ID = "MEM-3can-core-memory-lane-registry-20260523"
    _CORE_ROUTE_TRIGGER = re.compile(
        r"3can|memory|route|graph|edge|node|weight|ticket|github|pull request|\bPR\b|"
        r"runtime|path|error|preference|project[-_ ]?isolation|cross[-_ ]?project|"
        r"proxy|backend lane|"
        r"desktop|file[-_ ]?system|artifact|archive|quarantine|export|media|video|folder|"
        r"\u8bb0\u5fc6|\u9519\u8bef|\u504f\u597d|"
        r"\u8fb9|\u8282\u70b9|\u6743\u91cd|\u56fe\u8c31|\u534f\u4f5c",
        re.IGNORECASE,
    )
    _CORE_REGISTRY_ANCHOR_TRIGGER = re.compile(
        r"3can|memory|graph|edge|node|weight|\u8bb0\u5fc6|\u56fe\u8c31|\u8282\u70b9|\u8fb9|\u6743\u91cd",
        re.IGNORECASE,
    )
    _PRODUCT_ROUTE_TRIGGER = re.compile(
        r"product|saas|ecommerce|commerce|merchant|store|rpa|"
        r"\u4ea7\u54c1|\u5546\u5bb6|\u5e97\u94fa|\u987e\u95ee",
        re.IGNORECASE,
    )
    _CORE_TOPIC_PATTERNS = {
        "github": re.compile(r"github|\bgh\b|pull request|\bpr\b|push|branch|repo|\u4e0a\u4f20|\u4ed3\u5e93", re.IGNORECASE),
        "3can": re.compile(r"3can|memory|route|graph|edge|node|weight|hook|\u8bb0\u5fc6|\u56fe\u8c31|\u8282\u70b9|\u8fb9|\u6743\u91cd", re.IGNORECASE),
        "ticket": re.compile(r"ticket|prepare|ttl|expire|\u5931\u6548|\u8fc7\u671f", re.IGNORECASE),
        "runtime": re.compile(r"windows|powershell|runtime|path|wsl|\u8def\u5f84|\u73af\u5883", re.IGNORECASE),
        "user": re.compile(r"preference|user|\u504f\u597d|\u7528\u6237", re.IGNORECASE),
        "file_system": re.compile(
            r"desktop|file[-_ ]?system|filesystem|project_fs_guard|artifact|archive|quarantine|export|media|video|"
            r"folder|asset|path|\u6587\u4ef6|\u76ee\u5f55|\u8def\u5f84|\u843d\u76d8|\u5f52\u6863",
            re.IGNORECASE,
        ),
        "project_isolation": re.compile(
            r"project[-_ ]?isolation|cross[-_ ]?project|proxy|backend lane|"
            r"port isolation|frontend contamination|contamination",
            re.IGNORECASE,
        ),
    }
    _CORE_DYNAMIC_GENERIC_TOPICS = {"3can"}
    _CORE_DYNAMIC_GENERIC_TERMS = {
        "3can",
        "memory",
        "memories",
        "error",
        "errors",
        "warning",
        "warnings",
        "user",
        "users",
        "preference",
        "preferences",
        "project",
        "projects",
        "coordination",
        "edge",
        "edges",
        "node",
        "nodes",
        "weight",
        "weights",
        "graph",
        "route",
        "routing",
        "codex",
        "main",
    }
    # Historical ERR cases are searchable knowledge, not an always-on guard lane.
    # Ordinary runtime/path/GitHub work must not pull thousands of incident nodes
    # into the hot route merely because those broad topics appear in the task.
    _CORE_DYNAMIC_REPEATED_ERR_TOPICS: set[str] = set()
    _CANONICAL_ERROR_IDENTITY_FIELD = re.compile(
        r"\[\s*(project(?:_id|_identity)?|operation(?:_class)?|component|error(?:_type)?)"
        r"\s*=\s*([^\]]+?)\s*\]",
        re.IGNORECASE,
    )
    _CORE_REPEATED_ERR_MAX_PER_ROUTE = 3
    _CORE_ERROR_WARNING_MAX_PER_ROUTE = 8
    _ROUTE_RELEVANT_EDGE_MAX = 64
    _ROUTE_SOLUTION_EDGE_TYPES = {
        "resolves",
        "verified_by",
        "applies_to",
        "supersedes",
        "regressed_from",
    }
    # Public engine code never embeds a particular user's memory IDs, product
    # constitution, project ports, or historical incidents. Project-specific
    # hints are read from the registry node and graph content at runtime.
    _CORE_NODE_TOPIC_HINTS = {_CORE_MEMORY_REGISTRY_NODE_ID: {"3can"}}
    _CORE_LANE_FALLBACKS = {
        "user_preferences": [],
        "environment_constraints": [],
        "error_warnings": [],
        "project_constitution": [],
        "project_file_system": [],
    }
    _DEFAULT_CORE_LANE_WEIGHTS = {
        "user_preferences": 100.0,
        "error_warnings": 100.0,
        "environment_constraints": 95.0,
        "project_constitution": 90.0,
        "project_file_system": 95.0,
    }
    _NODE_EXEC_PRIORITY = {"critical": 100.0, "high": 80.0, "medium": 50.0, "low": 25.0}
    _NODE_EXEC_TYPE_BONUS = {"feedback": 7.0, "config": 6.0, "knowledge": 4.0, "reference": 3.0, "decision": 3.0, "session": -10.0}
    _NODE_EXEC_STATUS_FACTOR = {"active": 1.0, "blocked": 0.2, "deprecated": 0.2, "dormant": 0.35, "archived": 0.15}

    def _build_cooccurrence(self) -> dict[str, set[str]]:
        """从所有节点的activation_keywords构建共现词表。

        逻辑: 如果"云服务器"和"SSH"同时出现在SEC-autodl的keywords里,
        那么查"云服务器"时自动扩展出"SSH"。
        """
        cooc: dict[str, set[str]] = {}
        for node in self.nodes.values():
            if node.status in {NodeStatus.dormant, NodeStatus.archived}:
                continue
            kws = [kw.lower() for kw in node.activation_keywords if len(kw) >= 2]
            for kw in kws:
                if kw not in cooc:
                    cooc[kw] = set()
                cooc[kw].update(k for k in kws if k != kw)
        return cooc

    # v8 Layer 1+2: 自动化短代码解析 (替代hardcoded mapping)

    _CODE_PATTERN = re.compile(r"\b([A-Z]{1,5}\d{1,4}[a-z]?)\b")  # FP3, S55c, KB4, E6...
    _CLICK_LOG_FILE = GRAPH_DIR / "click_log.json"

    def _build_code_index(self) -> None:
        """Layer 1: 从所有节点文本自动提取短代码→节点ID的反向索引。
        零配置，换项目自动重建。节点create/update时增量维护。
        """
        self._code_index.clear()
        for nid in sorted(self.nodes):
            node = self.nodes[nid]
            if node.status in {NodeStatus.dormant, NodeStatus.archived}:
                continue
            # 扫描所有文本字段
            text = f"{nid} {node.name} {node.content.description or ''} {' '.join(node.activation_keywords)}"
            codes = self._CODE_PATTERN.findall(text)
            for code in codes:
                code_upper = code.upper()
                if code_upper not in self._code_index:
                    self._code_index[code_upper] = []
                if nid not in self._code_index[code_upper]:
                    self._code_index[code_upper].append(nid)

    def _build_kw_df(self) -> None:
        """v9.0 Wave 2: 构建 kw → 节点频率表 (IDF 用).
        活跃节点 + kw 小写去空白. 用于 _kw_idf() 对热重 kw (intf 426 / doc 313) 自动降权.
        """
        self._kw_df.clear()
        active_n = 0
        for node in self.nodes.values():
            if node.status in {NodeStatus.dormant, NodeStatus.archived}:
                continue
            active_n += 1
            seen: set[str] = set()
            for kw in node.activation_keywords:
                if not isinstance(kw, str):
                    continue
                k = kw.lower().strip()
                if len(k) < 2 or k in seen:
                    continue
                seen.add(k)
                self._kw_df[k] = self._kw_df.get(k, 0) + 1
        self._kw_N = active_n

    def _kw_idf(self, kw: str) -> float:
        """Smoothed IDF: log((N+1)/(df+1)) + 1, 裁剪到 [0.2, 3.0].
        - 稀有 kw (df=1, N=1372): idf ≈ log(1373/2)+1 ≈ 7.5 → 封 3.0
        - 中等 kw (df=10, N=1372): idf ≈ log(1373/11)+1 ≈ 5.8 → 封 3.0
        - 常见 kw (df=50, N=1372): idf ≈ log(1373/51)+1 ≈ 4.3 → 封 3.0
        - 热重 kw (df=200, N=1372): idf ≈ log(1373/201)+1 ≈ 2.9
        - 巨重 kw (df=426=intf): idf ≈ log(1373/427)+1 ≈ 2.17
        - 全图 kw (df=N): idf ≈ 1.0 (最小权重)
        实际用 [0.2, 3.0] 封顶, 保持稳定.
        """
        if not self._kw_N:
            return 1.0
        k = kw.lower().strip()
        df = self._kw_df.get(k, 0)
        import math
        raw = math.log((self._kw_N + 1) / (df + 1)) + 1.0
        return max(0.2, min(3.0, raw))

    def _load_click_log(self) -> None:
        """Layer 2: 加载历史使用反馈。"""
        if self._CLICK_LOG_FILE.exists():
            try:
                self._click_log = json.loads(self._CLICK_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._click_log = {}

    def _save_click_log(self) -> None:
        _atomic_write_json(self._CLICK_LOG_FILE, self._click_log)

    def record_route_feedback(
        self,
        query: str,
        node_signals: list[tuple[str, float]],
        *,
        promote_keywords: bool = False,
        allow_reserved: bool = False,
    ) -> dict[str, Any]:
        """Validate and record one explicit route-feedback batch.

        The whole input is checked before click-log or keyword state changes.
        Protected durable families are never changed through feedback; their
        metadata changes go through canonical durable writeback.
        """

        clean_query = str(query or "").strip()
        if not clean_query:
            raise ValueError("route_feedback_query_required")
        if not isinstance(node_signals, list) or not node_signals:
            raise ValueError("route_feedback_nodes_required")

        normalized: list[tuple[str, float]] = []
        seen: set[str] = set()
        for node_id, raw_signal in node_signals:
            clean_node_id = str(node_id or "").strip()
            if not clean_node_id or clean_node_id not in self.nodes:
                raise ValueError(f"route_feedback_node_not_found:{clean_node_id}")
            if clean_node_id in seen:
                raise ValueError(f"route_feedback_node_duplicate:{clean_node_id}")
            if self._reserved_error_knowledge_id(clean_node_id) and not allow_reserved:
                raise ValueError(
                    f"route_feedback_reserved_node_forbidden:{clean_node_id}"
                )
            try:
                signal = float(raw_signal)
            except (TypeError, ValueError) as exc:
                raise ValueError("route_feedback_signal_invalid") from exc
            if not math.isfinite(signal) or not -1.0 <= signal <= 1.0:
                raise ValueError("route_feedback_signal_invalid")
            normalized.append((clean_node_id, signal))
            seen.add(clean_node_id)

        key = clean_query.upper()
        click_state = self._click_log.setdefault(key, {})
        for node_id, signal in normalized:
            current = click_state.get(node_id, 0)
            click_state[node_id] = round(current + signal, 2)
        self._save_click_log()

        promoted: list[dict[str, Any]] = []
        if promote_keywords:
            for node_id, signal in normalized:
                protected = (
                    semantic_id_family(node_id) in _AUTHORITY_PROTECTED_FAMILIES
                )
                if signal <= 0 or protected:
                    continue
                added = self._heal_keywords(clean_query, node_id)
                if added:
                    promoted.append({"node_id": node_id, "added_keywords": added})
        return {"recorded": len(normalized), "promoted": promoted}

    def record_outcome(
        self,
        query: str,
        used_node_id: str,
        signal: float = 1.0,
    ) -> None:
        """Compatibility wrapper over the canonical feedback owner."""

        self.record_route_feedback(
            query,
            [(used_node_id, signal)],
            allow_reserved=True,
        )

    # ── Miss Healer: 自动keyword扩展 (v8.3) ──

    def _load_pending_keywords(self) -> None:
        if self._PENDING_KW_FILE.exists():
            try:
                self._pending_keywords = json.loads(
                    self._PENDING_KW_FILE.read_text(encoding="utf-8"))
            except Exception:
                self._pending_keywords = {}

    def _save_pending_keywords(self) -> None:
        _atomic_write_json(self._PENDING_KW_FILE, self._pending_keywords)

    def _prune_route_buffer_locked(self, now: float) -> None:
        stale_keys = [
            key
            for key, value in self._route_buffer.items()
            if now - float(value.get("ts", 0.0)) > _ROUTE_BUFFER_TTL_SECONDS
        ]
        for key in stale_keys:
            self._route_buffer.pop(key, None)

        overflow = len(self._route_buffer) - _ROUTE_BUFFER_MAX_ENTRIES
        if overflow <= 0:
            return
        oldest_keys = sorted(
            self._route_buffer,
            key=lambda key: float(self._route_buffer[key].get("ts", 0.0)),
        )[:overflow]
        for key in oldest_keys:
            self._route_buffer.pop(key, None)

    def _record_route_buffer(
        self,
        agent_id: str,
        query: str,
        node_ids: list[str],
        *,
        route_id: str,
        session_instance_id: str | None = None,
    ) -> None:
        """Record one route under an exact session/route correlation key."""
        now = time.time()
        key = (agent_id, session_instance_id or "", route_id)
        with self._route_buffer_lock:
            self._route_buffer[key] = {
                "agent_id": agent_id,
                "session_instance_id": session_instance_id,
                "route_id": route_id,
                "query": query,
                "node_ids": list(node_ids),
                "ts": now,
            }
            self._prune_route_buffer_locked(now)

    def infer_outcome(
        self,
        agent_id: str,
        accessed_node_id: str,
        *,
        session_instance_id: str | None = None,
        route_id: str | None = None,
    ) -> dict | None:
        """当agent读取节点时, 自动推断上次route的outcome.

        逻辑:
        - 如果accessed_node在上次route结果中 → positive outcome (+0.5, 弱于显式+1.0)
        - 如果不在结果中(agent自己grep找到的) → 记录为feedback correction
        - 超过120秒不关联

        返回推断结果或None.
        """
        now = time.time()
        with self._route_buffer_lock:
            self._prune_route_buffer_locked(now)
            if session_instance_id is not None:
                if route_id is None:
                    return None
                buf = self._route_buffer.get(
                    (agent_id, session_instance_id, route_id)
                )
                correlation_mode = "session_exact"
            elif route_id is not None:
                buf = self._route_buffer.get((agent_id, "", route_id))
                correlation_mode = "legacy_route_exact"
            else:
                candidates = [
                    value
                    for key, value in self._route_buffer.items()
                    if key[0] == agent_id and key[1] == ""
                ]
                buf = max(
                    candidates,
                    key=lambda value: float(value.get("ts", 0.0)),
                    default=None,
                )
                correlation_mode = "legacy_agent_latest"
            if buf is not None:
                buf = dict(buf)

        if not buf:
            return None

        query = buf["query"]
        routed_ids = buf["node_ids"]
        outcome_meta = {
            "route_id": buf["route_id"],
            "session_instance_id": buf["session_instance_id"],
            "route_correlation_mode": correlation_mode,
            "legacy_route_correlation": correlation_mode != "session_exact",
        }
        accessed_node = self.nodes.get(accessed_node_id)
        if accessed_node is not None:
            accessed_node.activation_count += 1
            if (
                semantic_id_family(accessed_node_id)
                not in _AUTHORITY_PROTECTED_FAMILIES
            ):
                accessed_node.content.extra["last_accessed_at"] = dt.datetime.now(
                    dt.timezone.utc
                ).isoformat()
            self._save_node(accessed_node)

        if accessed_node_id in routed_ids:
            # Route命中 → 弱positive
            self.record_route_feedback(
                query,
                [(accessed_node_id, 0.5)],
                promote_keywords=True,
                allow_reserved=True,
            )
            return {
                "type": "hit",
                "query": query,
                "node_id": accessed_node_id,
                "signal": 0.5,
                **outcome_meta,
            }
        else:
            # Route未命中 → agent自己找到的, 记录correction + 扩展keyword
            node_signals = [(accessed_node_id, 1.0)]
            if routed_ids and routed_ids[0] != accessed_node_id:
                node_signals.append((routed_ids[0], -0.3))
            self.record_route_feedback(
                query,
                node_signals,
                promote_keywords=True,
                allow_reserved=True,
            )
            return {
                "type": "miss_correction",
                "query": query,
                "node_id": accessed_node_id,
                **outcome_meta,
            }

    def _heal_keywords(self, query: str, node_id: str) -> list[str]:
        """从query中提取novel tokens, 增量确认后添加到节点keywords.

        流程: 新token先进pending(confirm_count=1), 达到3次确认后正式添加.
        防止噪音词(一次性查询)污染keyword空间.
        """
        node = self.nodes.get(node_id)
        if not node:
            return []
        if self._reserved_error_knowledge_id(node_id):
            return []

        # 提取query中的有意义token
        tokens = re.findall(r"[\u4e00-\u9fa5]{2,}|[A-Za-z][A-Za-z0-9]{1,}", query)
        existing_kw = {kw.lower() for kw in node.activation_keywords}
        # 过滤掉已有keywords和停用词
        _stopwords = {"the", "and", "for", "with", "how", "what", "why", "when", "this", "that",
                       "from", "about", "which", "where", "does", "have", "been", "will",
                       "是什么", "怎么", "如何", "什么", "为什么", "哪里", "哪个", "这个", "那个"}
        novel = {
            token.casefold(): token
            for token in tokens
            if token.casefold() not in existing_kw
            and token.casefold() not in _stopwords
        }
        if not novel:
            return []

        # 增量确认
        if node_id not in self._pending_keywords:
            self._pending_keywords[node_id] = {}
        pending = self._pending_keywords[node_id]

        promoted = []
        for tok_lower, token in novel.items():
            pending[tok_lower] = pending.get(tok_lower, 0) + 1
            if pending[tok_lower] >= 3:
                # 3次确认 → 正式添加
                node.activation_keywords.append(token)
                del pending[tok_lower]
                promoted.append(token)

        if promoted:
            self._embedding_cache_state = "dirty"
            node.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
            self._save_node(node)
            self._update_single_embedding(node_id)

        self._save_pending_keywords()
        return promoted

    def _resolve_short_code(self, query: str) -> list[str] | None:
        """Layer 3: 短代码查询路由器。先查click_log(学习的)→再查code_index(自动的)。
        返回node_ids列表或None(不是短代码)。
        """
        raw = query.strip()
        # 判断是否短代码
        if not (len(raw) <= 8 and re.fullmatch(r"[A-Za-z]+\d+[A-Za-z0-9]*", raw)):
            return None

        code_upper = raw.upper()

        # 优先: click_log (outcome-verified映射)
        if code_upper in self._click_log:
            log = self._click_log[code_upper]
            # 只用signal >= 3的(至少3次positive confirmation)
            confirmed = {
                nid: sig
                for nid, sig in log.items()
                if (
                    sig >= 3.0
                    and nid in self.nodes
                    and semantic_id_family(nid) not in _AUTHORITY_PROTECTED_FAMILIES
                )
            }
            if confirmed:
                return sorted(
                    confirmed,
                    key=lambda node_id: (-confirmed[node_id], node_id),
                )

        # 次优: code_index (自动提取)
        if code_upper in self._code_index:
            candidates = sorted(
                nid for nid in self._code_index[code_upper] if nid in self.nodes
            )
            if candidates:
                return candidates

        return None

    def _expand_query(self, task: str) -> str:
        """Query Expansion: 共现词表扩展。

        短代码也走此函数，_resolve_short_code结果作为RRF第4信号注入。
        本函数只做语义query的共现词扩展。
        """
        if self._cooccurrence is None:
            self._cooccurrence = self._build_cooccurrence()

        task_lower = task.lower()
        expansions: list[str] = []
        seen = set(task_lower.split())

        # 统计每个词出现在多少个节点里 (用于过滤泛词)
        kw_node_count: dict[str, int] = {}
        for node in self.nodes.values():
            for kw in node.activation_keywords:
                kw_lower = kw.lower()
                kw_node_count[kw_lower] = kw_node_count.get(kw_lower, 0) + 1

        for kw in sorted(self._cooccurrence):
            related = self._cooccurrence[kw]
            if kw in task_lower:
                # 只取"专有"的共现词：出现在<15个节点里的
                specific_related = [
                    r for r in related
                    if kw_node_count.get(r, 0) < 15 and r not in task_lower and r not in seen and len(r) >= 2
                ]
                # 按稀有度排序(出现越少越专有)
                specific_related.sort(
                    key=lambda value: (kw_node_count.get(value, 0), value)
                )
                for r in specific_related[:3]:
                    expansions.append(r)
                    seen.add(r)
            if len(expansions) >= 5:
                break

        if expansions:
            return task + " " + " ".join(expansions[:5])
        return task

    def _expand_query_for_route(self, task: str) -> tuple[str, dict[str, Any]]:
        """Build route query text from built-in cooccurrence plus optional adapters."""
        cooccurrence_query = self._expand_query(task)
        variants: list[dict[str, Any]] = []
        adapters: list[str] = []
        errors: list[str] = []
        try:
            from query_expander import get_default_expander

            expander = get_default_expander()
            adapters = list(getattr(expander, "active_adapters", []))
            for query, score in expander.expand(task, top_k=5):
                query = str(query or "").strip()
                if not query or query == task:
                    continue
                variants.append({"query": query, "score": round(float(score), 4), "source": "adapter"})
        except Exception as exc:
            errors.append(type(exc).__name__)

        pieces: list[str] = []
        seen: set[str] = set()
        for item in [task, cooccurrence_query, *[v["query"] for v in variants]]:
            value = str(item or "").strip()
            if value and value not in seen:
                pieces.append(value)
                seen.add(value)
        combined = " ".join(pieces)
        weighted_queries: list[dict[str, Any]] = [
            {"query": task, "weight": 1.0, "source": "original"}
        ]
        weighted_seen = {task}
        if cooccurrence_query != task and cooccurrence_query not in weighted_seen:
            weighted_queries.append(
                {
                    "query": cooccurrence_query,
                    "weight": 0.35,
                    "source": "cooccurrence",
                }
            )
            weighted_seen.add(cooccurrence_query)
        for variant in variants[:5]:
            query = str(variant["query"])
            if query in weighted_seen:
                continue
            weighted_queries.append(
                {
                    "query": query,
                    "weight": round(
                        max(0.125, min(0.25, float(variant["score"]) * 0.25)),
                        4,
                    ),
                    "source": "adapter",
                }
            )
            weighted_seen.add(query)
        meta = {
            "original_query": task,
            "expanded_query": combined,
            "cooccurrence_expanded": cooccurrence_query != task,
            "adapter_names": adapters,
            "variants": variants[:5],
            "weighted_queries": weighted_queries,
            "errors": errors,
        }
        return combined, meta

    # ── 意图分类规则 (MAGMA启发) ──
    _INTENT_RULES: dict[str, tuple[list[str], list[str]]] = {
        # intent: (trigger_keywords, boosted_prefixes)
        "health":  (["health", "healthy", "readiness", "node threshold", "stats"],
                    ["INTF-"]),
        "security": (["secret", "credential", "password", "cookie", "recovery code", "api key"],
                     ["SEC-"]),
        "status":  (["进度", "状态", "当前", "current", "status", "多少条", "多少个", "baseline"],
                    ["MOD-", "SEC-", "PROG-"]),
        "agent":   (["codex", "opus", "sonnet", "agent", "谁在", "做了什么", "session", "S5", "S6"],
                    ["HO-", "SES-", "AGT-", "PROG-"]),
        "why":     (["为什么", "why", "原因", "根因", "决策", "decision", "错误", "教训"],
                    ["DEC-", "FEE-", "ERR-"]),
        "intf":    (["intf", "函数", "方法", "签名", "table", "column", "schema", "upsert", "insert", "def ", "class ", "api/"],
                    ["INTF-"]),
        "infra":   (["ssh", "服务器", "cloud", "云端", "远程", "gpu", "autodl", "训练机", "端口", "port"],
                    ["SEC-", "MOD-"]),
        "strategy": (["战略", "定位", "竞品", "benchmark", "开源", "对比", "3can"],
                     ["STR-", "DOC-", "DEC-", "PRO-"]),
        "user":    (["昵称", "用户", "偏好", "ka", "oops", "画像", "沟通风格"],
                    ["USR-", "FEE-"]),
    }

    def _classify_intent(self, task: str) -> tuple[str, list[str]]:
        """MAGMA启发的意图分类 → 返回(intent, boosted_prefixes)。"""
        task_lower = task.lower()
        for intent, (triggers, prefixes) in self._INTENT_RULES.items():
            if any(t in task_lower for t in triggers):
                return intent, prefixes
        return "general", []

    def _score_keyword(self, node, nid: str, task_lower: str, query_tokens: set[str],
                        boosted_prefixes: list[str], is_short_code: bool) -> tuple[float, float, float, int]:
        """计算单节点的keyword/intent/tier分数。返回(kw_score, intent_score, tier_boost, exact_matches).

        v9.0 Wave 2: kw 命中不再 += 1.0 统一加权, 改 += idf(kw). 热重 kw (intf 426 节点) 降到 ~2.17,
        稀有 kw (df<10) 保持 3.0, 让 query 的精确标签命中真正有区分度.
        """
        kw_score = 0.0
        for kw in node.activation_keywords:
            if kw.lower() in task_lower:
                kw_score += self._kw_idf(kw)
        if node.name.lower() in task_lower or task_lower in node.name.lower():
            kw_score += 0.8
        for prefix in ("ERR", "SEC", "MOD", "MCP", "HO", "RUL"):
            if prefix.lower() in task_lower and nid.startswith(prefix):
                kw_score += 0.5

        intent_score = 0.0
        if boosted_prefixes and any(nid.startswith(p) for p in boosted_prefixes):
            intent_score = 1.0

        # Tier boost (keyword/desc/notes token match)
        exact_matches = 0
        for kw in node.activation_keywords:
            if kw.lower() in query_tokens:
                exact_matches += 1
        id_lower = nid.lower()
        if any(tok in id_lower and len(tok) >= 3 for tok in query_tokens):
            exact_matches += 1

        desc_lower = (node.content.description or "").lower()
        desc_matches = sum(1 for tok in query_tokens if len(tok) >= 3 and tok in desc_lower)
        notes_head = (node.content.notes or "")[:500].lower()
        notes_matches = sum(1 for tok in query_tokens if len(tok) >= 3 and tok in notes_head)

        tier_boost = 0.0
        if exact_matches > 0 or desc_matches > 0 or notes_matches > 0:
            tier_boost = min(exact_matches * 0.2, 0.6) + min(desc_matches * 0.1, 0.3) + min(notes_matches * 0.05, 0.2)
            if is_short_code:
                tier_boost += 0.5

        return kw_score, intent_score, tier_boost, exact_matches

    def _core_manifest_payload(self) -> tuple[dict[str, Any], str]:
        node = self.nodes.get(self._CORE_MEMORY_REGISTRY_NODE_ID)
        if not node:
            return {}, "missing_manifest"
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return {}, "manifest_without_extra"
        return extra, "3can-manifest"

    def _normalize_lane_map(self, value: Any) -> dict[str, list[str]]:
        if not isinstance(value, dict):
            return {}
        lanes: dict[str, list[str]] = {}
        for lane_name, node_ids in value.items():
            if not isinstance(lane_name, str) or not isinstance(node_ids, list):
                continue
            clean = [str(item) for item in node_ids if str(item).strip()]
            if clean:
                lanes[lane_name] = list(dict.fromkeys(clean))
        return lanes

    def _normalize_lane_weights(self, value: Any) -> dict[str, float]:
        weights = dict(self._DEFAULT_CORE_LANE_WEIGHTS)
        if not isinstance(value, dict):
            return weights
        for lane_name, raw_weight in value.items():
            if not isinstance(lane_name, str):
                continue
            try:
                weights[lane_name] = max(0.0, min(100.0, float(raw_weight)))
            except (TypeError, ValueError):
                continue
        return weights

    def _normalize_required_edges(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        edges: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            source = str(item.get("source") or "").strip()
            target = str(item.get("target") or "").strip()
            edge_type = str(item.get("type") or "requires").strip()
            if not source or not target or not edge_type:
                continue
            try:
                weight = float(item.get("weight", 1.0))
            except (TypeError, ValueError):
                weight = 1.0
            edges.append({
                "source": source,
                "target": target,
                "type": edge_type,
                "weight": max(0.0, min(10.0, weight)),
                "description": str(item.get("description") or "")[:160],
            })
        return edges

    def _core_required_lanes(self, task: str, manifest: dict[str, Any]) -> list[str]:
        lanes = [str(item) for item in manifest.get("required_default_lanes") or [] if str(item).strip()]
        if not lanes:
            lanes = ["user_preferences", "environment_constraints", "error_warnings"]
        if self._PRODUCT_ROUTE_TRIGGER.search(task) and "project_constitution" not in lanes:
            lanes.append("project_constitution")
        task_topics = self._core_task_topics(task)
        if "file_system" in task_topics and "project_file_system" not in lanes:
            lanes.append("project_file_system")
        return lanes

    def _core_task_topics(self, task: str) -> set[str]:
        topics = {
            topic for topic, pattern in self._CORE_TOPIC_PATTERNS.items()
            if pattern.search(task or "")
        }
        if self._PRODUCT_ROUTE_TRIGGER.search(task or ""):
            topics.add("product")
        if not topics and self._CORE_ROUTE_TRIGGER.search(task or ""):
            topics.add("3can")
        return topics

    def _parse_route_datetime(self, value: Any) -> dt.datetime | None:
        if value is None:
            return None
        if isinstance(value, dt.datetime):
            parsed = value
        elif isinstance(value, (int, float)):
            try:
                parsed = dt.datetime.fromtimestamp(float(value), tz=dt.timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        else:
            text = str(value).strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = dt.datetime.fromisoformat(text)
            except ValueError:
                return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)

    def _temporal_task_policy(self, task: str, _expanded_task: str) -> dict[str, Any]:
        # Query expansion improves retrieval recall; it must not invent temporal
        # governance intent that the caller did not express.
        text = task or ""
        matches = sorted({match.group(0).lower() for match in self._TEMPORAL_ROUTE_TRIGGER.finditer(text)})[:16]
        freshness_required = bool(self._TEMPORAL_FRESHNESS_TRIGGER.search(text))
        validity_focus = bool(self._TEMPORAL_VALIDITY_TRIGGER.search(text))
        error_focus = bool(self._TEMPORAL_ERROR_TRIGGER.search(text))
        enabled = bool(matches)
        half_life = self._TEMPORAL_FRESH_HALF_LIFE_DAYS if freshness_required else self._TEMPORAL_DEFAULT_HALF_LIFE_DAYS
        return {
            "enabled": enabled,
            "triggered_terms": matches,
            "freshness_required": freshness_required,
            "validity_focus": validity_focus,
            "error_focus": error_focus,
            "half_life_days": half_life,
            "max_boost": self._TEMPORAL_ROUTE_MAX_BOOST,
            "max_penalty": self._TEMPORAL_ROUTE_MAX_PENALTY,
        }

    def _node_temporal_fields(self, node: Node, now: dt.datetime) -> dict[str, Any]:
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        valid_from = self._parse_route_datetime(
            extra.get("valid_from") or extra.get("effective_from") or extra.get("effective_at")
        )
        valid_until = self._parse_route_datetime(
            extra.get("valid_until") or extra.get("expires_at") or extra.get("expiry_at")
        )
        invalidated_by = extra.get("invalidated_by") or extra.get("superseded_by") or extra.get("replaced_by")
        observed_at = self._parse_route_datetime(
            extra.get("last_seen_at")
            or extra.get("observed_at")
            or extra.get("updated_at")
            or node.updated_at
            or node.created_at
        )
        if observed_at is None:
            observed_at = self._parse_route_datetime(node.created_at)
        age_days = None
        if observed_at is not None:
            age_days = max(0.0, (now - observed_at).total_seconds() / 86400.0)
        return {
            "observed_at": observed_at,
            "age_days": age_days,
            "valid_from": valid_from,
            "valid_until": valid_until,
            "future_effective": bool(valid_from and valid_from > now),
            "expired": bool(valid_until and valid_until < now),
            "invalidated": bool(invalidated_by),
            "invalidated_by": str(invalidated_by) if invalidated_by else "",
        }

    def _apply_temporal_route_boost(
        self,
        rrf_scores: dict[str, float],
        temporal_policy: dict[str, Any],
        eligible_node_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        if not temporal_policy.get("enabled"):
            return {
                **temporal_policy,
                "boosted_node_count": 0,
                "penalized_node_count": 0,
                "top_boosts": [],
                "top_penalties": [],
            }

        now = dt.datetime.now(dt.timezone.utc)
        half_life = max(1.0, float(temporal_policy.get("half_life_days") or self._TEMPORAL_DEFAULT_HALF_LIFE_DAYS))
        max_boost = float(temporal_policy.get("max_boost") or self._TEMPORAL_ROUTE_MAX_BOOST)
        max_penalty = float(temporal_policy.get("max_penalty") or self._TEMPORAL_ROUTE_MAX_PENALTY)
        freshness_required = bool(temporal_policy.get("freshness_required"))
        validity_focus = bool(temporal_policy.get("validity_focus"))
        error_focus = bool(temporal_policy.get("error_focus"))
        boosts: list[dict[str, Any]] = []
        penalties: list[dict[str, Any]] = []

        for node_id in list(rrf_scores):
            if eligible_node_ids is not None and node_id not in eligible_node_ids:
                continue
            if node_id.startswith("ERR-") and not (error_focus or validity_focus):
                continue
            node = self.nodes.get(node_id)
            if not node:
                continue
            fields = self._node_temporal_fields(node, now)
            age_days = fields.get("age_days")
            boost = 0.0
            if isinstance(age_days, (int, float)):
                recency_score = 0.5 ** (float(age_days) / half_life)
                boost = max_boost * recency_score
                if boost > 0:
                    rrf_scores[node_id] = round(rrf_scores[node_id] + boost, 6)
                    boosts.append({
                        "node_id": node_id,
                        "boost": round(boost, 6),
                        "age_days": round(float(age_days), 2),
                        "observed_at": fields["observed_at"].isoformat() if fields.get("observed_at") else "",
                    })

            stale_for_current_query = freshness_required and not validity_focus and (
                fields.get("expired") or fields.get("future_effective") or fields.get("invalidated")
            )
            if stale_for_current_query:
                penalty = min(max_penalty, max_boost + boost)
                rrf_scores[node_id] = round(rrf_scores[node_id] - penalty, 6)
                penalties.append({
                    "node_id": node_id,
                    "penalty": round(penalty, 6),
                    "expired": bool(fields.get("expired")),
                    "future_effective": bool(fields.get("future_effective")),
                    "invalidated_by": fields.get("invalidated_by") or "",
                })

        boosts.sort(key=lambda item: (-float(item["boost"]), item["node_id"]))
        penalties.sort(key=lambda item: (-float(item["penalty"]), item["node_id"]))
        return {
            **temporal_policy,
            "now": now.isoformat(),
            "eligible_node_count": len(eligible_node_ids) if eligible_node_ids is not None else len(rrf_scores),
            "boosted_node_count": len(boosts),
            "penalized_node_count": len(penalties),
            "top_boosts": boosts[:12],
            "top_penalties": penalties[:12],
        }

    @staticmethod
    def _node_extra(node: Node | None) -> dict[str, Any]:
        extra = getattr(getattr(node, "content", None), "extra", None)
        return extra if isinstance(extra, dict) else {}

    def _node_project_values(self, node: Node | None) -> tuple[str, str]:
        """Project applicability projected from existing node metadata."""

        extra = self._node_extra(node)
        nested = extra.get("project_identity")
        nested = nested if isinstance(nested, dict) else {}
        applicability = extra.get("applicability")
        applicability = applicability if isinstance(applicability, dict) else {}
        error_case = extra.get("error_case")
        error_case = error_case if isinstance(error_case, dict) else {}
        project_id = (
            extra.get("project_id")
            or nested.get("project_id")
            or applicability.get("project_id")
            or error_case.get("project_id")
            or ""
        )
        namespace = (
            extra.get("project_namespace")
            or nested.get("project_namespace")
            or applicability.get("project_namespace")
            or error_case.get("project_namespace")
            or ""
        )
        return (
            str(project_id).strip().casefold(),
            str(namespace).strip().casefold(),
        )

    def _project_applicability(
        self,
        node: Node | None,
        *,
        project_id: str,
        project_namespace: str,
    ) -> str:
        """Classify existing project metadata without guessing missing scope."""

        node_project, node_namespace = self._node_project_values(node)
        extra = self._node_extra(node)
        applicability = extra.get("applicability")
        applicability = applicability if isinstance(applicability, dict) else {}
        declared_scope = str(applicability.get("scope") or "").strip().casefold()
        explicit_shared = declared_scope in {"global", "shared"}
        if explicit_shared and (node_project or node_namespace):
            return "mismatch"
        if explicit_shared:
            return "explicit_shared"

        requested_project = str(project_id or "").strip().casefold()
        requested_namespace = str(project_namespace or "").strip().casefold()
        if (
            (requested_project and node_project and requested_project != node_project)
            or (
                requested_namespace
                and node_namespace
                and requested_namespace != node_namespace
            )
        ):
            return "mismatch"
        if (
            requested_project
            and requested_namespace
            and node_project == requested_project
            and node_namespace == requested_namespace
        ):
            return "exact_project"
        return "unscoped_unknown"

    def _superseded_node_ids(self) -> set[str]:
        return {
            edge.target
            for edge in self.edges
            if self._edge_type_value(edge.type) == "supersedes"
        }

    def _node_is_superseded(
        self,
        node_id: str,
        node: Node | None,
        superseded_ids: set[str] | None = None,
    ) -> bool:
        if not node:
            return False
        if node.status == NodeStatus.deprecated:
            return True
        if node_id in (superseded_ids if superseded_ids is not None else self._superseded_node_ids()):
            return True
        extra = self._node_extra(node)
        return bool(
            extra.get("invalidated_by")
            or extra.get("superseded_by")
            or extra.get("replaced_by")
        )

    def _core_project_scoped_node_ids(self) -> set[str]:
        manifest, _ = self._core_manifest_payload()
        lanes = self._normalize_lane_map(
            manifest.get("memory_lanes") or manifest.get("lanes")
        )
        return {
            node_id
            for lane in self._PROJECT_SCOPED_CORE_LANES
            for node_id in lanes.get(lane, [])
        }

    def _current_reality_policy(
        self,
        req: RoutingRequest,
        expanded_task: str,
        *,
        explicit_error: bool,
        exact_code: bool,
    ) -> dict[str, Any]:
        # Governance intent is user-authored.  Retrieval expansion may contain
        # words such as "handoff" or "current" from old graph records and must
        # not change the current/history contract.
        text = req.task or ""
        current = bool(self._CURRENT_REALITY_TRIGGER.search(text))
        historical = bool(self._HISTORY_ROUTE_TRIGGER.search(text))
        durable_evidence = bool(self._DURABLE_EVIDENCE_TRIGGER.search(text))
        enabled = bool(
            (current or durable_evidence)
            and (not historical or durable_evidence)
            and not explicit_error
            and not exact_code
        )
        return {
            "enabled": enabled,
            "intent": (
                "durable_source_evidence"
                if enabled and durable_evidence and historical
                else "current_project_reality"
                if enabled
                else "history_or_specialized" if historical or explicit_error or exact_code else "ordinary"
            ),
            "durable_evidence_requested": durable_evidence,
            "historical_requested": historical,
            "project_id": str(req.project_id or "").casefold(),
            "project_namespace": str(req.project_namespace or "").casefold(),
            "external_verification_required": bool(
                enabled and self._EXTERNAL_TRUTH_TRIGGER.search(text)
            ),
            "sediment_families": sorted(self._CURRENT_SEDIMENT_FAMILIES),
        }

    def _apply_current_reality_policy(
        self,
        rrf_scores: dict[str, float],
        policy: dict[str, Any],
        *,
        superseded_ids: set[str] | None = None,
        scoped_core_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        requested_project = str(policy.get("project_id") or "").casefold()
        requested_namespace = str(policy.get("project_namespace") or "").casefold()
        project_scoped = bool(requested_project and requested_namespace)
        superseded_ids = (
            superseded_ids if superseded_ids is not None else self._superseded_node_ids()
        )
        scoped_core_ids = (
            scoped_core_ids
            if scoped_core_ids is not None
            else self._core_project_scoped_node_ids()
        )
        excluded_superseded: list[str] = []
        excluded_mismatch: list[str] = []
        demoted_sediment: list[str] = []
        demoted_unproven: list[str] = []
        boosted_durable: list[str] = []
        applicability_counts: Counter[str] = Counter()

        for node_id in list(rrf_scores):
            node = self.nodes.get(node_id)
            if not node:
                continue
            if (
                self._node_is_superseded(node_id, node, superseded_ids)
                and not policy.get("historical_requested")
            ):
                excluded_superseded.append(node_id)
                rrf_scores.pop(node_id, None)
                continue
            if not policy.get("enabled") and not project_scoped:
                continue

            applicability = self._project_applicability(
                node,
                project_id=requested_project,
                project_namespace=requested_namespace,
            )
            applicability_counts[applicability] += 1
            if project_scoped and applicability == "mismatch":
                excluded_mismatch.append(node_id)
                rrf_scores.pop(node_id, None)
                continue
            if not policy.get("enabled"):
                continue

            family = semantic_id_family(node_id)
            if family in self._CURRENT_SEDIMENT_FAMILIES:
                rrf_scores[node_id] = round(
                    rrf_scores[node_id] - self._CURRENT_SEDIMENT_PENALTY,
                    6,
                )
                demoted_sediment.append(node_id)
            elif (
                node_id in scoped_core_ids
                and requested_project
                and applicability != "exact_project"
            ):
                rrf_scores[node_id] = round(
                    rrf_scores[node_id] - self._CURRENT_UNPROVEN_CORE_PENALTY,
                    6,
                )
                demoted_unproven.append(node_id)
            elif family in self._CURRENT_DURABLE_FAMILIES:
                boost = (
                    self._CURRENT_DURABLE_BOOST
                    if applicability == "exact_project"
                    else self._CURRENT_DURABLE_BOOST / 2
                    if applicability == "explicit_shared"
                    else 0.0
                )
                if boost:
                    rrf_scores[node_id] = round(rrf_scores[node_id] + boost, 6)
                    boosted_durable.append(node_id)

        return {
            **policy,
            "excluded_superseded_node_ids": sorted(excluded_superseded)[:20],
            "excluded_project_mismatch_node_ids": sorted(excluded_mismatch)[:20],
            "demoted_sediment_node_ids": sorted(demoted_sediment)[:20],
            "demoted_unproven_core_node_ids": sorted(demoted_unproven)[:20],
            "boosted_durable_node_ids": sorted(boosted_durable)[:20],
            "excluded_superseded_count": len(excluded_superseded),
            "excluded_project_mismatch_count": len(excluded_mismatch),
            "demoted_sediment_count": len(demoted_sediment),
            "demoted_unproven_core_count": len(demoted_unproven),
            "boosted_durable_count": len(boosted_durable),
            "project_applicability_counts": dict(applicability_counts),
            "project_applicability_order": [
                "exact_project",
                "explicit_shared",
                "unscoped_unknown",
                "mismatch_excluded",
            ],
        }

    def _prioritize_current_reality(
        self,
        node_ids: list[str],
        policy: dict[str, Any],
    ) -> list[str]:
        """Stable policy ordering after an optional cross-encoder rerank."""

        if not policy.get("enabled"):
            return node_ids
        requested_project = str(policy.get("project_id") or "").casefold()
        requested_namespace = str(policy.get("project_namespace") or "").casefold()

        def group(node_id: str) -> int:
            family = semantic_id_family(node_id)
            if family in self._CURRENT_SEDIMENT_FAMILIES:
                return 3
            node = self.nodes.get(node_id)
            applicability = self._project_applicability(
                node,
                project_id=requested_project,
                project_namespace=requested_namespace,
            )
            if applicability == "exact_project":
                return 0
            if applicability == "explicit_shared":
                return 1
            return 2

        return sorted(node_ids, key=group)

    def _current_reality_node_allowed(
        self,
        node_id: str,
        policy: dict[str, Any],
        *,
        superseded_ids: set[str] | None = None,
    ) -> bool:
        node = self.nodes.get(node_id)
        if not node:
            return False
        if (
            self._node_is_superseded(node_id, node, superseded_ids)
            and not policy.get("historical_requested")
        ):
            return False
        requested_project = str(policy.get("project_id") or "").casefold()
        requested_namespace = str(policy.get("project_namespace") or "").casefold()
        if requested_project and requested_namespace and self._project_applicability(
            node,
            project_id=requested_project,
            project_namespace=requested_namespace,
        ) == "mismatch":
            return False
        return True

    def _core_node_topics(self, node_id: str, node: Node | None) -> set[str]:
        if node_id in self._CORE_NODE_TOPIC_HINTS:
            return set(self._CORE_NODE_TOPIC_HINTS[node_id])
        topics: set[str] = set()
        if node:
            text = " ".join([
                node_id,
                node.name or "",
                " ".join(node.activation_keywords[:12]),
                (node.content.description or "")[:400],
                (node.content.current_state or "")[:240],
            ])
            for topic, pattern in self._CORE_TOPIC_PATTERNS.items():
                if pattern.search(text):
                    topics.add(topic)
            if self._PRODUCT_ROUTE_TRIGGER.search(text):
                topics.add("product")
        return topics

    def _core_task_terms(self, task: str) -> set[str]:
        return {
            term
            for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", (task or "").lower())
            if term not in self._CORE_DYNAMIC_GENERIC_TERMS
            and term not in {"the", "and", "for", "with", "should", "not"}
        }

    def _core_high_signal_terms(self, task: str) -> set[str]:
        return {
            term
            for term in self._core_task_terms(task)
            if any(ch.isdigit() for ch in term) or "-" in term or "_" in term
        }

    def _core_repeated_error_requested(self, task: str, task_topics: set[str]) -> bool:
        del task_topics  # kept in the signature for compatibility with callers.
        raw_task = task or ""
        if is_error_intent(raw_task):
            return True
        if self._OPERATIONAL_ERROR_STRONG_TRIGGER.search(raw_task):
            return True
        return bool(
            self._OPERATIONAL_ERROR_REMEDY_TRIGGER.search(raw_task)
            and self._OPERATIONAL_ERROR_CORRELATED_SYMPTOM.search(raw_task)
        )

    @staticmethod
    def _canonical_error_identity_value(value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().casefold())

    def _canonical_error_identity_from_task(self, task: str) -> dict[str, str]:
        aliases = {
            "project": "project_id",
            "project_id": "project_id",
            "project_identity": "project_id",
            "operation": "operation",
            "operation_class": "operation",
            "component": "component",
            "error": "error_type",
            "error_type": "error_type",
        }
        fields: dict[str, str] = {}
        for raw_key, raw_value in self._CANONICAL_ERROR_IDENTITY_FIELD.findall(task or ""):
            key = aliases.get(raw_key.strip().lower())
            value = self._canonical_error_identity_value(raw_value)
            if key and value:
                fields[key] = value
        return fields

    def _error_case_identity(self, node: Node | None) -> dict[str, str]:
        if not node:
            return {}
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return {}
        canonical = extra.get("error_case")
        canonical = canonical if isinstance(canonical, dict) else {}
        applicability = canonical.get("applicability")
        applicability = applicability if isinstance(applicability, dict) else {}
        project = (
            extra.get("project_identity")
            or extra.get("project_id")
            or canonical.get("project_id")
            or applicability.get("project_id")
            or ""
        )
        if isinstance(project, dict):
            project = project.get("project_id") or project.get("project_name") or ""
        identity = {
            "project_id": self._canonical_error_identity_value(project or "local-project"),
            "operation": self._canonical_error_identity_value(
                extra.get("operation_class")
                or extra.get("operation")
                or canonical.get("operation")
                or applicability.get("operation")
                or ""
            ),
            "component": self._canonical_error_identity_value(
                extra.get("component")
                or canonical.get("component")
                or applicability.get("component")
                or ""
            ),
            "error_type": self._canonical_error_identity_value(
                extra.get("error_type")
                or canonical.get("error_type")
                or applicability.get("error_type")
                or extra.get("error")
                or ""
            ),
        }
        return identity if all(identity.values()) else {}

    def _is_error_case_node(self, node_id: str, node: Node | None) -> bool:
        folded_id = str(node_id or "").casefold()
        if (
            folded_id.startswith("err-case-")
            or folded_id.startswith("err-repeated-")
            or folded_id.startswith("errcase-")
            or folded_id.startswith("err-")
        ):
            return True
        if not node:
            return False
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return False
        if self._is_historical_error_node(node):
            return True
        kind = str(extra.get("kind") or extra.get("schema") or "").strip().casefold()
        return (
            kind in {"error_case", "legacy_error_case", "3can.error-case/v1"}
            or bool(extra.get("loop_signature") or extra.get("fingerprint"))
            and "occurrence_count" in extra
        )

    def _is_historical_error_node(self, node: Node | None) -> bool:
        """Return whether a node carries the explicit historical error-lane contract."""

        if not node:
            return False
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return False
        return (
            str(extra.get("knowledge_tier") or "").strip().casefold() == "historical"
            and str(extra.get("route_visibility") or "").strip().casefold()
            == "explicit_error_only"
        )

    def _historical_error_searchable(self, node: Node | None) -> bool:
        if not self._is_historical_error_node(node):
            return False
        extra = getattr(node.content, "extra", None) or {}
        return extra.get("searchable") is True

    def _is_error_artifact_node(self, node_id: str, node: Node | None) -> bool:
        folded_id = str(node_id or "").casefold()
        if folded_id.startswith(("err-", "errcase-", "fix-", "evd-")):
            return True
        if self._is_error_case_node(node_id, node):
            return True
        if not node:
            return False
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return False
        kind = str(extra.get("kind") or "").strip().casefold()
        schema = str(extra.get("schema_version") or extra.get("schema") or "").strip().casefold()
        return (
            kind in {"error_case", "legacy_error_case", "error_resolution", "resolution_evidence"}
            or schema.startswith("3can.error-case/")
            or schema.startswith("3can.error-resolution/")
            or schema.startswith("3can.resolution-evidence/")
        )

    def _error_case_exact_match_kind(self, task: str, node_id: str, node: Node | None) -> str:
        raw_task = task or ""
        if re.search(
            rf"(?<![\w.:\-]){re.escape(node_id)}(?![\w.:\-])",
            raw_task,
            re.IGNORECASE,
        ):
            return "case_id"
        raw_task = raw_task.casefold()
        extra = getattr(node.content, "extra", None) or {} if node else {}
        if isinstance(extra, dict):
            fingerprint = str(
                extra.get("fingerprint") or extra.get("loop_signature") or ""
            ).strip()
            if fingerprint and fingerprint.casefold() in raw_task:
                return "fingerprint"
        requested = self._canonical_error_identity_from_task(task)
        if set(requested) != {"project_id", "operation", "component", "error_type"}:
            return ""
        identity = self._error_case_identity(node)
        if identity and identity == requested:
            return "canonical_identity"
        if identity:
            try:
                requested_fingerprint = deterministic_fingerprint(
                    project_id=requested["project_id"],
                    operation=requested["operation"],
                    component=requested["component"],
                    error_type=requested["error_type"],
                )
                stored_fingerprint = str(
                    (extra if isinstance(extra, dict) else {}).get("fingerprint")
                    or (extra if isinstance(extra, dict) else {}).get("loop_signature")
                    or ""
                )
                if stored_fingerprint and stored_fingerprint == requested_fingerprint:
                    return "canonical_fingerprint"
            except ValueError:
                return ""
        return ""

    def _error_case_status(self, node: Node | None) -> str:
        if not node:
            return ""
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return ""
        raw = (
            extra.get("case_status")
            or extra.get("error_case_status")
            or extra.get("gate_status")
            or (
                extra.get("error_case", {}).get("state")
                if isinstance(extra.get("error_case"), dict)
                else ""
            )
            or ""
        )
        value = str(raw).strip().lower()
        if value.startswith("resolved"):
            return "resolved"
        if value.startswith("diagnosed"):
            return "diagnosed"
        if value.startswith("mitigated"):
            return "mitigated"
        if value.startswith("regressed"):
            return "regressed"
        if value.startswith("superseded"):
            return "superseded"
        if value in {"observed", "known"}:
            return value
        return value

    def _error_case_has_verified_solution(self, node: Node | None) -> bool:
        if not node:
            return False
        extra = getattr(node.content, "extra", None) or {}
        if not isinstance(extra, dict):
            return False
        canonical = extra.get("error_case")
        canonical = canonical if isinstance(canonical, dict) else {}
        active_resolution = canonical.get("active_resolution")
        active_resolution = active_resolution if isinstance(active_resolution, dict) else {}
        evidence = extra.get("verification_evidence") or extra.get("evidence") or []
        if not evidence:
            evidence = active_resolution.get("evidence") or []
        if not isinstance(evidence, list):
            return False
        typed_verified_receipts = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            metadata = item.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            reference = str(item.get("ref") or item.get("reference") or "").strip()
            verifier = str(item.get("verifier") or metadata.get("verifier") or "").strip()
            integrity_ref = str(
                item.get("digest")
                or metadata.get("digest")
                or ""
            ).strip()
            verification_status = str(
                item.get("verification_status")
                or metadata.get("verification_status")
                or ""
            ).strip().casefold()
            if (
                str(item.get("kind") or "").strip()
                and reference
                and verifier
                and integrity_ref
                and item.get("verified") is True
                and verification_status == "signed_attestation_verified"
            ):
                typed_verified_receipts.append(item)
        structurally_verified = bool(
            self._error_case_status(node) == "resolved"
            and (
                extra.get("solution_summary")
                or extra.get("resolution")
                or active_resolution.get("solution_summary")
            )
            and typed_verified_receipts
        )
        if not structurally_verified:
            return False
        solution_id = str(
            extra.get("current_resolution_id")
            or active_resolution.get("resolution_id")
            or ""
        ).strip()
        return self._verified_solution_node_for_case(node.id, solution_id)

    def _verified_solution_node_for_case(
        self,
        case_id: str,
        solution_id: str,
    ) -> bool:
        return self._verified_solution_bundle_for_case(
            case_id,
            solution_id,
        ) is not None

    def _verified_solution_bundle_for_case(
        self,
        case_id: str,
        solution_id: str,
    ) -> dict[str, Any] | None:
        """Return an atomic ERR/FIX/EVD bundle only after graph verification."""

        if not solution_id.casefold().startswith("fix-"):
            return None
        solution = self.nodes.get(solution_id)
        if not solution:
            return None
        extra = getattr(solution.content, "extra", None) or {}
        if (
            not isinstance(extra, dict)
            or str(extra.get("kind") or "").strip().casefold() != "error_resolution"
            or str(extra.get("error_id") or "").strip() != case_id
        ):
            return None
        evidence_id = str(extra.get("evidence_id") or "").strip()
        if not evidence_id.casefold().startswith("evd-"):
            return None
        evidence = self.nodes.get(evidence_id)
        if self._node_is_superseded(solution_id, solution) or self._node_is_superseded(
            evidence_id,
            evidence,
        ):
            return None
        evidence_extra = getattr(evidence.content, "extra", None) or {} if evidence else {}
        if (
            not isinstance(evidence_extra, dict)
            or str(evidence_extra.get("kind") or "").strip().casefold()
            != "resolution_evidence"
            or str(evidence_extra.get("error_id") or "").strip() != case_id
            or str(evidence_extra.get("resolution_id") or "").strip() != solution_id
            or not str(evidence_extra.get("verified_at") or "").strip()
            or not str(evidence_extra.get("verified_by") or "").strip()
        ):
            return None
        evidence_receipts = evidence_extra.get("evidence")
        if not isinstance(evidence_receipts, list):
            return None
        signed_receipts = []
        for receipt in evidence_receipts:
            if not isinstance(receipt, dict):
                continue
            metadata = receipt.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            reference = str(
                receipt.get("ref") or receipt.get("reference") or ""
            ).strip()
            verifier = str(
                receipt.get("verifier") or metadata.get("verifier") or ""
            ).strip()
            digest = str(
                receipt.get("digest") or metadata.get("digest") or ""
            ).strip()
            verification_status = str(
                receipt.get("verification_status")
                or metadata.get("verification_status")
                or ""
            ).strip().casefold()
            if (
                str(receipt.get("kind") or "").strip()
                and reference
                and verifier
                and digest
                and receipt.get("verified") is True
                and verification_status == "signed_attestation_verified"
            ):
                signed_receipts.append(receipt)
        if not signed_receipts:
            return None
        edge_keys = {self._edge_key(edge) for edge in self.edges}
        if (
            (solution_id, case_id, "resolves") not in edge_keys
            or (solution_id, evidence_id, "verified_by") not in edge_keys
        ):
            return None
        return {
            "case_id": case_id,
            "resolution_id": solution_id,
            "evidence_id": evidence_id,
            "required_node_ids": [case_id, solution_id, evidence_id],
        }

    def _error_node_allowed_for_route(self, node_id: str, node: Node | None, *, explicit_error: bool) -> bool:
        folded_id = str(node_id or "").casefold()
        if self._is_historical_error_node(node):
            # Historical incidents are semantic evidence, never ambient route
            # context. ``route_blocking=false`` is enforced structurally by
            # downgrading their registry ``requires`` edges in the offline
            # migration; this gate governs visibility only.
            return explicit_error and self._historical_error_searchable(node)
        is_error_case = self._is_error_case_node(node_id, node)
        is_error_artifact = self._is_error_artifact_node(node_id, node)
        if not explicit_error:
            # Legacy ERR lesson nodes are part of the same historical-noise
            # problem as canonical ErrorCases.  They remain directly
            # retrievable, but never compete in an ordinary route.
            return not is_error_artifact
        # FIX/EVD records never compete as free-standing route candidates.
        # A FIX is attached only after the owning ErrorCase and its evidence
        # graph have both been verified.
        if folded_id.startswith(("fix-", "evd-")):
            return False
        if not is_error_case:
            return True
        # Superseded raw cases stay available through direct retrieval but do not
        # compete in normal error recall. Canonical resolved cases remain useful
        # because they carry the solution future sessions need.
        return self._error_case_status(node) != "superseded"

    def _rank_error_case_ids_for_cap(
        self,
        task: str,
        rrf_scores: dict[str, float],
    ) -> list[str]:
        """Rank canonical cases before historical recall, preserving direct lookup."""

        ranked = sorted(rrf_scores, key=lambda item: (-rrf_scores[item], item))

        def ranking_key(node_id: str) -> tuple[int, float, int, str]:
            node = self.nodes.get(node_id)
            match_kind = self._error_case_exact_match_kind(task, node_id, node)
            if match_kind in {"case_id", "fingerprint"}:
                return (0, -rrf_scores[node_id], 0, node_id)
            historical = self._is_historical_error_node(node)
            # Canonical cases win near-ties, but the boost is deliberately
            # bounded: a strongly matching historical incident must still be
            # able to enter the three-case semantic recall cap.
            adjusted_score = rrf_scores[node_id] + (
                0.0 if historical else self._CANONICAL_ERROR_CASE_CAP_BOOST
            )
            return (1, -adjusted_score, 1 if historical else 0, node_id)

        return sorted(
            (
                node_id
                for node_id in ranked
                if self._is_error_case_node(node_id, self.nodes.get(node_id))
            ),
            key=ranking_key,
        )

    def _limit_repeated_error_results(self, node_ids: list[str]) -> list[str]:
        selected: list[str] = []
        error_case_count = 0
        for node_id in node_ids:
            if (
                str(node_id or "").casefold().startswith(("err-", "errcase-"))
                or self._is_error_case_node(node_id, self.nodes.get(node_id))
            ):
                if error_case_count >= self._CORE_REPEATED_ERR_MAX_PER_ROUTE:
                    continue
                error_case_count += 1
            selected.append(node_id)
        return selected

    def _apply_verified_solution_route_boost(
        self,
        task: str,
        rrf_scores: dict[str, float],
        *,
        explicit_error: bool,
    ) -> dict[str, Any]:
        """Prioritize only verified solutions with an exact applicability proof."""

        if not explicit_error:
            return {
                "enabled": False,
                "reason": "ordinary_route",
                "boosted_case_ids": [],
            }
        matches: list[tuple[str, str]] = []
        for node_id, node in self.nodes.items():
            if (
                node_id not in rrf_scores
                or not self._is_error_case_node(node_id, node)
                or not self._error_case_has_verified_solution(node)
            ):
                continue
            match_kind = self._error_case_exact_match_kind(task, node_id, node)
            if match_kind:
                matches.append((node_id, match_kind))
        if not matches:
            return {
                "enabled": True,
                "reason": "no_exact_verified_solution",
                "boosted_case_ids": [],
            }

        current_max = max(rrf_scores.values(), default=0.0)
        matches.sort(key=lambda item: (-rrf_scores.get(item[0], 0.0), item[0]))
        for offset, (node_id, _match_kind) in enumerate(matches):
            rrf_scores[node_id] = round(current_max + 1.0 - offset * 0.000001, 6)
        return {
            "enabled": True,
            "reason": "exact_verified_solution",
            "boosted_case_ids": [node_id for node_id, _match_kind in matches],
            "match_kinds": {
                node_id: match_kind for node_id, match_kind in matches
            },
        }

    def _apply_exact_error_case_route_boost(
        self,
        task: str,
        rrf_scores: dict[str, float],
        *,
        explicit_error: bool,
    ) -> dict[str, Any]:
        """Keep an explicitly identified ErrorCase retrievable before resolution."""

        if not explicit_error:
            return {
                "enabled": False,
                "reason": "ordinary_route",
                "boosted_case_ids": [],
            }
        matches: list[tuple[str, str]] = []
        for node_id, node in self.nodes.items():
            if node_id not in rrf_scores or not self._is_error_case_node(node_id, node):
                continue
            match_kind = self._error_case_exact_match_kind(task, node_id, node)
            if match_kind:
                matches.append((node_id, match_kind))
        if not matches:
            return {
                "enabled": True,
                "reason": "no_exact_error_case",
                "boosted_case_ids": [],
            }

        match_priority = {
            "case_id": 4,
            "fingerprint": 3,
            "canonical_identity": 2,
            "canonical_fingerprint": 1,
        }
        current_max = max(rrf_scores.values(), default=0.0)
        matches.sort(
            key=lambda item: (
                -match_priority.get(item[1], 0),
                -rrf_scores.get(item[0], 0.0),
                item[0],
            )
        )
        for offset, (node_id, _match_kind) in enumerate(matches):
            rrf_scores[node_id] = round(current_max + 1.0 - offset * 0.000001, 6)
        return {
            "enabled": True,
            "reason": "exact_error_case",
            "boosted_case_ids": [node_id for node_id, _match_kind in matches],
            "match_kinds": {
                node_id: match_kind for node_id, match_kind in matches
            },
        }

    def _attach_verified_solution_nodes(
        self,
        selected_ids: list[str],
        scores: dict[str, float],
        rrf_scores: dict[str, float],
        *,
        prioritized_case_ids: list[str],
        max_nodes: int,
        protected_case_ids: list[str] | None = None,
    ) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
        """Attach complete ERR/FIX/EVD bundles without ever returning an orphan."""

        requested_max = max(0, int(max_nodes or 0))
        selected = list(dict.fromkeys(selected_ids))
        attached_solutions: list[str] = []
        attached_evidence: list[str] = []
        bundles: list[dict[str, Any]] = []
        priority_set = set(prioritized_case_ids) | set(protected_case_ids or [])
        for case_id in prioritized_case_ids:
            if case_id not in selected:
                continue
            case = self.nodes.get(case_id)
            extra = getattr(case.content, "extra", None) or {} if case else {}
            candidates: list[str] = []
            if isinstance(extra, dict):
                current_resolution = str(extra.get("current_resolution_id") or "").strip()
                canonical = extra.get("error_case")
                canonical = canonical if isinstance(canonical, dict) else {}
                active_resolution = canonical.get("active_resolution")
                active_resolution = active_resolution if isinstance(active_resolution, dict) else {}
                current_resolution = (
                    current_resolution
                    or str(active_resolution.get("resolution_id") or "").strip()
                )
                if current_resolution:
                    candidates.append(current_resolution)
            candidates.extend(
                edge.source
                for edge in self.edges
                if self._edge_type_value(edge.type) == "resolves"
                and edge.target == case_id
            )
            for solution_id in dict.fromkeys(candidates):
                bundle = self._verified_solution_bundle_for_case(
                    case_id,
                    solution_id,
                )
                if not bundle:
                    continue
                evidence_id = str(bundle["evidence_id"])
                bundle_node_ids = [solution_id, evidence_id]
                if all(node_id in selected for node_id in bundle_node_ids):
                    attached_solutions.append(solution_id)
                    attached_evidence.append(evidence_id)
                    bundles.append({
                        **bundle,
                        "selection_status": "complete",
                        "missing_node_ids": [],
                    })
                    break

                # Rebuild both bundle members together. If the route budget
                # cannot fit both, remove any pre-existing orphan and attach
                # neither.
                without_bundle = [
                    node_id for node_id in selected if node_id not in bundle_node_ids
                ]
                drop_count = (
                    max(0, len(without_bundle) + len(bundle_node_ids) - requested_max)
                    if requested_max
                    else 0
                )
                droppable = [
                    node_id
                    for node_id in reversed(without_bundle)
                    if node_id not in priority_set
                    and node_id not in attached_solutions
                    and node_id not in attached_evidence
                ]
                if len(droppable) < drop_count:
                    selected = without_bundle
                    for node_id in bundle_node_ids:
                        scores.pop(node_id, None)
                    bundles.append({
                        **bundle,
                        "selection_status": "partial",
                        "missing_node_ids": list(bundle_node_ids),
                    })
                    break

                dropped = set(droppable[:drop_count])
                for node_id in dropped | set(bundle_node_ids):
                    scores.pop(node_id, None)
                selected = [
                    node_id for node_id in without_bundle if node_id not in dropped
                ]
                case_index = selected.index(case_id)
                selected.insert(case_index + 1, solution_id)
                selected.insert(case_index + 2, evidence_id)
                scores[solution_id] = max(
                    rrf_scores.get(solution_id, 0.0),
                    scores.get(case_id, 0.0) - 0.000001,
                )
                scores[evidence_id] = max(
                    rrf_scores.get(evidence_id, 0.0),
                    scores.get(case_id, 0.0) - 0.000002,
                )
                attached_solutions.append(solution_id)
                attached_evidence.append(evidence_id)
                bundles.append({
                    **bundle,
                    "selection_status": "complete",
                    "missing_node_ids": [],
                })
                break
        if requested_max:
            selected = selected[:requested_max]
        return selected, attached_solutions, attached_evidence, bundles

    def _core_node_text_for_terms(self, node_id: str, node: Node | None) -> str:
        if not node:
            return node_id.lower()
        return " ".join([
            node_id,
            node.name or "",
            " ".join(node.activation_keywords[:24]),
            (node.content.description or "")[:400],
            (node.content.current_state or "")[:400],
            (node.content.notes or "")[:500],
        ]).lower()

    def _core_node_relevant_to_task(
        self,
        node_id: str,
        node: Node | None,
        lane: str,
        task_topics: set[str],
    ) -> bool:
        if lane == "project_constitution":
            return "product" in task_topics
        if lane == "project_file_system":
            return "file_system" in task_topics
        topics = self._core_node_topics(node_id, node)
        if not topics:
            return True
        return bool(topics & task_topics)

    def _core_lane_node_sort_key(
        self,
        node_id: str,
        lane: str,
        task: str,
        task_topics: set[str],
        *,
        hot_edge_counts: dict[str, int],
    ) -> tuple[int, int, int, float, int, int, str]:
        node = self.nodes.get(node_id)
        topics = self._core_node_topics(node_id, node)
        node_text = self._core_node_text_for_terms(node_id, node)
        task_terms = self._core_task_terms(task)
        high_signal_terms = self._core_high_signal_terms(task)
        topic_hits = len(topics & task_topics)
        high_signal_hits = sum(1 for term in high_signal_terms if term in node_text)
        term_hits = sum(1 for term in task_terms if term in node_text)
        priority_weight = self._NODE_EXEC_PRIORITY.get(self._node_priority_value(node), 50.0) if node else 0.0
        activation = int(node.activation_count or 0) if node else 0
        edge_count = int(hot_edge_counts.get(node_id, 0))
        stable_bias = (
            1
            if lane == "error_warnings"
            and not self._is_error_artifact_node(node_id, node)
            else 0
        )
        return (-stable_bias, -topic_hits, -high_signal_hits, -term_hits, -priority_weight, -activation - edge_count, node_id)

    def _limit_core_lane_nodes(
        self,
        lane: str,
        node_ids: list[str],
        task: str,
        task_topics: set[str],
        *,
        hot_edge_counts: dict[str, int],
    ) -> list[str]:
        ordered = sorted(
            node_ids,
            key=lambda node_id: self._core_lane_node_sort_key(
                node_id,
                lane,
                task,
                task_topics,
                hot_edge_counts=hot_edge_counts,
            ),
        )
        if lane != "error_warnings":
            return ordered

        explicit_error = self._core_repeated_error_requested(task, task_topics)
        stable = [
            node_id
            for node_id in ordered
            if not self._is_error_artifact_node(node_id, self.nodes.get(node_id))
        ]
        repeated = [
            node_id
            for node_id in ordered
            if explicit_error
            and self._is_error_case_node(node_id, self.nodes.get(node_id))
        ][: self._CORE_REPEATED_ERR_MAX_PER_ROUTE]
        remaining = max(0, self._CORE_ERROR_WARNING_MAX_PER_ROUTE - len(stable))
        return stable[: self._CORE_ERROR_WARNING_MAX_PER_ROUTE] + repeated[:remaining]

    def _select_core_lane_nodes(
        self,
        lane: str,
        node_ids: list[str],
        task: str,
        task_topics: set[str],
        *,
        hot_edge_counts: dict[str, int],
    ) -> list[str]:
        candidate_ids = list(node_ids) or list(self._CORE_LANE_FALLBACKS.get(lane, []))
        existing = [node_id for node_id in candidate_ids if node_id in self.nodes]
        selected = [
            node_id for node_id in existing
            if self._core_node_relevant_to_task(node_id, self.nodes.get(node_id), lane, task_topics)
        ]
        if selected:
            return self._limit_core_lane_nodes(
                lane,
                selected,
                task,
                task_topics,
                hot_edge_counts=hot_edge_counts,
            )
        fallback = [
            node_id for node_id in self._CORE_LANE_FALLBACKS.get(lane, [])
            if node_id in existing
        ]
        fallback_selected = fallback[:1] if fallback else existing[:1]
        return self._limit_core_lane_nodes(
            lane,
            fallback_selected,
            task,
            task_topics,
            hot_edge_counts=hot_edge_counts,
        )

    def _core_node_applicability(
        self,
        node_id: str,
        lane: str,
        *,
        project_id: str,
        project_namespace: str,
        superseded_ids: set[str],
    ) -> tuple[bool, str]:
        node = self.nodes.get(node_id)
        if not node:
            return False, "missing"
        if self._node_is_superseded(node_id, node, superseded_ids):
            return False, "deprecated_or_superseded"
        if lane not in self._PROJECT_SCOPED_CORE_LANES:
            return True, "global_or_query_scoped"

        node_project, node_namespace = self._node_project_values(node)
        requested_project = str(project_id or "").strip().casefold()
        requested_namespace = str(project_namespace or "").strip().casefold()
        if not requested_project or not requested_namespace:
            return False, "project_applicability_unproven"
        if not node_project:
            return False, "project_applicability_unproven"
        if not node_namespace:
            return False, "project_namespace_unproven"
        if node_project != requested_project:
            return False, "project_mismatch"
        if node_namespace != requested_namespace:
            return False, "project_namespace_mismatch"
        return True, "project_match"

    def _core_dynamic_edge_node_relevant(
        self,
        node_id: str,
        node: Node | None,
        task: str,
        task_topics: set[str],
    ) -> bool:
        if (
            self._is_error_case_node(node_id, node)
            and not self._core_repeated_error_requested(task, task_topics)
        ):
            return False
        task_terms = self._core_task_terms(task)
        high_signal_terms = self._core_high_signal_terms(task)
        if not high_signal_terms:
            return False
        if not task_terms or not node:
            return False
        node_text = self._core_node_text_for_terms(node_id, node)
        return any(term in node_text for term in high_signal_terms) and sum(1 for term in task_terms if term in node_text) >= 2

    def _augment_core_lanes_from_edges(
        self,
        lanes: dict[str, list[str]],
        task: str,
        task_topics: set[str],
    ) -> dict[str, list[str]]:
        augmented = {lane: list(node_ids) for lane, node_ids in lanes.items()}
        error_ids = augmented.setdefault("error_warnings", [])
        seen = set(error_ids)
        for edge in self.edges:
            edge_type = str(getattr(edge.type, "value", edge.type))
            if edge.source != self._CORE_MEMORY_REGISTRY_NODE_ID or edge_type != "requires":
                continue
            node_id = str(edge.target or "")
            if not node_id.startswith("ERR-") or node_id in seen:
                continue
            node = self.nodes.get(node_id)
            if not node:
                continue
            if self._core_dynamic_edge_node_relevant(node_id, node, task, task_topics):
                error_ids.append(node_id)
                seen.add(node_id)
        return augmented

    def _core_required_edge_relevant(
        self,
        edge: dict[str, Any],
        selected_core_ids: set[str],
        task_topics: set[str],
    ) -> bool:
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        if target in selected_core_ids:
            return True
        if source in selected_core_ids and source != self._CORE_MEMORY_REGISTRY_NODE_ID:
            return True
        for node_id in (source, target):
            if node_id == self._CORE_MEMORY_REGISTRY_NODE_ID:
                continue
            node = self.nodes.get(node_id)
            if node and self._core_node_relevant_to_task(node_id, node, "edge_contract", task_topics):
                return True
        return False

    def _node_status_value(self, node: Node) -> str:
        return str(getattr(node.status, "value", node.status))

    def _node_priority_value(self, node: Node) -> str:
        return str(getattr(node.priority, "value", node.priority))

    def _node_type_value(self, node: Node) -> str:
        return str(getattr(node.type, "value", node.type))

    def _node_is_hot_route_candidate(
        self,
        node_id: str,
        superseded_ids: set[str] | None = None,
    ) -> bool:
        nodes = getattr(self, "nodes", None)
        if nodes is None:
            # Some callers score a supplied edge/rank fixture without loading
            # node storage.  There is no historical/deprecated evidence to
            # filter in that mode.
            return True
        node = nodes.get(node_id)
        if not node or node.status in {NodeStatus.dormant, NodeStatus.archived}:
            return False
        if self._node_is_superseded(node_id, node, superseded_ids):
            return False
        return self._error_node_allowed_for_route(
            node_id,
            node,
            explicit_error=False,
        )

    def _hot_edge_counts(self, superseded_ids: set[str]) -> dict[str, int]:
        counts: Counter[str] = Counter()
        for edge in self.edges:
            if not (
                self._node_is_hot_route_candidate(edge.source, superseded_ids)
                and self._node_is_hot_route_candidate(edge.target, superseded_ids)
            ):
                continue
            counts[edge.source] += 1
            if edge.target != edge.source:
                counts[edge.target] += 1
        return dict(counts)

    def _core_node_execution_weight(
        self,
        node: Node,
        lanes: list[str],
        lane_weights: dict[str, float],
        *,
        hot_edge_counts: dict[str, int],
    ) -> float:
        lane_base = max((lane_weights.get(lane, 50.0) for lane in lanes), default=50.0)
        priority_weight = self._NODE_EXEC_PRIORITY.get(self._node_priority_value(node), 50.0)
        type_bonus = self._NODE_EXEC_TYPE_BONUS.get(self._node_type_value(node), 0.0)
        heat = min(int(node.activation_count or 0) * 1.5, 8.0)
        edge_bonus = min(int(hot_edge_counts.get(node.id, 0)) * 2.0, 8.0)
        raw = lane_base * 0.55 + priority_weight * 0.35 + type_bonus + heat + edge_bonus
        factor = self._NODE_EXEC_STATUS_FACTOR.get(self._node_status_value(node), 0.5)
        return round(max(0.0, min(100.0, raw * factor)), 2)

    def _edge_as_route_meta(self, edge: Edge) -> dict[str, Any]:
        return {
            "source": edge.source,
            "target": edge.target,
            "type": self._edge_type_value(edge.type),
            "weight": edge.weight,
            "description": edge.description[:160],
        }

    def _edge_route_boost_value(self, edge: Edge, *, reverse: bool = False) -> float:
        edge_type = self._edge_type_value(edge.type)
        type_factor = self._EDGE_ROUTE_TYPE_FACTOR.get(edge_type, 0.35)
        reverse_factor = self._EDGE_ROUTE_REVERSE_FACTOR.get(edge_type, 0.50) if reverse else 1.0
        try:
            weight = max(0.0, float(edge.weight or 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        raw = self._EDGE_ROUTE_BASE * weight * type_factor * reverse_factor
        return round(min(self._EDGE_ROUTE_PER_EDGE_CAP, raw), 6)

    def _apply_graph_traversal_boost(
        self,
        rrf_scores: dict[str, float],
        top_set: set[str],
        *,
        include_historical: bool = False,
        superseded_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        boost_by_node: dict[str, float] = defaultdict(float)
        edge_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        type_counts: Counter[str] = Counter()
        direction_counts: Counter[str] = Counter()
        anchor_floor = min(
            (float(rrf_scores[node_id]) for node_id in top_set),
            default=0.0,
        )
        non_anchor_ceiling = max(
            0.0,
            anchor_floor - self._EDGE_ROUTE_RANK_EPSILON,
        )

        superseded_ids = (
            superseded_ids if superseded_ids is not None else self._superseded_node_ids()
        )
        for edge in self.edges:
            if not include_historical and not (
                self._node_is_hot_route_candidate(edge.source, superseded_ids)
                and self._node_is_hot_route_candidate(edge.target, superseded_ids)
            ):
                continue
            routes = (
                (edge.source, edge.target, False),
                (edge.target, edge.source, True),
            )
            for source_id, target_id, reverse in routes:
                if source_id not in top_set or target_id not in rrf_scores or target_id in top_set:
                    continue
                remaining = self._EDGE_ROUTE_PER_NODE_CAP - boost_by_node[target_id]
                remaining = min(
                    remaining,
                    non_anchor_ceiling - float(rrf_scores[target_id]),
                )
                if remaining <= 0:
                    continue
                boost = min(self._edge_route_boost_value(edge, reverse=reverse), remaining)
                if boost <= 0:
                    continue
                rrf_scores[target_id] = round(rrf_scores[target_id] + boost, 6)
                boost_by_node[target_id] += boost
                edge_type = self._edge_type_value(edge.type)
                direction = "reverse" if reverse else "forward"
                type_counts[edge_type] += 1
                direction_counts[direction] += 1
                edge_evidence[target_id].append({
                    "source": edge.source,
                    "target": edge.target,
                    "type": edge_type,
                    "direction": direction,
                    "boost": round(boost, 6),
                })

        top_boosts = []
        for node_id, boost in boost_by_node.items():
            top_boosts.append({
                "node_id": node_id,
                "boost": round(float(boost), 6),
                "edges": sorted(
                    edge_evidence.get(node_id, []),
                    key=lambda item: float(item.get("boost") or 0.0),
                    reverse=True,
                )[:3],
            })
        top_boosts.sort(key=lambda item: (-float(item["boost"]), item["node_id"]))

        return {
            "enabled": True,
            "anchor_count": len(top_set),
            "boosted_node_count": len(boost_by_node),
            "base": self._EDGE_ROUTE_BASE,
            "per_edge_cap": self._EDGE_ROUTE_PER_EDGE_CAP,
            "per_node_cap": self._EDGE_ROUTE_PER_NODE_CAP,
            "rank_preserving": True,
            "anchor_floor": round(anchor_floor, 6),
            "type_factors": dict(self._EDGE_ROUTE_TYPE_FACTOR),
            "reverse_factors": dict(self._EDGE_ROUTE_REVERSE_FACTOR),
            "type_counts": dict(type_counts),
            "direction_counts": dict(direction_counts),
            "top_boosts": top_boosts[:12],
        }

    def _build_core_memory_route_graph(
        self,
        task: str,
        selected_ids: list[str],
        *,
        project_id: str = "",
        project_namespace: str = "",
        superseded_ids: set[str] | None = None,
    ) -> dict[str, Any]:
        manifest, registry_source = self._core_manifest_payload()
        lanes = self._normalize_lane_map(manifest.get("memory_lanes") or manifest.get("lanes"))
        lane_weights = self._normalize_lane_weights(manifest.get("lane_weights") or manifest.get("memory_lane_weights"))
        required_edges = self._normalize_required_edges(manifest.get("required_edges") or manifest.get("memory_lane_required_edges"))
        core_node_ids = set().union(*(set(ids) for ids in lanes.values())) if lanes else set()
        selected_set = set(selected_ids)
        triggered = bool(self._CORE_ROUTE_TRIGGER.search(task)) or bool(selected_set & core_node_ids) or self._CORE_MEMORY_REGISTRY_NODE_ID in selected_set

        if not triggered:
            return {
                "triggered": False,
                "registry_source": registry_source,
                "registry_node_id": self._CORE_MEMORY_REGISTRY_NODE_ID,
            }

        superseded_ids = (
            superseded_ids if superseded_ids is not None else self._superseded_node_ids()
        )
        hot_edge_counts = self._hot_edge_counts(superseded_ids)

        task_topics = self._core_task_topics(task)
        explicit_error = self._core_repeated_error_requested(task, task_topics)
        selected_error_ids = {
            node_id
            for node_id in selected_ids
            if self._is_error_case_node(node_id, self.nodes.get(node_id))
        }

        def _core_error_visible(node_id: str) -> bool:
            node = self.nodes.get(node_id)
            if not self._is_error_artifact_node(node_id, node):
                return True
            return (
                explicit_error
                and node_id in selected_error_ids
                and self._is_error_case_node(node_id, node)
            )

        required_lanes = self._core_required_lanes(task, manifest)
        lanes = self._augment_core_lanes_from_edges(lanes, task, task_topics)
        node_to_lanes: dict[str, list[str]] = {}
        lane_selected_nodes: dict[str, list[str]] = {}
        optional_nodes: list[dict[str, str]] = []
        def _applicable(node_id: str, lane: str) -> bool:
            allowed, reason = self._core_node_applicability(
                node_id,
                lane,
                project_id=project_id,
                project_namespace=project_namespace,
                superseded_ids=superseded_ids,
            )
            if not allowed:
                optional_nodes.append(
                    {"node_id": node_id, "lane": lane, "reason": reason}
                )
            return allowed

        for lane in required_lanes:
            selected_lane_nodes = self._select_core_lane_nodes(
                lane,
                lanes.get(lane, []),
                task,
                task_topics,
                hot_edge_counts=hot_edge_counts,
            )
            selected_lane_nodes = [
                node_id
                for node_id in selected_lane_nodes
                if _core_error_visible(node_id) and _applicable(node_id, lane)
            ]
            lane_selected_nodes[lane] = selected_lane_nodes
            for node_id in selected_lane_nodes:
                node_to_lanes.setdefault(node_id, [])
                if lane not in node_to_lanes[node_id]:
                    node_to_lanes[node_id].append(lane)

        selected_core_ids = set(node_to_lanes)
        relevant_required_edges = [
            edge for edge in required_edges
            if self._core_required_edge_relevant(edge, selected_core_ids, task_topics)
            and _core_error_visible(str(edge.get("source") or ""))
            and _core_error_visible(str(edge.get("target") or ""))
            and all(
                node_id == self._CORE_MEMORY_REGISTRY_NODE_ID
                or node_id in selected_core_ids
                for node_id in (
                    str(edge.get("source") or ""),
                    str(edge.get("target") or ""),
                )
            )
        ]
        for edge in required_edges:
            if edge not in relevant_required_edges:
                continue
            for key in ("source", "target"):
                node_id = str(edge.get(key) or "")
                if (
                    node_id
                    and node_id in self.nodes
                    and _core_error_visible(node_id)
                ):
                    node_to_lanes.setdefault(node_id, ["edge_contract"])

        existing_edge_sigs = {self._edge_key(edge) for edge in self.edges}
        missing_required_edges = [
            edge for edge in relevant_required_edges
            if (edge["source"], edge["target"], edge["type"]) not in existing_edge_sigs
        ]
        critical_missing_edges = [edge for edge in missing_required_edges if float(edge.get("weight") or 0.0) >= 0.9]

        node_weights = []
        for node_id, node_lanes in node_to_lanes.items():
            node = self.nodes.get(node_id)
            if not node:
                continue
            node_weights.append({
                "id": node_id,
                "lanes": node_lanes,
                "execution_weight": self._core_node_execution_weight(
                    node,
                    node_lanes,
                    lane_weights,
                    hot_edge_counts=hot_edge_counts,
                ),
                "priority": self._node_priority_value(node),
                "status": self._node_status_value(node),
                "type": self._node_type_value(node),
                "activation_count": node.activation_count,
                "edge_count": int(hot_edge_counts.get(node_id, 0)),
            })
        node_weights.sort(key=lambda item: (-float(item["execution_weight"]), item["id"]))

        must_consume = [
            item["id"] for item in node_weights
            if float(item["execution_weight"]) >= 80.0
            and any(lane != "edge_contract" for lane in item.get("lanes", []))
            # Error knowledge is query-scoped recall, never global mandatory
            # memory. Exact matching/ranking is handled by the main route path.
            and not self._is_error_artifact_node(
                item["id"],
                self.nodes.get(item["id"]),
            )
        ]
        if self._CORE_REGISTRY_ANCHOR_TRIGGER.search(task or "") and self._CORE_MEMORY_REGISTRY_NODE_ID in node_to_lanes:
            must_consume.append(self._CORE_MEMORY_REGISTRY_NODE_ID)
            must_consume = list(dict.fromkeys(must_consume))
        repeated_must = [
            node_id
            for node_id in must_consume
            if self._is_error_case_node(node_id, self.nodes.get(node_id))
        ]
        selected_must = [node_id for node_id in must_consume if node_id in selected_set]
        missing_must = [node_id for node_id in must_consume if node_id not in selected_set]
        relevant_core_edges = [
            self._edge_as_route_meta(edge)
            for edge in self.edges
            if edge.source in node_to_lanes and edge.target in node_to_lanes
        ][:30]

        return {
            "triggered": True,
            "status": "pass" if not critical_missing_edges else "warn",
            "registry_source": registry_source,
            "registry_node_id": self._CORE_MEMORY_REGISTRY_NODE_ID,
            "task_topics": sorted(task_topics),
            "required_lanes": required_lanes,
            "lane_selected_nodes": lane_selected_nodes,
            "optional_semantic_nodes": sorted(
                {json.dumps(item, sort_keys=True): item for item in optional_nodes}.values(),
                key=lambda item: (item["lane"], item["node_id"], item["reason"]),
            ),
            "project_scope": {
                "project_id": project_id or None,
                "project_namespace": project_namespace or None,
                "policy": "project_scoped_lanes_require_proven_applicability",
            },
            "lane_weights": lane_weights,
            "node_weights": node_weights[:20],
            "must_consume_node_ids": must_consume,
            "selected_must_consume_node_ids": selected_must,
            "missing_must_consume_node_ids": missing_must,
            "required_edges_count": len(relevant_required_edges),
            "global_required_edges_count": len(required_edges),
            "missing_required_edges": missing_required_edges,
            "critical_missing_edges": critical_missing_edges,
            "repeated_error_policy": {
                "dynamic_repeated_err_topics": sorted(self._CORE_DYNAMIC_REPEATED_ERR_TOPICS),
                "explicit_error_requested": self._core_repeated_error_requested(task, task_topics),
                "max_repeated_must_consume": self._CORE_REPEATED_ERR_MAX_PER_ROUTE,
                "max_error_warning_nodes": self._CORE_ERROR_WARNING_MAX_PER_ROUTE,
                "repeated_must_consume_count": len(repeated_must),
                "over_cap": len(repeated_must) > self._CORE_REPEATED_ERR_MAX_PER_ROUTE,
            },
            "edge_count": len(relevant_core_edges),
            "edges": relevant_core_edges,
            "pack_status": "complete" if not missing_must else "partial",
        }

    def _attach_core_memory_nodes(
        self,
        sorted_ids: list[str],
        scores: dict[str, float],
        rrf_scores: dict[str, float],
        coordination_graph: dict[str, Any],
        max_nodes: int,
        *,
        protected_ids: list[str] | None = None,
    ) -> list[str]:
        if not coordination_graph.get("triggered"):
            return sorted_ids
        protected_set = {
            node_id
            for node_id in (protected_ids or [])
            if node_id in self.nodes
        }
        must_consume = [nid for nid in coordination_graph.get("must_consume_node_ids", []) if nid in self.nodes]
        missing = [nid for nid in must_consume if nid not in sorted_ids]
        if not missing:
            coordination_graph["injected_node_ids"] = []
            coordination_graph["injection_policy"] = {
                "mode": "bounded_must_consume",
                "max_nodes_requested": int(max_nodes or 0),
                "missing_before_injection": 0,
                "injected_count": 0,
                "hard_gate_overrode_max_nodes": False,
                "protected_node_ids": [
                    node_id
                    for node_id in (protected_ids or [])
                    if node_id in sorted_ids
                ],
            }
            return sorted_ids
        requested_max_nodes = int(max_nodes or 0)
        injected = list(missing)
        selected = list(sorted_ids)
        for nid in injected:
            if nid not in selected:
                selected.append(nid)
                scores[nid] = max(rrf_scores.get(nid, 0.0), 0.000001)
        injected_set = set(injected)
        must_set = set(must_consume)
        while requested_max_nodes and len(selected) > requested_max_nodes:
            drop_index = None
            for idx in range(len(selected) - 1, -1, -1):
                if (
                    selected[idx] not in must_set
                    and selected[idx] not in injected_set
                    and selected[idx] not in protected_set
                ):
                    drop_index = idx
                    break
            if drop_index is None:
                break
            scores.pop(selected[drop_index], None)
            selected.pop(drop_index)

        ordered_protected = [
            node_id
            for node_id in (protected_ids or [])
            if node_id in selected
        ]
        ordered_must = [
            node_id
            for node_id in must_consume
            if node_id in selected and node_id not in protected_set
        ]
        ordered_other = [
            node_id
            for node_id in selected
            if node_id not in protected_set and node_id not in must_set
        ]
        selected = ordered_protected + ordered_must + ordered_other
        if requested_max_nodes:
            selected = selected[:requested_max_nodes]
        selected_set = set(selected)
        coordination_graph["injected_node_ids"] = injected
        coordination_graph["selected_must_consume_node_ids"] = [nid for nid in must_consume if nid in selected_set]
        coordination_graph["missing_must_consume_node_ids"] = [nid for nid in must_consume if nid not in selected_set]
        coordination_graph["pack_status"] = "complete" if not coordination_graph["missing_must_consume_node_ids"] else "partial"
        coordination_graph["injection_policy"] = {
            "mode": "bounded_must_consume",
            "max_nodes_requested": requested_max_nodes,
            "missing_before_injection": len(missing),
            "injected_count": len(injected),
            "hard_gate_overrode_max_nodes": False,
            "overrode_by_count": 0,
            "protected_node_ids": ordered_protected,
        }
        return selected

    def _start_reranker_warmup(self) -> None:
        mode = (_os.environ.get("THREECAN_RERANKER_MODE") or "adaptive").strip().lower()
        warmup = (_os.environ.get("THREECAN_RERANKER_WARMUP") or "background").strip().lower()
        if mode in {"0", "false", "off", "none", "disabled", "disable"}:
            return
        if warmup in {"0", "false", "off", "none", "disabled", "disable"}:
            return
        shared_model = _cn_reranker_shared_model
        if shared_model is not None:
            self._cn_reranker = shared_model
            self._cn_reranker_warmup_started = True
            self._cn_reranker_warmup_error_code = ""
            return
        if getattr(self, "_cn_reranker_warmup_started", False) or hasattr(self, "_cn_reranker"):
            return
        self._cn_reranker_warmup_started = True
        self._cn_reranker_loading = True

        def _warmup() -> None:
            try:
                self._cn_reranker = _load_cn_reranker_singleflight()
                self._cn_reranker_warmup_error_code = ""
            except Exception:
                self._cn_reranker_warmup_error_code = "reranker_warmup_failed"
                logging.getLogger("3can").exception("reranker warmup failed")
            finally:
                self._cn_reranker_loading = False

        warmup_thread = threading.Thread(
            target=_warmup,
            name="3can-reranker-warmup",
            daemon=True,
        )
        self._cn_reranker_warmup_thread = warmup_thread
        warmup_thread.start()

    def _reranker_warmup_meta(self) -> dict[str, Any]:
        has_error = bool(getattr(self, "_cn_reranker_warmup_error_code", ""))
        return {
            "started": bool(getattr(self, "_cn_reranker_warmup_started", False)),
            "loading": bool(getattr(self, "_cn_reranker_loading", False)),
            "ready": bool(hasattr(self, "_cn_reranker")),
            "error_code": "reranker_warmup_failed" if has_error else "",
        }

    def _reranker_policy(self, req: RoutingRequest, pool_count: int) -> dict[str, Any]:
        mode = (getattr(req, "mode", None) or "slim").strip().lower()
        env_mode = (_os.environ.get("THREECAN_RERANKER_MODE") or "adaptive").strip().lower()
        if env_mode in {"0", "false", "off", "none", "disabled", "disable"}:
            return {
                "enabled": False,
                "mode": "off",
                "reason": "disabled_by_env",
                "pool_limit": self._RERANKER_POOL_LIMIT,
                "candidate_limit": 0,
                "candidate_count": 0,
            }

        default_cap = self._RERANKER_DEFAULT_CAP_BY_MODE.get(mode, self._RERANKER_DEFAULT_CAP_BY_MODE["slim"])
        raw_cap = (_os.environ.get("THREECAN_RERANKER_MAX_CANDIDATES") or "").strip()
        cap = default_cap
        if raw_cap:
            try:
                cap = int(raw_cap)
            except ValueError:
                cap = default_cap
        if cap <= 0:
            return {
                "enabled": False,
                "mode": "off",
                "reason": "candidate_cap_zero",
                "pool_limit": self._RERANKER_POOL_LIMIT,
                "candidate_limit": 0,
                "candidate_count": 0,
            }

        raw_fast_modes = _os.environ.get("THREECAN_RERANKER_FAST_MODES")
        if raw_fast_modes is None:
            raw_fast_modes = "skeleton,slim"
        if raw_fast_modes.strip().lower() in {"", "0", "false", "off", "none", "disabled", "disable"}:
            fast_modes: set[str] = set()
        else:
            fast_modes = {item.strip().lower() for item in raw_fast_modes.split(",") if item.strip()}
        if mode in fast_modes and env_mode not in {"force", "always", "all"}:
            self._start_reranker_warmup()
            return {
                "enabled": False,
                "mode": "adaptive",
                "reason": "fast_hook_mode_reranker_skipped",
                "pool_limit": self._RERANKER_POOL_LIMIT,
                "candidate_limit": 0,
                "candidate_count": 0,
                "warmup": self._reranker_warmup_meta(),
            }

        foreground_cold_load = (
            (_os.environ.get("THREECAN_RERANKER_FOREGROUND_COLD_LOAD") or "full-only").strip().lower()
            in {"1", "true", "yes", "on", "all"}
        )
        if mode in {"skeleton", "slim"} and not hasattr(self, "_cn_reranker") and not foreground_cold_load:
            self._start_reranker_warmup()
            return {
                "enabled": False,
                "mode": "adaptive",
                "reason": "foreground_cold_load_blocked_for_hook_route",
                "pool_limit": self._RERANKER_POOL_LIMIT,
                "candidate_limit": 0,
                "candidate_count": 0,
                "warmup": self._reranker_warmup_meta(),
            }

        min_needed = max(1, min(int(req.max_nodes or 1), pool_count))
        limit = min(pool_count, self._RERANKER_POOL_LIMIT, max(min_needed, cap))
        return {
            "enabled": True,
            "mode": "adaptive",
            "reason": f"{mode}_candidate_cap",
            "pool_limit": self._RERANKER_POOL_LIMIT,
            "candidate_limit": limit,
            "candidate_count": limit,
            "warmup": self._reranker_warmup_meta(),
        }

    def route(self, req: RoutingRequest) -> RoutingResponse:
        """分层路由 v8.0 (3-path SymRAG + RRF fusion + FlashRank re-ranking)。

        架构 (类神经4层):
          Layer 1 - Bi-encoder粗召回 (感觉神经元: BGE-M3 semantic)
          Layer 2 - RRF多信号融合 (丘脑: embedding×keyword×intent独立排名→位置融合)
          Layer 3 - Cross-encoder精排 (联想皮层: FlashRank逐对attention)
          Layer 4 - Feedback修正 (海马体: activation heat + route/feedback累积)

        4-signal RRF routing (SymRAG NeSy 2025):
          Signal 1 - Embedding语义 | Signal 2 - Keyword匹配
          Signal 3 - Hybrid自适应 | Signal 4 - Code-index精确匹配(短代码)
          Path A - 短代码: 4信号RRF(code权重5x) + FlashRank精排
          Path B - 自然语言: 3信号RRF + FlashRank精排
        """
        if self._emb_matrix is None or len(self._emb_matrix) == 0:
            return RoutingResponse(
                activated_nodes=[], relevant_edges=[], scores={},
                total_nodes=len(self.nodes), total_edges=len(self.edges),
            )

        # ── Step 0: Query分析 ──
        route_id = req.route_id or f"route-{uuid.uuid4().hex}"
        intent, boosted_prefixes = self._classify_intent(req.task)
        precision_mode = (intent == "intf")
        expanded_task, query_expansion_meta = self._expand_query_for_route(req.task)
        task_lower = req.task.lower()
        raw_task_topics = self._core_task_topics(req.task)
        explicit_error_route = self._core_repeated_error_requested(req.task, raw_task_topics)

        query_tokens_lower: set[str] = set()
        for tok in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fa5]+", req.task):
            query_tokens_lower.add(tok.lower())

        raw = req.task.strip()
        is_short_code = bool(re.fullmatch(r"[A-Za-z]+\d+[A-Za-z0-9]*", raw)) and len(raw) <= 8
        query_codes = list(
            dict.fromkeys(
                code.upper() for code in self._CODE_PATTERN.findall(req.task)
            )
        )
        current_reality_policy = self._current_reality_policy(
            req,
            expanded_task,
            explicit_error=explicit_error_route,
            exact_code=is_short_code,
        )
        route_superseded_ids = self._superseded_node_ids()
        route_scoped_core_ids = self._core_project_scoped_node_ids()
        K_RRF = 60  # RRF常数, Elasticsearch标准值

        # ── Step 0.5: 短代码解析 (结果注入RRF第4信号, 不再bypass) ──
        code_resolved_ids: list[str] | None = None
        if is_short_code:
            code_resolved_ids = self._resolve_short_code(req.task)
        elif query_codes:
            embedded_code_ids: list[str] = []
            for code in query_codes:
                resolved = self._resolve_short_code(code) or []
                if len(resolved) > self._RERANKER_POOL_LIMIT:
                    continue
                for node_id in resolved:
                    if node_id not in embedded_code_ids:
                        embedded_code_ids.append(node_id)
            if len(embedded_code_ids) <= self._RERANKER_POOL_LIMIT:
                code_resolved_ids = embedded_code_ids or None

        # ── Step 1: 3-path信号计算 (完整pipeline) ──
        weighted_queries = query_expansion_meta.get("weighted_queries") or [
            {"query": req.task, "weight": 1.0, "source": "original"}
        ]
        query_matrix = _encode(
            [str(item["query"]) for item in weighted_queries]
        )
        query_weights = np.asarray(
            [float(item["weight"]) for item in weighted_queries],
            dtype=np.float32,
        )
        query_emb = np.average(query_matrix, axis=0, weights=query_weights)
        query_norm = float(np.linalg.norm(query_emb))
        if query_norm > 0.0:
            query_emb = query_emb / query_norm
        similarities = self._emb_matrix @ query_emb
        priority_bonus = {"critical": 0.06, "high": 0.03, "medium": 0.0, "low": -0.03}

        # 3个独立ranking信号
        emb_ranks: dict[str, float] = {}   # 纯语义排名
        kw_ranks: dict[str, float] = {}    # 纯keyword排名
        hybrid_ranks: dict[str, float] = {}  # 综合排名(保留向后兼容)

        for i, nid in enumerate(self._node_id_order):
            node = self.nodes.get(nid)
            if not node or node.status == NodeStatus.dormant:
                continue
            if (
                node.status == NodeStatus.archived
                and not current_reality_policy.get("historical_requested")
            ):
                continue
            if not self._error_node_allowed_for_route(
                nid,
                node,
                explicit_error=explicit_error_route,
            ):
                continue

            emb_score = float(similarities[i])
            kw_score, intent_score, tier_boost, exact_matches = self._score_keyword(
                node, nid, task_lower, query_tokens_lower, boosted_prefixes, is_short_code)

            # Signal 1: 纯embedding
            emb_total = emb_score + priority_bonus.get(node.priority, 0.0)
            # INTF降权: 仅零激活+零keyword命中时生效 (v8.3: activation-gated)
            if (nid.startswith("INTF-") and not precision_mode
                    and kw_score < 1.0 and node.activation_count == 0):
                emb_total += self._INTF_DEMOTION
            if nid.startswith("AGT-") and intent != "agent" and kw_score < 2.0:
                emb_total += self._INTF_DEMOTION
            emb_ranks[nid] = emb_total

            # Signal 2: 纯keyword + tier
            kw_total = kw_score + tier_boost + intent_score * 0.5
            if kw_total > 0.0:
                kw_ranks[nid] = kw_total

            # Signal 3: hybrid (v7.4 adaptive作为第三信号)
            total_kws = max(len(node.activation_keywords), 1)
            kw_ratio = kw_score / total_kws
            if kw_ratio >= 0.15 and kw_score >= 2.0:
                w_emb, w_kw, w_int = 0.35, 0.45, 0.20
            elif kw_score >= 1.0:
                w_emb, w_kw, w_int = 0.45, 0.35, 0.20
            else:
                w_emb, w_kw, w_int = 0.60, 0.25, 0.15
            hybrid_score = w_emb * emb_score + w_kw * min(kw_score, 3.0) / 3.0 + w_int * intent_score
            hybrid_score += priority_bonus.get(node.priority, 0.0) + tier_boost
            heat = min(node.activation_count * self._ACTIVATION_HEAT_WEIGHT, 0.05)
            hybrid_score += heat
            if (nid.startswith("INTF-") and not precision_mode
                    and kw_score < 1.0 and node.activation_count == 0):
                hybrid_score += self._INTF_DEMOTION
            if nid.startswith("AGT-") and intent != "agent" and kw_score < 2.0:
                hybrid_score += self._INTF_DEMOTION
            hybrid_ranks[nid] = hybrid_score

        # ── Step 2: RRF Fusion (替代手工权重) ──
        # 将3个signal独立排序, 用 1/(K+rank) 位置融合
        def _rank_list(scores_dict: dict[str, float]) -> dict[str, int]:
            sorted_nids = sorted(
                scores_dict,
                key=lambda node_id: (-scores_dict[node_id], node_id),
            )
            return {nid: rank + 1 for rank, nid in enumerate(sorted_nids)}

        emb_rank_map = _rank_list(emb_ranks)
        kw_rank_map = _rank_list(kw_ranks)
        hybrid_rank_map = _rank_list(hybrid_ranks)

        all_nids = sorted(set(emb_ranks) | set(kw_ranks) | set(hybrid_ranks))

        # Signal 4: code_index精确匹配 (auto-index作为RRF第4信号)
        code_rank_map: dict[str, int] = {}
        if code_resolved_ids:
            _code_set = set(code_resolved_ids)
            for nid in all_nids:
                if nid in _code_set:
                    code_rank_map[nid] = code_resolved_ids.index(nid) + 1

        active_embedding_backend = self._embedding_backend_id()
        if is_short_code and code_resolved_ids:
            # Path A: 短代码 + auto-index命中 → code信号权重最高
            rrf_weights = (0.5, 3.0, 0.0, 5.0)  # (dense, lexical, legacy-hybrid, exact-code)
        elif is_short_code:
            # Path A': 短代码但无auto-index命中 → 仅3信号
            rrf_weights = (0.5, 3.0, 0.0, 0.0)
        elif code_resolved_ids:
            # Natural-language queries may carry a discriminative project code.
            # Reuse the exact index without treating the whole query as a code.
            rrf_weights = (1.0, 1.0, 0.0, 5.0)
        elif precision_mode:
            # Path B-intf: 接口查询 → hybrid权重2x
            rrf_weights = (1.0, 1.5, 0.0, 0.0)
        elif active_embedding_backend == "hashing-blake2b-char-ngram-v1":
            # The deterministic minimal-install fallback has weaker semantic
            # recall than BGE-M3, so let explicit lexical evidence lead.
            rrf_weights = (0.75, 1.5, 0.0, 0.0)
        else:
            # Path B/C: 自然语言 → 均匀
            rrf_weights = (1.0, 1.0, 0.0, 0.0)

        rrf_scores: dict[str, float] = {}
        for nid in all_nids:
            rrf = 0.0
            for weight, rank_map in zip(
                rrf_weights,
                (emb_rank_map, kw_rank_map, hybrid_rank_map, code_rank_map),
            ):
                rank = rank_map.get(nid)
                if weight and rank is not None:
                    rrf += weight / (K_RRF + rank)
            rrf_scores[nid] = round(rrf, 6)

        # Error knowledge is opt-in and globally bounded before any ranking
        # metadata is produced.  This prevents temporal/traversal/core route
        # metadata from becoming a second channel for the historical ERR
        # cluster.
        error_solution_policy = self._apply_verified_solution_route_boost(
            req.task,
            rrf_scores,
            explicit_error=explicit_error_route,
        )
        # Exact identity is the final and strongest ErrorCase signal. A directly
        # named unresolved case must not be evicted by three verified canonical
        # siblings before the strict ErrorCase cap is applied.
        exact_error_case_policy = self._apply_exact_error_case_route_boost(
            req.task,
            rrf_scores,
            explicit_error=explicit_error_route,
        )
        if explicit_error_route:
            ranked_error_ids = self._rank_error_case_ids_for_cap(
                req.task,
                rrf_scores,
            )
            allowed_error_ids = set(
                ranked_error_ids[: self._CORE_REPEATED_ERR_MAX_PER_ROUTE]
            )
            rrf_scores = {
                node_id: score
                for node_id, score in rrf_scores.items()
                if (
                    not self._is_error_artifact_node(
                        node_id,
                        self.nodes.get(node_id),
                    )
                    or node_id in allowed_error_ids
                )
            }
            error_solution_policy["boosted_case_ids"] = [
                node_id
                for node_id in error_solution_policy.get("boosted_case_ids", [])
                if node_id in allowed_error_ids
            ]
            error_solution_policy["match_kinds"] = {
                node_id: kind
                for node_id, kind in error_solution_policy.get(
                    "match_kinds",
                    {},
                ).items()
                if node_id in allowed_error_ids
            }
            exact_error_case_policy["boosted_case_ids"] = [
                node_id
                for node_id in exact_error_case_policy.get("boosted_case_ids", [])
                if node_id in allowed_error_ids
            ]
            exact_error_case_policy["match_kinds"] = {
                node_id: kind
                for node_id, kind in exact_error_case_policy.get(
                    "match_kinds",
                    {},
                ).items()
                if node_id in allowed_error_ids
            }

        # Step 2.5: bounded time-aware ranking signal, only active when the
        # query asks for latest/current/temporal/validity semantics.
        temporal_policy = self._temporal_task_policy(req.task, expanded_task)
        temporal_window = max(80, req.max_nodes * 12)
        temporal_eligible = set(
            sorted(
                rrf_scores,
                key=lambda node_id: (-rrf_scores[node_id], node_id),
            )[:temporal_window]
        )
        temporal_route_boost = self._apply_temporal_route_boost(
            rrf_scores,
            temporal_policy,
            eligible_node_ids=temporal_eligible,
        )
        current_reality_policy = self._apply_current_reality_policy(
            rrf_scores,
            current_reality_policy,
            superseded_ids=route_superseded_ids,
            scoped_core_ids=route_scoped_core_ids,
        )

        # ── Step 3: Graph Traversal Boost ──
        # Traversal evidence must not change merely because the caller asks for
        # a wider result packet. Keep one bounded seed set for every route size.
        top_set = set(
            sorted(
                rrf_scores,
                key=lambda node_id: (-rrf_scores[node_id], node_id),
            )[: self._GRAPH_TRAVERSAL_ANCHOR_LIMIT]
        )
        graph_traversal_boost = self._apply_graph_traversal_boost(
            rrf_scores,
            top_set,
            include_historical=explicit_error_route,
            superseded_ids=route_superseded_ids,
        )

        # ── Step 3.5 v9.2 Path 2: Leiden Community Boost ──
        # 同 community 的兄弟节点 +0.002 (主要涨 R@3 — 让 query 的目标集群一起浮出)
        top_communities: set[int] = set()
        for nid in list(top_set):
            n = self.nodes.get(nid)
            if not n:
                continue
            extra = getattr(n.content, "extra", None) or {}
            cid = extra.get("community_id") if isinstance(extra, dict) else None
            if cid is not None:
                top_communities.add(cid)
        if top_communities:
            for nid in rrf_scores:
                if nid in top_set:
                    continue
                n = self.nodes.get(nid)
                if not n:
                    continue
                extra = getattr(n.content, "extra", None) or {}
                cid = extra.get("community_id") if isinstance(extra, dict) else None
                if cid in top_communities:
                    rrf_scores[nid] = round(rrf_scores[nid] + 0.002, 6)

        # ── Step 4: Cross-encoder Re-ranking (top-15 → top-K) ──
        # 优先: bge-reranker-v2-m3 (中文原生, 568M)  fallback: FlashRank ms-marco
        candidate_pool = sorted(
            rrf_scores,
            key=lambda node_id: (-rrf_scores[node_id], node_id),
        )[: self._RERANKER_POOL_LIMIT]
        reranker_policy = self._reranker_policy(req, len(candidate_pool))
        top_candidates = candidate_pool[: int(reranker_policy.get("candidate_limit") or 0)]
        reranked = list(candidate_pool)  # fallback if reranker unavailable or disabled
        reranker_backend = "none"

        try:
            if not (reranker_policy.get("enabled") and top_candidates):
                raise RuntimeError("__3can_reranker_disabled__")
            if not hasattr(self, '_cn_reranker'):
                self._cn_reranker = _load_cn_reranker_singleflight()
            reranker_backend = "sentence-transformers:BAAI/bge-reranker-v2-m3"
            pairs = []
            for nid in top_candidates:
                node = self.nodes[nid]
                doc = f"{node.name}. {node.content.description or ''}. {' '.join(node.activation_keywords[:8])}"
                pairs.append([expanded_task, doc[:400]])
            if pairs:
                # v8.4: RRF-reranker fusion — 不再让cross-encoder完全覆盖RRF证据。
                # 归一化两路分数 → alpha·rrf + (1-alpha)·rerank (alpha=0.5)。
                # 动机: 短抽象中文query下reranker易把语义正确的节点硬压低(实测 rank2→rank9)。
                rerank_scores = np.asarray(self._cn_reranker.predict(pairs), dtype=float)
                rrf_vec = np.asarray([rrf_scores[nid] for nid in top_candidates], dtype=float)
                def _minmax(v: np.ndarray) -> np.ndarray:
                    lo, hi = float(v.min()), float(v.max())
                    return (v - lo) / (hi - lo) if hi > lo else np.zeros_like(v)
                fused = 0.5 * _minmax(rrf_vec) + 0.5 * _minmax(rerank_scores)
                order = sorted(
                    range(len(top_candidates)),
                    key=lambda index: (-float(fused[index]), top_candidates[index]),
                )
                reranked = [top_candidates[i] for i in order]
        except Exception as _cn_err:
            import logging
            logging.getLogger("3can").debug(f"bge-reranker unavailable ({_cn_err}), trying FlashRank")
            if str(_cn_err) == "__3can_reranker_disabled__":
                top_candidates = []
            try:
                if not top_candidates:
                    raise RuntimeError("__3can_reranker_disabled__")
                from flashrank import Ranker, RerankRequest
                if not hasattr(self, '_reranker'):
                    self._reranker = Ranker(model_name="ms-marco-MiniLM-L-12-v2",
                                            cache_dir=str(GRAPH_DIR / ".flashrank_cache"))
                reranker_backend = "flashrank:ms-marco-MiniLM-L-12-v2"
                passages = []
                for nid in top_candidates:
                    node = self.nodes[nid]
                    text = f"{node.name}. {node.content.description or ''}. Keywords: {', '.join(node.activation_keywords[:10])}"
                    passages.append({"id": nid, "text": text[:512]})
                if passages:
                    results = self._reranker.rerank(RerankRequest(query=expanded_task, passages=passages))
                    reranked = [r["id"] for r in results]
            except Exception as _fl_err:
                if str(_fl_err) != "__3can_reranker_disabled__":
                    logging.getLogger("3can").warning(f"FlashRank also failed: {_fl_err}")

        reranked = self._prioritize_current_reality(
            reranked,
            current_reality_policy,
        )

        # ── Step 5: 取top-K, 合并分数 ──
        prioritized_error_cases = list(
            dict.fromkeys(
                [
                    node_id
                    for node_id in (
                        exact_error_case_policy.get("boosted_case_ids", [])
                        + error_solution_policy.get("boosted_case_ids", [])
                    )
                    if node_id in reranked
                ]
            )
        )
        verified_error_cases = [
            node_id
            for node_id in error_solution_policy.get("boosted_case_ids", [])
            if node_id in prioritized_error_cases
        ]
        directly_named_error_cases = [
            node_id
            for node_id, match_kind in exact_error_case_policy.get(
                "match_kinds",
                {},
            ).items()
            if match_kind == "case_id" and node_id in prioritized_error_cases
        ]
        if prioritized_error_cases:
            priority_set = set(prioritized_error_cases)
            reranked = prioritized_error_cases + [
                node_id for node_id in reranked if node_id not in priority_set
            ]
        sorted_ids = self._limit_repeated_error_results(reranked)[:req.max_nodes]
        semantic_result_ids = list(sorted_ids)
        # 分数用RRF值（re-ranking改变了顺序但RRF分数仍用于展示）
        scores = {nid: rrf_scores.get(nid, 0.0) for nid in sorted_ids}
        core_memory_graph = self._build_core_memory_route_graph(
            req.task,
            sorted_ids,
            project_id=req.project_id or "",
            project_namespace=req.project_namespace or "",
            superseded_ids=route_superseded_ids,
        )
        sorted_ids = self._attach_core_memory_nodes(
            sorted_ids,
            scores,
            rrf_scores,
            core_memory_graph,
            req.max_nodes,
            protected_ids=prioritized_error_cases,
        )
        protected_memory_ids = (
            core_memory_graph.get("must_consume_node_ids", [])
            if isinstance(core_memory_graph, dict)
            else []
        )
        (
            sorted_ids,
            attached_solution_ids,
            attached_evidence_ids,
            verified_solution_bundles,
        ) = self._attach_verified_solution_nodes(
            sorted_ids,
            scores,
            rrf_scores,
            prioritized_case_ids=verified_error_cases,
            max_nodes=req.max_nodes,
            protected_case_ids=[
                *directly_named_error_cases,
                *protected_memory_ids,
            ],
        )
        sorted_ids = [
            node_id
            for node_id in sorted_ids
            if self._current_reality_node_allowed(
                node_id,
                current_reality_policy,
                superseded_ids=route_superseded_ids,
            )
        ]

        # dormant补全
        if len(sorted_ids) < req.max_nodes:
            remaining = req.max_nodes - len(sorted_ids)
            used = set(sorted_ids)
            dormant_pool = {nid: hybrid_ranks.get(nid, 0) for nid, n in self.nodes.items()
                           if n.status == NodeStatus.dormant
                           and nid not in used
                           and self._current_reality_node_allowed(
                               nid,
                               current_reality_policy,
                               superseded_ids=route_superseded_ids,
                           )
                           and self._error_node_allowed_for_route(
                               nid,
                               n,
                               explicit_error=explicit_error_route,
                           )}
            sorted_dormant = sorted(
                dormant_pool,
                key=lambda node_id: (-dormant_pool[node_id], node_id),
            )[:remaining]
            sorted_ids += sorted_dormant
            for nid in sorted_dormant:
                scores[nid] = dormant_pool.get(nid, 0.01)

        # ── Step 6: 永不返回空 ──
        if not sorted_ids:
            fallback = sorted(
                [
                    n
                    for n in self.nodes.values()
                    if not n.id.startswith("INTF-")
                    and n.status == NodeStatus.active
                    and self._current_reality_node_allowed(
                        n.id,
                        current_reality_policy,
                        superseded_ids=route_superseded_ids,
                    )
                    and self._error_node_allowed_for_route(
                        n.id,
                        n,
                        explicit_error=explicit_error_route,
                    )
                ],
                key=lambda n: (n.updated_at, n.id), reverse=True,
            )[:req.max_nodes]
            sorted_ids = [n.id for n in fallback]
            scores = {n.id: 0.1 for n in fallback}

        sorted_ids = self._limit_repeated_error_results(sorted_ids)[:req.max_nodes]
        activated = []
        for nid in sorted_ids:
            node = self.nodes[nid]
            activated.append(node)

        # 相关边 + 1-hop扩展
        relevant_edges = []
        if req.include_edges:
            id_set = set(sorted_ids)
            internal_edges = [
                edge
                for edge in self.edges
                if edge.source in id_set and edge.target in id_set
            ]
            # Every edge endpoint must be present in activated_nodes.  One-hop
            # solution edges previously leaked ERR/FIX identifiers through
            # relevant_edges and packed edge_evidence on ordinary routes.
            relevant_edges = internal_edges[: self._ROUTE_RELEVANT_EDGE_MAX]

        route_meta = {
            "route_id": route_id,
            "session_instance_id": req.session_instance_id,
            "route_correlation_mode": (
                "session_exact"
                if req.session_instance_id is not None
                else "legacy_agent"
            ),
            "intent": intent,
            "precision_mode": precision_mode,
            "query_expansion": query_expansion_meta,
            "embedding_backend": active_embedding_backend,
            "reranker_backend": reranker_backend,
            "candidate_count": len(all_nids),
            "candidate_pool_count": len(candidate_pool),
            "top_candidate_count": len(top_candidates),
            "semantic_result_ids": semantic_result_ids,
            "activation_policy": "verified-consumption-only",
            "reranker_policy": reranker_policy,
            "rrf_weights": list(rrf_weights),
            "code_signal": {
                "mode": (
                    "exact"
                    if is_short_code and code_resolved_ids
                    else "embedded"
                    if code_resolved_ids
                    else "none"
                ),
                "tokens": query_codes,
                "resolved_count": len(code_resolved_ids or []),
            },
            "fusion_policy": "independent-dense-lexical-exact-v1",
            "legacy_hybrid_vote": "disabled_correlated_signal",
            "expanded_query_changed": expanded_task != req.task,
            "temporal_route_policy": temporal_route_boost,
            "current_reality_policy": current_reality_policy,
            "graph_traversal_boost": graph_traversal_boost,
            "core_memory_graph": core_memory_graph,
            "execution_context": {
                key: value
                for key, value in {
                    "project_id": req.project_id,
                    "project_namespace": req.project_namespace,
                    "workspace_id": req.workspace_id,
                    "workorder_id": req.workorder_id,
                }.items()
                if value is not None
            },
            "error_route_policy": {
                "explicit_error_requested": explicit_error_route,
                "historical_repeated_default": "excluded",
                "max_repeated_error_cases": self._CORE_REPEATED_ERR_MAX_PER_ROUTE,
                "relevant_edge_cap": self._ROUTE_RELEVANT_EDGE_MAX,
                "resolved_cases_are_non_blocking": True,
                "exact_error_case_ranking": exact_error_case_policy,
                "verified_solution_ranking": error_solution_policy,
                "attached_solution_node_ids": attached_solution_ids,
                "attached_evidence_node_ids": attached_evidence_ids,
                "verified_solution_bundles": verified_solution_bundles,
            },
        }

        # 记录路由活动
        self.log_activity(
            req.agent_id, "route", f"query='{req.task[:60]}' → {len(sorted_ids)} nodes",
            affected_nodes=sorted_ids,
        )
        # 更新Agent路由计数
        if req.agent_id in self.agents:
            self.agents[req.agent_id].total_routes += 1
            self._save_agents()

        # Miss Healer: 记录route buffer供infer_outcome自动推断
        self._record_route_buffer(
            req.agent_id,
            req.task,
            sorted_ids,
            route_id=route_id,
            session_instance_id=req.session_instance_id,
        )

        return RoutingResponse(
            activated_nodes=activated, relevant_edges=relevant_edges,
            scores={k: scores[k] for k in sorted_ids},
            total_nodes=len(self.nodes), total_edges=len(self.edges),
            route_meta=route_meta,
        )

    # ── 自动回写（session结束时调用） ──

    def session_writeback(
        self,
        changes: list[dict],
        agent_id: str = "unknown",
        execution_context: dict[str, str] | None = None,
        provenance: DurableProvenance | None = None,
    ) -> list[str]:
        """自动审计并回写节点变更。

        changes = [
            {"node_id": "MOD-kb", "field": "current_state", "value": "2800条"},
            {"node_id": "MOD-distill", "field": "blockers", "action": "remove", "value": "Judge+入库待跑"},
            {"node_id": None, "action": "create", "name": "新发现X", "cluster": "...", ...},
        ]
        返回被更新的node_id列表。
        """
        if not isinstance(changes, list):
            raise ValueError("writeback_changes_must_be_list")
        context = {
            key: str(value).strip()
            for key, value in (execution_context or {}).items()
            if key in {
                "project_id",
                "project_namespace",
                "workspace_id",
                "workorder_id",
            }
            and str(value).strip()
            and str(value).strip().casefold() != "unspecified"
        }
        durable_provenance = provenance or DurableProvenance()
        durable_provenance_permitted = self._durable_provenance_permits_current(
            durable_provenance
        )
        now = dt.datetime.now(dt.timezone.utc).isoformat()

        # Validate the whole batch before the first durable mutation.  Older
        # behavior silently counted unknown fields and missing nodes as
        # successful writebacks, which made hook/MCP callers believe knowledge
        # had been persisted when nothing had changed.
        normalized: list[tuple[str, Any, Any]] = []
        for index, change in enumerate(changes):
            if not isinstance(change, dict):
                raise ValueError(f"writeback_change_not_object:{index}")
            nid = change.get("node_id")
            action = str(change.get("action") or "update").strip().lower()

            if action == "create":
                if nid:
                    raise ValueError(f"writeback_create_node_id_forbidden:{index}")
                keywords = change.get("keywords", [])
                if not isinstance(keywords, list) or not all(
                    isinstance(item, str) for item in keywords
                ):
                    raise ValueError(f"writeback_create_keywords_invalid:{index}")
                req = NodeCreate(
                    name=change.get("name", "unnamed"),
                    cluster=change.get("cluster", "业务逻辑"),
                    content=NodeContent(
                        description=change.get("description", ""),
                        current_state=change.get("current_state", ""),
                        extra=dict(context),
                    ),
                    activation_keywords=keywords,
                )
                normalized.append(("create", req, change))
                continue

            if not isinstance(nid, str) or not nid:
                raise ValueError(f"writeback_node_id_required:{index}")
            node = self.nodes.get(nid)
            if node is None:
                raise ValueError(f"writeback_node_not_found:{nid}")

            if semantic_id_family(nid) in _AUTHORITY_PROTECTED_FAMILIES:
                node_project, node_namespace = self._node_project_values(node)
                incoming_project = str(context.get("project_id") or "").casefold()
                incoming_namespace = str(
                    context.get("project_namespace") or ""
                ).casefold()
                if node_project and incoming_project and node_project != incoming_project:
                    raise ValueError(f"writeback_project_id_mismatch:{nid}")
                if (
                    node_namespace
                    and incoming_namespace
                    and node_namespace != incoming_namespace
                ):
                    raise ValueError(f"writeback_project_namespace_mismatch:{nid}")

            field = change.get("field")
            if field not in _SESSION_WRITEBACK_FIELDS:
                raise ValueError(f"writeback_field_unsupported:{field}")

            if (
                semantic_id_family(nid) in _AUTHORITY_PROTECTED_FAMILIES
                and field in _DURABLE_CURRENT_FIELDS
            ):
                if not durable_provenance_permitted:
                    raise ValueError(f"writeback_durable_provenance_required:{nid}")
                node_project, node_namespace = self._node_project_values(node)
                incoming_project = str(context.get("project_id") or "").casefold()
                incoming_namespace = str(
                    context.get("project_namespace") or ""
                ).casefold()
                if node_project and not incoming_project:
                    raise ValueError(f"writeback_project_id_required:{nid}")
                if node_namespace and not incoming_namespace:
                    raise ValueError(f"writeback_project_namespace_required:{nid}")
            value = change.get("value")

            if field in {
                "current_state",
                "description",
                "last_session",
                "notes",
            }:
                if action not in {"set", "update"} or not isinstance(value, str):
                    raise ValueError(f"writeback_scalar_change_invalid:{field}")
            elif field == "status":
                if action not in {"set", "update"}:
                    raise ValueError("writeback_status_action_invalid")
                try:
                    value = NodeStatus(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("writeback_status_value_invalid") from exc
            elif field in {"blockers", "tech_stack"}:
                allowed_actions = (
                    {"add", "remove", "set"}
                    if field == "blockers"
                    else {"add", "set"}
                )
                if action not in allowed_actions:
                    raise ValueError(f"writeback_collection_action_invalid:{field}")
                if action == "set":
                    if not isinstance(value, list) or not all(
                        isinstance(item, str) for item in value
                    ):
                        raise ValueError(
                            f"writeback_collection_value_invalid:{field}"
                        )
                    value = list(value)
                elif not isinstance(value, str) or not value:
                    raise ValueError(
                        f"writeback_collection_value_invalid:{field}"
                    )

            normalized.append(("update", (node, field, action, value), change))

        updated = []

        for operation, prepared, change in normalized:
            if operation == "create":
                req = prepared
                node = self.create_node(req)
                updated.append(node.id)
                continue

            node, field, action, value = prepared
            nid = node.id
            changed = False

            if field == "current_state":
                changed = node.content.current_state != value
                node.content.current_state = value
            elif field == "description":
                changed = node.content.description != value
                node.content.description = value
            elif field == "blockers":
                if action == "remove" and value in node.content.blockers:
                    node.content.blockers.remove(value)
                    changed = True
                elif action == "add" and value not in node.content.blockers:
                    node.content.blockers.append(value)
                    changed = True
                elif action == "set":
                    changed = node.content.blockers != value
                    node.content.blockers = value
            elif field == "status":
                changed = node.status != value
                node.status = value
            elif field == "notes":
                changed = node.content.notes != value
                node.content.notes = value
            elif field == "tech_stack":
                if action == "add":
                    if value not in node.content.tech_stack:
                        node.content.tech_stack.append(value)
                        changed = True
                elif action == "set":
                    changed = node.content.tech_stack != value
                    node.content.tech_stack = value
            elif field == "last_session":
                changed = node.content.last_session != value
                node.content.last_session = value

            provenance_refresh = False
            if (
                semantic_id_family(nid) in _AUTHORITY_PROTECTED_FAMILIES
                and field in _DURABLE_CURRENT_FIELDS
            ):
                stored_provenance = node.content.extra.get("durable_provenance")
                expected_provenance = durable_provenance.model_dump(mode="json")
                provenance_refresh = not isinstance(stored_provenance, dict) or any(
                    stored_provenance.get(key) != expected
                    for key, expected in expected_provenance.items()
                )
            changed = changed or provenance_refresh

            if not changed:
                continue

            if semantic_id_family(nid) in _AUTHORITY_PROTECTED_FAMILIES:
                if field in _DURABLE_CURRENT_FIELDS:
                    node.content.extra["durable_provenance"] = {
                        **durable_provenance.model_dump(mode="json"),
                        "agent_id": agent_id,
                        "workorder_id": context.get("workorder_id", ""),
                        "recorded_at": now,
                    }

            node.updated_at = now
            writer = change.get("updated_by", agent_id or "auto-writeback")
            node.updated_by = writer
            # primary_author: 首次writer锁定, 后续contributor追加
            if not getattr(node, "primary_author", None) or node.primary_author == "system":
                node.primary_author = writer
            elif writer != node.primary_author and writer not in node.contributors:
                node.contributors.append(writer)
            self._save_node(node)
            self._update_single_embedding(nid)
            updated.append(nid)

        # 记录回写活动
        if updated:
            self.log_activity(
                agent_id, "writeback",
                f"updated {len(updated)} nodes: {', '.join(updated[:5])}",
                affected_nodes=updated,
            )
            # 更新Agent回写计数
            if agent_id in self.agents:
                self.agents[agent_id].total_writebacks += 1
                self._save_agents()

        return updated

    # ── 用户偏好沉淀 ──

    def _preference_profile_config(self) -> dict[str, str]:
        registry = self.nodes.get(self._CORE_MEMORY_REGISTRY_NODE_ID)
        registry_extra = getattr(registry.content, "extra", None) or {} if registry else {}
        configured = (
            registry_extra.get("preference_profile")
            if isinstance(registry_extra, dict)
            else {}
        )
        if not isinstance(configured, dict):
            configured = {}
        return {
            "node_id": (
                _os.environ.get("THREECAN_PROFILE_NODE_ID")
                or configured.get("node_id")
                or "USR-default-profile"
            ),
            "name": (
                _os.environ.get("THREECAN_PROFILE_NODE_NAME")
                or configured.get("name")
                or "User preference profile"
            ),
            "cluster": (
                _os.environ.get("THREECAN_PROFILE_CLUSTER")
                or configured.get("cluster")
                or "User preferences"
            ),
        }

    def learn_preference(self, key: str, value: str, context: str = "") -> Node:
        """Persist a preference into the configured generic profile node.

        key: 偏好类别 (如 "沟通风格", "技术偏好", "审美偏向")
        value: 具体偏好内容
        context: 触发场景
        """
        profile = self._preference_profile_config()
        profile_id = profile["node_id"]
        user_node = self.nodes.get(profile_id)
        if not user_node:
            user_node = self.create_node(NodeCreate(
                id=profile_id,
                name=profile["name"],
                cluster=profile["cluster"],
                type="feedback", priority="high",
                content=NodeContent(description="Automatically retained user preferences"),
            ))

        # 追加到extra字段
        prefs = user_node.content.extra.get("preferences", {})
        if key not in prefs:
            prefs[key] = []
        entry = {"value": value, "context": context,
                 "time": dt.datetime.now(dt.timezone.utc).isoformat()}
        prefs[key].append(entry)
        # 保留每类最近5条
        prefs[key] = prefs[key][-5:]
        user_node.content.extra["preferences"] = prefs
        user_node.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
        self._save_node(user_node)
        self._update_single_embedding(profile_id)
        return user_node

    # ── 统计 ──

    def stats(self) -> GraphStats:
        clusters: dict[str, int] = Counter()
        types: dict[str, int] = Counter()
        active_count = blocked_count = 0
        for n in self.nodes.values():
            clusters[n.cluster] += 1
            types[n.type] += 1
            if n.status == NodeStatus.active:
                active_count += 1
            elif n.status == NodeStatus.blocked:
                blocked_count += 1

        conn_count: dict[str, int] = defaultdict(int)
        for e in self.edges:
            conn_count[e.source] += 1
            conn_count[e.target] += 1
        mc = sorted(conn_count.items(), key=lambda x: x[1], reverse=True)[:5]
        mc_list = [{"id": nid, "name": self.nodes[nid].name if nid in self.nodes else "?",
                     "connections": cnt} for nid, cnt in mc]

        return GraphStats(
            total_nodes=len(self.nodes), total_edges=len(self.edges),
            active_nodes=active_count, blocked_nodes=blocked_count,
            clusters=dict(clusters), node_types=dict(types),
            most_connected=mc_list,
            last_updated=max((n.updated_at for n in self.nodes.values()), default=""),
        )

    def project_reality_diagnostics(self) -> dict[str, Any]:
        """Logical hot/history projection; diagnostic only, never readiness."""

        superseded_ids = self._superseded_node_ids()
        historical_ids = {
            node_id
            for node_id, node in self.nodes.items()
            if self._is_historical_error_node(node)
        }
        hot_ids = {
            node_id
            for node_id in self.nodes
            if self._node_is_hot_route_candidate(node_id, superseded_ids)
        }
        durable_ids = {
            node_id
            for node_id in hot_ids
            if semantic_id_family(node_id) in self._CURRENT_DURABLE_FAMILIES
        }
        sediment_ids = {
            node_id
            for node_id in self.nodes
            if semantic_id_family(node_id) in self._CURRENT_SEDIMENT_FAMILIES
        }
        hot_relations = sum(
            1
            for edge in self.edges
            if edge.source in hot_ids and edge.target in hot_ids
        )
        historical_archive_relations = sum(
            1
            for edge in self.edges
            if edge.source in historical_ids or edge.target in historical_ids
        )
        historical_only_relations = sum(
            1
            for edge in self.edges
            if edge.source in historical_ids and edge.target in historical_ids
        )
        return {
            "schema": "3can.effective-project-reality/v1",
            "status": "observed",
            "hard_gate": False,
            "raw_node_count": len(self.nodes),
            "raw_edge_count": len(self.edges),
            "historical_archive_node_count": len(historical_ids),
            "hot_route_eligible_node_count": len(hot_ids),
            "durable_current_candidate_count": len(durable_ids),
            "session_handoff_count": len(sediment_ids),
            "hot_relation_count": hot_relations,
            "non_hot_relation_count": len(self.edges) - hot_relations,
            "historical_archive_relation_count": historical_archive_relations,
            "historical_only_relation_count": historical_only_relations,
            "semantic_quality": {
                "status": "validating",
                "criteria_source": "real-query benchmark required",
            },
        }

    # ═══════════════════════════════════════════════
    # FEATURE 5: 节点生命周期管理
    # ═══════════════════════════════════════════════

    def lifecycle_sweep(self, stale_days: int = 30, archive_days: int = 60) -> dict:
        """R13 生命周期扫描 (30天基准线, 永不删除)。

        Active  → Dormant:  stale_days天(默认30)未被route/writeback且activation=0
        Dormant → Archived: archive_days天(默认60)仍未命中
        Dormant → Active: activation_count>0 且最近被访问

        Archived节点: 默认route不可见；明确历史查询和 /api/nodes/{id} 可查。
        Archived → Active 需要显式 status 更新，普通读取不会静默复活。
        30天基准认知: 项目节奏下超30天未引用的数据大概率已降序严重。
        """
        now = dt.datetime.now(dt.timezone.utc)
        stale_cutoff = (now - dt.timedelta(days=stale_days)).isoformat()
        archive_cutoff = (now - dt.timedelta(days=archive_days)).isoformat()

        promoted = []   # dormant/archived → active (被最近访问复活)
        demoted = []    # active → dormant (30天未命中)
        archived = []   # dormant → archived (60天仍未命中)
        protected = []

        # 计算每节点的活跃edge数
        active_edges: dict[str, int] = defaultdict(int)
        for e in self.edges:
            src_node = self.nodes.get(e.source)
            tgt_node = self.nodes.get(e.target)
            if src_node and src_node.status == NodeStatus.active:
                active_edges[e.source] += 1
            if tgt_node and tgt_node.status == NodeStatus.active:
                active_edges[e.target] += 1

        superseded_ids = self._superseded_node_ids()
        for nid, node in self.nodes.items():
            if (
                self._reserved_error_knowledge_id(nid)
                or (
                    semantic_id_family(nid) in _AUTHORITY_PROTECTED_FAMILIES
                    and not self._node_is_superseded(nid, node, superseded_ids)
                )
            ):
                protected.append(nid)
                continue
            if node.status in (NodeStatus.blocked,):
                continue

            last_touch = str(
                node.content.extra.get("last_accessed_at")
                or node.updated_at
                or node.created_at
                or ""
            )
            has_active_edges = active_edges.get(nid, 0) > 0

            if node.status == NodeStatus.active:
                # Active → Dormant: 30天+未命中+无活跃edge
                if last_touch < stale_cutoff and node.activation_count == 0 and not has_active_edges:
                    node.status = NodeStatus.dormant
                    node.updated_by = "lifecycle-sweep-30d"
                    self._save_node(node)
                    demoted.append(nid)

            elif node.status == NodeStatus.dormant:
                # Dormant → Active: 复活 (近期 correlated exact read)
                if node.activation_count > 0 and last_touch >= stale_cutoff:
                    node.status = NodeStatus.active
                    node.updated_by = "lifecycle-reactivated"
                    self._save_node(node)
                    promoted.append(nid)
                # Dormant → Archived: 60天仍无命中
                elif last_touch < archive_cutoff and node.activation_count == 0:
                    node.status = NodeStatus.archived
                    node.content.extra["archived_at"] = now.isoformat()
                    node.content.extra["archive_reason"] = "lifecycle_inactive"
                    node.updated_by = "lifecycle-archived-60d"
                    self._save_node(node)
                    archived.append(nid)

        return {
            "total_scanned": len(self.nodes),
            "demoted_to_dormant": len(demoted),
            "archived": len(archived),
            "reactivated": len(promoted),
            "demoted_ids": demoted[:30],
            "archived_ids": archived[:30],
            "promoted_ids": promoted[:20],
            "protected_node_count": len(protected),
            "protected_node_ids": protected[:30],
            "baseline": f"{stale_days}d dormant / {archive_days}d archive",
        }

    def get_lifecycle_stats(self) -> dict:
        """节点生命周期分布统计。"""
        status_counts = Counter()
        cluster_status: dict[str, dict] = {}
        never_accessed = 0
        stale_candidates = 0

        now = dt.datetime.now(dt.timezone.utc)
        stale_cutoff = (now - dt.timedelta(days=30)).isoformat()

        for n in self.nodes.values():
            status_counts[n.status] += 1
            cluster_status.setdefault(n.cluster, Counter())[n.status] += 1
            if n.activation_count == 0:
                never_accessed += 1
            if n.updated_at < stale_cutoff and n.activation_count == 0:
                stale_candidates += 1

        return {
            "status_distribution": dict(status_counts),
            "never_accessed": never_accessed,
            "stale_candidates": stale_candidates,
            "total": len(self.nodes),
            "health_ratio": round(1 - stale_candidates / max(len(self.nodes), 1), 2),
        }

    # ── R12-R16 节点瘦身体系 (v2.3) ──

    def health_scan(self) -> dict[str, Any]:
        """只读体检: 孤节点/零激活/相似名/prefix倾斜/合并候选。不修改数据。"""
        # 1. 连通性: 有edge的节点集合
        connected: set[str] = set()
        for e in self.edges:
            connected.add(e.source)
            connected.add(e.target)
        orphan_ids = [nid for nid in self.nodes if nid not in connected]

        # 2. 零激活
        zero_act_ids = [nid for nid, n in self.nodes.items() if n.activation_count == 0]

        # 3. Prefix分布
        prefix_counts: Counter = Counter()
        for nid in self.nodes:
            prefix_counts[nid.split("-")[0]] += 1

        # 4. 相似名(前20字符)组
        name_groups: dict[str, list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            key = n.name[:20].lower().strip()
            name_groups[key].append(nid)
        similar_groups = {k: v for k, v in name_groups.items() if len(v) >= 3}

        # 5. Embedding cosine合并候选 (仅同cluster采样, 避免O(n^2)全量)
        merge_candidates: list[dict] = []
        if self._emb_matrix is not None and len(self._node_id_order) > 0:
            cluster_buckets: dict[str, list[int]] = defaultdict(list)
            for i, nid in enumerate(self._node_id_order):
                node = self.nodes.get(nid)
                if node:
                    cluster_buckets[node.cluster].append(i)

            for cluster, indices in cluster_buckets.items():
                if len(indices) < 2 or len(indices) > 200:
                    continue  # 超大cluster跳过全量比对
                sub_matrix = self._emb_matrix[indices]
                sims = sub_matrix @ sub_matrix.T
                for a_idx in range(len(indices)):
                    for b_idx in range(a_idx + 1, len(indices)):
                        score = float(sims[a_idx, b_idx])
                        if score >= 0.92:
                            nid_a = self._node_id_order[indices[a_idx]]
                            nid_b = self._node_id_order[indices[b_idx]]
                            merge_candidates.append({
                                "node_a": nid_a,
                                "node_b": nid_b,
                                "cosine": round(score, 4),
                                "cluster": cluster,
                            })
                            if len(merge_candidates) >= 50:
                                break
                    if len(merge_candidates) >= 50:
                        break
                if len(merge_candidates) >= 50:
                    break

        # 6. DOC+HO双份检测 (同source_file)
        source_map: dict[str, list[str]] = defaultdict(list)
        for nid, n in self.nodes.items():
            for f in n.content.key_files:
                source_map[f].append(nid)
        duplicate_sources = {f: ids for f, ids in source_map.items() if len(ids) >= 2}

        total = len(self.nodes)
        orphan_pct = round(100 * len(orphan_ids) / max(total, 1), 1)
        zero_pct = round(100 * len(zero_act_ids) / max(total, 1), 1)

        return {
            "total_nodes": total,
            "total_edges": len(self.edges),
            "orphan_nodes": len(orphan_ids),
            "orphan_pct": orphan_pct,
            "zero_activation": len(zero_act_ids),
            "zero_activation_pct": zero_pct,
            "prefix_distribution": dict(prefix_counts.most_common(15)),
            "similar_name_groups": len(similar_groups),
            "similar_name_samples": {k: v[:5] for k, v in list(similar_groups.items())[:10]},
            "merge_candidates_cosine": merge_candidates[:30],
            "duplicate_source_files": {f: ids for f, ids in list(duplicate_sources.items())[:20]},
            "health_score": round(100 - orphan_pct * 0.4 - zero_pct * 0.3 - len(merge_candidates) * 0.5, 1),
            "alerts": [
                a for a in [
                    f"孤节点率{orphan_pct}%>30%，建议补edge或dormant" if orphan_pct > 30 else None,
                    f"零激活率{zero_pct}%>70%，建议lifecycle_sweep" if zero_pct > 70 else None,
                    f"{len(merge_candidates)}对cosine≥0.92合并候选" if merge_candidates else None,
                    f"INTF占{prefix_counts.get('INTF',0)}/{total}={round(100*prefix_counts.get('INTF',0)/max(total,1))}%，过重" if prefix_counts.get("INTF", 0) / max(total, 1) > 0.30 else None,
                ] if a
            ],
        }

    def merge_nodes(self, keep_id: str, remove_id: str, approver: str = "system") -> dict:
        """合并两个节点: keep保留, remove的keywords/notes/edges/contributors并入keep, remove转dormant。

        永不删除。approver必须是Ka或admin。
        """
        if not str(approver or "").strip() or str(approver).casefold() == "unknown":
            return {"error": "merge_approver_required"}
        if keep_id == remove_id:
            return {"error": "merge_nodes_must_be_distinct"}
        try:
            self._assert_error_knowledge_mutation_owner(keep_id, remove_id)
        except PermissionError as exc:
            return {
                "error": "error_knowledge_merge_forbidden",
                "detail": str(exc),
            }
        keep = self.nodes.get(keep_id)
        remove = self.nodes.get(remove_id)
        if not keep:
            return {"error": f"keep节点 {keep_id} 不存在"}
        if not remove:
            return {"error": f"remove节点 {remove_id} 不存在"}
        if semantic_id_family(keep_id) in _AUTHORITY_PROTECTED_FAMILIES:
            return {
                "error": "durable_current_merge_forbidden",
                "guidance": "create an authoritative replacement and add a supersedes edge",
            }
        try:
            self.validate_supersession(keep_id, remove_id)
        except ValueError as exc:
            return {"error": str(exc)}

        now = dt.datetime.now(dt.timezone.utc).isoformat()

        # activation_keywords并集
        kw_set = set(keep.activation_keywords)
        kw_set.update(remove.activation_keywords)
        keep.activation_keywords = sorted(kw_set)

        # notes append
        if remove.content.notes:
            separator = f"\n\n--- merged from {remove_id} ({now[:10]}) ---\n"
            keep.content.notes = (keep.content.notes or "") + separator + remove.content.notes
            # 超2000字截断
            if len(keep.content.notes) > 2000:
                keep.content.notes = keep.content.notes[:1950] + "\n...[truncated by merge]"

        # description补充
        if remove.content.description and remove.content.description not in (keep.content.description or ""):
            keep.content.description = (keep.content.description or "") + " | " + remove.content.description
            if len(keep.content.description) > 500:
                keep.content.description = keep.content.description[:480] + "..."

        # key_files并集
        keep.content.key_files = list(set(keep.content.key_files + remove.content.key_files))

        # contributors合并
        contributors = set(getattr(keep, "contributors", []))
        contributors.update(getattr(remove, "contributors", []))
        remove_author = getattr(remove, "primary_author", "system")
        if remove_author != "system":
            contributors.add(remove_author)
        keep.contributors = sorted(contributors)

        # activation_count累加
        keep.activation_count += remove.activation_count

        # edges重定向: remove的edges → keep
        redirected = 0
        for edge in self.edges:
            if edge.source == remove_id:
                edge.source = keep_id
                redirected += 1
            if edge.target == remove_id:
                edge.target = keep_id
                redirected += 1
        cleanup = self.cleanup_edges(apply=True)

        # 建supersedes edge
        from models import EdgeCreate
        self.create_edge(EdgeCreate(
            source=keep_id, target=remove_id,
            type="supersedes", weight=1.0,
            description=f"merged by {approver} at {now[:10]}",
        ))

        # remove → dormant
        remove.status = NodeStatus.dormant
        remove.updated_at = now
        remove.updated_by = f"merged-into-{keep_id}"

        # keep更新
        keep.updated_at = now
        keep.updated_by = f"merge-by-{approver}"

        self._save_node(keep)
        self._save_node(remove)
        self._save_edges()
        self._update_single_embedding(keep_id)

        return {
            "keep": keep_id,
            "removed": remove_id,
            "approver": approver,
            "keywords_merged": len(keep.activation_keywords),
            "edges_redirected": redirected,
            "edges_normalized": (
                cleanup["removed_self_edges"]
                + cleanup["removed_duplicate_edges"]
            ),
            "remove_status": "dormant",
        }

    def batch_dormant(self, node_ids: list[str], reason: str = "health-scan") -> dict:
        """批量将节点转dormant (不删除)。"""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        done = []
        skipped_protected = []
        for nid in node_ids:
            if (
                self._reserved_error_knowledge_id(nid)
                or semantic_id_family(nid) in _AUTHORITY_PROTECTED_FAMILIES
            ):
                skipped_protected.append(nid)
                continue
            node = self.nodes.get(nid)
            if node and node.status == NodeStatus.active:
                node.status = NodeStatus.dormant
                node.updated_at = now
                node.updated_by = f"batch-dormant-{reason}"
                self._save_node(node)
                done.append(nid)
        return {
            "dormant_count": len(done),
            "ids": done[:30],
            "skipped_protected_ids": skipped_protected[:30],
        }

    def export_graph(self) -> dict[str, Any]:
        nodes_data = []
        for n in self.nodes.values():
            nodes_data.append({
                "id": n.id, "name": n.name, "cluster": n.cluster, "layer": n.layer,
                "type": n.type, "status": n.status, "priority": n.priority,
                "activation_count": n.activation_count, "keywords": n.activation_keywords,
                "description": n.content.description, "current_state": n.content.current_state,
                "tech_stack": n.content.tech_stack, "key_files": n.content.key_files,
                "blockers": n.content.blockers, "last_session": n.content.last_session,
                "notes": n.content.notes, "updated_at": n.updated_at, "updated_by": n.updated_by,
                "primary_author": getattr(n, "primary_author", "system"),
                "contributors": getattr(n, "contributors", []),
            })
        links_data = [{"source": e.source, "target": e.target, "type": e.type,
                        "weight": e.weight, "description": e.description} for e in self.edges]
        return {"nodes": nodes_data, "links": links_data}

    # ── 工具 ──

    def _gen_id(self, cluster: str) -> str:
        prefix_map = {
            "数据基础设施": "DAT", "架构设计": "ARC", "工具链": "TL",
            "业务逻辑": "BIZ", "前端UI": "FE", "运营知识": "OPS",
            "密钥配置": "SEC", "会话记录": "SES", "系统元节点": "SYS",
            "产品与工艺": "PRD", "战略决策": "STR", "Agent协作": "AGT",
            "训练与模型": "MDL", "错误与教训": "ERR", "项目模块": "MOD",
            "用户画像": "USR", "外部引用": "REF", "反馈与规则": "FEE",
        }
        prefix = prefix_map.get(cluster, "N")
        return f"{prefix}-{uuid.uuid4().hex[:6]}"

    # ═══════════════════════════════════════════════
    # FEATURE 1: 同步层 — memory/目录变更检测
    # ═══════════════════════════════════════════════

    _file_hashes: dict[str, str] = {}  # path → md5
    _sync_thread: threading.Thread | None = None
    _sync_running = False

    def start_sync_watcher(self, watch_dirs: list[Path], interval: int = 30) -> None:
        """启动后台线程，定期扫描目录变更并更新节点。"""
        if self._sync_running:
            return
        self._sync_running = True

        # 初始快照
        for d in watch_dirs:
            if d.exists():
                for f in d.glob("*.md"):
                    self._file_hashes[str(f)] = self._hash_file(f)
                for f in d.glob("*.json"):
                    self._file_hashes[str(f)] = self._hash_file(f)

        def _watch_loop():
            while self._sync_running:
                changes = self._detect_changes(watch_dirs)
                if changes:
                    print(f"[3CAN Sync] Detected {len(changes)} file changes")
                    self._apply_file_changes(changes)
                time.sleep(interval)

        self._sync_thread = threading.Thread(target=_watch_loop, daemon=True, name="3can-sync")
        self._sync_thread.start()
        print(f"[3CAN Sync] Watcher started: {len(watch_dirs)} dirs, {interval}s interval")

    def stop_sync_watcher(self) -> None:
        self._sync_running = False

    @staticmethod
    def _hash_file(path: Path) -> str:
        try:
            return hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            return ""

    def _detect_changes(self, watch_dirs: list[Path]) -> list[dict]:
        """检测新增/修改/删除的文件。"""
        changes = []
        current_files: set[str] = set()

        for d in watch_dirs:
            if not d.exists():
                continue
            for pattern in ("*.md", "*.json", "*.py"):
                for f in d.glob(pattern):
                    fpath = str(f)
                    current_files.add(fpath)
                    new_hash = self._hash_file(f)
                    old_hash = self._file_hashes.get(fpath)

                    if old_hash is None:
                        changes.append({"type": "created", "path": fpath, "file": f})
                    elif new_hash != old_hash:
                        changes.append({"type": "modified", "path": fpath, "file": f})
                    self._file_hashes[fpath] = new_hash

        # 检测删除
        for fpath in list(self._file_hashes.keys()):
            if fpath not in current_files:
                changes.append({"type": "deleted", "path": fpath})
                del self._file_hashes[fpath]

        return changes

    def _apply_file_changes(self, changes: list[dict]) -> None:
        """将文件变更同步到图节点 + 被动Agent更新。"""
        for change in changes:
            path = change["path"]
            ctype = change["type"]
            fname = Path(path).stem

            # ── 被动Agent更新: 解析handoff/session memory自动checkin ──
            if ctype in ("created", "modified"):
                f = change.get("file")
                if f and f.suffix == ".md" and f.exists():
                    try:
                        content = f.read_text(encoding="utf-8")
                        self._passive_agent_update(f, content)
                    except Exception:
                        content = ""

            # 找到引用此文件的节点 (严格匹配: 只看 id 和 key_files, 不用 name 子串)
            matching_nodes = []
            fname_norm = fname.lower()
            for nid, node in self.nodes.items():
                if self._reserved_error_knowledge_id(nid):
                    continue
                nid_norm = nid.lower()
                # (1) id 完全等于文件名, 或文件名是 id 的去日期后缀 (ERR-foo-20260422 ← foo.md)
                if fname_norm == nid_norm or nid_norm.startswith(fname_norm + "-"):
                    matching_nodes.append(nid)
                # (2) 文件名精确出现在 key_files (作为独立路径元素, 不是子串)
                elif any(fname == Path(kf).stem for kf in node.content.key_files):
                    matching_nodes.append(nid)

            if ctype in ("created", "modified"):
                f = change.get("file")
                if f and f.suffix == ".md" and f.exists():
                    try:
                        content = f.read_text(encoding="utf-8")[:300]
                        for nid in matching_nodes:
                            node = self.nodes[nid]
                            # 非破坏性: 只在 notes 为空, 或上次也是 sync-watcher 写的时候才覆盖
                            # 防止盲覆盖 agent 主动写入的 structured notes
                            prev_notes = (node.content.notes or "").strip()
                            prev_by = getattr(node, "updated_by", "") or ""
                            if prev_notes and prev_by != "sync-watcher":
                                logging.getLogger("3can").info(
                                    f"[3CAN Sync] Skip overwrite {nid}: notes set by {prev_by}"
                                )
                                continue
                            node.content.notes = content
                            node.updated_at = dt.datetime.now(dt.timezone.utc).isoformat()
                            node.updated_by = "sync-watcher"
                            self._save_node(node)
                            self._update_single_embedding(nid)
                            print(f"  [3CAN Sync] Updated {nid} from {Path(path).name}")
                    except Exception as _e:
                        logging.getLogger("3can").debug(f"Ignored: {_e}")

            if not matching_nodes and ctype == "created":
                # 自动为新文件创建节点 + 增量embedding (不需要reload)
                f = change.get("file")
                if f and f.suffix == ".md" and f.exists():
                    self._auto_create_node_for_file(f)

    def _auto_create_node_for_file(self, filepath: Path) -> None:
        """为新检测到的文件自动创建节点 + 增量embedding。

        不需要reload全图。30秒内新文件自动可被route命中。
        """
        try:
            try:
                content = filepath.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                logging.getLogger("3can").warning(
                    "Skip auto-create for non-UTF8 file %s: %s", filepath, exc
                )
                return
            fname = filepath.stem
            relpath = filepath.name

            # 提取标题
            title = ""
            for line in content.split("\n")[:10]:
                if line.startswith("# "):
                    title = line[2:].strip()[:80]
                    break
            if not title:
                title = fname.replace("-", " ").replace("_", " ")[:60]

            # 推断类型和cluster
            path_lower = str(filepath).lower()
            if "handoff" in path_lower:
                cluster, prefix, node_type = "项目交接", "HO", "session"
                priority = "high"
            elif "memory" in path_lower:
                cluster, prefix, node_type = "会话记录", "MEM", "session"
                priority = "medium"
            elif "rules" in path_lower:
                cluster, prefix, node_type = "反馈与规则", "RUL", "feedback"
                priority = "high"
            elif "specs" in path_lower or "docs" in path_lower:
                cluster, prefix, node_type = "项目文档", "DOC", "knowledge"
                priority = "high"
            else:
                cluster, prefix, node_type = "项目文档", "DOC", "knowledge"
                priority = "medium"

            # 生成节点ID
            stem = fname.replace("_", "-")[:30]
            node_id = f"{prefix}-{stem}"

            # 避免ID冲突
            if node_id in self.nodes:
                node_id = f"{prefix}-{stem}-{uuid.uuid4().hex[:4]}"

            # 提取关键词
            import re
            keywords = re.findall(r"S\d+[a-d]?", fname)
            words = re.findall(r"[A-Z][a-z]{3,}|[a-z]{4,}", title)
            keywords.extend(words[:5])
            keywords.append(fname)

            # 创建节点
            req = NodeCreate(
                id=node_id,
                name=title,
                cluster=cluster,
                layer="L2",
                type=node_type,
                priority=priority,
                content=NodeContent(
                    description=f"Auto-ingested: {relpath}",
                    key_files=[relpath],
                    last_session="auto-sync",
                    notes=content[:400],
                ),
                activation_keywords=keywords[:12],
            )
            self.create_node(req)  # create_node已包含_update_single_embedding
            print(f"  [3CAN Sync] AUTO-CREATED node {node_id} from {relpath} (title: {title[:40]})")

        except Exception as e:
            print(f"  [3CAN Sync] Failed to auto-create node for {filepath.name}: {e}")

    def _passive_agent_update(self, filepath: Path, content: str) -> None:
        """从handoff/session memory文件自动提取agent信息并更新registry。

        解析两种格式:
        1. Handoff: "- **Session**: S59 (Opus, 全天session)" / "- **日期**: ..."
        2. Session memory frontmatter: "originSessionId: xxx" / "type: project/session"

        正常落盘 = 自动checkin + writeback。不需要agent主动调API。
        """
        fname = filepath.name
        # ── 解析handoff文件 (docs/specs/handoffs/active/*.md) ──
        if "handoff" in str(filepath).lower() or fname.startswith("2026-"):
            agent_id = None
            session_label = ""
            task_summary = ""
            status = ""

            for line in content[:2000].split("\n"):
                line_stripped = line.strip()
                # "- **Session**: S59 (Opus, 全天session)"
                if "**Session**" in line_stripped or "**session**" in line_stripped:
                    session_label = line_stripped.split(":", 1)[-1].strip().strip("*")
                    # 推断agent_id
                    ll = session_label.lower()
                    if "opus" in ll and ("video" in ll or "视频" in ll):
                        agent_id = "opus2-video"
                    elif "opus" in ll and ("3can" in ll or "neural" in ll):
                        agent_id = "opus3"
                    elif "codex" in ll:
                        agent_id = "codex-cli"
                    elif "sonnet" in ll:
                        agent_id = "sonnet"
                    elif "opus" in ll:
                        agent_id = "opus-main"
                # "- **状态**: 验证通过，进入MVP迭代"
                if "**状态**" in line_stripped or "**status**" in line_stripped.lower():
                    status = line_stripped.split(":", 1)[-1].strip().strip("*")
                # "- **关键结论**: ..."
                if "**关键结论**" in line_stripped or "**结论**" in line_stripped:
                    task_summary = line_stripped.split(":", 1)[-1].strip().strip("*")[:100]
                # "# S59 Handoff: ..."
                if line_stripped.startswith("# ") and not task_summary:
                    task_summary = line_stripped[2:].strip()[:100]

            if agent_id:
                self.agent_checkin(
                    agent_id=agent_id,
                    current_task=task_summary or f"handoff: {fname}",
                    meta={"source": "passive-handoff", "file": fname, "status": status},
                )
                print(f"  [3CAN Passive] Agent {agent_id} auto-updated from handoff: {fname}")

        # ── 解析session memory文件 (memory/session-*.md) ──
        if fname.startswith("session-") and fname.endswith(".md"):
            agent_id = None
            session_id = ""
            task_summary = ""

            # YAML frontmatter解析
            if content.startswith("---"):
                end_idx = content.find("---", 3)
                if end_idx > 0:
                    frontmatter = content[3:end_idx]
                    for line in frontmatter.split("\n"):
                        line_stripped = line.strip()
                        if line_stripped.startswith("name:"):
                            task_summary = line_stripped.split(":", 1)[-1].strip()[:100]
                        if line_stripped.startswith("originSessionId:"):
                            session_id = line_stripped.split(":", 1)[-1].strip()

            # 从文件名推断session号和agent
            # session-20260413-S60-script-precision-gate-a-v2.md
            # session-20260413-S59-3CAN-validation.md
            name_lower = fname.lower()
            if "3can" in name_lower or "neural" in name_lower:
                agent_id = "opus3"
            elif "video" in name_lower or "gate-a" in name_lower or "script-precision" in name_lower:
                agent_id = "opus2-video"
            elif "codex" in name_lower:
                agent_id = "codex-cli"
            else:
                # Default to opus-main for generic session memories
                agent_id = "opus-main"

            if agent_id:
                self.agent_checkin(
                    agent_id=agent_id,
                    current_task=task_summary or fname,
                    session_id=session_id,
                    meta={"source": "passive-session-memory", "file": fname},
                )
                print(f"  [3CAN Passive] Agent {agent_id} auto-updated from session: {fname}")

    # ═══════════════════════════════════════════════
    # FEATURE 2: Memory目录全量rescan
    # ═══════════════════════════════════════════════

    def rescan_memory_dir(self, memory_dir: Path) -> dict:
        """扫描memory/目录，检测与图节点的差异，返回新增/修改/孤立的文件列表。"""
        if not memory_dir.exists():
            return {"error": f"Directory not found: {memory_dir}"}

        md_files = list(memory_dir.glob("*.md"))
        md_files = [f for f in md_files if f.name != "MEMORY.md"]

        # 当前图中来自memory的节点
        mem_node_names = set()
        for n in self.nodes.values():
            if n.cluster in ("会话记录", "反馈与规则", "战略决策", "外部引用", "业务逻辑", "工具链", "训练与模型"):
                mem_node_names.add(n.name.lower().replace(" ", ""))

        new_files = []
        modified = []
        for f in md_files:
            # 检查是否有对应节点
            stem = f.stem.lower().replace("-", "").replace("_", "")
            found = False
            for nid, node in self.nodes.items():
                node_stem = node.name.lower().replace(" ", "").replace("-", "").replace("_", "")
                if stem[:12] in node_stem or node_stem[:12] in stem:
                    found = True
                    # 检查内容是否比节点新
                    file_mtime = dt.datetime.fromtimestamp(f.stat().st_mtime, tz=dt.timezone.utc).isoformat()
                    if file_mtime > node.updated_at:
                        modified.append({"file": f.name, "node_id": nid, "file_mtime": file_mtime})
                    break
            if not found:
                new_files.append(f.name)

        return {
            "total_files": len(md_files),
            "new_files": new_files,
            "modified_since_sync": modified,
            "new_count": len(new_files),
            "modified_count": len(modified),
        }

    # ═══════════════════════════════════════════════
    # FEATURE 4: Multi-Agent Coordination Layer
    # ═══════════════════════════════════════════════

    def _save_agents(self) -> None:
        _atomic_write_json(
            AGENTS_FILE,
            [agent.model_dump() for agent in self.agents.values()],
        )

    def _save_activity_log(self) -> None:
        # 只保留最近 MAX_ACTIVITY_LOG 条
        self.activity_log = self.activity_log[-MAX_ACTIVITY_LOG:]
        _atomic_write_json(
            ACTIVITY_FILE,
            [entry.model_dump() for entry in self.activity_log],
        )

    @staticmethod
    def _compute_entry_hash(entry: ActivityEntry) -> str:
        """v9.3 SHA256(timestamp + agent_id + action + detail + sorted(affected_nodes) + meta + prev_hash)."""
        import hashlib
        affected = ",".join(sorted(entry.affected_nodes or []))
        meta_str = json.dumps(entry.meta or {}, sort_keys=True, ensure_ascii=False)
        payload = f"{entry.timestamp}|{entry.agent_id}|{entry.action}|{entry.detail}|{affected}|{meta_str}|{entry.prev_hash}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def log_activity(self, agent_id: str, action: str, detail: str = "",
                     affected_nodes: list[str] | None = None, meta: dict | None = None) -> ActivityEntry:
        """记录一条活动日志. v9.3: hash chain. v9.4: 实时 WS broadcast (基座#6)."""
        prev = self.activity_log[-1].self_hash if self.activity_log else "0" * 64
        if not prev:
            prev = "0" * 64
        entry = ActivityEntry(
            agent_id=agent_id,
            action=action,
            detail=detail,
            affected_nodes=affected_nodes or [],
            meta=meta or {},
            prev_hash=prev,
        )
        entry.self_hash = self._compute_entry_hash(entry)
        self.activity_log.append(entry)
        self._save_activity_log()
        # v9.4 基座#6: 实时 broadcast 给订阅 agents (延迟调度, 不阻塞)
        try:
            broadcaster = getattr(self, "_ws_broadcaster", None)
            if broadcaster:
                broadcaster(entry)
        except Exception:
            pass  # 订阅失败不影响主流程
        return entry

    def verify_activity_chain(self) -> dict:
        """v9.3 校验 activity_log 的 hash chain 完整性. 返回 {valid, n, breaks}."""
        breaks = []
        valid = True
        for i, e in enumerate(self.activity_log):
            if not e.self_hash:  # 老条目无 hash, 跳过
                continue
            expected = self._compute_entry_hash(e)
            if expected != e.self_hash:
                valid = False
                breaks.append({"idx": i, "ts": e.timestamp, "reason": "self_hash mismatch"})
            if i > 0:
                prev_e = self.activity_log[i - 1]
                if prev_e.self_hash and e.prev_hash != prev_e.self_hash:
                    valid = False
                    breaks.append({"idx": i, "ts": e.timestamp, "reason": "prev_hash mismatch"})
        return {"valid": valid, "n_entries": len(self.activity_log), "breaks": breaks[:10]}

    def agent_checkin(self, agent_id: str, name: str = "", role: str = "",
                      current_task: str = "", session_id: str = "",
                      capabilities: list[str] | None = None,
                      meta: dict | None = None) -> AgentInfo:
        """Agent签到/注册。已注册则更新，未注册则创建。"""
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        existing = self.agents.get(agent_id)

        if existing:
            # 更新
            if name:
                existing.name = name
            if role:
                existing.role = role
            if current_task:
                existing.current_task = current_task
            if session_id:
                existing.session_id = session_id
            if capabilities is not None:
                existing.capabilities = capabilities
            if meta:
                existing.meta.update(meta)
            existing.status = AgentStatus.online
            existing.last_checkin = now
            existing.checkin_count += 1
        else:
            # 新注册
            existing = AgentInfo(
                agent_id=agent_id,
                name=name or agent_id,
                role=role,
                status=AgentStatus.online,
                current_task=current_task,
                session_id=session_id,
                capabilities=capabilities or [],
                last_checkin=now,
                checkin_count=1,
                meta=meta or {},
            )

        self.agents[agent_id] = existing
        self._save_agents()
        self.log_activity(agent_id, "checkin", f"{existing.name} checked in: {current_task or 'idle'}")
        return existing

    def agent_update_task(self, agent_id: str, current_task: str, status: str = "busy") -> AgentInfo | None:
        """Agent更新当前任务状态。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return None
        agent.current_task = current_task
        agent.status = AgentStatus(status) if status in AgentStatus.__members__ else AgentStatus.busy
        agent.last_checkin = dt.datetime.now(dt.timezone.utc).isoformat()
        self._save_agents()
        self.log_activity(agent_id, "task_update", current_task)
        return agent

    def list_agents(self, status_filter: str | None = None) -> list[AgentInfo]:
        """列出所有注册的Agent。"""
        agents = list(self.agents.values())
        if status_filter:
            agents = [a for a in agents if a.status == status_filter]
        return sorted(agents, key=lambda a: a.last_checkin, reverse=True)

    def get_activity(self, agent_id: str | None = None, action: str | None = None,
                     limit: int = 50) -> list[ActivityEntry]:
        """查询活动日志。"""
        entries = self.activity_log
        if agent_id:
            entries = [e for e in entries if e.agent_id == agent_id]
        if action:
            entries = [e for e in entries if e.action == action]
        return entries[-limit:]

    # ═══════════════════════════════════════════════
    # FEATURE 3: 代码仓库INTF节点自动发现
    # ═══════════════════════════════════════════════

    def discover_interfaces(self, code_dir: Path, patterns: list[str] | None = None) -> list[dict]:
        """扫描代码目录，自动提取接口信息，返回建议创建的INTF节点列表。

        扫描对象: .py文件中的public函数签名、常量路径、关键字段名。
        不自动创建节点，返回建议列表供审核。
        """
        if patterns is None:
            patterns = ["tools/**/*.py", "scripts/**/*.py"]

        suggestions = []
        seen_files: set[str] = set()
        existing_intf_files = set()
        for n in self.nodes.values():
            if n.id.startswith("INTF-"):
                for kf in n.content.key_files:
                    existing_intf_files.add(kf)

        for pattern in patterns:
            for pyfile in code_dir.glob(pattern):
                relpath = str(pyfile.relative_to(code_dir)).replace("\\", "/")
                if relpath in seen_files or relpath in existing_intf_files:
                    continue
                if "venv" in relpath or "__pycache__" in relpath or "test" in relpath.lower():
                    continue
                seen_files.add(relpath)

                try:
                    src = pyfile.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue

                # 提取public函数
                funcs = re.findall(r'^(?:async )?def (\w+)\(([^)]*)\)', src, re.MULTILINE)
                public_funcs = [(n, a) for n, a in funcs if not n.startswith("_")]
                if not public_funcs:
                    continue

                # 提取字段名
                fields = re.findall(
                    r'["\']('
                    r'output_\w+|instruction|category|system|verdict|best_score|'
                    r'best_model|merged_answer|key_facts|source_type|reliability|'
                    r'data_quality_score|confidence'
                    r')["\']',
                    src,
                )
                fields = list(dict.fromkeys(fields))

                # 提取路径常量
                paths = re.findall(r'(\w+)\s*=\s*(?:Path|PROJECT_ROOT\s*/)\s*["\']([^"\']*)["\']', src)

                # 文件docstring
                docstring = ""
                try:
                    tree = ast.parse(src)
                    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
                        docstring = str(tree.body[0].value.value).split("\n")[0][:100]
                except Exception:
                    pass

                suggestions.append({
                    "file": relpath,
                    "description": docstring or f"Interface for {pyfile.stem}",
                    "public_functions": [f"{n}({a[:50]})" for n, a in public_funcs[:6]],
                    "fields": fields[:10],
                    "path_constants": paths[:5],
                    "suggested_id": f"INTF-{pyfile.stem.replace('_','-')[:20]}",
                })

        return suggestions
