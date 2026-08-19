#!/usr/bin/env python3
"""3CAN deep-research skill gate and Codex hook helper.

This wrapper is intentionally local and deterministic. It does not perform web
research by itself; it decides when research is mandatory, records a source
ledger, and blocks mutating work or final answers until the ledger exists.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
ACTIVE_PROJECT_ROOT = Path(
    os.environ.get("THREECAN_PROJECT_ROOT") or Path.cwd()
).resolve()
DEFAULT_STATE_FILE = ACTIVE_PROJECT_ROOT / "test-results" / "3can" / "research_hook_state.json"
DEFAULT_LEDGER_DIR = ACTIVE_PROJECT_ROOT / "test-results" / "3can" / "research_ledgers"
DEFAULT_FAILURE_STATE = ACTIVE_PROJECT_ROOT / "test-results" / "3can" / "research_failure_signals.json"
DEFAULT_SOURCE_ARTIFACT_DIR = ACTIVE_PROJECT_ROOT / "test-results" / "3can" / "research_sources"
DEFAULT_MIN_SOURCES = 3
MAX_COLLECT_BYTES = 1_000_000


TRIGGER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "explicit_research_or_web",
        re.compile(
            r"(联网|调研|研究|搜索|检索|查一下|看一下有没有|资料来源|source|citation|"
            r"research|web\s*search|browse|look\s*up|verify)",
            re.I,
        ),
    ),
    (
        "complex_or_multi_constraint",
        re.compile(
            r"(复杂|多约束|拆解|架构|方案|系统性|完整|严格|审计|优化|落地|推进|"
            r"\b1[、.)].*\b2[、.)]|\bfirst\b.*\bsecond\b)",
            re.I | re.S,
        ),
    ),
    (
        "failure_escalation",
        re.compile(
            r"(反复|连续|多次|第[三四五六七八九十0-9]+次|失败|卡住|试错|还是不行|"
            r"10来次|loop|retry|again|stuck|flaky)",
            re.I,
        ),
    ),
    (
        "rpa_or_platform_intel",
        re.compile(
            r"(RPA|短视频|博主|直播|评论区|转评赞|点赞|转发|收藏|抖音|小红书|B站|TikTok|"
            r"YouTube|切帧|关键帧|语音转录|字幕|ASR|OCR|爬取|抓取)",
            re.I,
        ),
    ),
    (
        "keyword_planning",
        re.compile(r"(关键词|搜索词|query|语义变体|演化|权重|同义词|黑话|术语|运营)", re.I),
    ),
    (
        "current_or_time_sensitive",
        re.compile(
            r"(最新|最近|当前|现在|today|yesterday|tomorrow|latest|recent|current|"
            r"价格|定价|政策|法规|合规|schedule|changelog|release|pricing|law|regulation)",
            re.I,
        ),
    ),
    (
        "technical_selection",
        re.compile(
            r"(技术选型|对比|竞品|社区反馈|官方文档|能力提升|benchmark|alternative|"
            r"compare|comparison|community feedback|official docs?)",
            re.I,
        ),
    ),
    (
        "provider_api_model_change",
        re.compile(
            r"(API|SDK|MCP|provider|模型|model|OpenAI|Claude|Codex|LiteLLM|Langfuse|"
            r"Firecrawl|Tavily|VEO|Seedance|HappyHorse|DeepSeek|Qwen|Doubao|APIMart)",
            re.I,
        ),
    ),
)

EVIDENCE_DIMENSIONS = (
    "authority",
    "recency",
    "practice_value",
    "reproducibility",
    "task_relevance",
    "community_signal",
    "risk",
    "conflict",
)

POSITIVE_EVIDENCE_DIMENSIONS = (
    "authority",
    "recency",
    "practice_value",
    "reproducibility",
    "task_relevance",
    "community_signal",
)

NEGATIVE_EVIDENCE_DIMENSIONS = ("risk", "conflict")

SOURCE_TYPES = (
    "official_primary",
    "academic_or_standard",
    "targeted_web",
    "github_or_issue",
    "model_hub_or_dataset",
    "community_practice",
    "community_forum",
    "benchmark_or_user_report",
    "public_platform_signal",
    "rpa_video_comment_asr_ocr",
    "rpa_pipeline_artifact",
    "known_3can_context",
)

SOURCE_STRATEGIES: dict[str, list[str]] = {
    "standard": [
        "official_primary",
        "academic_or_standard",
        "github_or_issue",
        "community_forum",
        "targeted_web",
        "known_3can_context",
    ],
    "deep": [
        "official_primary",
        "academic_or_standard",
        "github_or_issue",
        "model_hub_or_dataset",
        "community_forum",
        "benchmark_or_user_report",
        "public_platform_signal",
        "targeted_web",
        "known_3can_context",
    ],
}

SOURCE_FAMILIES: dict[str, frozenset[str]] = {
    "primary": frozenset({"official_primary"}),
    "academic": frozenset({"academic_or_standard", "benchmark_or_user_report"}),
    "implementation": frozenset({"github_or_issue", "model_hub_or_dataset"}),
    "community": frozenset({"community_practice", "community_forum"}),
    "platform": frozenset(
        {"public_platform_signal", "rpa_video_comment_asr_ocr", "rpa_pipeline_artifact"}
    ),
    "web": frozenset({"targeted_web"}),
}

LEGACY_TIER_ALIASES = {"quick": "standard", "rpa_deep": "deep"}

RPA_EVIDENCE_KINDS = (
    "video",
    "comment",
    "asr",
    "ocr",
    "engagement",
    "keyframe",
    "transcript",
    "profile",
    "search_result",
    "screenshot",
    "other",
)

RPA_APPROVAL_FLAGS = (
    "login",
    "private_data",
    "paid_api",
    "bulk_scrape",
    "account_write",
    "publish",
    "store_data_write",
)

SAFE_RPA_PROBE_PLATFORMS = ("creator-content", "taobao")

TIME_BUDGETS: dict[str, dict[str, Any]] = {
    "standard": {
        "decision_check_minutes": 6,
        "target_minutes": 8,
        "hard_cap_minutes": 10,
        "min_sources": 30,
        "min_source_families": 5,
        "min_query_variants": 6,
        "max_query_variants": 14,
        "sidecar_required": True,
    },
    "deep": {
        "decision_check_minutes": 15,
        "target_minutes": 25,
        "hard_cap_minutes": 30,
        "min_sources": 90,
        "min_source_families": 6,
        "min_query_variants": 18,
        "max_query_variants": 30,
        "sidecar_required": True,
        "approval_note": "Real platform login, paid API, bulk scraping, or private data requires approval.",
    },
}

MUTATING_COMMAND = re.compile(
    r"(\bapply_patch\b|\bgit\s+(commit|push|tag)\b|\bnpm\s+install\b|\bpip\s+install\b|"
    r"\buv\s+add\b|\bpoetry\s+add\b|\bsed\s+-i\b|\btee\b.*>|>\s*[\w./-]+|"
    r"\bmkdir\b|\btouch\b|\bmv\b|\bcp\b|\brm\b|\bchmod\b)",
    re.I,
)

READ_ONLY_COMMAND = re.compile(
    r"^\s*(sed\s+-n|rg\b|cat\b|head\b|tail\b|nl\b|wc\b|python3?\s+-m\s+py_compile|"
    r"python3?\s+-m\s+pytest|pytest\b|git\s+(status|diff|show|log)\b)",
    re.I,
)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _print_json(data: Any) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def _safe_load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


def _safe_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _state_key(session_id: str, turn_id: str) -> str:
    return f"{session_id or 'unknown-session'}::{turn_id or 'unknown-turn'}"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


class ResearchHTMLExtractor(HTMLParser):
    """Small stdlib HTML extractor for public source artifacts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.meta_description = ""
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg", "canvas"}:
            self._skip_depth += 1
            return
        if lowered == "title":
            self._in_title = True
            return
        if lowered == "meta":
            attr_map = {key.lower(): (value or "") for key, value in attrs}
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            if not self.meta_description and (name == "description" or prop == "og:description"):
                self.meta_description = attr_map.get("content", "").strip()
        if lowered in {"p", "div", "li", "br", "section", "article", "h1", "h2", "h3", "tr"}:
            self.text_parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style", "noscript", "svg", "canvas"} and self._skip_depth:
            self._skip_depth -= 1
        if lowered == "title":
            self._in_title = False
        if lowered in {"p", "div", "li", "section", "article", "h1", "h2", "h3", "tr"}:
            self.text_parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title_parts.append(data)
            return
        self.text_parts.append(data)


