from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[1]
PROXY_SERVER = ROOT / "proxy" / "server.py"


def _load_proxy_server() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "threecan_proxy_process_safety_test",
        PROXY_SERVER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def proxy_server() -> ModuleType:
    module = _load_proxy_server()
    # Existing process-lifecycle tests exercise the generic proxy primitives.
    # Dedicated tests below opt back into the production single-writer mode.
    module.SINGLE_WRITER_GRAPH = False
    yield module
    asyncio.run(module.client.aclose())


def _snapshot(
    module: ModuleType,
    *,
    pid: int,
    port: int,
    nonce: str,
    creation_id: str = "creation-1",
) -> dict[str, Any]:
    return {
        "status": "found",
        "pid": pid,
        "executable_path": str(Path(module.sys.executable).resolve()),
        "command_argv": module._managed_backend_command(port, nonce),
        "creation_id": creation_id,
    }


def _slot_state(
    module: ModuleType,
    *,
    pid: int = 4321,
    port: int = 9702,
    nonce: str = "a" * 32,
) -> dict[str, Any]:
    snapshot = _snapshot(
        module,
        pid=pid,
        port=port,
        nonce=nonce,
    )
    return {
        "port": port,
        "status": "healthy",
        "pid": pid,
        "started_at": 1.0,
        "process_identity": module._managed_process_identity(
            pid=pid,
            port=port,
            start_nonce=nonce,
            snapshot=snapshot,
        ),
    }


def _install_fake_managed_spawn(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    *,
    pid: int = 9001,
) -> tuple[list[str], list[list[str]]]:
    lifecycle: list[str] = []
    started: list[list[str]] = []

    class FakeProcess:
        returncode = None

        def __init__(self) -> None:
            self.pid = pid

        def poll(self):
            return self.returncode

        def terminate(self):
            lifecycle.append("terminate")
            self.returncode = 0

        def wait(self, timeout):
            lifecycle.append("wait")
            return self.returncode

        def kill(self):
            lifecycle.append("kill")
            self.returncode = -9

    def fake_popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess()

    def fake_wait(process, *, port, start_nonce, timeout_sec=6.0):
        return _snapshot(
            module,
            pid=process.pid,
            port=port,
            nonce=start_nonce,
            creation_id="recovery-creation",
        )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_wait_for_managed_snapshot", fake_wait)
    monkeypatch.setattr(module.asyncio, "to_thread", immediate_to_thread)
    return lifecycle, started


@pytest.mark.parametrize(
    ("last_error", "expected"),
    [
        (87, {"status": "not_found"}),
        (
            5,
            {
                "status": "unavailable",
                "reason": "process_handle_open_failed",
                "os_error": 5,
            },
        ),
        (
            0,
            {
                "status": "unavailable",
                "reason": "process_handle_open_failed",
                "os_error": 0,
            },
        ),
    ],
)
def test_windows_open_process_failure_only_treats_error_87_as_not_found(
    proxy_server: ModuleType,
    last_error: int,
    expected: dict[str, Any],
) -> None:
    assert proxy_server._windows_open_process_failure(last_error) == expected


@pytest.mark.parametrize(
    ("query_succeeded", "exit_code", "expected"),
    [
        (True, 259, {"status": "active"}),
        (True, 0, {"status": "not_found", "exit_code": 0}),
        (True, 7, {"status": "not_found", "exit_code": 7}),
        (
            False,
            0,
            {
                "status": "unavailable",
                "reason": "process_exit_code_query_failed",
            },
        ),
    ],
)
def test_windows_exit_code_distinguishes_exited_process_objects(
    proxy_server: ModuleType,
    query_succeeded: bool,
    exit_code: int,
    expected: dict[str, Any],
) -> None:
    assert proxy_server._windows_exit_code_state(
        query_succeeded=query_succeeded,
        exit_code=exit_code,
    ) == expected


