"""3CAN single-writer slot proxy.

- 对外统一端口 9700 (所有agent接入点)
- 后端 green=9701, blue=9702 (轮换端口；共享图一次仅一个 writer)
- /api/admin/* 管控部署/健康检查/切换/显式恢复
- 非admin请求转发到active backend

启动: python proxy/server.py
前置: green backend 在 9701 已运行 (backend/app.py --port 9701)
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response

PROXY_DIR = Path(__file__).resolve().parent
ENGINE_ROOT = PROXY_DIR.parent
BACKEND_DIR = ENGINE_ROOT / "backend"
GRAPH_DIR_ENV = "THREECAN_GRAPH_DIR"
PROXY_STATE_ENV = "THREECAN_PROXY_STATE_FILE"
_CONTENT_ADDRESSED_RUNTIME_RE = re.compile(r"3can-runtime-[0-9a-f]{64}")

DEFAULT_STATE = {
    "active": "green",
    "green": {"port": 9701, "status": "unknown", "pid": None, "started_at": None},
    "blue":  {"port": 9702, "status": "idle", "pid": None, "started_at": None},
}
PROCESS_IDENTITY_VERSION = "3can.proxy-managed-process/v1"
PROCESS_EXIT_TIMEOUT_MS = 5000
SINGLE_WRITER_GRAPH = True
WINDOWS_ERROR_INVALID_PARAMETER = 87
WINDOWS_STILL_ACTIVE = 259
_START_NONCE_RE = re.compile(r"[0-9a-f]{32}")
_MANAGED_BACKEND_BOOTSTRAP = (
    "import os,runpy,sys;"
    "script=sys.argv[1];port=sys.argv[2];nonce=sys.argv[3];"
    "os.environ['THREECAN_PROCESS_START_NONCE']=nonce;"
    "sys.argv=[script,'--port',port,'--host','127.0.0.1'];"
    "runpy.run_path(script,run_name='__main__')"
)


def _is_content_addressed_runtime_release() -> bool:
    return bool(_CONTENT_ADDRESSED_RUNTIME_RE.fullmatch(ENGINE_ROOT.name))


def _path_has_reparse_component(path: Path) -> bool:
    current = Path(path).absolute()
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"runtime_external_path_probe_failed:{type(exc).__name__}"
            ) from exc
        else:
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if stat.S_ISLNK(metadata.st_mode) or bool(
                attributes
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
            ):
                return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def _path_is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _required_external_directory(name: str) -> Path:
    raw = os.environ.get(name, "").strip()
    if not raw:
        raise RuntimeError(f"{name.lower()}_required")
    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        raise RuntimeError(f"{name.lower()}_not_absolute")
    if _path_has_reparse_component(requested):
        raise RuntimeError(f"{name.lower()}_reparse")
    resolved = requested.resolve(strict=False)
    if not resolved.is_dir():
        raise RuntimeError(f"{name.lower()}_missing")
    if _path_is_within(resolved, ENGINE_ROOT) or _path_is_within(
        ENGINE_ROOT, resolved
    ):
        raise RuntimeError(f"{name.lower()}_inside_runtime_release")
    return resolved


def _resolve_state_file() -> Path:
    raw = os.environ.get(PROXY_STATE_ENV, "").strip()
    immutable_release = _is_content_addressed_runtime_release()
    if not raw:
        if immutable_release:
            raise RuntimeError("threecan_proxy_state_file_required")
        return PROXY_DIR / "proxy_state.json"

    requested = Path(raw).expanduser()
    if not requested.is_absolute():
        raise RuntimeError("threecan_proxy_state_file_not_absolute")
    if _path_has_reparse_component(requested):
        raise RuntimeError("threecan_proxy_state_file_reparse")
    resolved = requested.resolve(strict=False)
    parent = resolved.parent
    if immutable_release and not parent.is_dir():
        raise RuntimeError("threecan_proxy_state_parent_missing")
    if _path_is_within(resolved, ENGINE_ROOT):
        raise RuntimeError("threecan_proxy_state_file_inside_runtime_release")
    if immutable_release:
        graph_root = _required_external_directory(GRAPH_DIR_ENV)
        if _path_is_within(resolved, graph_root):
            raise RuntimeError("threecan_proxy_state_file_inside_graph")
    return resolved


STATE_FILE = _resolve_state_file()


def _validate_state_file_path(*, allow_missing_parent: bool = False) -> None:
    if _path_has_reparse_component(STATE_FILE):
        raise OSError("proxy_state_path_reparse")
    if not allow_missing_parent and not STATE_FILE.parent.is_dir():
        raise OSError("proxy_state_parent_missing")
    if STATE_FILE.exists():
        metadata = STATE_FILE.lstat()
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if not stat.S_ISREG(metadata.st_mode) or bool(
            attributes
            & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        ) or int(getattr(metadata, "st_nlink", 1)) != 1:
            raise OSError("proxy_state_file_not_regular")


def _fsync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    flush_file_buffers = kernel32.FlushFileBuffers
    flush_file_buffers.argtypes = [ctypes.c_void_p]
    flush_file_buffers.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    handle = create_file(
        str(Path(path)),
        0x40000000,  # GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,  # OPEN_EXISTING
        0x02000000 | 0x80000000,  # BACKUP_SEMANTICS | WRITE_THROUGH
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(
            ctypes.get_last_error(),
            "proxy_state_directory_open_failed",
            str(path),
        )
    try:
        if not flush_file_buffers(handle):
            raise OSError(
                ctypes.get_last_error(),
                "proxy_state_directory_flush_failed",
                str(path),
            )
    finally:
        close_handle(handle)


def _atomic_replace_write_through(source: Path, destination: Path) -> None:
    if os.name != "nt":
        os.replace(source, destination)
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
    ]
    move_file_ex.restype = ctypes.c_int
    if not move_file_ex(
        str(source),
        str(destination),
        0x00000001 | 0x00000008,  # REPLACE_EXISTING | WRITE_THROUGH
    ):
        raise OSError(
            ctypes.get_last_error(),
            "proxy_state_atomic_replace_failed",
            str(destination),
        )


def _write_durable_file(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def load_state() -> dict:
    _validate_state_file_path(
        allow_missing_parent=not _is_content_addressed_runtime_release()
    )
    if STATE_FILE.exists():
        try:
            loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            _validate_persisted_state(loaded)
            return loaded
        except Exception as exc:
            if _is_content_addressed_runtime_release():
                raise RuntimeError("immutable_proxy_state_invalid") from exc
    return json.loads(json.dumps(DEFAULT_STATE))


def save_state(state: dict) -> None:
    try:
        _validate_persisted_state(state)
        payload = (
            json.dumps(state, ensure_ascii=False, indent=2) + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OSError("proxy_state_schema_invalid") from exc
    if _is_content_addressed_runtime_release():
        _validate_state_file_path()
    else:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _validate_state_file_path()
    previous_payload = STATE_FILE.read_bytes() if STATE_FILE.exists() else None
    temporary = STATE_FILE.with_name(
        f".{STATE_FILE.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    )
    rollback = STATE_FILE.with_name(
        f".{STATE_FILE.name}.{os.getpid()}.{secrets.token_hex(4)}.rollback"
    )
    try:
        _write_durable_file(temporary, payload)
        _validate_state_file_path()
        _atomic_replace_write_through(temporary, STATE_FILE)
        _validate_state_file_path()
        _fsync_directory(STATE_FILE.parent)
    except OSError:
        try:
            current_payload = (
                STATE_FILE.read_bytes() if STATE_FILE.is_file() else None
            )
        except OSError:
            current_payload = b"__unreadable__"
        if current_payload != previous_payload:
            try:
                if previous_payload is None:
                    STATE_FILE.unlink(missing_ok=True)
                else:
                    rollback.unlink(missing_ok=True)
                    _write_durable_file(rollback, previous_payload)
                    _atomic_replace_write_through(rollback, STATE_FILE)
                try:
                    _fsync_directory(STATE_FILE.parent)
                except OSError:
                    pass
            except OSError as rollback_error:
                raise OSError(
                    "proxy_state_commit_and_rollback_failed"
                ) from rollback_error
        raise
    finally:
        temporary.unlink(missing_ok=True)
        rollback.unlink(missing_ok=True)


app = FastAPI(title="3CAN Proxy", version="v9-single-writer")
_admin_process_lock = asyncio.Lock()

# httpx客户端持久化连接
client = httpx.AsyncClient(timeout=60.0, limits=httpx.Limits(max_connections=100))


def active_port() -> int:
    return state[state["active"]]["port"]


def other_slot() -> str:
    return "blue" if state["active"] == "green" else "green"


def _path_identity(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).resolve(strict=False))).replace("\\", "/")


def _managed_backend_command(port: int, start_nonce: str) -> list[str]:
    if not _START_NONCE_RE.fullmatch(start_nonce):
        raise ValueError("invalid managed-backend start nonce")
    return [
        str(Path(sys.executable).resolve()),
        "-B",
        "-c",
        _MANAGED_BACKEND_BOOTSTRAP,
        str((BACKEND_DIR / "app.py").resolve()),
        str(int(port)),
        start_nonce,
    ]


def _validate_persisted_state(candidate: Any) -> None:
    if not isinstance(candidate, dict) or candidate.get("active") not in {
        "green",
        "blue",
    }:
        raise ValueError("proxy_state_root_invalid")
    observed_pids: set[int] = set()
    observed_ports: set[int] = set()
    for slot in ("green", "blue"):
        slot_state = candidate.get(slot)
        if not isinstance(slot_state, dict):
            raise ValueError(f"proxy_state_slot_invalid:{slot}")
        port = slot_state.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not (
            1 <= port <= 65535
        ):
            raise ValueError(f"proxy_state_port_invalid:{slot}")
        if port in observed_ports:
            raise ValueError("proxy_state_ports_not_unique")
        observed_ports.add(port)
        pid = slot_state.get("pid")
        identity = slot_state.get("process_identity")
        if pid is None:
            if identity is not None:
                raise ValueError(f"proxy_state_orphan_identity:{slot}")
            continue
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
            raise ValueError(f"proxy_state_pid_invalid:{slot}")
        if pid in observed_pids:
            raise ValueError("proxy_state_pids_not_unique")
        observed_pids.add(pid)
        if not isinstance(identity, dict):
            raise ValueError(f"proxy_state_identity_missing:{slot}")
        nonce = str(identity.get("start_nonce") or "")
        expected_command = (
            _managed_backend_command(port, nonce)
            if _START_NONCE_RE.fullmatch(nonce)
            else []
        )
        if (
            identity.get("schema_version") != PROCESS_IDENTITY_VERSION
            or identity.get("pid") != pid
            or identity.get("port") != port
            or _path_identity(identity.get("engine_root") or "")
            != _path_identity(ENGINE_ROOT)
            or _path_identity(identity.get("backend_entrypoint") or "")
            != _path_identity(BACKEND_DIR / "app.py")
            or _path_identity(identity.get("python_executable") or "")
            != _path_identity(sys.executable)
            or not str(identity.get("creation_id") or "")
            or identity.get("command_argv") != expected_command
        ):
            raise ValueError(f"proxy_state_identity_invalid:{slot}")


state = load_state()


def _windows_command_line_to_argv(command_line: str) -> list[str]:
    if not command_line:
        return []
    argc = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    argv_pointer = command_line_to_argv(command_line, ctypes.byref(argc))
    if not argv_pointer:
        return []
    try:
        return [argv_pointer[index] for index in range(argc.value)]
    finally:
        local_free = ctypes.windll.kernel32.LocalFree
        local_free.argtypes = [ctypes.c_void_p]
        local_free.restype = ctypes.c_void_p
        local_free(ctypes.cast(argv_pointer, ctypes.c_void_p))


def _windows_open_process_failure(last_error: int) -> dict[str, Any]:
    """Classify an OpenProcess failure without treating access denial as exit."""

    if int(last_error) == WINDOWS_ERROR_INVALID_PARAMETER:
        # Windows reports ERROR_INVALID_PARAMETER when the PID no longer exists.
        return {"status": "not_found"}
    return {
        "status": "unavailable",
        "reason": "process_handle_open_failed",
        "os_error": int(last_error),
    }


def _windows_exit_code_state(
    *,
    query_succeeded: bool,
    exit_code: int,
) -> dict[str, Any]:
    """Classify a process that can still be opened after it has exited."""

    if not query_succeeded:
        return {
            "status": "unavailable",
            "reason": "process_exit_code_query_failed",
        }
    if int(exit_code) != WINDOWS_STILL_ACTIVE:
        return {"status": "not_found", "exit_code": int(exit_code)}
    return {"status": "active"}


def _process_snapshot(pid: int) -> dict[str, Any]:
    """Return an OS-backed process identity or a typed unavailable state."""

    if int(pid) <= 0:
        return {"status": "not_found"}
    if os.name == "nt":
        process_query_limited_information = 0x1000
        process_command_line_information = 60
        kernel32 = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll

        class FileTime(ctypes.Structure):
            _fields_ = [
                ("low", ctypes.c_uint32),
                ("high", ctypes.c_uint32),
            ]

        class UnicodeString(ctypes.Structure):
            _fields_ = [
                ("length", ctypes.c_ushort),
                ("maximum_length", ctypes.c_ushort),
                ("buffer", ctypes.c_void_p),
            ]

        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_wchar_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        query_image.restype = ctypes.c_int
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        get_process_times.restype = ctypes.c_int
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        get_exit_code.restype = ctypes.c_int
        query_process = ntdll.NtQueryInformationProcess
        query_process.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        query_process.restype = ctypes.c_long
        get_last_error = kernel32.GetLastError
        get_last_error.argtypes = []
        get_last_error.restype = ctypes.c_uint32
        set_last_error = kernel32.SetLastError
        set_last_error.argtypes = [ctypes.c_uint32]
        set_last_error.restype = None

        set_last_error(0)
        handle = open_process(
            process_query_limited_information,
            False,
            int(pid),
        )
        if not handle:
            return _windows_open_process_failure(int(get_last_error()))
        try:
            exit_code = ctypes.c_uint32()
            exit_state = _windows_exit_code_state(
                query_succeeded=bool(
                    get_exit_code(handle, ctypes.byref(exit_code))
                ),
                exit_code=int(exit_code.value),
            )
            if exit_state["status"] != "active":
                return exit_state

            image_buffer = ctypes.create_unicode_buffer(32768)
            image_length = ctypes.c_uint32(len(image_buffer))
            if not query_image(
                handle,
                0,
                image_buffer,
                ctypes.byref(image_length),
            ):
                return {
                    "status": "unavailable",
                    "reason": "process_executable_query_failed",
                }

            creation = FileTime()
            exit_time = FileTime()
            kernel_time = FileTime()
            user_time = FileTime()
            if not get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel_time),
                ctypes.byref(user_time),
            ):
                return {
                    "status": "unavailable",
                    "reason": "process_creation_time_query_failed",
                }

            required = ctypes.c_uint32()
            query_process(
                handle,
                process_command_line_information,
                None,
                0,
                ctypes.byref(required),
            )
            if required.value < ctypes.sizeof(UnicodeString):
                return {
                    "status": "unavailable",
                    "reason": "process_command_line_size_unavailable",
                }
            command_buffer = ctypes.create_string_buffer(required.value)
            status = query_process(
                handle,
                process_command_line_information,
                command_buffer,
                required.value,
                ctypes.byref(required),
            )
            if status != 0:
                return {
                    "status": "unavailable",
                    "reason": "process_command_line_query_failed",
                }
            unicode_command = UnicodeString.from_buffer(command_buffer)
            if not unicode_command.buffer or not unicode_command.length:
                return {
                    "status": "unavailable",
                    "reason": "process_command_line_empty",
                }
            command_line = ctypes.wstring_at(
                unicode_command.buffer,
                unicode_command.length // ctypes.sizeof(ctypes.c_wchar),
            )
            creation_id = f"{creation.high:08x}{creation.low:08x}"
            executable_path = image_buffer.value
        finally:
            close_handle(handle)
        try:
            command_argv = _windows_command_line_to_argv(command_line)
        except Exception:
            command_argv = []
        return {
            "status": "found",
            "pid": int(pid),
            "executable_path": executable_path,
            "command_argv": command_argv,
            "creation_id": creation_id,
        }

    proc_dir = Path("/proc") / str(int(pid))
    if not proc_dir.exists():
        return {"status": "not_found"}
    try:
        command_argv = [
            item.decode(errors="surrogateescape")
            for item in (proc_dir / "cmdline").read_bytes().split(b"\0")
            if item
        ]
        executable_path = os.readlink(proc_dir / "exe")
        stat_text = (proc_dir / "stat").read_text(encoding="utf-8")
        stat_fields = stat_text[stat_text.rfind(")") + 2:].split()
        creation_id = stat_fields[19]
    except FileNotFoundError:
        return {"status": "not_found"}
    except (OSError, IndexError, UnicodeError) as exc:
        return {
            "status": "unavailable",
            "reason": f"process_inspection_failed:{type(exc).__name__}",
        }
    return {
        "status": "found",
        "pid": int(pid),
        "executable_path": executable_path,
        "command_argv": command_argv,
        "creation_id": creation_id,
    }


def _listener_pids(port: int) -> dict[str, Any]:
    """Return listener PIDs for one TCP port, or an explicit unavailable state."""

    if not (1 <= int(port) <= 65535):
        return {"status": "unavailable", "reason": "invalid_port"}
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=6,
            )
        except Exception as exc:
            return {
                "status": "unavailable",
                "reason": f"listener_inspection_failed:{type(exc).__name__}",
            }
        if completed.returncode != 0:
            return {
                "status": "unavailable",
                "reason": "listener_inspection_command_failed",
            }
        listeners: set[int] = set()
        suffix = f":{int(port)}"
        for raw_line in completed.stdout.splitlines():
            parts = raw_line.split()
            if (
                len(parts) >= 5
                and parts[0].casefold() == "tcp"
                and parts[1].endswith(suffix)
                and parts[3].upper() == "LISTENING"
            ):
                try:
                    listeners.add(int(parts[-1]))
                except ValueError:
                    continue
        return {"status": "ok", "pids": sorted(listeners)}

    proc_root = Path("/proc")
    if not proc_root.exists():
        return {
            "status": "unavailable",
            "reason": "listener_inspection_not_supported",
        }
    socket_inodes: set[str] = set()
    try:
        for table_name in ("tcp", "tcp6"):
            table = proc_root / "net" / table_name
            if not table.is_file():
                continue
            for line in table.read_text(encoding="ascii").splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10:
                    continue
                local_address, connection_state, inode = fields[1], fields[3], fields[9]
                try:
                    local_port = int(local_address.rsplit(":", 1)[1], 16)
                except (IndexError, ValueError):
                    continue
                if local_port == int(port) and connection_state == "0A":
                    socket_inodes.add(inode)
        listeners: set[int] = set()
        for candidate in proc_root.iterdir():
            if not candidate.name.isdigit():
                continue
            try:
                for descriptor in (candidate / "fd").iterdir():
                    target = os.readlink(descriptor)
                    match = re.fullmatch(r"socket:\[(\d+)\]", target)
                    if match and match.group(1) in socket_inodes:
                        listeners.add(int(candidate.name))
                        break
            except (FileNotFoundError, PermissionError):
                continue
        return {"status": "ok", "pids": sorted(listeners)}
    except (OSError, UnicodeError) as exc:
        return {
            "status": "unavailable",
            "reason": f"listener_inspection_failed:{type(exc).__name__}",
        }


def _snapshot_matches_managed_command(
    snapshot: dict[str, Any],
    *,
    port: int,
    start_nonce: str,
) -> bool:
    expected = _managed_backend_command(port, start_nonce)
    actual = snapshot.get("command_argv")
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    if _path_identity(actual[0]) != _path_identity(expected[0]):
        return False
    if actual[1:3] != expected[1:3]:
        return False
    if _path_identity(actual[3]) != _path_identity(expected[3]):
        return False
    return actual[4:] == expected[4:]


def _managed_process_identity(
    *,
    pid: int,
    port: int,
    start_nonce: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    if snapshot.get("status") != "found":
        raise ValueError("managed process snapshot is unavailable")
    if not _snapshot_matches_managed_command(
        snapshot,
        port=port,
        start_nonce=start_nonce,
    ):
        raise ValueError("managed process command does not match")
    executable = str(snapshot.get("executable_path") or "")
    creation_id = str(snapshot.get("creation_id") or "")
    if (
        int(snapshot.get("pid") or 0) != int(pid)
        or _path_identity(executable) != _path_identity(sys.executable)
        or not creation_id
    ):
        raise ValueError("managed process OS identity does not match")
    return {
        "schema_version": PROCESS_IDENTITY_VERSION,
        "pid": int(pid),
        "port": int(port),
        "engine_root": str(PROXY_DIR.parent.resolve()),
        "backend_entrypoint": str((BACKEND_DIR / "app.py").resolve()),
        "python_executable": str(Path(sys.executable).resolve()),
        "start_nonce": start_nonce,
        "creation_id": creation_id,
        "command_argv": list(snapshot["command_argv"]),
    }


def _verify_managed_backend_process(slot_state: dict[str, Any]) -> dict[str, Any]:
    """Verify every persisted and live identity field before termination."""

    identity = slot_state.get("process_identity")
    if not isinstance(identity, dict):
        return {"ok": False, "reason": "managed_process_identity_missing"}
    try:
        pid = int(slot_state.get("pid") or 0)
        port = int(slot_state.get("port") or 0)
        identity_pid = int(identity.get("pid") or 0)
        identity_port = int(identity.get("port") or 0)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "managed_process_identity_invalid"}
    start_nonce = str(identity.get("start_nonce") or "")
    if (
        identity.get("schema_version") != PROCESS_IDENTITY_VERSION
        or pid <= 0
        or pid != identity_pid
        or not (1 <= port <= 65535)
        or port != identity_port
        or not _START_NONCE_RE.fullmatch(start_nonce)
        or _path_identity(identity.get("engine_root") or "")
        != _path_identity(PROXY_DIR.parent)
        or _path_identity(identity.get("backend_entrypoint") or "")
        != _path_identity(BACKEND_DIR / "app.py")
        or _path_identity(identity.get("python_executable") or "")
        != _path_identity(sys.executable)
        or not _snapshot_matches_managed_command(
            {"command_argv": identity.get("command_argv")},
            port=port,
            start_nonce=start_nonce,
        )
    ):
        return {"ok": False, "reason": "managed_process_identity_mismatch"}

    snapshot = _process_snapshot(pid)
    if snapshot.get("status") != "found":
        return {
            "ok": False,
            "reason": (
                "managed_process_not_found"
                if snapshot.get("status") == "not_found"
                else str(snapshot.get("reason") or "process_inspection_unavailable")
            ),
        }
    if (
        int(snapshot.get("pid") or 0) != pid
        or str(snapshot.get("creation_id") or "") != str(identity.get("creation_id") or "")
        or _path_identity(snapshot.get("executable_path") or "")
        != _path_identity(sys.executable)
        or snapshot.get("command_argv") != identity.get("command_argv")
        or not _snapshot_matches_managed_command(
            snapshot,
            port=port,
            start_nonce=start_nonce,
        )
    ):
        return {"ok": False, "reason": "live_process_identity_mismatch"}

    listeners = _listener_pids(port)
    if listeners.get("status") != "ok":
        return {
            "ok": False,
            "reason": str(listeners.get("reason") or "listener_inspection_unavailable"),
        }
    listener_pids = {int(value) for value in listeners.get("pids") or []}
    if listener_pids and pid not in listener_pids:
        return {"ok": False, "reason": "managed_port_owned_by_different_process"}
    return {
        "ok": True,
        "pid": pid,
        "port": port,
        "snapshot": snapshot,
        "listener_state": "owned" if pid in listener_pids else "not_yet_listening",
    }


def _verify_managed_backend_absent(slot_state: dict[str, Any]) -> dict[str, Any]:
    """Confirm that one fully identified managed process and its port are gone.

    ``managed_process_not_found`` is trustworthy only after
    ``_verify_managed_backend_process`` has validated every persisted identity
    field. Listener inspection is a separate fail-closed gate so PID exit
    cannot be mistaken for port availability.
    """

    verification = _verify_managed_backend_process(slot_state)
    if verification.get("ok"):
        return {
            "ok": False,
            "reason": "managed_process_still_running",
            "pid": int(verification.get("pid") or slot_state.get("pid") or 0),
        }
    reason = str(
        verification.get("reason") or "process_inspection_unavailable"
    )
    fresh_empty_state = bool(
        reason == "managed_process_identity_missing"
        and slot_state.get("pid") is None
        and slot_state.get("process_identity") is None
    )
    if fresh_empty_state:
        port = int(slot_state.get("port") or 0)
        listeners = _listener_pids(port)
        if listeners.get("status") != "ok":
            return {
                "ok": False,
                "reason": str(
                    listeners.get("reason")
                    or "listener_inspection_unavailable"
                ),
            }
        listener_pids = [int(value) for value in listeners.get("pids") or []]
        if listener_pids:
            return {
                "ok": False,
                "reason": "managed_port_still_occupied",
                "listener_pids": listener_pids,
            }
        return {
            "ok": True,
            "pid": None,
            "port": port,
            "managed_identity_absent": True,
            "process_status": "never_started",
            "listener_state": "empty",
            "bootstrap_empty_state": True,
        }
    if reason != "managed_process_not_found":
        return {"ok": False, "reason": reason}

    port = int(slot_state.get("port") or 0)
    listeners = _listener_pids(port)
    if listeners.get("status") != "ok":
        return {
            "ok": False,
            "reason": str(
                listeners.get("reason") or "listener_inspection_unavailable"
            ),
        }
    listener_pids = [int(value) for value in listeners.get("pids") or []]
    if listener_pids:
        return {
            "ok": False,
            "reason": "managed_port_still_occupied",
            "listener_pids": listener_pids,
        }

    identity = slot_state["process_identity"]
    pid = int(slot_state["pid"])
    return {
        "ok": True,
        "pid": pid,
        "port": port,
        "identity_verified": True,
        "process_status": "not_found",
        "listener_state": "empty",
        "last_exit_observation": {
            "pid": pid,
            "observed_at": time.time(),
            "process_status": "not_found",
            "listener_state": "empty",
            "reason": "managed_process_not_found",
            "creation_id": str(identity["creation_id"]),
            "start_nonce": str(identity["start_nonce"]),
        },
    }


def _windows_open_termination_handle(pid: int) -> Any | None:
    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    process_synchronize = 0x00100000
    open_process = ctypes.windll.kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    return open_process(
        process_terminate
        | process_query_limited_information
        | process_synchronize,
        False,
        int(pid),
    ) or None


def _windows_terminate_handle(handle: Any) -> bool:
    terminate_process = ctypes.windll.kernel32.TerminateProcess
    terminate_process.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    terminate_process.restype = ctypes.c_int
    return bool(terminate_process(handle, 0))


def _windows_wait_for_process_exit(
    handle: Any,
    timeout_ms: int = PROCESS_EXIT_TIMEOUT_MS,
) -> bool:
    wait_for_single_object = ctypes.windll.kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait_for_single_object.restype = ctypes.c_uint32
    return int(wait_for_single_object(handle, max(1, int(timeout_ms)))) == 0


def _windows_close_termination_handle(handle: Any) -> None:
    close_handle = ctypes.windll.kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    close_handle(handle)


def _posix_open_pidfd(pid: int) -> int:
    if not hasattr(os, "pidfd_open"):
        raise NotImplementedError("pidfd_open_unavailable")
    return os.pidfd_open(int(pid))


def _posix_send_termination(process_fd: int) -> None:
    if not hasattr(signal, "pidfd_send_signal"):
        raise NotImplementedError("pidfd_send_signal_unavailable")
    signal.pidfd_send_signal(process_fd, signal.SIGTERM)


def _posix_wait_for_process_exit(
    process_fd: int,
    timeout_ms: int = PROCESS_EXIT_TIMEOUT_MS,
) -> bool:
    if not hasattr(select, "poll"):
        return False
    poller = select.poll()
    poller.register(process_fd, select.POLLIN)
    events = poller.poll(max(1, int(timeout_ms)))
    return any(
        descriptor == process_fd
        and event_mask & (select.POLLIN | getattr(select, "POLLHUP", 0))
        for descriptor, event_mask in events
    )


def _posix_close_pidfd(process_fd: int) -> None:
    os.close(process_fd)


def _terminate_verified_windows_backend(
    slot_state: dict[str, Any],
    first: dict[str, Any],
) -> dict[str, Any]:
    pid = int(first["pid"])
    handle = _windows_open_termination_handle(pid)
    if handle is None:
        return {"ok": False, "reason": "process_handle_open_failed"}
    try:
        second = _verify_managed_backend_process(slot_state)
        if (
            not second.get("ok")
            or second.get("snapshot") != first.get("snapshot")
        ):
            return {
                "ok": False,
                "reason": "process_identity_changed_before_termination",
            }
        if not _windows_terminate_handle(handle):
            return {"ok": False, "reason": "terminate_process_failed"}
        if not _windows_wait_for_process_exit(handle):
            return {"ok": False, "reason": "process_exit_not_confirmed"}
    finally:
        _windows_close_termination_handle(handle)
    return {
        "ok": True,
        "pid": pid,
        "identity_verified": True,
        "exit_confirmed": True,
    }


def _terminate_verified_posix_backend(
    slot_state: dict[str, Any],
    first: dict[str, Any],
) -> dict[str, Any]:
    pid = int(first["pid"])
    try:
        process_fd = _posix_open_pidfd(pid)
    except NotImplementedError:
        return {
            "ok": False,
            "reason": "stable_process_handle_unavailable",
        }
    except OSError:
        return {"ok": False, "reason": "process_handle_open_failed"}
    try:
        second = _verify_managed_backend_process(slot_state)
        if (
            not second.get("ok")
            or second.get("snapshot") != first.get("snapshot")
        ):
            return {
                "ok": False,
                "reason": "process_identity_changed_before_termination",
            }
        try:
            _posix_send_termination(process_fd)
        except NotImplementedError:
            return {
                "ok": False,
                "reason": "stable_process_handle_unavailable",
            }
        except OSError:
            return {"ok": False, "reason": "terminate_process_failed"}
        if not _posix_wait_for_process_exit(process_fd):
            return {"ok": False, "reason": "process_exit_not_confirmed"}
    finally:
        _posix_close_pidfd(process_fd)
    return {
        "ok": True,
        "pid": pid,
        "identity_verified": True,
        "exit_confirmed": True,
    }


def _terminate_verified_backend(slot_state: dict[str, Any]) -> dict[str, Any]:
    """Terminate and confirm exit through the same stable OS handle."""

    first = _verify_managed_backend_process(slot_state)
    if not first.get("ok"):
        return first
    if os.name == "nt":
        return _terminate_verified_windows_backend(slot_state, first)
    return _terminate_verified_posix_backend(slot_state, first)


def _wait_for_managed_snapshot(
    process: subprocess.Popen,
    *,
    port: int,
    start_nonce: str,
    timeout_sec: float = 6.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.2, timeout_sec)
    last: dict[str, Any] = {"status": "not_found"}
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return {
                "status": "unavailable",
                "reason": f"managed_backend_exited:{process.returncode}",
            }
        last = _process_snapshot(process.pid)
        if (
            last.get("status") == "found"
            and _snapshot_matches_managed_command(
                last,
                port=port,
                start_nonce=start_nonce,
            )
        ):
            return last
        time.sleep(0.05)
    return {
        "status": "unavailable",
        "reason": str(last.get("reason") or "managed_process_identity_timeout"),
    }


def _stop_spawned_process(process: subprocess.Popen) -> None:
    """Stop only the Popen handle created by this request."""

    try:
        process.terminate()
        process.wait(timeout=3)
        return
    except Exception:
        pass
    try:
        process.kill()
        process.wait(timeout=3)
    except Exception:
        pass


async def _spawn_and_persist_managed_backend(
    slot: str,
    *,
    last_exit_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Spawn, identify, and atomically persist one managed backend."""

    port = int(state[slot]["port"])
    start_nonce = secrets.token_hex(16)
    command = _managed_backend_command(port, start_nonce)
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.Popen(
        command,
        cwd=str(BACKEND_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=environment,
    )
    previous_slot_state = dict(state[slot])
    persisted = False
    try:
        snapshot = await asyncio.to_thread(
            _wait_for_managed_snapshot,
            proc,
            port=port,
            start_nonce=start_nonce,
        )
        try:
            process_identity = _managed_process_identity(
                pid=proc.pid,
                port=port,
                start_nonce=start_nonce,
                snapshot=snapshot,
            )
        except ValueError as exc:
            raise HTTPException(
                503,
                detail={
                    "error": "managed_backend_identity_unavailable",
                    "reason": str(exc),
                },
            ) from exc
        state[slot]["pid"] = proc.pid
        state[slot]["status"] = "starting"
        state[slot]["started_at"] = time.time()
        state[slot]["process_identity"] = process_identity
        if last_exit_observation is not None:
            state[slot]["last_exit_observation"] = dict(last_exit_observation)
        try:
            save_state(state)
        except OSError as exc:
            raise HTTPException(
                503,
                detail={
                    "error": "managed_backend_state_persist_failed",
                    "slot": slot,
                },
            ) from exc
        persisted = True
    except BaseException:
        if not persisted:
            _stop_spawned_process(proc)
            state[slot] = previous_slot_state
        raise
    return {
        "slot": slot,
        "port": port,
        "pid": proc.pid,
        "status": "starting",
        "hint": "调用 /api/admin/health 等待ready",
    }


# ── Admin API ──

@app.get("/api/admin/state")
async def admin_state(live: bool = False):
    """返回proxy+两个slot的状态. live=true顺带探活更新state."""
    if live:
        for s in ("green", "blue"):
            port = state[s]["port"]
            try:
                r = await client.get(f"http://localhost:{port}/api/stats", timeout=2.0)
                state[s]["status"] = "healthy" if r.status_code == 200 else "unhealthy"
                if r.status_code == 200:
                    d = r.json()
                    state[s]["nodes"] = d.get("total_nodes")
            except Exception:
                state[s]["status"] = "offline"
        save_state(state)
    return state


_failover_counters: dict[str, int] = {"green": 0, "blue": 0}


async def _try_auto_failover():
    """Retain the legacy hook; single-writer mode always returns False."""
    async with _admin_process_lock:
        return await _try_auto_failover_transaction()


async def _try_auto_failover_transaction():
    """Serialize automatic and administrative active-slot changes."""
    if SINGLE_WRITER_GRAPH:
        # Both slots share one graph root and therefore one exclusive runtime
        # lock. A second healthy standby cannot exist without violating the
        # single-writer contract, so an automatic slot switch is never safe.
        return False
    other = "blue" if state["active"] == "green" else "green"
    port = state[other]["port"]
    try:
        r = await client.get(f"http://localhost:{port}/api/stats", timeout=2.0)
        if r.status_code == 200:
            old = state["active"]
            state["active"] = other
            save_state(state)
            print(f"[Proxy] Auto-failover: {old} → {other}")
            return True
    except Exception:
        pass
    return False


@app.get("/api/admin/health")
async def admin_health(slot: str | None = None):
    """探测指定slot (或全部) 的健康状态."""
    slots_to_check = [slot] if slot else ["green", "blue"]
    result = {}
    for s in slots_to_check:
        if s not in state:
            result[s] = {"error": "invalid slot"}
            continue
        port = state[s]["port"]
        try:
            r = await client.get(f"http://localhost:{port}/api/stats", timeout=3.0)
            if r.status_code == 200:
                d = r.json()
                result[s] = {"status": "healthy", "port": port, "nodes": d.get("total_nodes"), "edges": d.get("total_edges")}
                state[s]["status"] = "healthy"
            else:
                result[s] = {"status": "unhealthy", "port": port, "http_code": r.status_code}
                state[s]["status"] = "unhealthy"
        except Exception as e:
            result[s] = {"status": "offline", "port": port, "error": str(e)[:100]}
            state[s]["status"] = "offline"
    save_state(state)
    return result


@app.post("/api/admin/deploy")
async def admin_deploy(payload: dict):
    """部署新版本到非active slot.

    Body: {slot?: "blue"|"green", auto_detect?: True}  — 默认部署到非active slot
    """
    async with _admin_process_lock:
        return await _admin_deploy_transaction(payload)


async def _admin_deploy_transaction(payload: dict):
    """Run one deploy as an indivisible process-ownership transaction."""
    target = payload.get("slot") or other_slot()
    if target not in ("green", "blue"):
        raise HTTPException(400, "slot必须是green或blue")
    if target == state["active"]:
        raise HTTPException(400, f"不能部署到active slot ({target}), 请指定另一个")
    if state[target].get("pid"):
        prior = _verify_managed_backend_process(state[target])
        reason = (
            "slot_already_has_verified_backend"
            if prior.get("ok")
            else "slot_has_unverified_process_state"
        )
        raise HTTPException(
            409,
            detail={"error": reason, "slot": target, "fail_closed": True},
        )

    if SINGLE_WRITER_GRAPH:
        active = state["active"]
        active_state = state[active]
        active_pid = active_state.get("pid")
        if payload.get("single_writer_active_stopped") is not True:
            raise HTTPException(
                409,
                detail={
                    "error": "single_writer_graph_requires_cutover",
                    "slot": target,
                    "active": active,
                    "active_pid": active_pid,
                    "reason": "explicit_active_stop_evidence_required",
                    "fail_closed": True,
                },
            )

        # The internal flag only selects the controlled cutover path. It is
        # never accepted as evidence by itself: the exact persisted process
        # identity must now be absent from the OS and its port must be idle.
        active_absence = _verify_managed_backend_absent(active_state)
        if not active_absence.get("ok"):
            reason = str(
                active_absence.get("reason")
                or "active_process_inspection_unavailable"
            )
            if reason == "managed_process_still_running":
                reason = "active_backend_still_running"
            elif reason == "managed_port_still_occupied":
                reason = "active_port_still_occupied"
            detail = {
                "error": "single_writer_graph_requires_cutover",
                "slot": target,
                "active": active,
                "active_pid": active_pid,
                "reason": reason,
                "fail_closed": True,
            }
            if active_absence.get("listener_pids"):
                detail["listener_pids"] = active_absence["listener_pids"]
            raise HTTPException(
                409,
                detail=detail,
            )

    return await _spawn_and_persist_managed_backend(target)


@app.post("/api/admin/recover-active")
async def admin_recover_active(payload: dict):
    """Recover a stopped active backend without changing its slot."""

    async with _admin_process_lock:
        return await _admin_recover_active_transaction(payload)


async def _admin_recover_active_transaction(payload: dict):
    """Restart only an exactly identified, observably stopped active backend."""

    if payload.get("confirm") != "recover-stopped-active":
        raise HTTPException(
            400,
            detail={
                "error": "active_backend_recovery_confirmation_required",
                "expected_confirm": "recover-stopped-active",
                "fail_closed": True,
            },
        )

    active = state["active"]
    active_state = state[active]
    previous_pid = active_state.get("pid")
    absence = _verify_managed_backend_absent(active_state)
    if not absence.get("ok"):
        reason = str(
            absence.get("reason") or "active_process_inspection_unavailable"
        )
        if reason == "managed_process_still_running":
            reason = "active_backend_still_running"
        elif reason == "managed_port_still_occupied":
            reason = "active_port_still_occupied"
        detail = {
            "error": "active_backend_recovery_precondition_failed",
            "active": active,
            "active_pid": previous_pid,
            "reason": reason,
            "fail_closed": True,
        }
        if absence.get("listener_pids"):
            detail["listener_pids"] = absence["listener_pids"]
        raise HTTPException(409, detail=detail)

    spawned = await _spawn_and_persist_managed_backend(
        active,
        last_exit_observation=absence.get("last_exit_observation"),
    )
    absent_pid = absence.get("pid")
    return {
        **spawned,
        "recovered_active": active,
        "previous_pid": int(absent_pid) if absent_pid is not None else None,
        "identity_verified": bool(absence.get("identity_verified")),
        "bootstrap_empty_state": bool(absence.get("bootstrap_empty_state")),
    }


@app.post("/api/admin/switch")
async def admin_switch(payload: dict):
    """切换active slot. 前提: 目标slot健康."""
    async with _admin_process_lock:
        return await _admin_switch_transaction(payload)


async def _admin_switch_transaction(payload: dict):
    """Run one active-slot switch as an indivisible state transaction."""
    to = payload.get("to")
    if to not in ("green", "blue"):
        raise HTTPException(400, "to必须是green或blue")
    if to == state["active"]:
        return {"noop": True, "active": to}

    identity = _verify_managed_backend_process(state[to])
    if not identity.get("ok"):
        raise HTTPException(
            409,
            detail={
                "error": "switch_process_identity_unverified",
                "slot": to,
                "reason": identity.get("reason"),
                "fail_closed": True,
            },
        )

    # 切换前做一次健康检查
    port = state[to]["port"]
    try:
        r = await client.get(f"http://localhost:{port}/api/stats", timeout=3.0)
        if r.status_code != 200:
            raise HTTPException(503, f"目标slot {to} 未就绪 (http {r.status_code})")
        stats = r.json()
    except Exception as e:
        raise HTTPException(503, f"目标slot {to} 不可达: {e}")
    if not isinstance(stats, dict):
        raise HTTPException(
            503,
            detail={
                "error": "switch_health_payload_invalid",
                "slot": to,
                "reason": "stats_payload_must_be_object",
                "fail_closed": True,
            },
        )
    nodes = stats.get("total_nodes")
    edges = stats.get("total_edges")
    if (
        isinstance(nodes, bool)
        or not isinstance(nodes, int)
        or nodes < 0
        or isinstance(edges, bool)
        or not isinstance(edges, int)
        or edges < 0
    ):
        raise HTTPException(
            503,
            detail={
                "error": "switch_health_payload_invalid",
                "slot": to,
                "reason": "graph_counts_must_be_non_negative_integers",
                "fail_closed": True,
            },
        )

    old = state["active"]
    previous_slot_state = dict(state[to])
    state["active"] = to
    state[to]["status"] = "healthy"
    state[to]["nodes"] = nodes
    state[to]["edges"] = edges
    try:
        save_state(state)
    except OSError as exc:
        state["active"] = old
        state[to] = previous_slot_state
        raise HTTPException(
            503,
            detail={
                "error": "switch_state_persist_failed",
                "switched_from": old,
                "requested_active": to,
                "fail_closed": True,
            },
        ) from exc
    return {"switched_from": old, "active": to, "port": port}


@app.post("/api/admin/retire")
async def admin_retire(payload: dict):
    """关闭指定slot. 不能是active slot."""
    async with _admin_process_lock:
        return await _admin_retire_transaction(payload)


async def _admin_retire_transaction(payload: dict):
    """Run one retire as an indivisible process-ownership transaction."""
    slot = payload.get("slot")
    if slot not in ("green", "blue"):
        raise HTTPException(400, "slot必须是green或blue")
    if slot == state["active"]:
        raise HTTPException(400, f"不能retire active slot ({slot}), 请先 /api/admin/switch")

    pid = state[slot]["pid"]
    if not pid:
        return {"retired": slot, "noop": True}
    termination = _terminate_verified_backend(state[slot])
    orphan_state_repaired = False
    last_exit_observation: dict[str, Any] | None = None
    if (
        not termination.get("ok")
        and termination.get("reason") == "managed_process_not_found"
    ):
        absence = _verify_managed_backend_absent(state[slot])
        if not absence.get("ok"):
            termination = {
                "ok": False,
                "reason": absence.get("reason"),
            }
            if absence.get("listener_pids"):
                termination["listener_pids"] = absence["listener_pids"]
        else:
            last_exit_observation = absence["last_exit_observation"]
            termination = {
                "ok": True,
                "pid": int(pid),
                "identity_verified": True,
                "exit_confirmed": True,
                "already_exited": True,
            }
            orphan_state_repaired = True
    if (
        not termination.get("ok")
        or termination.get("exit_confirmed") is not True
    ):
        raise HTTPException(
            409,
            detail={
                "error": "retire_process_identity_unverified",
                "slot": slot,
                "reason": termination.get("reason"),
                "fail_closed": True,
            },
        )
    previous_slot_state = dict(state[slot])
    state[slot]["pid"] = None
    state[slot]["status"] = "idle"
    state[slot]["started_at"] = None
    state[slot].pop("process_identity", None)
    if last_exit_observation is not None:
        state[slot]["last_exit_observation"] = last_exit_observation
    try:
        save_state(state)
    except OSError as exc:
        state[slot] = previous_slot_state
        detail = {
            "error": "retire_state_persist_failed",
            "slot": slot,
            "pid": int(pid),
            "process_terminated": not orphan_state_repaired,
            "fail_closed": True,
        }
        if orphan_state_repaired:
            detail["orphan_state_repair_pending"] = True
        raise HTTPException(
            503,
            detail=detail,
        ) from exc
    result = {
        "retired": slot,
        "pid": int(pid),
        "identity_verified": True,
        "exit_confirmed": True,
    }
    if orphan_state_repaired:
        result["orphan_state_repaired"] = True
        result["already_exited"] = True
    return result


# ── 通用转发 ──

async def forward(request: Request, path: str) -> Response:
    """转发任意请求到active backend."""
    port = active_port()
    url = f"http://localhost:{port}/{path}"
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    try:
        r = await client.request(
            request.method,
            url,
            content=body,
            headers=headers,
            params=request.query_params,
        )
        return Response(content=r.content, status_code=r.status_code, headers={k: v for k, v in r.headers.items() if k.lower() not in ("content-encoding", "transfer-encoding", "connection")})
    except (httpx.ConnectError, httpx.ReadTimeout):
        if SINGLE_WRITER_GRAPH:
            return Response(
                content=json.dumps(
                    {
                        "error": f"backend {state['active']}:{port} 不可达",
                        "automatic_failover": "disabled_single_writer_graph",
                    }
                ),
                status_code=503,
                media_type="application/json",
            )
        # 计数 active 失败, 超过阈值尝试自动failover
        slot = state["active"]
        _failover_counters[slot] = _failover_counters.get(slot, 0) + 1
        if _failover_counters[slot] >= 3:
            failed = await _try_auto_failover()
            _failover_counters[slot] = 0
            if failed:
                return await forward(request, path)
        return Response(content=json.dumps({"error": f"backend {state['active']}:{port} 不可达"}), status_code=503, media_type="application/json")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(path: str, request: Request):
    # admin路径不转发, 其余全转
    if path.startswith("api/admin/"):
        raise HTTPException(404, "admin path unmatched")
    return await forward(request, path)


if __name__ == "__main__":
    import argparse
    import uvicorn
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9700)
    parser.add_argument("--host", default="127.0.0.1",
                        help="Default 127.0.0.1 (localhost-only, safe). Pass --host 0.0.0.0 "
                             "to expose on LAN — no auth at this layer, only use on trusted networks.")
    args = parser.parse_args()
    if args.host != "127.0.0.1":
        print("=" * 72)
        print(f"[3CAN SECURITY WARNING] proxy listening on {args.host}:{args.port}")
        print("  This API has NO authentication. Anyone on reachable network can")
        print("  read/write your knowledge graph. Use 127.0.0.1 unless you know why.")
        print("=" * 72, flush=True)
    uvicorn.run(app, host=args.host, port=args.port)
