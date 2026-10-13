"""Notes maintenance: original packet replay, integrity, CAS and scoped tickets."""

import copy
import datetime as dt
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import test_runtimehook_writeback as writeback_tests
import test_async_graph_concurrency as graph_tests
import runtimehook_writeback as wb
from test_runtimehook_plugin import _activate, _session_id, PLUGIN_CLI, PROJECT_KIT_CLI

lane = writeback_tests.lane
plain_repo = writeback_tests.plain_repo
isolated_scope_cache = writeback_tests.isolated_scope_cache
graph_runtime = graph_tests.graph_runtime


@pytest.fixture
def archived_lane(lane, monkeypatch):
    first = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    snapshot = copy.deepcopy(lane.node)
    marker = first["event_id"]
    archive_id = "SES-RHARCH-" + "a" * 64
    snapshot_sha = wb.cp.digest(snapshot)
    import hashlib

    notes_sha = hashlib.sha256(snapshot["content"]["notes"].encode()).hexdigest()
    ref = {
        "node_id": archive_id,
        "notes_sha256": notes_sha,
        "snapshot_sha256": snapshot_sha,
    }
    stored = copy.deepcopy(snapshot)
    stored["content"].pop("notes")
    archive = {
        "id": archive_id,
        "status": "archived",
        "content": {
            "notes": snapshot["content"]["notes"],
            "extra": {
                "project_id": "fixture",
                "project_namespace": "fixture",
                "runtimehook_archive": {
                    "schema": "3can.runtimehook-notes-archive/v1",
                    "source_node_id": snapshot["id"],
                    "snapshot": stored,
                },
            },
        },
    }
    lane.node["content"]["notes"] = "当前阶段仍PARTIAL；历史在归档。\n" + marker
    lane.node["content"]["extra"]["runtimehook_notes_archives"] = {
        "schema": "3can.runtimehook-notes-archives/v1",
        "archives": [ref],
    }
    old = lane.request

    def request(base, path, **kwargs):
        if path == "/api/nodes/" + archive_id:
            lane.calls.append((path, None))
            return True, copy.deepcopy(archive)
        return old(base, path, **kwargs)

    monkeypatch.setattr(lane.client, "_try_json_request", request)
    lane.calls.clear()
    return lane, archive, snapshot


def test_old_delta_reads_original_archive_without_append(archived_lane):
    lane, _, _ = archived_lane
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "ALREADY_RECORDED"
    assert not any(payload for _, payload in lane.calls)


@pytest.mark.parametrize(
    "tamper", ["notes", "snapshot", "project", "source", "status", "reference"]
)
def test_archived_marker_never_accepts_tampered_or_foreign_history(
    archived_lane, tamper
):
    lane, archive, _ = archived_lane
    if tamper == "notes":
        archive["content"]["notes"] += "changed"
    elif tamper == "snapshot":
        archive["content"]["extra"]["runtimehook_archive"]["snapshot"]["updated_at"] = (
            "changed"
        )
    elif tamper == "project":
        archive["content"]["extra"]["project_namespace"] = "foreign"
    elif tamper == "source":
        archive["content"]["extra"]["runtimehook_archive"]["source_node_id"] = (
            "DOC-other"
        )
    elif tamper == "status":
        archive["status"] = "active"
    else:
        lane.node["content"]["extra"]["runtimehook_notes_archives"]["archives"][0][
            "node_id"
        ] = "../../foreign"
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "UNAVAILABLE"
    assert not any(payload for _, payload in lane.calls if "/api/activity/log" not in _)