def test_managed_backend_command_carries_exact_root_port_and_nonce(
    proxy_server: ModuleType,
) -> None:
    nonce = "b" * 32
    command = proxy_server._managed_backend_command(9792, nonce)

    assert len(command) == 7
    assert proxy_server._path_identity(command[0]) == proxy_server._path_identity(
        proxy_server.sys.executable
    )
    assert command[1:3] == ["-B", "-c"]
    assert command[3] == proxy_server._MANAGED_BACKEND_BOOTSTRAP
    assert proxy_server._path_identity(command[4]) == proxy_server._path_identity(
        proxy_server.BACKEND_DIR / "app.py"
    )
    assert command[5:] == ["9792", nonce]


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    [
        (
            lambda state: state["process_identity"].update(
                {"start_nonce": "c" * 32}
            ),
            "managed_process_identity_mismatch",
        ),
        (
            lambda state: state["process_identity"].update({"port": 9799}),
            "managed_process_identity_mismatch",
        ),
        (
            lambda state: state["process_identity"].update(
                {"engine_root": str(Path.cwd() / "sibling-engine")}
            ),
            "managed_process_identity_mismatch",
        ),
        (
            lambda state: state["process_identity"]["command_argv"].__setitem__(
                3,
                str(Path.cwd() / "sibling-engine" / "backend" / "app.py"),
            ),
            "managed_process_identity_mismatch",
        ),
        (
            lambda state: state["process_identity"].update(
                {"creation_id": "different-creation"}
            ),
            "live_process_identity_mismatch",
        ),
    ],
)
def test_process_verification_rejects_identity_drift(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    mutation,
    expected_reason: str,
) -> None:
    slot = _slot_state(proxy_server)
    live_snapshot = copy.deepcopy(
        slot["process_identity"]
    )
    live_snapshot = {
        "status": "found",
        "pid": live_snapshot["pid"],
        "executable_path": live_snapshot["python_executable"],
        "command_argv": live_snapshot["command_argv"],
        "creation_id": live_snapshot["creation_id"],
    }
    mutation(slot)
    monkeypatch.setattr(
        proxy_server,
        "_process_snapshot",
        lambda pid: copy.deepcopy(live_snapshot),
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": [4321]},
    )

    result = proxy_server._verify_managed_backend_process(slot)

    assert result == {"ok": False, "reason": expected_reason}


def test_process_verification_requires_expected_pid_on_occupied_port(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = _slot_state(proxy_server)
    live = _snapshot(
        proxy_server,
        pid=4321,
        port=9702,
        nonce="a" * 32,
    )
    monkeypatch.setattr(proxy_server, "_process_snapshot", lambda pid: live)
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": [9999]},
    )

    result = proxy_server._verify_managed_backend_process(slot)

    assert result == {
        "ok": False,
        "reason": "managed_port_owned_by_different_process",
    }


def test_process_verification_accepts_exact_managed_identity(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slot = _slot_state(proxy_server)
    live = _snapshot(
        proxy_server,
        pid=4321,
        port=9702,
        nonce="a" * 32,
    )
    monkeypatch.setattr(proxy_server, "_process_snapshot", lambda pid: live)
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": [4321]},
    )

    result = proxy_server._verify_managed_backend_process(slot)

    assert result["ok"] is True
    assert result["pid"] == 4321
    assert result["port"] == 9702
    assert result["listener_state"] == "owned"


def test_termination_fails_closed_before_opening_an_os_handle(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: {"ok": False, "reason": "live_process_identity_mismatch"},
    )

    result = proxy_server._terminate_verified_backend({"pid": 4321})

    assert result == {
        "ok": False,
        "reason": "live_process_identity_mismatch",
    }


def test_windows_termination_requires_exit_on_the_same_handle(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {
        "ok": True,
        "pid": 4321,
        "snapshot": {"creation_id": "creation-1"},
    }
    lifecycle: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: copy.deepcopy(first),
    )
    monkeypatch.setattr(
        proxy_server,
        "_windows_open_termination_handle",
        lambda pid: "stable-handle",
    )
    monkeypatch.setattr(
        proxy_server,
        "_windows_terminate_handle",
        lambda handle: lifecycle.append(("terminate", handle)) or True,
    )
    monkeypatch.setattr(
        proxy_server,
        "_windows_wait_for_process_exit",
        lambda handle: lifecycle.append(("wait", handle)) or False,
    )
    monkeypatch.setattr(
        proxy_server,
        "_windows_close_termination_handle",
        lambda handle: lifecycle.append(("close", handle)),
    )

    result = proxy_server._terminate_verified_windows_backend({}, first)

    assert result == {"ok": False, "reason": "process_exit_not_confirmed"}
    assert lifecycle == [
        ("terminate", "stable-handle"),
        ("wait", "stable-handle"),
        ("close", "stable-handle"),
    ]
    assert "process_synchronize" in PROXY_SERVER.read_text(encoding="utf-8")


def test_posix_termination_requires_exit_on_the_same_pidfd(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = {
        "ok": True,
        "pid": 4321,
        "snapshot": {"creation_id": "creation-1"},
    }
    lifecycle: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: copy.deepcopy(first),
    )
    monkeypatch.setattr(
        proxy_server,
        "_posix_open_pidfd",
        lambda pid: 92,
    )
    monkeypatch.setattr(
        proxy_server,
        "_posix_send_termination",
        lambda process_fd: lifecycle.append(("terminate", process_fd)),
    )
    monkeypatch.setattr(
        proxy_server,
        "_posix_wait_for_process_exit",
        lambda process_fd: lifecycle.append(("wait", process_fd)) or False,
    )
    monkeypatch.setattr(
        proxy_server,
        "_posix_close_pidfd",
        lambda process_fd: lifecycle.append(("close", process_fd)),
    )

    result = proxy_server._terminate_verified_posix_backend({}, first)

    assert result == {"ok": False, "reason": "process_exit_not_confirmed"}
    assert lifecycle == [
        ("terminate", 92),
        ("wait", 92),
        ("close", 92),
    ]