def _dedupe(items: list[str], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = re.sub(r"\s+", " ", item.strip())
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if limit is not None and len(result) >= limit:
            break
    return result


def _compact_text(text: str, max_chars: int | None = None) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if max_chars is not None and len(compact) > max_chars:
        return compact[:max_chars].rstrip()
    return compact


def extract_html_summary(html: str, *, max_chars: int = 4000) -> dict[str, str]:
    parser = ResearchHTMLExtractor()
    parser.feed(html)
    parser.close()
    return {
        "title": _compact_text(" ".join(parser.title_parts), 300),
        "meta_description": _compact_text(parser.meta_description, 500),
        "text_excerpt": _compact_text(" ".join(parser.text_parts), max_chars),
    }


def _decode_response(raw: bytes, content_type: str) -> str:
    charset = "utf-8"
    match = re.search(r"charset=([A-Za-z0-9_.-]+)", content_type or "", re.I)
    if match:
        charset = match.group(1)
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def collect_public_url(
    url: str,
    *,
    output_dir: Path,
    source_type: str = "targeted_web",
    timeout: float = 8.0,
    max_chars: int = 4000,
) -> dict[str, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {
            "ok": False,
            "status": "block",
            "url": url,
            "source_type": source_type,
            "error": "Only http(s) public URLs can be collected by this adapter.",
        }
    normalized_source_type = source_type if source_type in SOURCE_TYPES else "targeted_web"
    artifact: dict[str, Any] = {
        "ok": False,
        "status": "failed",
        "adapter": "public_url_extract",
        "url": url,
        "source_type": normalized_source_type,
        "collected_at": _now(),
        "max_collect_bytes": MAX_COLLECT_BYTES,
        "stores_secrets": False,
        "stores_raw_html": False,
    }
    try:
        request = Request(
            url,
            headers={
                "User-Agent": "3CANResearchHarness/1.0 (+public-source-artifact)",
                "Accept": "text/html,application/xhtml+xml,text/plain,application/json;q=0.8,*/*;q=0.2",
            },
        )
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_COLLECT_BYTES + 1)
            content_type = response.headers.get("content-type", "")
            status_code = getattr(response, "status", None) or response.getcode()
    except HTTPError as exc:
        artifact.update({"http_status": exc.code, "error": f"HTTPError: {exc.reason}"})
        return artifact
    except URLError as exc:
        artifact.update({"error": f"URLError: {exc.reason}"})
        return artifact
    except Exception as exc:
        artifact.update({"error": f"{type(exc).__name__}: {exc}"})
        return artifact

    truncated = len(raw) > MAX_COLLECT_BYTES
    if truncated:
        raw = raw[:MAX_COLLECT_BYTES]
    text = _decode_response(raw, content_type)
    if "html" in content_type.lower() or "<html" in text[:500].lower():
        summary = extract_html_summary(text, max_chars=max_chars)
    else:
        summary = {
            "title": "",
            "meta_description": "",
            "text_excerpt": _compact_text(text, max_chars),
        }

    artifact.update(
        {
            "ok": True,
            "status": "collected",
            "http_status": status_code,
            "content_type": content_type,
            "byte_count": len(raw),
            "truncated": truncated,
            "content_hash": hashlib.sha256(raw).hexdigest()[:16],
            **summary,
        }
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_now().replace(':', '').replace('-', '')}_{_hash_text(url)}.json"
    path = output_dir / name
    _safe_write_json(path, artifact)
    artifact["artifact_path"] = str(path)
    return artifact


def normalize_search_result(
    result: dict[str, Any],
    *,
    provider: str = "manual",
    query: str = "",
    source_type: str = "targeted_web",
) -> dict[str, Any]:
    url = str(result.get("url") or result.get("link") or result.get("href") or "").strip()
    title = str(result.get("title") or result.get("name") or "").strip()
    snippet = str(result.get("snippet") or result.get("content") or result.get("description") or "").strip()
    score = result.get("score", result.get("rank_score", result.get("relevance_score")))
    normalized_source_type = str(result.get("source_type") or source_type)
    if normalized_source_type not in SOURCE_TYPES:
        normalized_source_type = "targeted_web"
    return {
        "ok": bool(re.match(r"^https?://", url, re.I)),
        "status": "search_result",
        "adapter": "search_result_import",
        "provider": provider,
        "query": query,
        "url": url,
        "source_type": normalized_source_type,
        "title": title,
        "meta_description": snippet,
        "text_excerpt": snippet,
        "search_score": score,
        "collected_at": _now(),
        "stores_secrets": False,
        "stores_raw_html": False,
        "content_hash": _hash_text(json.dumps({"provider": provider, "query": query, "url": url, "title": title}, sort_keys=True)),
    }


def _extract_search_results(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("results", "items", "data", "organic_results"):
        value = payload.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return [payload]


def import_search_results(
    *,
    input_file: Path,
    output_dir: Path,
    provider: str = "manual",
    query: str = "",
    source_type: str = "targeted_web",
    limit: int = 10,
) -> dict[str, Any]:
    payload = _safe_load_json(input_file, {})
    results = _extract_search_results(payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for index, result in enumerate(results[: max(0, limit)]):
        artifact = normalize_search_result(result, provider=provider, query=query, source_type=source_type)
        if not artifact["ok"]:
            invalid.append({"index": str(index), "reason": "missing valid http(s) url"})
            continue
        name = f"{_now().replace(':', '').replace('-', '')}_{index:03d}_{_hash_text(artifact['url'])}.json"
        path = output_dir / name
        _safe_write_json(path, artifact)
        artifact["artifact_path"] = str(path)
        artifacts.append(artifact)
    return {
        "ok": bool(artifacts),
        "status": "imported" if artifacts else "block",
        "adapter": "search_result_import",
        "provider": provider,
        "query": query,
        "input_file": str(input_file),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "invalid_results": invalid,
        "output_dir": str(output_dir),
    }


def _as_string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,;\n]", value) if item.strip()]
    return []


def _requires_rpa_approval(payload: dict[str, Any]) -> tuple[bool, list[str]]:
    flags = {item.lower() for item in _as_string_list(payload.get("risk_flags"))}
    for key in RPA_APPROVAL_FLAGS:
        if payload.get(key) is True or payload.get(f"requires_{key}") is True:
            flags.add(key)
    if payload.get("requires_login") is True:
        flags.add("login")
    if payload.get("uses_paid_api") is True:
        flags.add("paid_api")
    if payload.get("bulk") is True or payload.get("bulk_scrape") is True:
        flags.add("bulk_scrape")
    return bool(flags.intersection(RPA_APPROVAL_FLAGS)), sorted(flags)


def normalize_rpa_artifact(
    payload: dict[str, Any],
    *,
    approval_id: str = "",
    adapter: str = "rpa_pipeline_artifact_import",
) -> dict[str, Any]:
    url = str(payload.get("source_url") or payload.get("url") or "").strip()
    parsed = urlparse(url)
    source_type = str(payload.get("source_type") or "rpa_pipeline_artifact")
    if source_type not in SOURCE_TYPES:
        source_type = "rpa_pipeline_artifact"
    evidence_kind = str(payload.get("evidence_kind") or payload.get("kind") or "other").strip().lower()
    if evidence_kind not in RPA_EVIDENCE_KINDS:
        evidence_kind = "other"
    approval_required, risk_flags = _requires_rpa_approval(payload)
    approval = str(approval_id or payload.get("approval_id") or "").strip()
    title = str(payload.get("title") or payload.get("name") or "").strip()
    text_excerpt = _compact_text(
        str(
            payload.get("text_excerpt")
            or payload.get("snippet")
            or payload.get("transcript_excerpt")
            or payload.get("ocr_excerpt")
            or payload.get("description")
            or ""
        ),
        1600,
    )
    metadata = {
        "platform": str(payload.get("platform") or ""),
        "task_id": str(payload.get("task_id") or ""),
        "adapter": str(payload.get("adapter") or adapter),
        "evidence_kind": evidence_kind,
        "captured_at": str(payload.get("captured_at") or payload.get("collected_at") or ""),
        "engagement": payload.get("engagement") if isinstance(payload.get("engagement"), dict) else {},
        "content_hash": str(payload.get("content_hash") or ""),
        "risk_flags": risk_flags,
        "approval_required": approval_required,
        "approval_id": approval,
    }
    artifact: dict[str, Any] = {
        "ok": False,
        "status": "block",
        "adapter": "rpa_pipeline_artifact_import",
        "url": url,
        "source_type": source_type,
        "title": title,
        "meta_description": text_excerpt[:500],
        "text_excerpt": text_excerpt,
        "rpa_metadata": metadata,
        "collected_at": _now(),
        "stores_secrets": False,
        "stores_raw_html": False,
    }
    if parsed.scheme not in {"http", "https"}:
        artifact["error"] = "RPA source artifacts must carry a public http(s) source URL."
        return artifact
    if approval_required and not approval:
        artifact["error"] = "Approval id is required for login/private/paid/bulk/write/publish RPA evidence."
        return artifact
    artifact.update(
        {
            "ok": True,
            "status": "rpa_artifact",
            "content_hash": metadata["content_hash"]
            or _hash_text(json.dumps({"url": url, "title": title, "text_excerpt": text_excerpt}, sort_keys=True)),
        }
    )
    return artifact


def import_rpa_artifact(
    *,
    input_file: Path,
    output_dir: Path,
    approval_id: str = "",
) -> dict[str, Any]:
    payload = _safe_load_json(input_file, {})
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "status": "block",
            "adapter": "rpa_pipeline_artifact_import",
            "input_file": str(input_file),
            "error": "RPA artifact input must be a JSON object.",
        }
    artifact = normalize_rpa_artifact(payload, approval_id=approval_id)
    if not artifact["ok"]:
        artifact["input_file"] = str(input_file)
        return artifact
    output_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_now().replace(':', '').replace('-', '')}_{_hash_text(artifact['url'])}.json"
    path = output_dir / name
    _safe_write_json(path, artifact)
    artifact["artifact_path"] = str(path)
    artifact["input_file"] = str(input_file)
    return {
        "ok": True,
        "status": "imported",
        "adapter": "rpa_pipeline_artifact_import",
        "input_file": str(input_file),
        "artifact": artifact,
        "artifact_path": str(path),
        "output_dir": str(output_dir),
    }