def test_new_delta_appends_to_active_notes_without_scanning_archives(archived_lane):
    lane, _, _ = archived_lane
    lane.event["summary"] = "新的独立阶段，未完成验收。"
    result = wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    assert result["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    assert not any("SES-RHARCH-" in path for path, _ in lane.calls)
    assert lane.node["content"]["extra"]["runtimehook_notes_archives"]["archives"]


@pytest.fixture
def maintenance(lane, tmp_path):
    notes = importlib.import_module("runtimehook_notes")
    wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    original = copy.deepcopy(lane.node)
    plan = notes.prepare(
        lane.node, lane.binding, "当前仍PARTIAL；保留未解决故障及源证据。"
    )
    path = lane.root / "plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    context = {
        k: lane.binding[k]
        for k in (
            "agent_id",
            "project_id",
            "project_namespace",
            "workspace_id",
            "workorder_id",
        )
    }
    future = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)).isoformat()
    ticket = {
        "ticket_id": "rt-maintenance",
        "agent_id": context["agent_id"],
        **context,
        "target_digest": "b" * 64,
        "scope_digest": "c" * 64,
        "project_identity_verified": True,
        "target_digest_source": "server_verified_snapshot_v1",
        "scope": {
            "target_files": [str(path)],
            "scope_keywords": [
                lane.binding["node_id"],
                "runtimehook-notes-compaction",
                plan["plan_sha256"],
            ],
        },
        "task_type": "NotesCompaction",
    }
    nodes = {lane.node["id"]: lane.node}
    calls = []

    def request(url, *, payload=None, method="GET"):
        calls.append((url, copy.deepcopy(payload), method))
        if url.startswith("/api/nodes/"):
            node_id = url[len("/api/nodes/") :]
            if method == "PUT":
                node = nodes[node_id]
                if payload["expected_updated_at"] != node["updated_at"]:
                    raise notes.NotesError("CONFLICT")
                patch = copy.deepcopy(payload)
                if "content" in patch:
                    node["content"]["extra"].update(patch["content"].pop("extra", {}))
                    node["content"].update(patch.pop("content"))
                node.update(
                    {k: v for k, v in patch.items() if k != "expected_updated_at"}
                )
                node["updated_at"] = "opaque-maintained-" + str(len(calls))
            return copy.deepcopy(nodes.get(node_id))
        if url == "/api/nodes?force=true":
            node = copy.deepcopy(payload)
            node["status"] = payload.get("status", "active")
            node["updated_at"] = "archive-created"
            nodes[node["id"]] = node
            return copy.deepcopy(node)
        if url == "/api/route/ticket/rt-maintenance":
            return copy.deepcopy(ticket)
        if url.endswith("/consume"):
            return {
                "ok": True,
                "consume_count": 1,
                "activity_hash": "d" * 64,
                "completion_deadline": future,
            }
        pytest.fail(url)

    return notes, lane, original, plan, path, ticket, nodes, calls, request


def test_prepare_is_lossless_and_does_not_create_a_pass(maintenance):
    notes, lane, original, plan, *_ = maintenance
    archive = plan["archive"]
    restored = notes.restore_snapshot(archive)
    assert restored == original
    assert plan["before_bytes"] > plan["after_bytes"]
    assert "RUNTIMEHOOK_DELTA_" in plan["notes"]
    assert lane.node == original
    assert "PASS" not in plan["summary"]


def test_archive_is_archived_in_the_first_native_create(maintenance):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    assert plan["archive"]["status"] == "archived"
    notes.apply(plan, lane.binding, path, "rt-maintenance", request)
    assert not any(
        url == "/api/nodes/" + plan["archive"]["id"] and method == "PUT"
        for url, _, method in calls
    )


def test_same_scope_apply_archives_before_cas_and_preserves_metadata(maintenance):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    result = notes.apply(plan, lane.binding, path, "rt-maintenance", request)
    assert result["status"] == "COMPACTED_AND_READBACK_VERIFIED"
    active = nodes[original["id"]]
    assert active["content"]["extra"]["project_id"] == "fixture"
    assert notes.restore_snapshot(nodes[plan["archive"]["id"]]) == original
    assert nodes[plan["archive"]["id"]]["status"] == "archived"
    assert active["content"]["extra"]["runtimehook_notes_archives"]["archives"]
    assert next(
        i for i, c in enumerate(calls) if c[0] == "/api/nodes?force=true"
    ) < next(
        i
        for i, c in enumerate(calls)
        if c[0] == "/api/nodes/" + original["id"] and c[2] == "PUT"
    )