@pytest.mark.parametrize(
    ("terminate_function", "platform"),
    [
        ("_terminate_verified_windows_backend", "windows"),
        ("_terminate_verified_posix_backend", "posix"),
    ],
)
def test_stable_termination_reports_confirmed_exit(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    terminate_function: str,
    platform: str,
) -> None:
    first = {
        "ok": True,
        "pid": 4321,
        "snapshot": {"creation_id": "creation-1"},
    }
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: copy.deepcopy(first),
    )
    if platform == "windows":
        monkeypatch.setattr(
            proxy_server,
            "_windows_open_termination_handle",
            lambda pid: "stable-handle",
        )
        monkeypatch.setattr(
            proxy_server,
            "_windows_terminate_handle",
            lambda handle: True,
        )
        monkeypatch.setattr(
            proxy_server,
            "_windows_wait_for_process_exit",
            lambda handle: True,
        )
        monkeypatch.setattr(
            proxy_server,
            "_windows_close_termination_handle",
            lambda handle: None,
        )
    else:
        monkeypatch.setattr(proxy_server, "_posix_open_pidfd", lambda pid: 92)
        monkeypatch.setattr(
            proxy_server,
            "_posix_send_termination",
            lambda process_fd: None,
        )
        monkeypatch.setattr(
            proxy_server,
            "_posix_wait_for_process_exit",
            lambda process_fd: True,
        )
        monkeypatch.setattr(
            proxy_server,
            "_posix_close_pidfd",
            lambda process_fd: None,
        )

    result = getattr(proxy_server, terminate_function)({}, first)

    assert result == {
        "ok": True,
        "pid": 4321,
        "identity_verified": True,
        "exit_confirmed": True,
    }


def test_admin_retire_preserves_state_when_identity_is_unverified(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    proxy_server.state = copy.deepcopy(original)
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        proxy_server,
        "_terminate_verified_backend",
        lambda slot: {"ok": False, "reason": "start_nonce_mismatch"},
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_retire({"slot": "blue"}))

    assert raised.value.status_code == 409
    assert raised.value.detail["fail_closed"] is True
    assert proxy_server.state == original
    assert saved == []