def _load_json_object(value: str, *, label: str) -> dict[str, Any]:
    if not value:
        return {}
    data = json.loads(value)
    if not isinstance(data, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return data


def _rpa_probe_risk_flags(*, platform: str, adapter_task_id: str, params: dict[str, Any]) -> list[str]:
    flags = {item.lower() for item in _as_string_list(params.get("risk_flags"))}
    platform_key = platform.strip().lower()
    if platform_key not in SAFE_RPA_PROBE_PLATFORMS:
        flags.add("unknown_platform")
    if params.get("requires_login") is True or params.get("session_dir") or params.get("account_cookie"):
        flags.add("login")
    if params.get("uses_paid_api") is True or params.get("api_provider") or params.get("api_key"):
        flags.add("paid_api")
    if params.get("bulk") is True or params.get("bulk_scrape") is True:
        flags.add("bulk_scrape")
    if params.get("publish") is True or params.get("account_write") is True:
        flags.add("account_write")
    if params.get("store_data_write") is True:
        flags.add("store_data_write")
    if platform_key == "taobao" and adapter_task_id == "product_search" and params.get("use_mock") is False:
        flags.add("live_platform_collect")
    return sorted(flags)


def _text_from_review_card(card: dict[str, Any]) -> str:
    cleaned = card.get("cleaned") if isinstance(card.get("cleaned"), dict) else {}
    parts: list[str] = []
    for key in ("summary", "core_points", "action_steps", "data_points", "platform_mechanisms", "risk_flags"):
        value = cleaned.get(key)
        if isinstance(value, list):
            parts.extend(str(item) for item in value if str(item).strip())
        elif value:
            parts.append(str(value))
    return _compact_text(" ".join(parts), 1600)


def create_rpa_probe_artifacts(
    *,
    cards_payload: dict[str, Any],
    output_dir: Path,
    task_id: str,
    approval_id: str = "",
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    cards = cards_payload.get("cards") if isinstance(cards_payload, dict) else []
    if not isinstance(cards, list):
        return [], [{"index": "all", "reason": "cards payload is not a list"}]
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            invalid.append({"index": str(index), "reason": "card is not an object"})
            continue
        source = card.get("source") if isinstance(card.get("source"), dict) else {}
        source_url = str(source.get("source_url") or "").strip()
        if not re.match(r"^https?://", source_url, re.I):
            invalid.append({"index": str(index), "reason": "missing public http(s) source_url"})
            continue
        review_state = card.get("review_state") if isinstance(card.get("review_state"), dict) else {}
        payload = {
            "source_url": source_url,
            "platform": str(source.get("platform") or ""),
            "task_id": task_id,
            "evidence_kind": "transcript",
            "title": str(source.get("title") or ""),
            "text_excerpt": _text_from_review_card(card),
            "engagement": source.get("engagement") if isinstance(source.get("engagement"), dict) else {},
            "captured_at": str(source.get("collected_at") or ""),
            "content_hash": str(card.get("evidence_id") or ""),
            "adapter": "rpa_probe_adapter_review",
            "source_type": "rpa_pipeline_artifact",
            "approval_id": approval_id,
            "quality_label": str(review_state.get("quality_label") or ""),
            "quality_score": review_state.get("quality_score"),
        }
        artifact = normalize_rpa_artifact(payload, approval_id=approval_id, adapter="rpa_probe_adapter_review")
        if not artifact["ok"]:
            invalid.append({"index": str(index), "reason": str(artifact.get("error") or artifact.get("status"))})
            continue
        artifact["rpa_metadata"]["run_id"] = str(cards_payload.get("run_id") or card.get("run_id") or "")
        artifact["rpa_metadata"]["evidence_id"] = str(card.get("evidence_id") or "")
        artifact["rpa_metadata"]["quality_label"] = str(review_state.get("quality_label") or "")
        artifact["rpa_metadata"]["quality_score"] = review_state.get("quality_score")
        name = f"{_now().replace(':', '').replace('-', '')}_{index:03d}_{_hash_text(source_url)}.json"
        path = output_dir / name
        _safe_write_json(path, artifact)
        artifact["artifact_path"] = str(path)
        artifacts.append(artifact)
    return artifacts, invalid


def run_rpa_probe(
    *,
    mode: str,
    output_dir: Path | None = None,
    approval_id: str = "",
    task_id: str = "R5",
    merchant_id: str = "research-probe",
    platform: str = "creator-content",
    adapter_task_id: str = "",
    params: dict[str, Any] | None = None,
    db_path: Path | None = None,
    artifact_root: Path | None = None,
    cards_output: Path | None = None,
    cards_limit: int = 20,
    cross_validate: bool = True,
    project_root: Path | None = None,
) -> dict[str, Any]:
    selected_root = Path(
        project_root or os.environ.get("THREECAN_PROJECT_ROOT") or Path.cwd()
    ).resolve()
    if not (selected_root / "tools" / "rpa").is_dir():
        return {
            "ok": False,
            "status": "unavailable",
            "adapter": "rpa_probe",
            "error": "project_rpa_adapter_unavailable",
        }
    probe_dir = selected_root / "test-results" / "3can" / "rpa_probe"
    selected_output_dir = (
        output_dir.resolve()
        if output_dir is not None
        else selected_root / "test-results" / "3can" / "research_sources"
    )

    if mode == "control-plane":
        try:
            with _project_rpa_import_scope(selected_root):
                control_plane = importlib.import_module("tools.rpa.control_plane")
                summary = control_plane.build_control_plane_summary(
                    db_path or probe_dir / "rpa_probe.db"
                )
        except ModuleNotFoundError:
            return {
                "ok": False,
                "status": "unavailable",
                "adapter": "rpa_probe",
                "error": "project_rpa_adapter_unavailable",
            }

        return {
            "ok": True,
            "status": "control_plane",
            "adapter": "rpa_probe",
            "project_root_source": (
                "argument"
                if project_root
                else "environment"
                if os.environ.get("THREECAN_PROJECT_ROOT")
                else "cwd"
            ),
            "control_plane": summary,
            "output_dir": str(selected_output_dir),
        }
    if mode != "adapter-review":
        return {"ok": False, "status": "block", "adapter": "rpa_probe", "error": f"unsupported rpa probe mode: {mode}"}

    probe_params = dict(params or {})
    risk_flags = _rpa_probe_risk_flags(platform=platform, adapter_task_id=adapter_task_id or task_id, params=probe_params)
    if risk_flags and not approval_id:
        return {
            "ok": False,
            "status": "block",
            "adapter": "rpa_probe",
            "risk_flags": risk_flags,
            "error": "Approval id is required before running this RPA probe.",
        }

    selected_db_path = db_path or probe_dir / "rpa_probe.db"
    selected_artifact_root = artifact_root or probe_dir / "artifacts"
    selected_cards_output = cards_output or probe_dir / "cards.json"
    try:
        with _project_rpa_import_scope(selected_root):
            adapter_review = importlib.import_module("tools.rpa.adapter_review_pipeline")
            result = adapter_review.run_adapter_review_pipeline(
                task_id=task_id,
                merchant_id=merchant_id,
                platform=platform,
                adapter_task_id=adapter_task_id,
                params=probe_params,
                db_path=selected_db_path,
                artifact_root=selected_artifact_root,
                cards_output=selected_cards_output,
                cards_limit=cards_limit,
                cross_validate=cross_validate,
            )
    except ModuleNotFoundError:
        return {
            "ok": False,
            "status": "unavailable",
            "adapter": "rpa_probe",
            "error": "project_rpa_adapter_unavailable",
        }
    if not result.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "adapter": "rpa_probe",
            "probe": result,
            "risk_flags": risk_flags,
        }
    cards_payload = _safe_load_json(selected_cards_output, {})
    artifacts, invalid = create_rpa_probe_artifacts(
        cards_payload=cards_payload,
        output_dir=selected_output_dir,
        task_id=task_id,
        approval_id=approval_id,
    )
    return {
        "ok": bool(artifacts),
        "status": "probed" if artifacts else "block",
        "adapter": "rpa_probe",
        "mode": mode,
        "risk_flags": risk_flags,
        "probe": {
            "run_id": result.get("run_id"),
            "collect": result.get("collect"),
            "cross_validation": result.get("cross_validation"),
            "review_cards": result.get("review_cards"),
            "export": result.get("export"),
        },
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "invalid_cards": invalid,
        "db_path": str(selected_db_path),
        "cards_output": str(selected_cards_output),
        "output_dir": str(selected_output_dir),
    }


@contextmanager
def _project_rpa_import_scope(project_root: Path):
    """Load one project's tools.rpa namespace without leaking it to another project."""

    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if name == "tools" or name.startswith("tools.")
    }
    for name in saved_modules:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(project_root))
    importlib.invalidate_caches()
    try:
        yield
    finally:
        for name in list(sys.modules):
            if name == "tools" or name.startswith("tools."):
                sys.modules.pop(name, None)
        sys.modules.update(saved_modules)
        sys.path[:] = saved_path
        importlib.invalidate_caches()