def test_native_read_access_counters_do_not_stale_plan_or_get_overwritten(maintenance):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    nodes[original["id"]]["activation_count"] = 7
    nodes[original["id"]]["content"]["extra"]["last_accessed_at"] = "new-read"
    result = notes.apply(plan, lane.binding, path, "rt-maintenance", request)
    assert result["status"] == "COMPACTED_AND_READBACK_VERIFIED"
    assert nodes[original["id"]]["activation_count"] == 7
    assert nodes[original["id"]]["content"]["extra"]["last_accessed_at"] == "new-read"
    target_write = next(
        payload
        for url, payload, method in calls
        if url == "/api/nodes/" + original["id"] and method == "PUT"
    )
    assert set(target_write["content"]) == {"notes", "extra"}
    assert set(target_write["content"]["extra"]) == {notes.MANIFEST_KEY}


@pytest.mark.parametrize(
    "key",
    ["agent_id", "project_id", "project_namespace", "workspace_id", "workorder_id"],
)
def test_foreign_maintenance_binding_rejected_before_network(maintenance, key):
    notes, lane, _, plan, path, _, _, calls, request = maintenance
    with pytest.raises(notes.NotesError):
        notes.apply(
            plan, {**lane.binding, key: "foreign"}, path, "rt-maintenance", request
        )
    assert not calls


@pytest.mark.parametrize(
    "bad",
    ["scope", "target", "task", "agent", "context", "digest", "deadline", "consume"],
)
def test_ticket_gate_refuses_before_archive_or_target_writes(maintenance, bad):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    if bad == "scope":
        ticket["scope"]["scope_keywords"] = []
    elif bad == "target":
        ticket["scope"]["target_files"] = [str(path.parent / "other.json")]
    elif bad == "task":
        ticket["task_type"] = "Edit"
    elif bad == "agent":
        ticket["agent_id"] = "foreign"
    elif bad == "context":
        ticket["project_namespace"] = "foreign"
    elif bad == "digest":
        ticket["target_digest"] = "not-a-digest"

    def wrapped(url, **kwargs):
        result = request(url, **kwargs)
        if url.endswith("/consume"):
            if bad == "deadline":
                result["completion_deadline"] = "2000-01-01T00:00:00+00:00"
            elif bad == "consume":
                result["consume_count"] = 2
        return result

    with pytest.raises(notes.NotesError):
        notes.apply(plan, lane.binding, path, "rt-maintenance", wrapped)
    assert nodes[original["id"]] == original
    assert not any(
        method == "PUT" or url == "/api/nodes?force=true" for url, _, method in calls
    )


@pytest.mark.parametrize(
    "failure",
    ["version", "archive_readback", "target_cas", "target_readback", "plan_changed"],
)
def test_failure_preserves_full_history_without_retry(maintenance, failure):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    if failure == "version":
        nodes[original["id"]]["updated_at"] = "other-writer"

    def wrapped(url, **kwargs):
        if (
            failure == "target_cas"
            and url == "/api/nodes/" + original["id"]
            and kwargs.get("method") == "PUT"
        ):
            calls.append((url, kwargs.get("payload"), "PUT"))
            raise notes.NotesError("CONFLICT")
        result = request(url, **kwargs)
        if (
            url == "/api/nodes/" + plan["archive"]["id"]
            and result
            and result.get("status") == "archived"
        ):
            if failure == "archive_readback":
                result["content"]["notes"] += "tampered"
            elif failure == "plan_changed":
                path.write_text("{}", encoding="utf-8")
        if (
            failure == "target_readback"
            and url == "/api/nodes/" + original["id"]
            and kwargs.get("method", "GET") == "GET"
            and len(calls) > 5
        ):
            result["content"]["notes"] = "wrong readback"
        return result

    with pytest.raises(notes.NotesError):
        notes.apply(plan, lane.binding, path, "rt-maintenance", wrapped)
    if failure != "target_readback":
        assert nodes[original["id"]]["content"] == original["content"]
    assert (
        sum(
            url == "/api/nodes/" + original["id"] and method == "PUT"
            for url, _, method in calls
        )
        <= 1
    )
    if plan["archive"]["id"] in nodes:
        assert notes.restore_snapshot(nodes[plan["archive"]["id"]]) == original