def test_admin_retire_clears_identity_only_after_verified_termination(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        proxy_server,
        "_terminate_verified_backend",
        lambda slot: {
            "ok": True,
            "pid": slot["pid"],
            "identity_verified": True,
            "exit_confirmed": True,
        },
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    result = asyncio.run(proxy_server.admin_retire({"slot": "blue"}))

    assert result == {
        "retired": "blue",
        "pid": 4321,
        "identity_verified": True,
        "exit_confirmed": True,
    }
    assert proxy_server.state["blue"] == {
        "port": 9702,
        "status": "idle",
        "pid": None,
        "started_at": None,
    }
    assert saved[-1] == proxy_server.state


def test_admin_retire_repairs_stale_inactive_slot_after_exact_exit_evidence(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    saved: list[dict[str, Any]] = []
    monkeypatch.setattr(
        proxy_server,
        "_process_snapshot",
        lambda pid: {"status": "not_found"},
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": []},
    )
    monkeypatch.setattr(proxy_server.time, "time", lambda: 1234.5)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    result = asyncio.run(proxy_server.admin_retire({"slot": "blue"}))

    assert result == {
        "retired": "blue",
        "pid": 4321,
        "identity_verified": True,
        "exit_confirmed": True,
        "orphan_state_repaired": True,
        "already_exited": True,
    }
    assert proxy_server.state["blue"] == {
        "port": 9702,
        "status": "idle",
        "pid": None,
        "started_at": None,
        "last_exit_observation": {
            "pid": 4321,
            "observed_at": 1234.5,
            "process_status": "not_found",
            "listener_state": "empty",
            "reason": "managed_process_not_found",
            "creation_id": "creation-1",
            "start_nonce": "a" * 32,
        },
    }
    assert saved[-1] == proxy_server.state


def test_admin_retire_rolls_back_orphan_repair_when_state_cannot_persist(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    proxy_server.state = copy.deepcopy(original)
    monkeypatch.setattr(
        proxy_server,
        "_process_snapshot",
        lambda pid: {"status": "not_found"},
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": []},
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_retire({"slot": "blue"}))

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "error": "retire_state_persist_failed",
        "slot": "blue",
        "pid": 4321,
        "process_terminated": False,
        "orphan_state_repair_pending": True,
        "fail_closed": True,
    }
    assert proxy_server.state == original


@pytest.mark.parametrize(
    ("termination", "listeners", "expected_reason"),
    [
        (
            {"ok": False, "reason": "live_process_identity_mismatch"},
            {"status": "ok", "pids": []},
            "live_process_identity_mismatch",
        ),
        (
            {"ok": False, "reason": "process_handle_open_failed"},
            {"status": "ok", "pids": []},
            "process_handle_open_failed",
        ),
        (
            {"ok": False, "reason": "managed_process_not_found"},
            {"status": "ok", "pids": [9999]},
            "managed_port_still_occupied",
        ),
        (
            {"ok": False, "reason": "managed_process_not_found"},
            {"status": "unavailable", "reason": "netstat_denied"},
            "netstat_denied",
        ),
    ],
)
def test_admin_retire_refuses_unsafe_or_uninspectable_orphan_cleanup(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    termination: dict[str, Any],
    listeners: dict[str, Any],
    expected_reason: str,
) -> None:
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    proxy_server.state = copy.deepcopy(original)
    listener_calls: list[int] = []
    monkeypatch.setattr(
        proxy_server,
        "_terminate_verified_backend",
        lambda slot: copy.deepcopy(termination),
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: listener_calls.append(port) or copy.deepcopy(listeners),
    )
    monkeypatch.setattr(
        proxy_server,
        "_process_snapshot",
        lambda pid: {"status": "not_found"},
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(
            AssertionError("unsafe orphan cleanup must not persist")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_retire({"slot": "blue"}))

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "error": "retire_process_identity_unverified",
        "slot": "blue",
        "reason": expected_reason,
        "fail_closed": True,
    }
    assert proxy_server.state == original
    assert listener_calls == (
        [9702] if termination["reason"] == "managed_process_not_found" else []
    )


def test_admin_deploy_records_os_snapshot_and_random_start_nonce(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    started: list[list[str]] = []
    saved: list[dict[str, Any]] = []

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            raise AssertionError("verified spawn must not be terminated")

        def wait(self, timeout):
            raise AssertionError("verified spawn must not be waited")

        def kill(self):
            raise AssertionError("verified spawn must not be killed")

    def fake_popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess()

    def fake_wait(process, *, port, start_nonce, timeout_sec=6.0):
        return _snapshot(
            proxy_server,
            pid=process.pid,
            port=port,
            nonce=start_nonce,
        )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(proxy_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(proxy_server, "_wait_for_managed_snapshot", fake_wait)
    monkeypatch.setattr(proxy_server.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    result = asyncio.run(proxy_server.admin_deploy({"slot": "blue"}))

    identity = proxy_server.state["blue"]["process_identity"]
    assert result["pid"] == 4321
    assert identity["schema_version"] == proxy_server.PROCESS_IDENTITY_VERSION
    assert identity["port"] == 9702
    assert len(identity["start_nonce"]) == 32
    assert started == [identity["command_argv"]]
    assert saved[-1] == proxy_server.state


def test_single_writer_deploy_without_cutover_flag_fails_before_spawn(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.SINGLE_WRITER_GRAPH = True
    proxy_server.state = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    monkeypatch.setattr(
        proxy_server.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("single-writer preflight must run before spawn")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_deploy({"slot": "blue"}))

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "error": "single_writer_graph_requires_cutover",
        "slot": "blue",
        "active": "green",
        "active_pid": 111,
        "reason": "explicit_active_stop_evidence_required",
        "fail_closed": True,
    }


@pytest.mark.parametrize(
    ("verification", "listeners", "expected_reason"),
    [
        (
            {"ok": True, "pid": 111},
            {"status": "ok", "pids": []},
            "active_backend_still_running",
        ),
        (
            {"ok": False, "reason": "live_process_identity_mismatch"},
            {"status": "ok", "pids": []},
            "live_process_identity_mismatch",
        ),
        (
            {"ok": False, "reason": "process_handle_open_failed"},
            {"status": "ok", "pids": []},
            "process_handle_open_failed",
        ),
        (
            {"ok": False, "reason": "managed_process_not_found"},
            {"status": "ok", "pids": [777]},
            "active_port_still_occupied",
        ),
        (
            {"ok": False, "reason": "managed_process_not_found"},
            {"status": "unavailable", "reason": "netstat_denied"},
            "netstat_denied",
        ),
    ],
)
def test_single_writer_cutover_flag_never_bypasses_os_and_listener_evidence(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    verification: dict[str, Any],
    listeners: dict[str, Any],
    expected_reason: str,
) -> None:
    proxy_server.SINGLE_WRITER_GRAPH = True
    proxy_server.state = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: copy.deepcopy(verification),
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: copy.deepcopy(listeners),
    )
    monkeypatch.setattr(
        proxy_server.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unverified cutover must not spawn")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            proxy_server.admin_deploy(
                {"slot": "blue", "single_writer_active_stopped": True}
            )
        )

    assert raised.value.status_code == 409
    assert raised.value.detail["error"] == "single_writer_graph_requires_cutover"
    assert raised.value.detail["reason"] == expected_reason
    assert raised.value.detail["fail_closed"] is True
    if expected_reason == "active_port_still_occupied":
        assert raised.value.detail["listener_pids"] == [777]


def test_single_writer_deploy_spawns_only_after_active_exit_is_observed(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.SINGLE_WRITER_GRAPH = True
    proxy_server.state = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    started: list[list[str]] = []

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            raise AssertionError("verified spawn must not be terminated")

        def wait(self, timeout):
            raise AssertionError("verified spawn must not be waited")

        def kill(self):
            raise AssertionError("verified spawn must not be killed")

    def fake_popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess()

    def fake_wait(process, *, port, start_nonce, timeout_sec=6.0):
        return _snapshot(
            proxy_server,
            pid=process.pid,
            port=port,
            nonce=start_nonce,
        )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: {"ok": False, "reason": "managed_process_not_found"},
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": []},
    )
    monkeypatch.setattr(proxy_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(proxy_server, "_wait_for_managed_snapshot", fake_wait)
    monkeypatch.setattr(proxy_server.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(proxy_server, "save_state", lambda value: None)

    result = asyncio.run(
        proxy_server.admin_deploy(
            {"slot": "blue", "single_writer_active_stopped": True}
        )
    )

    assert result["pid"] == 4321
    assert len(started) == 1
    assert proxy_server.state["blue"]["pid"] == 4321


def test_admin_deploy_stops_spawn_when_identity_state_cannot_persist(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_blue = {"port": 9702, "pid": None, "status": "idle"}
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": copy.deepcopy(original_blue),
    }
    lifecycle: list[str] = []

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            lifecycle.append("terminate")

        def wait(self, timeout):
            lifecycle.append("wait")
            return 0

        def kill(self):
            lifecycle.append("kill")

    def fake_wait(process, *, port, start_nonce, timeout_sec=6.0):
        return _snapshot(
            proxy_server,
            pid=process.pid,
            port=port,
            nonce=start_nonce,
        )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(
        proxy_server.subprocess,
        "Popen",
        lambda command, **kwargs: FakeProcess(),
    )
    monkeypatch.setattr(proxy_server, "_wait_for_managed_snapshot", fake_wait)
    monkeypatch.setattr(proxy_server.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_deploy({"slot": "blue"}))

    assert raised.value.status_code == 503
    assert raised.value.detail["error"] == "managed_backend_state_persist_failed"
    assert lifecycle == ["terminate", "wait"]
    assert proxy_server.state["blue"] == original_blue


def test_concurrent_admin_deploy_spawns_one_managed_process_and_fails_closed(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    started: list[list[str]] = []
    saved: list[dict[str, Any]] = []

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            raise AssertionError("verified spawn must not be terminated")

        def wait(self, timeout):
            raise AssertionError("verified spawn must not be waited")

        def kill(self):
            raise AssertionError("verified spawn must not be killed")

    def fake_popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess()

    def fake_wait(process, *, port, start_nonce, timeout_sec=6.0):
        return _snapshot(
            proxy_server,
            pid=process.pid,
            port=port,
            nonce=start_nonce,
        )

    monkeypatch.setattr(proxy_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(proxy_server, "_wait_for_managed_snapshot", fake_wait)
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: {"ok": True},
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    async def exercise() -> list[Any]:
        first_in_snapshot = asyncio.Event()
        release_snapshot = asyncio.Event()

        async def controlled_to_thread(function, *args, **kwargs):
            first_in_snapshot.set()
            await release_snapshot.wait()
            return function(*args, **kwargs)

        monkeypatch.setattr(
            proxy_server.asyncio,
            "to_thread",
            controlled_to_thread,
        )
        first = asyncio.create_task(proxy_server.admin_deploy({"slot": "blue"}))
        await asyncio.wait_for(first_in_snapshot.wait(), timeout=1)
        second = asyncio.create_task(proxy_server.admin_deploy({"slot": "blue"}))
        await asyncio.sleep(0)
        assert len(started) == 1
        release_snapshot.set()
        return await asyncio.gather(first, second, return_exceptions=True)

    first_result, second_result = asyncio.run(exercise())

    assert first_result["pid"] == 4321
    assert isinstance(second_result, HTTPException)
    assert second_result.status_code == 409
    assert second_result.detail == {
        "error": "slot_already_has_verified_backend",
        "slot": "blue",
        "fail_closed": True,
    }
    assert len(started) == 1
    assert len(saved) == 1


def test_admin_process_lock_releases_after_deploy_exception(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    started: list[list[str]] = []
    save_attempts = 0

    class FakeProcess:
        returncode = None

        def __init__(self, pid: int):
            self.pid = pid

        def poll(self):
            return None

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

        def kill(self):
            self.returncode = -9

    def fake_popen(command, **kwargs):
        started.append(list(command))
        return FakeProcess(4320 + len(started))

    def fake_wait(process, *, port, start_nonce, timeout_sec=6.0):
        return _snapshot(
            proxy_server,
            pid=process.pid,
            port=port,
            nonce=start_nonce,
        )

    async def immediate_to_thread(function, *args, **kwargs):
        return function(*args, **kwargs)

    def fail_first_save(value):
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise OSError("disk full")

    monkeypatch.setattr(proxy_server.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(proxy_server, "_wait_for_managed_snapshot", fake_wait)
    monkeypatch.setattr(proxy_server.asyncio, "to_thread", immediate_to_thread)
    monkeypatch.setattr(proxy_server, "save_state", fail_first_save)

    async def exercise() -> tuple[HTTPException, dict[str, Any]]:
        try:
            await proxy_server.admin_deploy({"slot": "blue"})
        except HTTPException as exc:
            first_error = exc
        else:
            raise AssertionError("first deploy unexpectedly succeeded")
        second_result = await asyncio.wait_for(
            proxy_server.admin_deploy({"slot": "blue"}),
            timeout=1,
        )
        return first_error, second_result

    first_error, second_result = asyncio.run(exercise())

    assert first_error.status_code == 503
    assert first_error.detail["error"] == "managed_backend_state_persist_failed"
    assert second_result["pid"] == 4322
    assert proxy_server.state["blue"]["pid"] == 4322
    assert len(started) == 2


def test_admin_deploy_cancellation_reclaims_spawn_and_preserves_state(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    proxy_server.state = copy.deepcopy(original)
    lifecycle: list[str] = []

    class FakeProcess:
        pid = 4321
        returncode = None

        def poll(self):
            return None

        def terminate(self):
            lifecycle.append("terminate")
            self.returncode = 0

        def wait(self, timeout):
            lifecycle.append("wait")
            return self.returncode

        def kill(self):
            lifecycle.append("kill")

    monkeypatch.setattr(
        proxy_server.subprocess,
        "Popen",
        lambda command, **kwargs: FakeProcess(),
    )

    async def exercise() -> None:
        snapshot_started = asyncio.Event()
        never_release = asyncio.Event()

        async def blocked_to_thread(function, *args, **kwargs):
            snapshot_started.set()
            await never_release.wait()

        monkeypatch.setattr(
            proxy_server.asyncio,
            "to_thread",
            blocked_to_thread,
        )
        deployment = asyncio.create_task(
            proxy_server.admin_deploy({"slot": "blue"})
        )
        await asyncio.wait_for(snapshot_started.wait(), timeout=1)
        deployment.cancel()
        with pytest.raises(asyncio.CancelledError):
            await deployment

    asyncio.run(exercise())

    assert lifecycle == ["terminate", "wait"]
    assert proxy_server.state == original
    assert proxy_server._admin_process_lock.locked() is False


def test_single_writer_mode_disables_automatic_failover(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.SINGLE_WRITER_GRAPH = True
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "offline"},
        "blue": {"port": 9702, "pid": 222, "status": "healthy"},
    }
    proxy_server.state = copy.deepcopy(original)

    async def unexpected_probe(*args, **kwargs):
        raise AssertionError("single-writer failover must not probe standby")

    monkeypatch.setattr(proxy_server.client, "get", unexpected_probe)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(
            AssertionError("single-writer failover must not persist")
        ),
    )

    result = asyncio.run(proxy_server._try_auto_failover())

    assert result is False
    assert proxy_server.state == original


def test_admin_switch_rejects_unmanaged_healthy_listener_before_health_probe(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": {"port": 9702, "pid": None, "status": "healthy"},
    }
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: {
            "ok": False,
            "reason": "managed_process_identity_missing",
        },
    )

    async def unexpected_health_probe(*args, **kwargs):
        raise AssertionError("unmanaged target must not receive a health probe")

    monkeypatch.setattr(proxy_server.client, "get", unexpected_health_probe)

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_switch({"to": "blue"}))

    assert raised.value.status_code == 409
    assert raised.value.detail == {
        "error": "switch_process_identity_unverified",
        "slot": "blue",
        "reason": "managed_process_identity_missing",
        "fail_closed": True,
    }
    assert proxy_server.state["active"] == "green"


def test_admin_switch_requires_identity_then_health_before_persisting(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    checks: list[str] = []
    saved: list[dict[str, Any]] = []

    def verify(slot):
        checks.append("identity")
        return {"ok": True}

    class HealthyResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"total_nodes": 2321, "total_edges": 1013}

    async def healthy(*args, **kwargs):
        checks.append("health")
        return HealthyResponse()

    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        verify,
    )
    monkeypatch.setattr(proxy_server.client, "get", healthy)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    result = asyncio.run(proxy_server.admin_switch({"to": "blue"}))

    assert result == {
        "switched_from": "green",
        "active": "blue",
        "port": 9702,
    }
    assert checks == ["identity", "health"]
    assert saved[-1]["active"] == "blue"
    assert saved[-1]["blue"]["status"] == "healthy"
    assert saved[-1]["blue"]["nodes"] == 2321
    assert saved[-1]["blue"]["edges"] == 1013


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ([], "stats_payload_must_be_object"),
        (
            {"total_nodes": "2321", "total_edges": 1013},
            "graph_counts_must_be_non_negative_integers",
        ),
        (
            {"total_nodes": 2321, "total_edges": -1},
            "graph_counts_must_be_non_negative_integers",
        ),
    ],
)
def test_admin_switch_rejects_malformed_success_payload_before_state_mutation(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    reason: str,
) -> None:
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    proxy_server.state = copy.deepcopy(original)

    class MalformedResponse:
        status_code = 200

        @staticmethod
        def json():
            return payload

    async def malformed(*args, **kwargs):
        return MalformedResponse()

    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: {"ok": True},
    )
    monkeypatch.setattr(proxy_server.client, "get", malformed)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(
            AssertionError("invalid health payload must not be persisted")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_switch({"to": "blue"}))

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "error": "switch_health_payload_invalid",
        "slot": "blue",
        "reason": reason,
        "fail_closed": True,
    }
    assert proxy_server.state == original


def test_admin_switch_rolls_back_memory_when_state_cannot_persist(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }

    class HealthyResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"total_nodes": 2321, "total_edges": 1013}

    async def healthy(*args, **kwargs):
        return HealthyResponse()

    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: {"ok": True},
    )
    monkeypatch.setattr(proxy_server.client, "get", healthy)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_switch({"to": "blue"}))

    assert raised.value.status_code == 503
    assert raised.value.detail["error"] == "switch_state_persist_failed"
    assert raised.value.detail["fail_closed"] is True
    assert proxy_server.state["active"] == "green"
    assert proxy_server.state["blue"] == _slot_state(proxy_server)
    assert proxy_server._admin_process_lock.locked() is False