def load_source_artifacts(paths: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    artifacts: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for raw_path in paths:
        path = Path(raw_path)
        data = _safe_load_json(path, {})
        if not isinstance(data, dict) or not data.get("url"):
            invalid.append({"path": str(path), "reason": "missing or invalid source artifact"})
            continue
        if data.get("ok") is not True or data.get("status") not in {"collected", "search_result", "rpa_artifact"}:
            invalid.append({"path": str(path), "reason": str(data.get("error") or data.get("status") or "not collected")})
            continue
        artifacts.append(
            {
                "artifact_path": str(path),
                "status": str(data.get("status") or ""),
                "adapter": str(data.get("adapter") or ""),
                "url": str(data.get("url") or ""),
                "source_type": str(data.get("source_type") or "targeted_web"),
                "title": str(data.get("title") or ""),
                "meta_description": str(data.get("meta_description") or ""),
                "text_excerpt": str(data.get("text_excerpt") or ""),
                "content_hash": str(data.get("content_hash") or ""),
                "http_status": data.get("http_status"),
                "content_type": str(data.get("content_type") or ""),
                "provider": str(data.get("provider") or ""),
                "query": str(data.get("query") or ""),
                "search_score": data.get("search_score"),
                "rpa_metadata": data.get("rpa_metadata") if isinstance(data.get("rpa_metadata"), dict) else {},
                "collected_at": str(data.get("collected_at") or ""),
                "stores_raw_html": bool(data.get("stores_raw_html")),
                "stores_secrets": bool(data.get("stores_secrets")),
                "opened_verified": _artifact_proves_opened_source(data),
            }
        )
    return artifacts, invalid


def _artifact_proves_opened_source(artifact: dict[str, Any]) -> bool:
    status = str(artifact.get("status") or "")
    content_hash = str(artifact.get("content_hash") or "").strip()
    if status == "collected":
        http_status = artifact.get("http_status")
        return (
            isinstance(http_status, int)
            and 200 <= http_status < 400
            and bool(content_hash)
        )
    if status == "rpa_artifact":
        metadata = (
            artifact.get("rpa_metadata")
            if isinstance(artifact.get("rpa_metadata"), dict)
            else {}
        )
        upstream_hash = str(metadata.get("content_hash") or "").strip()
        observed_content = bool(
            str(artifact.get("text_excerpt") or "").strip()
            or metadata.get("captured_at")
            or metadata.get("engagement")
        )
        return bool(content_hash and upstream_hash and observed_content)
    return False


def _classify_research_tier(hit_rules: set[str]) -> str:
    if hit_rules & {"rpa_or_platform_intel", "failure_escalation"}:
        return "deep"
    if "technical_selection" in hit_rules and hit_rules & {
        "complex_or_multi_constraint",
        "provider_api_model_change",
    }:
        return "deep"
    return "standard"


def _normalize_research_tier(value: str) -> str:
    selected = LEGACY_TIER_ALIASES.get(value, value)
    return selected if selected in TIME_BUDGETS else "standard"


def _infer_query_planning(prompt: str, hit_rules: set[str]) -> dict[str, Any]:
    seed_terms = re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,}", prompt)
    seed_terms = list(dict.fromkeys(seed_terms))[:12]
    return {
        "required": bool(hit_rules & {"keyword_planning", "complex_or_multi_constraint", "rpa_or_platform_intel"}),
        "seed_terms": seed_terms,
        "expansion_axes": [
            "official_terms",
            "academic_terms",
            "implementation_terms",
            "community_terms",
            "english_chinese_variants",
            "negative_or_failure_terms",
        ],
        "platform_relevant": "rpa_or_platform_intel" in hit_rules,
    }


def _query_templates_for_tier(tier: str) -> dict[str, list[str]]:
    base = {
        "official_terms": [
            "{term} official docs",
            "{term} API reference",
            "{term} changelog release notes",
        ],
        "academic_terms": [
            "{term} paper arXiv DOI",
            "{term} benchmark reproducibility limitations",
        ],
        "implementation_terms": [
            "{term} GitHub repository issue",
            "{term} Hugging Face model dataset discussion",
        ],
        "community_terms": [
            "{term} Reddit forum field report",
            "{term} implementation gotcha",
        ],
        "english_chinese_variants": [
            "{term} 中文 文档",
            "{term} English tutorial",
        ],
        "negative_or_failure_terms": [
            "{term} error failure bug",
            "{term} limitation pricing quota",
        ],
    }
    if tier == "deep":
        base["platform_terms"] = [
            "{term} 抖音 博主 评论",
            "{term} 小红书 B站 博主 实测 字幕",
            "{term} TikTok creator comments transcript",
            "{term} YouTube review benchmark transcript",
        ]
    return base


def build_query_plan(question: str, *, tier: str = "", focus_terms: list[str] | None = None) -> dict[str, Any]:
    requirement = detect_research_requirement(question)
    selected_tier = tier or str(requirement.get("research_tier") or "standard")
    if selected_tier == "none":
        selected_tier = "standard"
    selected_tier = _normalize_research_tier(selected_tier)

    hit_rules = {item["rule"] for item in requirement.get("trigger_rules", [])}
    planning = _infer_query_planning(question, hit_rules)
    if selected_tier == "deep" and planning.get("platform_relevant"):
        axes = planning.get("expansion_axes", [])
        planning["expansion_axes"] = _dedupe([*axes, "platform_terms"])
    seed_terms = _dedupe([*(focus_terms or []), *planning.get("seed_terms", [])], limit=12)
    if not seed_terms:
        seed_terms = _dedupe([question], limit=1)

    max_variants = int(TIME_BUDGETS[selected_tier]["max_query_variants"])
    templates = _query_templates_for_tier(selected_tier)
    variants: list[dict[str, str]] = []
    for term in seed_terms:
        for axis in planning.get("expansion_axes", []):
            for template in templates.get(axis, []):
                variants.append({"axis": axis, "query": template.format(term=term)})
                if len(variants) >= max_variants:
                    break
            if len(variants) >= max_variants:
                break
        if len(variants) >= max_variants:
            break

    return {
        "question": question,
        "research_tier": selected_tier,
        "time_budget": TIME_BUDGETS[selected_tier],
        "source_strategy": SOURCE_STRATEGIES[selected_tier],
        "query_plan": {
            "seed_terms": seed_terms,
            "expansion_axes": planning.get("expansion_axes", []),
            "query_variants": variants,
            "max_query_variants": max_variants,
        },
        "evidence_dimensions": list(EVIDENCE_DIMENSIONS),
        "sidecar_judges": ["evidence_sufficiency", "task_fit"]
        if TIME_BUDGETS[selected_tier].get("sidecar_required")
        else [],
        "completion_gates": {
            "min_sources": TIME_BUDGETS[selected_tier]["min_sources"],
            "min_source_families": TIME_BUDGETS[selected_tier]["min_source_families"],
            "min_query_variants": TIME_BUDGETS[selected_tier]["min_query_variants"],
            "requires_context_status": True,
            "requires_contradiction_status": True,
        },
    }


def detect_research_requirement(prompt: str) -> dict[str, Any]:
    """Detect whether a prompt needs a mandatory deep-research pass."""
    text = prompt or ""
    hits: list[dict[str, str]] = []
    for name, pattern in TRIGGER_RULES:
        if pattern.search(text):
            hits.append({"rule": name})

    hit_rules = {hit["rule"] for hit in hits}
    provider_hit = "provider_api_model_change" in hit_rules
    requires = bool(
        hit_rules
        & {
            "explicit_research_or_web",
            "current_or_time_sensitive",
            "technical_selection",
            "failure_escalation",
            "rpa_or_platform_intel",
            "keyword_planning",
        }
    ) or (provider_hit and len(hits) >= 2)
    tier = _classify_research_tier(hit_rules) if requires else "none"
    time_budget = TIME_BUDGETS.get(tier, {})
    min_sources = int(time_budget.get("min_sources") or 0)

    return {
        "requires_research": requires,
        "status": "research_required" if requires else "pass",
        "trigger_rules": hits,
        "trigger_layer": "semantic_prompt",
        "research_tier": tier,
        "time_budget": time_budget,
        "min_sources": min_sources if requires else 0,
        "required_skill": "3can-deep-research" if requires else "",
        "source_strategy": SOURCE_STRATEGIES.get(tier, []),
        "query_planning": _infer_query_planning(text, hit_rules) if requires else {"required": False},
        "evidence_dimensions": list(EVIDENCE_DIMENSIONS) if requires else [],
        "sidecar_judges": ["evidence_sufficiency", "task_fit"] if time_budget.get("sidecar_required") else [],
        "reason": (
            "Task is time-sensitive, research-oriented, or provider/API/model related; "
            "use the repo skill and record a source ledger before mutating work or final conclusions."
            if requires
            else "No mandatory research trigger detected."
        ),
    }


def _load_state(state_file: Path) -> dict[str, Any]:
    state = _safe_load_json(state_file, {"turns": {}})
    if not isinstance(state, dict):
        return {"turns": {}}
    state.setdefault("turns", {})
    return state


def _save_requirement(
    *,
    state_file: Path,
    session_id: str,
    turn_id: str,
    prompt: str,
    requirement: dict[str, Any],
) -> dict[str, Any]:
    state = _load_state(state_file)
    key = _state_key(session_id, turn_id)
    state["turns"][key] = {
        "session_id": session_id,
        "turn_id": turn_id,
        "prompt_hash": _hash_text(prompt),
        "requires_research": bool(requirement.get("requires_research")),
        "status": "needs_research" if requirement.get("requires_research") else "pass",
        "trigger_rules": requirement.get("trigger_rules", []),
        "trigger_layer": requirement.get("trigger_layer", ""),
        "research_tier": requirement.get("research_tier", ""),
        "time_budget": requirement.get("time_budget", {}),
        "source_strategy": requirement.get("source_strategy", []),
        "query_planning": requirement.get("query_planning", {}),
        "sidecar_judges": requirement.get("sidecar_judges", []),
        "created_at": _now(),
        "updated_at": _now(),
    }
    _safe_write_json(state_file, state)
    return state["turns"][key]