def test_plan_file_must_equal_applied_object_before_network(maintenance):
    notes, lane, _, plan, path, _, _, calls, request = maintenance
    other = notes.prepare(
        plan["source"], lane.binding, "另一个有效候选，未授权用于本请求。"
    )
    path.write_text(json.dumps(other, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(notes.NotesError, match="NOTES_PLAN_FILE_MISMATCH"):
        notes.apply(plan, lane.binding, path, "rt-maintenance", request)
    assert not calls


def test_plan_replaced_after_first_read_cannot_authorize_old_object(
    maintenance, monkeypatch
):
    notes, lane, _, plan, path, _, _, calls, request = maintenance
    replacement = json.dumps(
        notes.prepare(plan["source"], lane.binding, "不同的有效计划。"),
        ensure_ascii=False,
    ).encode()
    opened = Path.open
    first = True

    class ReplacingRead(io.BytesIO):
        def read(self, size=-1):
            raw = super().read(size)
            with opened(path, "wb") as stream:
                stream.write(replacement)
            return raw

    def open_file(self, mode="r", *args, **kwargs):
        nonlocal first
        if self == path and mode == "rb" and first:
            first = False
            with opened(self, mode) as stream:
                return ReplacingRead(stream.read())
        return opened(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(notes.NotesError, match="NOTES_PLAN_FILE_CHANGED"):
        notes.apply(plan, lane.binding, path, "rt-maintenance", request)
    assert not any(
        method in {"POST", "PUT"} and "/api/nodes" in url for url, _, method in calls
    )


def test_repeated_plan_is_read_only_even_after_new_active_delta(maintenance):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    notes.apply(plan, lane.binding, path, "rt-maintenance", request)
    nodes[original["id"]]["content"]["notes"] += "\nnew independent meaning"
    calls.clear()
    assert (
        notes.apply(plan, lane.binding, path, "rt-maintenance", request)["status"]
        == "ALREADY_COMPACTED"
    )
    assert all(method == "GET" for _, _, method in calls)
    assert nodes[original["id"]]["content"]["notes"].endswith("new independent meaning")


def test_manifest_and_marker_budgets_do_not_raise_notes_limit(maintenance):
    notes, lane, original, _, *_ = maintenance
    original["content"]["extra"]["runtimehook_notes_archives"] = {
        "schema": notes.MANIFEST_SCHEMA,
        "archives": [
            {
                "node_id": "SES-RHARCH-" + "a" * 64,
                "notes_sha256": "b" * 64,
                "snapshot_sha256": "c" * 64,
            }
        ]
        * 32,
    }
    with pytest.raises(notes.NotesError):
        notes.prepare(original, lane.binding, "meaning")


def test_real_graph_generic_apis_preserve_archives_and_readback(
    graph_runtime, maintenance
):
    notes, lane, original, plan, path, ticket, nodes, calls, request = maintenance
    module, engine, graph_dir = graph_runtime
    engine.create_node(
        module.NodeCreate(
            id=original["id"],
            name="History",
            cluster="fixture",
            content=module.NodeContent(**original["content"]),
        )
    )
    actual = engine.get_node(original["id"]).model_dump()
    original.clear()
    original.update(actual)
    plan = notes.prepare(actual, lane.binding, "真实图谱原语隔离验证，生产未安装。")
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    ticket["scope"]["scope_keywords"] = [
        lane.binding["node_id"],
        "runtimehook-notes-compaction",
        plan["plan_sha256"],
    ]

    def real(url, *, payload=None, method="GET"):
        if url.startswith("/api/route/"):
            return request(url, payload=payload, method=method)
        if url == "/api/nodes?force=true":
            return engine.create_node(module.NodeCreate(**payload)).model_dump()
        node_id = url.removeprefix("/api/nodes/")
        if method == "PUT":
            return engine.update_node(
                node_id, module.NodeUpdate(**payload)
            ).model_dump()
        found = engine.get_node(node_id)
        return found.model_dump() if found else None

    result = notes.apply(plan, lane.binding, path, "rt-maintenance", real)
    assert result["status"] == "COMPACTED_AND_READBACK_VERIFIED"
    assert (
        notes.restore_snapshot(engine.get_node(plan["archive"]["id"]).model_dump())
        == actual
    )
    assert (
        json.loads(
            (graph_dir / "nodes" / f"{plan['archive']['id']}.json").read_text(
                encoding="utf-8"
            )
        )["status"]
        == "archived"
    )
    assert (
        engine.get_node(original["id"]).content.extra["project_namespace"] == "fixture"
    )
    route = engine.route(
        module.RoutingRequest(
            task="runtimehook notes archive",
            max_nodes=20,
            mode="skeleton",
            confirm_low_confidence=True,
        )
    )
    assert plan["archive"]["id"] not in {n.id for n in route.activated_nodes}


@pytest.mark.parametrize("cli", [PLUGIN_CLI, PROJECT_KIT_CLI])
@pytest.mark.parametrize("refusal", [False, True])
def test_actual_cli_prepare_and_apply_with_owned_stub_transport(
    lane, tmp_path, cli, refusal
):
    """Real controller subprocess + canonical client; no sockets or production state."""
    _activate(lane.root)
    wb.deliver(lane.settings, lane.root, lane.binding, lane.event)
    wire = tmp_path / "wire.json"
    wire.write_text(
        json.dumps({"nodes": {lane.node["id"]: lane.node}, "calls": []}),
        encoding="utf-8",
    )
    stub = tmp_path / "owned_client_stub.py"
    header = (
        "from pathlib import Path\nexec(compile(Path("
        + repr(str(writeback_tests.CLIENT))
        + ").read_text(encoding='utf-8'), 'canonical_client', 'exec'))\nWIRE = Path("
        + repr(str(wire))
        + ")\nSELECTOR = Path("
        + repr(lane.settings["selector_path"])
        + ")\n"
    )
    stub.write_text(
        header
        + """
def _try_json_request(base, path, *, payload=None, method="GET", **kwargs):
    state = json.loads(WIRE.read_text(encoding="utf-8"))
    state["calls"].append([method, path])
    if state.get("refuse") and method == "PUT":
        WIRE.write_text(json.dumps(state), encoding="utf-8")
        return False, {"http_status": 409, "detail": "private response must not leak"}
    if path == "/api/activity/log": value = {"ok": True, "self_hash": "e"*64}
    elif path.startswith("/api/stats"):
        selected = json.loads(SELECTOR.read_text(encoding="utf-8"))
        value = {"healthy": True, "total_nodes": 1, "readiness": {"production_ready": True},
                 "runtime_identity": _expected_runtime_identity(Path(selected["engine_root"]), Path(selected["graph_root"]))}
    elif path == "/api/agents/checkin": value = {"agent_id": payload["agent_id"]}
    elif path == "/api/nodes?force=true":
        value = dict(payload, updated_at="archive-created")
        state["nodes"][value["id"]] = value
    elif path.startswith("/api/nodes/"):
        value = state["nodes"].get(path.removeprefix("/api/nodes/"))
        if value is None: return False, {"http_status": 404}
        if method == "PUT":
            if value["updated_at"] != payload["expected_updated_at"]: return False, {"http_status": 409}
            patch = payload.get("content", {})
            value["content"]["extra"].update(patch.get("extra", {}))
            value["content"].update({k:v for k,v in patch.items() if k != "extra"})
            value["updated_at"] = "opaque-after-compaction"
    elif path == "/api/writeback":
        change = payload["changes"][0]
        value = state["nodes"][change["node_id"]]
        if value["updated_at"] != change["expected_updated_at"]: return False, {"http_status": 409}
        value["content"]["notes"] = change["value"]
        value["updated_at"] = "opaque-before-compaction"
        value = {"count": 1, "updated": [change["node_id"]]}
    elif path == "/api/route/ticket/rt-cli": value = state["ticket"]
    elif path == "/api/route/ticket/rt-cli/consume":
        from datetime import timedelta
        value = {"ok": True, "consume_count": 1, "activity_hash": "d"*64,
                 "completion_deadline": (datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat()}
    else: return False, {"http_status": 404}
    WIRE.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return True, value
""",
        encoding="utf-8",
    )
    settings = {
        **lane.settings,
        "schema": "3can.runtimehook-writeback/v1",
        "client_path": str(stub),
    }
    config = Path(os.environ["CODEX_HOME"]) / "runtimehook/writeback.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(settings), encoding="utf-8")
    base = [
        sys.executable,
        "-B",
        str(cli),
        "--root",
        str(lane.root),
        "--native-cwd",
        str(lane.root),
        "--session-id",
        _session_id(lane.root),
    ]

    def run(*args):
        completed = subprocess.run(
            base + list(args),
            cwd=lane.root,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        return completed.returncode, json.loads(completed.stdout)

    code, connected = run(
        "connect",
        "--agent-id",
        lane.binding["agent_id"],
        "--workorder-id",
        lane.binding["workorder_id"],
        "--node-id",
        lane.binding["node_id"],
        "--reference",
        "fixture:owned transport",
    )
    assert (
        code == 0
        and connected["writeback"]["status"] == "WRITTEN_AND_READBACK_VERIFIED"
    )
    summary = lane.root / "summary.txt"
    summary.write_text("仅隔离CLI契约验证，生产未接受。", encoding="utf-8")
    plan_path = lane.root / "private/plan.json"
    before = json.loads(wire.read_text(encoding="utf-8"))
    code, prepared = run(
        "notes-compact", "--plan", str(plan_path), "--summary-file", str(summary)
    )
    assert code == 0 and prepared["status"] == "PREPARED_READ_ONLY"
    after = json.loads(wire.read_text(encoding="utf-8"))
    assert after["nodes"] == before["nodes"]
    assert all(method == "GET" for method, _ in after["calls"][len(before["calls"]) :])
    code, missing = run("notes-compact", "--plan", str(plan_path), "--apply")
    assert code == 2 and "NOTES_TICKET_REQUIRED" in missing["error"]
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    state = json.loads(wire.read_text(encoding="utf-8"))
    state["ticket"] = {
        **{
            k: plan["binding"][k]
            for k in (
                "agent_id",
                "project_id",
                "project_namespace",
                "workspace_id",
                "workorder_id",
            )
        },
        "ticket_id": "rt-cli",
        "task_type": "NotesCompaction",
        "project_identity_verified": True,
        "target_digest_source": "server_verified_snapshot_v1",
        "target_digest": "b" * 64,
        "scope_digest": "c" * 64,
        "scope": {
            "target_files": [str(plan_path.resolve())],
            "scope_keywords": [
                lane.node["id"],
                "runtimehook-notes-compaction",
                plan["plan_sha256"],
            ],
        },
    }
    state["refuse"] = refusal
    wire.write_text(json.dumps(state), encoding="utf-8")
    code, applied = run(
        "notes-compact", "--plan", str(plan_path), "--apply", "--ticket-id", "rt-cli"
    )
    if refusal:
        assert code == 2 and "CONFLICT" in applied["error"]
        state = json.loads(wire.read_text(encoding="utf-8"))
        assert sum(path == "/api/activity/log" for _, path in state["calls"]) == 1
        receipts = list(
            (Path(os.environ["CODEX_HOME"]) / "runtimehook/sessions").rglob(
                "notes-maintenance-*.json"
            )
        )
        failed = [json.loads(p.read_text(encoding="utf-8")) for p in receipts]
        failed = [r for r in failed if r.get("error_code") == "CONFLICT"]
        assert len(failed) == 1 and failed[0]["issue_intake"] == "RECORDED"
        assert any(
            r["method"] == "POST" and r["path"] == "/api/nodes?force=true" and r["ok"]
            for r in failed[0]["requests"]
        )
        assert "private response" not in json.dumps(failed)
        assert state["nodes"][lane.node["id"]] == before["nodes"][lane.node["id"]]
        return
    assert code == 0 and applied["status"] == "COMPACTED_AND_READBACK_VERIFIED"
    code, repeated = run(
        "notes-compact", "--plan", str(plan_path), "--apply", "--ticket-id", "rt-cli"
    )
    assert code == 0 and repeated["status"] == "ALREADY_COMPACTED"