def test_admin_retire_preserves_owned_state_when_clear_cannot_persist(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "active": "green",
        "green": {"port": 9701, "pid": 111, "status": "healthy"},
        "blue": _slot_state(proxy_server),
    }
    proxy_server.state = copy.deepcopy(original)
    monkeypatch.setattr(
        proxy_server,
        "_terminate_verified_backend",
        lambda slot: {
            "ok": True,
            "pid": slot["pid"],
            "identity_verified": True,
            "exit_confirmed": True,
        },
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(proxy_server.admin_retire({"slot": "blue"}))

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "error": "retire_state_persist_failed",
        "slot": "blue",
        "pid": 4321,
        "process_terminated": True,
        "fail_closed": True,
    }
    assert proxy_server.state == original
    assert proxy_server._admin_process_lock.locked() is False


def test_recover_active_requires_exact_confirmation_before_observation(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_absent",
        lambda slot: (_ for _ in ()).throw(
            AssertionError("confirmation must precede process observation")
        ),
    )
    monkeypatch.setattr(
        proxy_server.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unconfirmed recovery must not spawn")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            proxy_server.admin_recover_active(
                {"confirm": "recover-active"}
            )
        )

    assert raised.value.status_code == 400
    assert raised.value.detail == {
        "error": "active_backend_recovery_confirmation_required",
        "expected_confirm": "recover-stopped-active",
        "fail_closed": True,
    }