def _mark_done(
    *,
    state_file: Path,
    session_id: str,
    turn_id: str,
    ledger_path: Path,
) -> None:
    state = _load_state(state_file)
    key = _state_key(session_id, turn_id)
    item = state["turns"].setdefault(key, {"session_id": session_id, "turn_id": turn_id})
    item["requires_research"] = True
    item["status"] = "research_done"
    item["ledger_path"] = str(ledger_path)
    item["updated_at"] = _now()
    _safe_write_json(state_file, state)


def _count_by(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        value = str(item.get(key) or "").strip() or "unknown"
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


def _load_json_files(directory: Path, *, max_recent: int = 5) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    if not directory.exists():
        return [], []
    loaded: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        data = _safe_load_json(path, {})
        if not isinstance(data, dict):
            invalid.append({"path": str(path), "reason": "not a JSON object"})
            continue
        data["_path"] = str(path)
        loaded.append(data)
        if len(loaded) >= max_recent:
            break
    return loaded, invalid


def _summarize_hook_state(state_file: Path, *, max_recent: int = 5) -> dict[str, Any]:
    state = _load_state(state_file)
    turns = [item for item in state.get("turns", {}).values() if isinstance(item, dict)]
    unresolved = [item for item in turns if item.get("requires_research") and item.get("status") != "research_done"]
    recent = sorted(turns, key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)[
        :max_recent
    ]
    return {
        "state_file": str(state_file),
        "exists": state_file.exists(),
        "turn_count": len(turns),
        "unresolved_count": len(unresolved),
        "status_counts": _count_by(turns, "status"),
        "recent_turns": [
            {
                "session_id": str(item.get("session_id") or ""),
                "turn_id": str(item.get("turn_id") or ""),
                "status": str(item.get("status") or ""),
                "research_tier": str(item.get("research_tier") or ""),
                "updated_at": str(item.get("updated_at") or ""),
            }
            for item in recent
        ],
    }


def _summarize_research_ledgers(ledger_dir: Path, *, max_recent: int = 5) -> dict[str, Any]:
    files = sorted(ledger_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if ledger_dir.exists() else []
    recent_payloads, invalid = _load_json_files(ledger_dir, max_recent=max_recent)
    return {
        "ledger_dir": str(ledger_dir),
        "exists": ledger_dir.exists(),
        "ledger_count": len(files),
        "recent_status_counts": _count_by(recent_payloads, "status"),
        "recent_tier_counts": _count_by(recent_payloads, "research_tier"),
        "recent_ledgers": [
            {
                "path": str(item.get("_path") or ""),
                "question_hash": _hash_text(str(item.get("question") or "")),
                "status": str(item.get("status") or ""),
                "research_tier": str(item.get("research_tier") or ""),
                "source_count": int(item.get("source_count") or 0),
                "sidecar_decision": str((item.get("sidecar_decision") or {}).get("decision") or ""),
            }
            for item in recent_payloads
        ],
        "invalid_recent_files": invalid,
    }


def _summarize_source_artifacts(source_dir: Path, *, max_recent: int = 5) -> dict[str, Any]:
    files = sorted(source_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True) if source_dir.exists() else []
    recent_payloads, invalid = _load_json_files(source_dir, max_recent=max_recent)
    return {
        "source_dir": str(source_dir),
        "exists": source_dir.exists(),
        "artifact_count": len(files),
        "recent_status_counts": _count_by(recent_payloads, "status"),
        "recent_source_type_counts": _count_by(recent_payloads, "source_type"),
        "recent_artifacts": [
            {
                "path": str(item.get("_path") or ""),
                "adapter": str(item.get("adapter") or ""),
                "status": str(item.get("status") or ""),
                "source_type": str(item.get("source_type") or ""),
                "url_hash": _hash_text(str(item.get("url") or "")),
                "content_hash": str(item.get("content_hash") or ""),
                "stores_raw_html": bool(item.get("stores_raw_html")),
                "stores_secrets": bool(item.get("stores_secrets")),
            }
            for item in recent_payloads
        ],
        "invalid_recent_files": invalid,
    }


def _summarize_hook_config(codex_dir: Path) -> dict[str, Any]:
    config_path = codex_dir / "config.toml"
    hooks_path = codex_dir / "hooks.json"
    config_text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    hooks_data = _safe_load_json(hooks_path, {})
    hooks = hooks_data.get("hooks", {}) if isinstance(hooks_data, dict) else {}
    commands: list[str] = []
    if isinstance(hooks, dict):
        for entries in hooks.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                for hook in entry.get("hooks", []) if isinstance(entry, dict) else []:
                    if isinstance(hook, dict) and hook.get("command"):
                        commands.append(str(hook["command"]))
    warnings: list[str] = []
    if config_path.exists() and "codex_hooks = true" not in config_text:
        warnings.append("codex_hooks_not_enabled")
    if hooks_path.exists() and not any("3can_research_harness.py" in command for command in commands):
        warnings.append("research_harness_hook_command_missing")
    if sys.platform.startswith("win") and any(command.strip().startswith("/usr/bin/") for command in commands):
        warnings.append("hook_command_uses_unix_python_on_windows")
    return {
        "codex_dir": str(codex_dir),
        "config_exists": config_path.exists(),
        "hooks_exists": hooks_path.exists(),
        "codex_hooks_enabled": "codex_hooks = true" in config_text,
        "hook_events": sorted(hooks.keys()) if isinstance(hooks, dict) else [],
        "research_hook_command_count": sum(1 for command in commands if "3can_research_harness.py" in command),
        "warnings": warnings,
    }


def build_status_report(
    *,
    state_file: Path = DEFAULT_STATE_FILE,
    ledger_dir: Path = DEFAULT_LEDGER_DIR,
    source_dir: Path = DEFAULT_SOURCE_ARTIFACT_DIR,
    failure_state_file: Path = DEFAULT_FAILURE_STATE,
    codex_dir: Path = PROJECT_ROOT / ".codex",
    skill_dir: Path = PROJECT_ROOT / ".agents" / "skills" / "3can-deep-research",
    max_recent: int = 5,
) -> dict[str, Any]:
    failure_state = _safe_load_json(failure_state_file, {})
    failure_items = failure_state.get("failures", {}) if isinstance(failure_state, dict) else {}
    hook_config = _summarize_hook_config(codex_dir)
    hook_state = _summarize_hook_state(state_file, max_recent=max_recent)
    ledger_summary = _summarize_research_ledgers(ledger_dir, max_recent=max_recent)
    source_summary = _summarize_source_artifacts(source_dir, max_recent=max_recent)
    warnings = [*hook_config["warnings"]]
    if hook_state["unresolved_count"]:
        warnings.append("unresolved_research_turns")
    return {
        "ok": True,
        "status": "warn" if warnings else "ready",
        "skill": {
            "skill_dir": str(skill_dir),
            "skill_md_exists": (skill_dir / "SKILL.md").exists(),
            "ledger_reference_exists": (skill_dir / "references" / "research-ledger.md").exists(),
        },
        "hook_config": hook_config,
        "hook_state": hook_state,
        "research_ledgers": ledger_summary,
        "source_artifacts": source_summary,
        "failure_signals": {
            "failure_state_file": str(failure_state_file),
            "exists": failure_state_file.exists(),
            "signature_count": len(failure_items) if isinstance(failure_items, dict) else 0,
        },
        "capabilities": {
            "commands": [
                "check",
                "plan",
                "collect-url",
                "import-search-result",
                "import-rpa-artifact",
                "rpa-probe",
                "failure-signal",
                "done",
                "judge",
                "status",
            ],
            "source_types": list(SOURCE_TYPES),
            "research_tiers": sorted(TIME_BUDGETS),
        },
        "warnings": warnings,
        "privacy": {
            "prints_source_excerpt": False,
            "prints_raw_html": False,
            "prints_secrets": False,
        },
    }


def _turn_requires_unresolved(state_file: Path, session_id: str, turn_id: str) -> dict[str, Any]:
    state = _load_state(state_file)
    item = state.get("turns", {}).get(_state_key(session_id, turn_id), {})
    unresolved = bool(item.get("requires_research")) and item.get("status") != "research_done"
    return {"unresolved": unresolved, "turn": item}


def _hook_json(data: dict[str, Any], state_file: Path) -> tuple[int, dict[str, Any]]:
    event = str(data.get("hook_event_name") or "")
    session_id = str(data.get("session_id") or "")
    turn_id = str(data.get("turn_id") or "")

    if event == "UserPromptSubmit":
        prompt = str(data.get("prompt") or "")
        requirement = detect_research_requirement(prompt)
        if not requirement["requires_research"]:
            return 0, {"continue": True}
        _save_requirement(
            state_file=state_file,
            session_id=session_id,
            turn_id=turn_id,
            prompt=prompt,
            requirement=requirement,
        )
        tier = requirement.get("research_tier", "standard")
        command = (
            "scripts/3can_research_harness.py done "
            f"--session-id {session_id or 'unknown-session'} "
            f"--turn-id {turn_id or 'unknown-turn'} "
            f"--research-tier {tier} "
            "--elapsed-minutes <active-research-minutes> "
            "--question \"<research question>\" "
            "--source-artifact <collected-source.json> [...] "
            "--query-variant \"<query used>\" [...] "
            "--context-status <used|unavailable|not_applicable> "
            "--contradiction-status <checked_no_material_conflict|resolved|unresolved|not_applicable> "
            "--sidecar-evidence-sufficiency pass --sidecar-task-fit pass"
        )
        budget = requirement.get("time_budget", {})
        return 0, {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    f"3CAN deep research is mandatory for this turn. Tier={tier}, "
                    f"decision_check={budget.get('decision_check_minutes', 5)}min, "
                    f"target={budget.get('target_minutes', 5)}min, "
                    f"hard_cap={budget.get('hard_cap_minutes', 10)}min. Invoke "
                    "$3can-deep-research, cover the required source families, keep citations visible, "
                    "run query planning and contradiction checks, use sidecar evidence/task-fit judgement, "
                    "and record the source ledger before file edits or final conclusions. "
                    f"Ledger command shape: {command}"
                ),
            }
        }

    if event == "PreToolUse":
        unresolved = _turn_requires_unresolved(state_file, session_id, turn_id)
        if not unresolved["unresolved"]:
            return 0, {"continue": True}
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        command = ""
        if isinstance(tool_input, dict):
            command = str(tool_input.get("command") or tool_input)
        else:
            command = str(tool_input)
        mutating = tool_name in {"apply_patch", "Edit", "Write"} or (
            tool_name == "Bash" and MUTATING_COMMAND.search(command) and not READ_ONLY_COMMAND.search(command)
        )
        if not mutating:
            return 0, {
                "systemMessage": "3CAN deep research is pending; read-only exploration is allowed."
            }
        return 2, {
            "decision": "block",
            "reason": (
                "3CAN deep research ledger is required before mutating tools for this turn. "
                f"Tier={unresolved['turn'].get('research_tier', 'unknown')}. "
                "Use $3can-deep-research and run scripts/3can_research_harness.py done with sources."
            ),
        }

    if event == "Stop":
        unresolved = _turn_requires_unresolved(state_file, session_id, turn_id)
        if unresolved["unresolved"]:
            return 2, {
                "decision": "block",
                "reason": (
                    "Complete the mandatory 3CAN deep research source ledger and cite sources before final answer."
                ),
            }
        return 0, {"continue": True}

    return 0, {"continue": True}


