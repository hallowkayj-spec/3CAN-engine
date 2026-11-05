"""Scoped notes maintenance using native archived nodes and versioned CAS."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote

import runtimehook_checkpoints as cp

ARCHIVE_SCHEMA = "3can.runtimehook-notes-archive/v1"
MANIFEST_SCHEMA = "3can.runtimehook-notes-archives/v1"
PLAN_SCHEMA = "3can.runtimehook-notes-plan/v1"
MANIFEST_KEY = "runtimehook_notes_archives"
MAX_ARCHIVES = 32
LIMIT = 256 * 1024
CONTEXT_KEYS = (
    "agent_id",
    "project_id",
    "project_namespace",
    "workspace_id",
    "workorder_id",
)
MARKER = re.compile(r"(?m)^RUNTIMEHOOK_DELTA_[a-f0-9]{64}$")


class NotesError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise NotesError(code)


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def semantic_content(content):
    value = copy.deepcopy(content)
    # Native retrieval records this without changing the opaque node version.
    value["extra"].pop("last_accessed_at", None)
    return value


def project_identity(extra: dict) -> dict[str, str]:
    """Accept complete, agreeing declarations; never merge or normalize them."""
    code = "NODE_PROJECT_BINDING_UNVERIFIED"
    keys = ("project_id", "project_namespace")
    require(isinstance(extra, dict), code)
    declarations = [extra] if any(k in extra for k in keys) else []
    if "project_identity" in extra:
        declarations.append(extra["project_identity"])
    require(declarations, code)
    result = {}
    for declared in declarations:
        require(
            isinstance(declared, dict)
            and all(
                isinstance(declared.get(k), str)
                and declared[k]
                and declared[k].strip() == declared[k]
                for k in keys
            ),
            code,
        )
        pair = {k: declared[k] for k in keys}
        require(not result or result == pair, code)
        result = pair
    return result


def identity(node, binding):
    require(
        isinstance(node, dict) and node.get("id") == binding["node_id"],
        "NOTES_NODE_MISMATCH",
    )
    require(
        isinstance(node.get("content"), dict)
        and isinstance(node["content"].get("extra"), dict),
        "NOTES_NODE_INVALID",
    )
    extra = node["content"]["extra"]
    require(
        project_identity(extra)
        == {k: binding[k] for k in ("project_id", "project_namespace")},
        "NODE_PROJECT_BINDING_UNVERIFIED",
    )


def references(node):
    manifest = node.get("content", {}).get("extra", {}).get(MANIFEST_KEY)
    if manifest is None:
        return []
    require(
        isinstance(manifest, dict) and manifest.get("schema") == MANIFEST_SCHEMA,
        "ARCHIVE_MANIFEST_INVALID",
    )
    refs = manifest.get("archives")
    require(
        isinstance(refs, list) and len(refs) <= MAX_ARCHIVES, "ARCHIVE_MANIFEST_INVALID"
    )
    for ref in refs:
        require(
            isinstance(ref, dict)
            and re.fullmatch(r"SES-RHARCH-[a-f0-9]{64}", str(ref.get("node_id", "")))
            and all(
                re.fullmatch(r"[a-f0-9]{64}", str(ref.get(k, "")))
                for k in ("notes_sha256", "snapshot_sha256")
            ),
            "ARCHIVE_MANIFEST_INVALID",
        )
    require(len({r["node_id"] for r in refs}) == len(refs), "ARCHIVE_MANIFEST_INVALID")
    return refs


def restore_snapshot(archive):
    require(isinstance(archive, dict), "ARCHIVE_READBACK_INVALID")
    content = archive.get("content", {})
    require(
        isinstance(content, dict)
        and isinstance(content.get("notes"), str)
        and isinstance(content.get("extra"), dict),
        "ARCHIVE_READBACK_INVALID",
    )
    meta = content["extra"].get("runtimehook_archive", {})
    require(isinstance(meta, dict), "ARCHIVE_READBACK_INVALID")
    require(
        meta.get("schema") == ARCHIVE_SCHEMA and isinstance(meta.get("snapshot"), dict),
        "ARCHIVE_READBACK_INVALID",
    )
    restored = copy.deepcopy(meta["snapshot"])
    require(
        isinstance(restored.get("content"), dict)
        and "notes" not in restored["content"],
        "ARCHIVE_READBACK_INVALID",
    )
    restored["content"]["notes"] = content.get("notes", "")
    require(restored.get("id") == meta.get("source_node_id"), "ARCHIVE_SOURCE_MISMATCH")
    return restored


def validate_archive(archive, ref, source):
    require(
        isinstance(archive, dict)
        and archive.get("id") == ref["node_id"]
        and archive.get("status") == "archived",
        "ARCHIVE_READBACK_INVALID",
    )
    restored = restore_snapshot(archive)
    require(
        sha(archive["content"]["notes"]) == ref["notes_sha256"], "ARCHIVE_HASH_MISMATCH"
    )
    require(cp.digest(restored) == ref["snapshot_sha256"], "ARCHIVE_SNAPSHOT_MISMATCH")
    binding = {
        "node_id": source["id"],
        **project_identity(source["content"]["extra"]),
    }
    identity(restored, binding)
    extra = archive["content"]["extra"]
    require(
        project_identity(extra)
        == {k: binding[k] for k in ("project_id", "project_namespace")},
        "ARCHIVE_PROJECT_MISMATCH",
    )
    return restored


def archived_entry(request, node, marker, entry):
    """No index match is ever accepted without the original full packet."""
    for ref in references(node):
        archive = request(
            "/api/nodes/" + quote(ref["node_id"], safe=""), phase="archive_read"
        )
        restored = validate_archive(archive, ref, node)
        notes = restored["content"]["notes"]
        if entry.strip() in notes:
            return True
    return False


def entries(node):
    text = node["content"].get("notes", "")
    found = {}
    for match in MARKER.finditer(text):
        try:
            packet, _ = json.JSONDecoder().raw_decode(text[match.end() :].lstrip("\n"))
        except ValueError:
            # A v1 tombstone is only justified later by a verified archive.
            require(bool(references(node)), "NOTES_MARKER_INVALID")
            continue
        require(
            isinstance(packet, dict)
            and match.group() == "RUNTIMEHOOK_DELTA_" + cp.digest(packet),
            "NOTES_MARKER_INVALID",
        )
        require(packet.get("node_id") == node["id"], "NOTES_MARKER_INVALID")
        found[match.group()] = packet
    return found


def prepare(node, binding, summary):
    identity(node, binding)
    require(node["id"].startswith(("DOC-", "SES-")), "NOTES_NODE_TYPE_UNSUPPORTED")
    require(
        "runtimehook_archive" not in node["content"]["extra"], "NOTES_ARCHIVE_IMMUTABLE"
    )
    require(
        all(
            re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,191}", str(binding.get(k, "")))
            for k in CONTEXT_KEYS
        ),
        "NOTES_CONTEXT_REQUIRED",
    )
    require(
        isinstance(summary, str)
        and summary.strip()
        and len(summary) <= 2000
        and "RUNTIMEHOOK_DELTA_" not in summary,
        "NOTES_SUMMARY_INVALID",
    )
    refs = references(node)
    require(len(refs) < MAX_ARCHIVES, "ARCHIVE_MANIFEST_FULL")
    original = node["content"].get("notes", "")
    markers = MARKER.findall(original)
    require(len(markers) == len(set(markers)), "NOTES_DUPLICATE_MARKER")
    entries(node)
    snapshot = copy.deepcopy(node)
    snapshot["content"].pop("notes", None)
    ref = {
        "node_id": "SES-RHARCH-"
        + cp.digest({"source": node["id"], "snapshot": cp.digest(node)}),
        "notes_sha256": sha(original),
        "snapshot_sha256": cp.digest(node),
    }
    archive = {
        "id": ref["node_id"],
        "name": "RuntimeHook full notes archive",
        "status": "archived",
        "cluster": node["cluster"] if "cluster" in node else "RuntimeHook",
        "content": {
            "description": "Complete original notes; exact retrieval only, not task acceptance.",
            "notes": original,
            "extra": {
                "project_id": binding["project_id"],
                "project_namespace": binding["project_namespace"],
                "runtimehook_archive": {
                    "schema": ARCHIVE_SCHEMA,
                    "source_node_id": node["id"],
                    "snapshot": snapshot,
                },
            },
        },
        "activation_keywords": [
            "runtimehook",
            "notes",
            "archive",
            "history",
            "exact-readback",
        ],
        "primary_author": binding["agent_id"],
    }
    notes = (
        summary.strip()
        + "\n\n完整历史归档: "
        + ref["node_id"]
        + "\nSHA256: "
        + ref["notes_sha256"]
        + "\n历史事件索引（不代表PASS）:\n"
        + "\n".join(markers)
    )
    require(
        len(notes.encode()) < len(original.encode()) and len(notes.encode()) < LIMIT,
        "NOTES_COMPACTION_NO_GAIN",
    )
    plan = {
        "schema": PLAN_SCHEMA,
        "binding": copy.deepcopy(binding),
        "source": copy.deepcopy(node),
        "summary": summary,
        "notes": notes,
        "archive": archive,
        "reference": ref,
        "before_bytes": len(original.encode()),
        "after_bytes": len(notes.encode()),
    }
    plan["plan_sha256"] = cp.digest(plan)
    return plan


def apply(plan, binding, path: Path, ticket_id, request):
    require(
        isinstance(plan, dict)
        and plan.get("schema") == PLAN_SCHEMA
        and plan.get("binding") == binding,
        "NOTES_PLAN_BINDING_MISMATCH",
    )
    require(
        plan.get("plan_sha256")
        == cp.digest({k: v for k, v in plan.items() if k != "plan_sha256"}),
        "NOTES_PLAN_HASH_MISMATCH",
    )
    # Recompute the entire candidate, not just a caller-controlled digest.
    require(
        prepare(plan["source"], binding, plan["summary"]) == plan, "NOTES_PLAN_INVALID"
    )
    raw = cp.read_bytes(path, 2 * 1024 * 1024)
    require(json.loads(raw.decode("utf-8-sig")) == plan, "NOTES_PLAN_FILE_MISMATCH")
    file_hash = hashlib.sha256(raw).hexdigest()
    source_path = "/api/nodes/" + quote(binding["node_id"], safe="")
    node = request(source_path)
    identity(node, binding)
    manifest = node["content"]["extra"].get(MANIFEST_KEY, {})
    if manifest.get("last_plan_sha256") == plan["plan_sha256"]:
        require(
            plan["reference"] in references(node)
            and node["content"]["notes"].startswith(plan["notes"]),
            "NOTES_APPLIED_READBACK_MISMATCH",
        )
        validate_archive(
            request("/api/nodes/" + plan["archive"]["id"]), plan["reference"], node
        )
        return {"status": "ALREADY_COMPACTED", "archive_id": plan["archive"]["id"]}
    require(
        node["updated_at"] == plan["source"]["updated_at"]
        and semantic_content(node["content"])
        == semantic_content(plan["source"]["content"]),
        "CONFLICT",
    )
    ticket = request("/api/route/ticket/" + quote(ticket_id, safe=""))
    require(
        isinstance(ticket, dict)
        and ticket.get("ticket_id") == ticket_id
        and ticket.get("project_identity_verified") is True
        and ticket.get("target_digest_source") == "server_verified_snapshot_v1"
        and all(ticket.get(k) == binding[k] for k in CONTEXT_KEYS),
        "NOTES_TICKET_CONTEXT_MISMATCH",
    )
    scope = ticket.get("scope", {})
    require(
        ticket.get("task_type") == "NotesCompaction"
        and isinstance(scope.get("target_files"), list)
        and len(scope["target_files"]) == 1
        and isinstance(scope["target_files"][0], str)
        and Path(scope["target_files"][0]).resolve() == path.resolve()
        and {binding["node_id"], "runtimehook-notes-compaction", plan["plan_sha256"]}
        <= set(scope.get("scope_keywords", [])),
        "NOTES_TICKET_SCOPE_MISMATCH",
    )
    require(
        all(
            re.fullmatch(r"[a-f0-9]{64}", str(ticket.get(k, "")))
            for k in ("target_digest", "scope_digest")
        ),
        "NOTES_TICKET_DIGEST_REQUIRED",
    )
    consumed = request(
        "/api/route/ticket/" + quote(ticket_id, safe="") + "/consume",
        method="POST",
        payload={
            "agent_id": binding["agent_id"],
            "tool_name": "RuntimeHookNotesCompaction",
            "tool_input_summary": plan["plan_sha256"],
            "target_digest": ticket["target_digest"],
            "scope_digest": ticket["scope_digest"],
        },
    )
    require(
        consumed.get("ok")
        and consumed.get("consume_count") == 1
        and re.fullmatch(r"[a-f0-9]{64}", str(consumed.get("activity_hash", ""))),
        "NOTES_TICKET_CONSUME_UNVERIFIED",
    )
    try:
        deadline = dt.datetime.fromisoformat(consumed["completion_deadline"])
        require(deadline.tzinfo is not None, "NOTES_TICKET_DEADLINE_INVALID")
    except (KeyError, ValueError):
        raise NotesError("NOTES_TICKET_DEADLINE_INVALID") from None

    def mutate(url, payload, method):
        require(dt.datetime.now(dt.timezone.utc) < deadline, "NOTES_TICKET_EXPIRED")
        require(
            hashlib.sha256(cp.read_bytes(path, 2 * 1024 * 1024)).hexdigest()
            == file_hash,
            "NOTES_PLAN_FILE_CHANGED",
        )
        return request(url, payload=payload, method=method)

    # Verify every prior reference before retaining indexed history.
    backed = set(entries(node))
    for ref in references(node):
        backed.update(
            entries(
                validate_archive(request("/api/nodes/" + ref["node_id"]), ref, node)
            )
        )
    require(
        set(MARKER.findall(node["content"]["notes"])) <= backed,
        "ARCHIVE_INDEX_UNBACKED",
    )
    archive_path = "/api/nodes/" + plan["archive"]["id"]
    archive = request(archive_path)
    if archive is None:
        archive = mutate("/api/nodes?force=true", plan["archive"], "POST")
    # The native create must honor archived status from its first durable write.
    validate_archive(archive, plan["reference"], node)
    validate_archive(request(archive_path), plan["reference"], node)
    content = copy.deepcopy(node["content"])
    content["notes"] = plan["notes"]
    content["extra"][MANIFEST_KEY] = {
        "schema": MANIFEST_SCHEMA,
        "archives": references(node) + [plan["reference"]],
        "last_plan_sha256": plan["plan_sha256"],
    }
    mutate(
        source_path,
        {
            "content": {
                "notes": content["notes"],
                "extra": {MANIFEST_KEY: content["extra"][MANIFEST_KEY]},
            },
            "expected_updated_at": node["updated_at"],
            "updated_by": binding["agent_id"],
        },
        "PUT",
    )
    actual = request(source_path)
    identity(actual, binding)
    require(
        semantic_content(actual["content"]) == semantic_content(content),
        "NOTES_APPLIED_READBACK_MISMATCH",
    )
    return {
        "status": "COMPACTED_AND_READBACK_VERIFIED",
        "archive_id": plan["archive"]["id"],
        "plan_sha256": plan["plan_sha256"],
        "before_bytes": plan["before_bytes"],
        "after_bytes": plan["after_bytes"],
        "updated_at": actual["updated_at"],
        "notes_sha256": sha(plan["notes"]),
        "ticket_id": ticket_id,
        "consume_activity_hash": consumed["activity_hash"],
    }