def test_recover_active_restarts_same_slot_after_exact_exit_evidence(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy_server.state = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    saved: list[dict[str, Any]] = []
    lifecycle, started = _install_fake_managed_spawn(
        proxy_server,
        monkeypatch,
    )
    monkeypatch.setattr(
        proxy_server,
        "_process_snapshot",
        lambda pid: {"status": "not_found"},
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": []},
    )
    monkeypatch.setattr(proxy_server.secrets, "token_hex", lambda size: "c" * 32)
    monkeypatch.setattr(proxy_server.time, "time", lambda: 1234.5)
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: saved.append(copy.deepcopy(value)),
    )

    result = asyncio.run(
        proxy_server.admin_recover_active(
            {"confirm": "recover-stopped-active"}
        )
    )

    assert result == {
        "slot": "green",
        "port": 9701,
        "pid": 9001,
        "status": "starting",
        "hint": "调用 /api/admin/health 等待ready",
        "recovered_active": "green",
        "previous_pid": 111,
        "identity_verified": True,
        "bootstrap_empty_state": False,
    }
    assert proxy_server.state["active"] == "green"
    assert proxy_server.state["green"]["pid"] == 9001
    assert proxy_server.state["green"]["status"] == "starting"
    assert proxy_server.state["green"]["started_at"] == 1234.5
    assert proxy_server.state["green"]["last_exit_observation"] == {
        "pid": 111,
        "observed_at": 1234.5,
        "process_status": "not_found",
        "listener_state": "empty",
        "reason": "managed_process_not_found",
        "creation_id": "creation-1",
        "start_nonce": "b" * 32,
    }
    identity = proxy_server.state["green"]["process_identity"]
    assert identity["pid"] == 9001
    assert identity["port"] == 9701
    assert identity["start_nonce"] == "c" * 32
    assert identity["creation_id"] == "recovery-creation"
    assert started == [identity["command_argv"]]
    assert lifecycle == []
    assert saved == [proxy_server.state]