def run_hook(state_file: Path) -> int:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _print_json({"systemMessage": f"3CAN research hook received invalid JSON: {exc}"})
        return 0
    code, payload = _hook_json(data, state_file)
    _print_json(payload)
    return code


def make_failure_signature(command: str, target_files: list[str], error_text: str) -> dict[str, Any]:
    normalized = json.dumps(
        {
            "command": re.sub(r"\s+", " ", command.strip().lower()),
            "target_files": sorted(path.strip().lower() for path in target_files if path.strip()),
            "error": re.sub(r"\s+", " ", error_text.strip().lower())[:1500],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return {
        "hash": _hash_text(normalized),
        "error_excerpt": re.sub(r"\s+", " ", error_text.strip())[:300],
    }


def record_failure_signal(
    *,
    state_file: Path,
    command: str,
    target_files: list[str],
    error_text: str,
    threshold: int = 3,
) -> dict[str, Any]:
    sig = make_failure_signature(command, target_files, error_text)
    state = _safe_load_json(state_file, {"signatures": {}})
    signatures = state.setdefault("signatures", {})
    item = signatures.setdefault(
        sig["hash"],
        {
            "count": 0,
            "command": command,
            "target_files": target_files,
            "error_excerpt": sig["error_excerpt"],
            "first_seen_at": _now(),
        },
    )
    item["count"] = int(item.get("count") or 0) + 1
    item["last_seen_at"] = _now()
    item["error_excerpt"] = sig["error_excerpt"]
    _safe_write_json(state_file, state)

    stop = item["count"] >= threshold
    return {
        "ok": not stop,
        "status": "stop_and_research" if stop else "recorded_failure",
        "signature": sig["hash"],
        "count": item["count"],
        "threshold": threshold,
        "trigger_layer": "failure_escalation",
        "required_skill": "3can-deep-research" if stop else "",
        "research_tier": "deep" if stop else "",
        "reason": (
            "Repeated failure threshold reached; stop blind edits and run deep research before continuing."
            if stop
            else "Failure recorded; continue local diagnosis until threshold is reached."
        ),
    }


def _parse_key_value_pairs(items: list[str], *, allowed_keys: tuple[str, ...] | None = None) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    allowed = set(allowed_keys or ())
    for item in items:
        if "=" not in item:
            raise ValueError(f"expected key=value pair: {item}")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        value = raw_value.strip()
        if allowed and key not in allowed:
            raise ValueError(f"unsupported key: {key}")
        if re.fullmatch(r"-?\d+(\.\d+)?", value):
            parsed[key] = float(value) if "." in value else int(value)
        else:
            parsed[key] = value
    return parsed


def _build_source_records(source_urls: list[str], source_types: list[str]) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for index, url in enumerate(source_urls):
        source_type = source_types[index] if index < len(source_types) else "targeted_web"
        if source_type not in SOURCE_TYPES:
            source_type = "targeted_web"
        records.append({"url": url, "source_type": source_type})
    return records


def _source_family_counts(source_types: list[str], context_status: str) -> dict[str, int]:
    counts = {
        family: sum(1 for source_type in source_types if source_type in members)
        for family, members in SOURCE_FAMILIES.items()
    }
    counts["internal"] = 1 if context_status == "used" else 0
    return {family: count for family, count in counts.items() if count}


def score_evidence(evidence_scores: dict[str, Any]) -> dict[str, Any]:
    numeric: dict[str, float] = {}
    for key in EVIDENCE_DIMENSIONS:
        value = evidence_scores.get(key)
        if isinstance(value, (int, float)):
            numeric[key] = max(0.0, min(5.0, float(value)))

    positive = [numeric[key] for key in POSITIVE_EVIDENCE_DIMENSIONS if key in numeric]
    negative = [numeric[key] for key in NEGATIVE_EVIDENCE_DIMENSIONS if key in numeric]
    positive_avg = sum(positive) / len(positive) if positive else 0.0
    negative_penalty = sum(negative) / len(negative) if negative else 0.0
    weighted = max(0.0, min(5.0, positive_avg - (negative_penalty * 0.35)))
    return {
        "score_available": bool(numeric),
        "positive_avg": round(positive_avg, 2),
        "negative_penalty": round(negative_penalty, 2),
        "weighted_score": round(weighted, 2),
        "threshold": 3.0,
    }


def judge_ledger(ledger: dict[str, Any]) -> dict[str, Any]:
    risks: list[str] = []
    tier = _normalize_research_tier(str(ledger.get("research_tier") or "standard"))
    budget = TIME_BUDGETS[tier]
    source_count = int(ledger.get("source_count") or 0)
    verified_external_source_count = int(
        ledger.get("verified_external_source_count") or 0
    )
    min_sources = max(int(ledger.get("min_sources") or 0), int(budget["min_sources"]))
    source_type_list = [
        item.get("source_type")
        for item in ledger.get("source_records", [])
        if isinstance(item, dict)
        and item.get("source_type")
        and item.get("opened_verified") is True
        and item.get("source_type") != "known_3can_context"
    ]
    source_types = set(source_type_list)
    context = ledger.get("internal_context", {}) if isinstance(ledger.get("internal_context"), dict) else {}
    context_status = str(context.get("status") or "")
    source_family_counts = _source_family_counts(source_type_list, context_status)
    source_family_count = len(
        [family for family in source_family_counts if family != "internal"]
    )
    query_plan = ledger.get("query_plan", {}) if isinstance(ledger.get("query_plan"), dict) else {}
    query_variants = query_plan.get("query_variants", []) if isinstance(query_plan.get("query_variants"), list) else []
    contradiction_status = str(ledger.get("contradiction_status") or "")
    platform_relevant = bool(ledger.get("platform_relevant"))
    elapsed_minutes = ledger.get("elapsed_minutes")
    score = score_evidence(ledger.get("evidence_scores", {}) if isinstance(ledger.get("evidence_scores"), dict) else {})
    sidecar = ledger.get("sidecar_judgement", {}) if isinstance(ledger.get("sidecar_judgement"), dict) else {}

    if ledger.get("status") != "pass":
        risks.append("ledger_status_not_pass")
    if not isinstance(elapsed_minutes, (int, float)) or float(elapsed_minutes) <= 0:
        risks.append("missing_elapsed_time")
    elif float(elapsed_minutes) > float(budget["hard_cap_minutes"]):
        risks.append("research_timebox_exceeded")
    if verified_external_source_count < min_sources:
        risks.append("insufficient_verified_external_source_count")
    if source_family_count < int(budget["min_source_families"]):
        risks.append("insufficient_source_family_coverage")
    if not source_types.intersection({"official_primary", "academic_or_standard"}):
        risks.append("missing_boundary_or_contract_source")
    if not source_types.intersection({"github_or_issue", "model_hub_or_dataset", "community_practice", "community_forum"}):
        risks.append("missing_implementation_or_practice_source")
    if tier == "deep" and not source_types.intersection({"academic_or_standard", "benchmark_or_user_report"}):
        risks.append("missing_academic_or_benchmark_source")
    if tier == "deep" and not source_types.intersection({"github_or_issue", "model_hub_or_dataset"}):
        risks.append("missing_implementation_evidence")
    if tier == "deep" and not source_types.intersection({"community_practice", "community_forum"}):
        risks.append("missing_community_evidence")
    if platform_relevant and not source_types.intersection(SOURCE_FAMILIES["platform"]):
        risks.append("missing_platform_signal")
    if len(query_variants) < int(budget["min_query_variants"]):
        risks.append("missing_query_plan")
    if context_status not in {"used", "unavailable", "not_applicable"}:
        risks.append("missing_internal_context_status")
    elif context_status == "used" and not _as_string_list(context.get("evidence_refs")):
        risks.append("missing_internal_context_evidence_ref")
    if contradiction_status not in {"checked_no_material_conflict", "resolved", "unresolved", "not_applicable"}:
        risks.append("missing_contradiction_status")
    elif contradiction_status == "unresolved":
        risks.append("unresolved_material_conflict")
    if not score["score_available"]:
        risks.append("missing_evidence_scores")
    elif score["weighted_score"] < score["threshold"]:
        risks.append("low_evidence_score")

    sidecar_required = bool(budget.get("sidecar_required"))
    if sidecar_required:
        if sidecar.get("evidence_sufficiency") not in {"pass", "sufficient"}:
            risks.append("sidecar_evidence_sufficiency_not_pass")
        if sidecar.get("task_fit") not in {"pass", "fit"}:
            risks.append("sidecar_task_fit_not_pass")

    if not risks:
        decision = "ready_for_decision"
    elif (
        "ledger_status_not_pass" in risks
        or "insufficient_verified_external_source_count" in risks
    ):
        decision = "continue_research"
    elif any(item.startswith("missing_") for item in risks) or "low_evidence_score" in risks:
        decision = "continue_research"
    else:
        decision = "needs_review"

    return {
        "ok": decision == "ready_for_decision",
        "decision": decision,
        "research_tier": tier,
        "source_count": source_count,
        "verified_external_source_count": verified_external_source_count,
        "elapsed_minutes": elapsed_minutes,
        "hard_cap_minutes": int(budget["hard_cap_minutes"]),
        "min_sources": min_sources,
        "source_family_count": source_family_count,
        "min_source_families": int(budget["min_source_families"]),
        "source_family_counts": source_family_counts,
        "source_type_counts": {
            source_type: source_type_list.count(source_type) for source_type in sorted(source_types) if source_type
        },
        "evidence_score": score,
        "risks": risks,
        "next_action": (
            "Proceed to engineering decision/writeback."
            if decision == "ready_for_decision"
            else "Continue targeted research or record sidecar judgement before mutating work."
        ),
    }


def record_done(
    *,
    question: str,
    source_urls: list[str],
    session_id: str,
    turn_id: str,
    state_file: Path,
    ledger_dir: Path,
    notes: str = "",
    min_sources: int = DEFAULT_MIN_SOURCES,
    research_tier: str = "standard",
    source_types: list[str] | None = None,
    query_terms: list[str] | None = None,
    query_variants: list[str] | None = None,
    evidence_scores: dict[str, Any] | None = None,
    sidecar_evidence_sufficiency: str = "not_recorded",
    sidecar_task_fit: str = "not_recorded",
    rpa_metadata: dict[str, Any] | None = None,
    source_artifact_files: list[str] | None = None,
    context_status: str = "",
    context_refs: list[str] | None = None,
    contradiction_status: str = "",
    platform_relevant: bool = False,
    elapsed_minutes: float = 0.0,
) -> dict[str, Any]:
    source_artifacts, invalid_artifacts = load_source_artifacts(source_artifact_files or [])
    artifact_urls = [str(item["url"]) for item in source_artifacts if item.get("url")]
    artifact_source_types = [str(item.get("source_type") or "targeted_web") for item in source_artifacts]
    urls = [url.strip() for url in [*artifact_urls, *source_urls] if url.strip()]
    invalid = [url for url in urls if not re.match(r"^https?://", url, re.I)]
    unique_urls = list(dict.fromkeys(urls))
    selected_tier = _normalize_research_tier(research_tier)
    required_min_sources = max(int(min_sources), int(TIME_BUDGETS[selected_tier]["min_sources"]))
    source_records = _build_source_records(unique_urls, [*artifact_source_types, *(source_types or [])])
    artifact_by_url = {item["url"]: item for item in source_artifacts if item.get("url")}
    for record in source_records:
        artifact = artifact_by_url.get(record["url"])
        record["opened_verified"] = bool(
            artifact and artifact.get("opened_verified") is True
        )
        if artifact:
            record.update(
                {
                    "artifact_path": str(artifact.get("artifact_path") or ""),
                    "adapter": str(artifact.get("adapter") or ""),
                    "title": str(artifact.get("title") or ""),
                    "content_hash": str(artifact.get("content_hash") or ""),
                    "http_status": artifact.get("http_status"),
                    "collected_at": str(artifact.get("collected_at") or ""),
                    "artifact_status": str(artifact.get("status") or ""),
                    "rpa_metadata": artifact.get("rpa_metadata") if isinstance(artifact.get("rpa_metadata"), dict) else {},
                }
            )
    verified_external_source_count = len(
        {
            record["url"]
            for record in source_records
            if record.get("opened_verified") is True
            and record.get("source_type") != "known_3can_context"
        }
    )
    structural_status = (
        "pass"
        if verified_external_source_count >= required_min_sources
        and not invalid
        and not invalid_artifacts
        else "block"
    )
    evidence = evidence_scores or {}
    score = score_evidence(evidence)

    ledger = {
        "status": structural_status,
        "question": question,
        "research_tier": selected_tier,
        "elapsed_minutes": elapsed_minutes,
        "time_budget": TIME_BUDGETS[selected_tier],
        "source_strategy": SOURCE_STRATEGIES[selected_tier],
        "session_id": session_id,
        "turn_id": turn_id,
        "source_urls": unique_urls,
        "source_records": source_records,
        "source_artifacts": source_artifacts,
        "invalid_source_artifacts": invalid_artifacts,
        "invalid_source_urls": invalid,
        "source_count": len(unique_urls),
        "verified_external_source_count": verified_external_source_count,
        "min_sources": required_min_sources,
        "query_plan": {
            "seed_terms": _dedupe(query_terms or []),
            "query_variants": [{"query": item} for item in _dedupe(query_variants or [])],
        },
        "notes": notes,
        "evidence_dimensions": list(EVIDENCE_DIMENSIONS),
        "evidence_scores": evidence,
        "evidence_score_summary": score,
        "source_type_guidance": {
            "official_primary": "docs, changelog, API reference, release notes, standards, regulator pages",
            "academic_or_standard": "paper, standard, or systematic technical review",
            "github_or_issue": "repository implementation, issue, pull request, or release evidence",
            "model_hub_or_dataset": "Hugging Face model/dataset card, discussion, version, license, or eval evidence",
            "community_practice": "implementation writeup or practitioner case",
            "community_forum": "Reddit or professional forum field report",
            "public_platform_signal": "public video/blogger/comment signals with timestamp and engagement when approved",
            "rpa_video_comment_asr_ocr": "authorized RPA capture with frame/OCR/ASR/engagement metadata; no private data",
            "rpa_pipeline_artifact": "safe local RPA probe or approved pipeline artifact normalized into source ledger evidence",
        },
        "sidecar_judgement": {
            "evidence_sufficiency": sidecar_evidence_sufficiency,
            "task_fit": sidecar_task_fit,
        },
        "internal_context": {
            "status": context_status,
            "evidence_refs": _dedupe(context_refs or []),
        },
        "contradiction_status": contradiction_status,
        "platform_relevant": platform_relevant,
        "rpa_metadata": rpa_metadata or {},
        "created_at": _now(),
        "required_writeback": "DOC/DEC/ERR/INTF when the conclusion is durable or changes engineering direction.",
    }
    ledger["sidecar_decision"] = judge_ledger(ledger)
    status = "pass" if structural_status == "pass" and ledger["sidecar_decision"]["ok"] else "block"
    ledger["status"] = status
    name = f"{_now().replace(':', '').replace('-', '')}_{_hash_text(question + session_id + turn_id)}.json"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = ledger_dir / name
    _safe_write_json(ledger_path, ledger)
    ledger["ledger_path"] = str(ledger_path)

    if status == "pass":
        _mark_done(
            state_file=state_file,
            session_id=session_id,
            turn_id=turn_id,
            ledger_path=ledger_path,
        )
        ledger["ok"] = True
    else:
        ledger["ok"] = False
        ledger["reason"] = (
            f"Research completion gates not met: {', '.join(ledger['sidecar_decision']['risks']) or 'invalid sources'}."
        )
    return ledger


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="3CAN deep-research hook and source-ledger helper.")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE_FILE))
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="Detect whether a prompt requires deep research.")
    check.add_argument("--prompt", required=True)

    plan = sub.add_parser("plan", help="Build a deterministic query/source plan for a research question.")
    plan.add_argument("--question", required=True)
    plan.add_argument("--research-tier", choices=sorted({*TIME_BUDGETS, *LEGACY_TIER_ALIASES}), default="")
    plan.add_argument("--focus-term", action="append", default=[])

    collect = sub.add_parser("collect-url", help="Collect a public http(s) URL into a source artifact.")
    collect.add_argument("--url", required=True)
    collect.add_argument("--source-type", choices=SOURCE_TYPES, default="targeted_web")
    collect.add_argument("--output-dir", default=str(DEFAULT_SOURCE_ARTIFACT_DIR))
    collect.add_argument("--timeout", type=float, default=8.0)
    collect.add_argument("--max-chars", type=int, default=4000)

    import_search = sub.add_parser("import-search-result", help="Import provider-neutral search result JSON into source artifacts.")
    import_search.add_argument("--input-file", required=True)
    import_search.add_argument("--output-dir", default=str(DEFAULT_SOURCE_ARTIFACT_DIR))
    import_search.add_argument("--provider", default="manual")
    import_search.add_argument("--query", default="")
    import_search.add_argument("--source-type", choices=SOURCE_TYPES, default="targeted_web")
    import_search.add_argument("--limit", type=int, default=10)

    import_rpa = sub.add_parser("import-rpa-artifact", help="Import an approved/offline RPA evidence JSON into a source artifact.")
    import_rpa.add_argument("--input-file", required=True)
    import_rpa.add_argument("--output-dir", default=str(DEFAULT_SOURCE_ARTIFACT_DIR))
    import_rpa.add_argument("--approval-id", default="")

    rpa_probe = sub.add_parser("rpa-probe", help="Run a safe local RPA probe and emit research source artifacts.")
    rpa_probe.add_argument("--mode", choices=["control-plane", "adapter-review"], default="control-plane")
    rpa_probe.add_argument("--task-id", default="R5")
    rpa_probe.add_argument("--merchant-id", default="research-probe")
    rpa_probe.add_argument("--platform", default="creator-content")
    rpa_probe.add_argument("--adapter-task-id", default="")
    rpa_probe.add_argument("--params-json", default="{}")
    rpa_probe.add_argument("--db-path", type=Path)
    rpa_probe.add_argument("--artifact-root", type=Path)
    rpa_probe.add_argument("--cards-output", type=Path)
    rpa_probe.add_argument("--cards-limit", type=int, default=20)
    rpa_probe.add_argument("--no-cross-validate", action="store_true")
    rpa_probe.add_argument("--output-dir")
    rpa_probe.add_argument("--approval-id", default="")
    rpa_probe.add_argument(
        "--project-root",
        type=Path,
        help="Physical project/worktree containing tools/rpa; defaults to THREECAN_PROJECT_ROOT, then cwd.",
    )

    failure = sub.add_parser("failure-signal", help="Record repeated failures and escalate to research at threshold.")
    failure.add_argument("--command", dest="failed_command", required=True)
    failure.add_argument("--target-file", dest="target_files", action="append", default=[])
    failure.add_argument("--error-text", required=True)
    failure.add_argument("--failure-state-file", default=str(DEFAULT_FAILURE_STATE))
    failure.add_argument("--threshold", type=int, default=3)

    sub.add_parser("hook", help="Run as a Codex lifecycle hook. Reads hook JSON from stdin.")

    status = sub.add_parser("status", help="Summarize research skill, hook, ledger, and source artifact status without printing source excerpts.")
    status.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    status.add_argument("--source-dir", default=str(DEFAULT_SOURCE_ARTIFACT_DIR))
    status.add_argument("--failure-state-file", default=str(DEFAULT_FAILURE_STATE))
    status.add_argument("--codex-dir", default=str(PROJECT_ROOT / ".codex"))
    status.add_argument("--skill-dir", default=str(PROJECT_ROOT / ".agents" / "skills" / "3can-deep-research"))
    status.add_argument("--max-recent", type=int, default=5)

    done = sub.add_parser("done", help="Record a completed research source ledger.")
    done.add_argument("--question", required=True)
    done.add_argument("--source-url", action="append", default=[])
    done.add_argument("--source-artifact", action="append", default=[], help="Path to collect-url source artifact JSON.")
    done.add_argument("--session-id", default="manual")
    done.add_argument("--turn-id", default="manual")
    done.add_argument("--ledger-dir", default=str(DEFAULT_LEDGER_DIR))
    done.add_argument("--notes", default="")
    done.add_argument("--min-sources", type=int, default=DEFAULT_MIN_SOURCES)
    done.add_argument(
        "--research-tier",
        choices=sorted({*TIME_BUDGETS, *LEGACY_TIER_ALIASES}),
        default="standard",
    )
    done.add_argument("--elapsed-minutes", type=float, default=0.0)
    done.add_argument("--source-type", action="append", default=[], choices=SOURCE_TYPES)
    done.add_argument("--query-term", action="append", default=[])
    done.add_argument("--query-variant", action="append", default=[])
    done.add_argument("--evidence-score", action="append", default=[], help="key=value, 0-5. Keys: evidence dimensions.")
    done.add_argument("--sidecar-evidence-sufficiency", default="not_recorded")
    done.add_argument("--sidecar-task-fit", default="not_recorded")
    done.add_argument("--context-status", choices=["used", "unavailable", "not_applicable"], default="")
    done.add_argument("--context-ref", action="append", default=[])
    done.add_argument(
        "--contradiction-status",
        choices=["checked_no_material_conflict", "resolved", "unresolved", "not_applicable"],
        default="",
    )
    done.add_argument("--platform-relevant", action="store_true")
    done.add_argument("--rpa-meta", action="append", default=[], help="key=value metadata for approved RPA/platform evidence.")

    judge = sub.add_parser("judge", help="Evaluate whether a research ledger is sufficient for task decision.")
    judge.add_argument("--ledger-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    state_file = Path(args.state_file)

    if args.command == "check":
        result = detect_research_requirement(args.prompt)
        _print_json(result)
        return 0 if result["status"] != "research_required" else 2

    if args.command == "plan":
        result = build_query_plan(args.question, tier=args.research_tier, focus_terms=args.focus_term)
        _print_json(result)
        return 0

    if args.command == "collect-url":
        result = collect_public_url(
            args.url,
            output_dir=Path(args.output_dir) if args.output_dir else None,
            source_type=args.source_type,
            timeout=args.timeout,
            max_chars=args.max_chars,
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "import-search-result":
        result = import_search_results(
            input_file=Path(args.input_file),
            output_dir=Path(args.output_dir),
            provider=args.provider,
            query=args.query,
            source_type=args.source_type,
            limit=args.limit,
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "import-rpa-artifact":
        result = import_rpa_artifact(
            input_file=Path(args.input_file),
            output_dir=Path(args.output_dir),
            approval_id=args.approval_id,
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "rpa-probe":
        try:
            params = _load_json_object(args.params_json, label="--params-json")
        except ValueError as exc:
            _print_json({"ok": False, "status": "block", "error": str(exc)})
            return 2
        result = run_rpa_probe(
            mode=args.mode,
            output_dir=Path(args.output_dir),
            approval_id=args.approval_id,
            task_id=args.task_id,
            merchant_id=args.merchant_id,
            platform=args.platform,
            adapter_task_id=args.adapter_task_id,
            params=params,
            db_path=args.db_path,
            artifact_root=args.artifact_root,
            cards_output=args.cards_output,
            cards_limit=args.cards_limit,
            cross_validate=not args.no_cross_validate,
            project_root=args.project_root,
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "hook":
        return run_hook(state_file)

    if args.command == "failure-signal":
        result = record_failure_signal(
            state_file=Path(args.failure_state_file),
            command=args.failed_command,
            target_files=args.target_files,
            error_text=args.error_text,
            threshold=args.threshold,
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "status":
        result = build_status_report(
            state_file=state_file,
            ledger_dir=Path(args.ledger_dir),
            source_dir=Path(args.source_dir),
            failure_state_file=Path(args.failure_state_file),
            codex_dir=Path(args.codex_dir),
            skill_dir=Path(args.skill_dir),
            max_recent=max(1, args.max_recent),
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "done":
        try:
            evidence_scores = _parse_key_value_pairs(args.evidence_score, allowed_keys=EVIDENCE_DIMENSIONS)
            rpa_metadata = _parse_key_value_pairs(args.rpa_meta)
        except ValueError as exc:
            _print_json({"ok": False, "status": "block", "error": str(exc)})
            return 2
        result = record_done(
            question=args.question,
            source_urls=args.source_url,
            session_id=args.session_id,
            turn_id=args.turn_id,
            state_file=state_file,
            ledger_dir=Path(args.ledger_dir),
            notes=args.notes,
            min_sources=args.min_sources,
            research_tier=args.research_tier,
            source_types=args.source_type,
            query_terms=args.query_term,
            query_variants=args.query_variant,
            evidence_scores=evidence_scores,
            sidecar_evidence_sufficiency=args.sidecar_evidence_sufficiency,
            sidecar_task_fit=args.sidecar_task_fit,
            rpa_metadata=rpa_metadata,
            source_artifact_files=args.source_artifact,
            context_status=args.context_status,
            context_refs=args.context_ref,
            contradiction_status=args.contradiction_status,
            platform_relevant=args.platform_relevant,
            elapsed_minutes=args.elapsed_minutes,
        )
        _print_json(result)
        return 0 if result["ok"] else 2

    if args.command == "judge":
        ledger = _safe_load_json(Path(args.ledger_file), {})
        result = judge_ledger(ledger)
        _print_json(result)
        return 0 if result["ok"] else 2

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