@pytest.mark.parametrize(
    ("verification", "listeners", "expected_reason", "expected_listeners"),
    [
        (
            {"ok": True, "pid": 111},
            {"status": "ok", "pids": []},
            "active_backend_still_running",
            None,
        ),
        (
            {"ok": False, "reason": "live_process_identity_mismatch"},
            {"status": "ok", "pids": []},
            "live_process_identity_mismatch",
            None,
        ),
        (
            {"ok": False, "reason": "process_handle_open_failed"},
            {"status": "ok", "pids": []},
            "process_handle_open_failed",
            None,
        ),
        (
            {"ok": False, "reason": "managed_process_not_found"},
            {"status": "ok", "pids": [777]},
            "active_port_still_occupied",
            [777],
        ),
    ],
)
def test_recover_active_fails_closed_before_spawn_when_exit_is_not_exact(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    verification: dict[str, Any],
    listeners: dict[str, Any],
    expected_reason: str,
    expected_listeners: list[int] | None,
) -> None:
    original = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    proxy_server.state = copy.deepcopy(original)
    listener_calls: list[int] = []
    monkeypatch.setattr(
        proxy_server,
        "_verify_managed_backend_process",
        lambda slot: copy.deepcopy(verification),
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: listener_calls.append(port) or copy.deepcopy(listeners),
    )
    monkeypatch.setattr(
        proxy_server.subprocess,
        "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe active recovery must not spawn")
        ),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            proxy_server.admin_recover_active(
                {"confirm": "recover-stopped-active"}
            )
        )

    assert raised.value.status_code == 409
    expected_detail = {
        "error": "active_backend_recovery_precondition_failed",
        "active": "green",
        "active_pid": 111,
        "reason": expected_reason,
        "fail_closed": True,
    }
    if expected_listeners is not None:
        expected_detail["listener_pids"] = expected_listeners
    assert raised.value.detail == expected_detail
    assert proxy_server.state == original
    assert listener_calls == (
        [9701] if verification.get("reason") == "managed_process_not_found" else []
    )


def test_recover_active_reclaims_spawn_and_rolls_back_when_persist_fails(
    proxy_server: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = {
        "active": "green",
        "green": _slot_state(
            proxy_server,
            pid=111,
            port=9701,
            nonce="b" * 32,
        ),
        "blue": {"port": 9702, "pid": None, "status": "idle"},
    }
    proxy_server.state = copy.deepcopy(original)
    lifecycle, started = _install_fake_managed_spawn(
        proxy_server,
        monkeypatch,
    )
    monkeypatch.setattr(
        proxy_server,
        "_process_snapshot",
        lambda pid: {"status": "not_found"},
    )
    monkeypatch.setattr(
        proxy_server,
        "_listener_pids",
        lambda port: {"status": "ok", "pids": []},
    )
    monkeypatch.setattr(
        proxy_server,
        "save_state",
        lambda value: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            proxy_server.admin_recover_active(
                {"confirm": "recover-stopped-active"}
            )
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == {
        "error": "managed_backend_state_persist_failed",
        "slot": "green",
    }
    assert len(started) == 1
    assert lifecycle == ["terminate", "wait"]
    assert proxy_server.state == original
    assert proxy_server._admin_process_lock.locked() is False
