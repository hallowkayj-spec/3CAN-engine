#!/usr/bin/env python3
"""Thin semantic supervisor with one local state per native Codex task.

RuntimeHook remembers Owner Intent and semantic review timing. It deliberately
does not own convergence selectors, candidate freshness, proof receipts, or Stop
correctness; those remain with 3can_convergence.py and Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# A project-local Hook must not dirty its caller by compiling sibling modules.
sys.dont_write_bytecode = True
import runtimehook_checkpoints as checkpoints  # noqa: E402
import runtimehook_writeback as writeback_adapter  # noqa: E402


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATE_ROOT = Path(".codex/runtimehook")
STATE_PATH = STATE_ROOT / "state.json"
STATE_SCHEMA = "3can.runtimehook-state/v1"
TEMPORARY_STATE_SCHEMA = "3can.runtimehook-state/v2"
CHECKPOINT_STATE_SCHEMA = "3can.runtimehook-state/v3"
SESSION_STATE_SCHEMA = "3can.runtimehook-state/v4"
SCOPE_SCHEMA = "3can.runtimehook-scope/v3"
MAX_STATE_BYTES = 64 * 1024
MAX_CONTEXT_CHARS = 4_000
INTENSITIES = {"light", "medium", "max"}
REVIEW_RESULTS = {
    "PASS",
    "PARTIAL",
    "FAIL",
    "UNVERIFIABLE",
    "CONTRADICTS",
    "UNREQUESTED",
}
BOUNDARY_KINDS = {"activation", "git", "stage", "episode"}
MAX_BOUNDARY_LABEL_CHARS = 240
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
SESSION_FAST_PATH = (
    "3CAN 快速指引：立即开展安全的本地工作。Git 负责精确源码事实；3CAN 提供项目含义、相关历史和协调记录。"
    "调用 3CAN 前读取 CODEX_HOME 下的全局 3CAN.md；项目副本缺失属于正常情况。"
    "仅在有助于当前判断时检索或 route。仅对契约要求票据的操作，在执行前即时获取新票据，"
    "绑定当前 AgentId、项目/命名空间、物理工作区及必要的 Workorder、目标和范围，并遵守 TTL 与完成期限。"
    "遇到拒绝不得盲重试：未执行操作的过期状态仅刷新一次；版本冲突先重读；身份或摘要不匹配时暂停该操作。"
    "持久含义按 AUTO_CLOSEOUT 或用户要求回写；本机若启用自动回写，在接入、关键阶段与错误进展通过 Skill 的 connect/review/error 自动落盘并核验回读。"
    "失败标记 UNAVAILABLE，不阻塞安全本地工作。本指引不激活 RuntimeHook，也不替代独立安全与证据门禁。"
    "外部契约不确定或反复发生不明失败时，用 3can-deep-research 将已读证据连接到判断和可执行验证；"
    "来源数量和研究记录不等于业务验收。"
)


JEV_REQUIRED_TEXT = (
    "本机已启用 RuntimeHook 全局必经：每个新建、恢复及派发任务均使用自己的身份和状态，不等待用户再次提醒。"
    "按 Skill 和任务大小建立检查点说明；开发检查点必经 Jev，"
    "在架构/依赖选型、关键实现/集成、失败改路及交付边界记录实际参数和证据。"
    "checkpoint 本地记录后，review 会自动调用 Jev；未调用、低分、低置信或有异议不能登记 PASS。"
    "不要用 observe/off、自写 PASS 或省略检查点替代；按具体异议补证据、调研或修复再验证，"
    "不得反复调用刷分。接口不可用只报告受影响验收未完成，继续独立安全工作。"
)


class RuntimeHookError(ValueError):
    """The local RuntimeHook state or requested transition is unavailable."""

    def __init__(self, message: str, *, context: dict[str, str | None] | None = None):
        self.context = context
        self.recovery = None
        if context is not None:
            action, hint = {
                "native_host": ("verify_host_binding",
                    "核实实际宿主目录；仅宿主锚点变化且开发目标未变时，由当前任务 bind-scope 更新观察关系。"),
                "workspace": ("workspace_migration_required",
                    "目标工作区已变化；核实同项目迁移后使用 migrate-workspace prepare/apply，保留原记录与回滚计划。"),
                "state_workspace": ("workspace_migration_required",
                    "同一任务状态仍属于原工作区；使用显式 migrate-workspace，不覆盖或重新初始化原目标。"),
                "workspace_marker": ("verify_workspace_identity",
                    "核实工作区 Git 标记的真实变化及归属；不得自动覆盖已登记身份。"),
            }[context["mismatch"]]
            self.recovery = {"action": action, "automatic": False, "hint": hint}
            message += (
                f"；登记工作区={context['registered_workspace']}；请求工作区={context['requested_workspace']}"
                f"；登记宿主={context.get('registered_native_cwd') or '未提供'}"
                f"；当前宿主={context.get('observed_native_cwd') or '未提供'}。"
                "此拒绝影响当前任务的 RuntimeHook；独立安全工作可继续，其他操作仍遵守各自授权与安全边界。"
                + hint
            )
        super().__init__(message)


def _configure_utf8_stdio() -> None:
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    if os.name == "nt":
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    for stream, errors in (
        (sys.stdin, "strict"),
        (sys.stdout, "strict"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors=errors)


def _is_redirect(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def _worktree_hint(path: Path) -> Path | None:
    try:
        current = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("无法解析工作目录") from exc
    boundary = None
    for directory in (current, *current.parents):
        if os.path.lexists(directory / ".git"):
            boundary = directory
    return boundary


def _git_executable(root: Path) -> Path:
    try:
        resolved_root = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("无法解析工作目录") from exc
    untrusted_root = _worktree_hint(resolved_root) or resolved_root
    executable_name = "git.exe" if os.name == "nt" else "git"
    for raw_directory in os.environ.get("PATH", "").split(os.pathsep):
        expanded = os.path.expandvars(raw_directory.strip().strip('"'))
        if not expanded:
            continue
        directory = Path(expanded).expanduser()
        if not directory.is_absolute():
            continue
        try:
            candidate = (directory / executable_name).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if candidate.is_relative_to(untrusted_root) or untrusted_root.is_relative_to(
            candidate.parent
        ):
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeHookError("可信 PATH 中没有可用的 Git 程序")


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        executable = _git_executable(root)
        completed = subprocess.run(
            [str(executable), "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeHookError("Git 状态不可用") from exc
    return completed


def _git_head(root: Path) -> str | None:
    if not os.path.lexists(root / ".git"):
        return None  # Non-Git tasks have no fabricated source checkpoint.
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head.returncode != 0:
        branch = _git(root, "symbolic-ref", "--quiet", "HEAD")
        if branch.returncode == 0 and branch.stdout.strip().startswith("refs/heads/"):
            if _git(root, "show-ref", "--verify", "--quiet", branch.stdout.strip()).returncode == 1:
                return None  # An unborn repository can initialize supervision.
        raise RuntimeHookError("Git HEAD 不可用")
    current_head = head.stdout.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", current_head):
        raise RuntimeHookError("Git HEAD 无效")
    return current_head


def _git_checkpoint(root: Path) -> tuple[str | None, bool]:
    current_head = _git_head(root)
    if not os.path.lexists(root / ".git"):
        return None, False
    status = _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=normal",
        "--ignore-submodules=none",
    )
    if status.returncode != 0:
        raise RuntimeHookError("Git 工作树状态不可用")
    return current_head, bool(status.stdout.strip())


def _repository_root(root: Path) -> Path:
    try:
        resolved = root.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("无法解析项目根目录") from exc
    if not resolved.is_dir():
        raise RuntimeHookError("工作区必须是现有目录")
    if _native_worktree(str(resolved)) is None:
        return resolved
    completed = _git(resolved, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        raise RuntimeHookError("RuntimeHook 需要 Git 工作树")
    try:
        actual = Path(completed.stdout.strip()).resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError("无法解析 Git 工作树根目录") from exc
    if actual != resolved:
        raise RuntimeHookError("--root 必须是精确的 Git 工作树根目录")
    return resolved


def _validate_directory(path: Path, expected: Path, *, label: str) -> None:
    if _is_redirect(path) or not path.is_dir():
        raise RuntimeHookError(f"{label} 不是直接目录")
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError) as exc:
        raise RuntimeHookError(f"{label} 无法解析") from exc
    if resolved != expected:
        raise RuntimeHookError(f"{label} 重定向到了专用路径之外")


def _legacy_state_root(root: Path) -> Path | None:
    codex = root / ".codex"
    state_root = root / STATE_ROOT
    if not os.path.lexists(codex):
        return None
    _validate_directory(codex, root / ".codex", label=".codex directory")
    if not os.path.lexists(state_root):
        return None
    _validate_directory(state_root, root / STATE_ROOT, label="RuntimeHook state root")

    tracked = _git(root, "ls-files", "--", STATE_ROOT.as_posix())
    ignored = _git(root, "check-ignore", "-q", "--", STATE_PATH.as_posix())
    if tracked.returncode != 0 or tracked.stdout.strip() or ignored.returncode != 0:
        raise RuntimeHookError(
            "RuntimeHook 状态目录必须未被跟踪且已被 Git 忽略"
        )
    return state_root


def _session_dir(session_id: str) -> Path:
    """Same owner for state and all task artifacts, independent of its workspace."""
    scope = _scope_path(session_id)
    return scope.parent.parent / "sessions" / scope.stem


def _state_root(root: Path, session_id: str, *, create: bool) -> Path | None:
    return _managed_directory(_session_dir(session_id), create=create)


@contextmanager
def _session_lock(session_id: str):
    """One native task writer; OS releases the lock if a process exits."""
    path = _scope_path(session_id).parent.parent / "locks" / (_scope_path(session_id).stem + ".lock")
    _managed_directory(path.parent, create=True)
    if os.path.lexists(path) and (_is_redirect(path) or not path.is_file()):
        raise RuntimeHookError("SESSION_LOCK_NOT_DIRECT_FILE")
    with path.open("a+b") as handle:
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeHookError("SESSION_BUSY：当前任务正在记录状态；保留现场，不并发覆盖") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _managed_directory(directory: Path, *, create: bool) -> Path | None:
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not home.is_absolute() or not directory.is_relative_to(home / "runtimehook"):
        raise RuntimeHookError("RuntimeHook 路径必须位于 CODEX_HOME/runtimehook")
    components = [home]
    for part in directory.relative_to(home).parts:
        components.append(components[-1] / part)
    # Validate each managed component before creating a child or following it.
    for path in components:
        if not os.path.lexists(path):
            if not create:
                return None
            path.mkdir(parents=path == home, exist_ok=True)
        _validate_directory(path, path, label="RuntimeHook session directory")
    return directory


def _text(value: Any, *, label: str) -> str:
    if not isinstance(value, str):
        raise RuntimeHookError(f"{label} 必须是文本")
    result = value.strip()
    if not result:
        raise RuntimeHookError(f"{label} 不得为空")
    return result


def _boundary_label(value: Any, *, label: str) -> str:
    result = _text(value, label=label)
    if len(result) > MAX_BOUNDARY_LABEL_CHARS:
        raise RuntimeHookError(
            f"{label} 超过 {MAX_BOUNDARY_LABEL_CHARS} 字符"
        )
    return result


def _validate_intent(intent: Any) -> None:
    if not isinstance(intent, dict):
        raise RuntimeHookError("RUN_INTENT 必须是对象")
    _text(intent.get("goal"), label="RUN_INTENT goal")
    acceptance = intent.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise RuntimeHookError("RUN_INTENT 验收要求必须是非空列表")
    seen: set[str] = set()
    for item in acceptance:
        if not isinstance(item, dict):
            raise RuntimeHookError("RUN_INTENT 验收项必须是对象")
        criterion_id = _text(item.get("id"), label="Acceptance ID")
        if not ID_PATTERN.fullmatch(criterion_id) or criterion_id in seen:
            raise RuntimeHookError("验收项 ID 必须有效且唯一")
        seen.add(criterion_id)
        _text(item.get("text"), label=f"Acceptance {criterion_id}")
    non_goals = intent.get("non_goals", [])
    if not isinstance(non_goals, list):
        raise RuntimeHookError("RUN_INTENT 不做事项必须是列表")
    for item in non_goals:
        _text(item, label="RUN_INTENT non-goal")


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema") not in {STATE_SCHEMA, TEMPORARY_STATE_SCHEMA, CHECKPOINT_STATE_SCHEMA, SESSION_STATE_SCHEMA}:
        raise RuntimeHookError("RuntimeHook 状态版本不受支持")
    session_state = value["schema"] == SESSION_STATE_SCHEMA
    if session_state and (
        not isinstance(value.get("session_id"), str)
        or not ID_PATTERN.fullmatch(value["session_id"])
        or not isinstance(value.get("workspace"), str)
        or not Path(value["workspace"]).is_absolute()
        or value.get("workspace_kind") not in {"git", "directory"}
    ):
        raise RuntimeHookError("会话状态缺少独立身份或工作区")
    status = value.get("status")
    if status not in {"active", "disabled_by_owner"}:
        raise RuntimeHookError("RuntimeHook 状态无效")
    activation_id = _text(value.get("activation_id"), label="activation_id")
    if not ID_PATTERN.fullmatch(activation_id):
        raise RuntimeHookError("activation_id 无效")
    _validate_intent(value.get("run_intent"))
    temporary = value.get("temporary_task")
    if temporary is not None:
        if value["schema"] not in {TEMPORARY_STATE_SCHEMA, CHECKPOINT_STATE_SCHEMA, SESSION_STATE_SCHEMA}:
            raise RuntimeHookError("临时任务需要 v2 状态，防止旧控制器误用主目标")
        _validate_intent(temporary)
        for key in ("reference", "resume_objective"):
            _text(temporary.get(key), label=f"临时任务 {key}")

    checkpoint = value.get("checkpoint")
    if checkpoint is not None:
        if value["schema"] not in {CHECKPOINT_STATE_SCHEMA, SESSION_STATE_SCHEMA} or not isinstance(checkpoint, dict):
            raise RuntimeHookError("检查点需要 v3 状态，旧控制器不得忽略 Jev 要求")
        checkpoints.jev._id(checkpoint.get("id"))
        if not isinstance(checkpoint.get("spec"), str) or not Path(checkpoint["spec"]).is_absolute():
            raise RuntimeHookError("检查点说明路径无效")

    intensity = value.get("internal_intensity")
    if not isinstance(intensity, dict) or intensity.get("level") not in INTENSITIES:
        raise RuntimeHookError("复核强度无效")
    _text(intensity.get("reason"), label="internal intensity reason")

    episode = value.get("current_episode")
    if episode is not None:
        _text(episode, label="episode objective")

    review = value.get("semantic_review")
    if not isinstance(review, dict):
        raise RuntimeHookError("语义复核必须是对象")
    result = review.get("result")
    if result != "PENDING" and result not in REVIEW_RESULTS:
        raise RuntimeHookError("语义复核结果无效")
    if result != "PENDING":
        if review.get("stage") not in {"episode", "final"}:
            raise RuntimeHookError("语义复核阶段无效")
        _text(review.get("reference"), label="semantic review reference")
    reviewed_git_head = review.get("reviewed_git_head")
    if result == "PASS" and review.get("stage") == "final":
        if not (session_state and value["workspace_kind"] == "directory" and reviewed_git_head is None) and (not isinstance(reviewed_git_head, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", reviewed_git_head
        )):
            raise RuntimeHookError("主任务最终 PASS 必须记录 reviewed_git_head")
    elif reviewed_git_head is not None:
        raise RuntimeHookError("只有主任务最终 PASS 才使用 reviewed_git_head")

    boundary = value.get("boundary")
    if boundary is not None:
        if not isinstance(boundary, dict):
            raise RuntimeHookError("复核边界必须是对象")
        sequence = boundary.get("sequence")
        reviewed_sequence = boundary.get("reviewed_sequence")
        if (
            not isinstance(sequence, int)
            or isinstance(sequence, bool)
            or sequence < 1
            or not isinstance(reviewed_sequence, int)
            or isinstance(reviewed_sequence, bool)
            or reviewed_sequence < 0
            or reviewed_sequence > sequence
        ):
            raise RuntimeHookError("复核边界序号无效")
        observed_git_head = boundary.get("observed_git_head")
        if not (session_state and observed_git_head is None) and (not isinstance(observed_git_head, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", observed_git_head
        )):
            raise RuntimeHookError("复核边界 Git HEAD 无效")
        if boundary.get("last_kind") not in BOUNDARY_KINDS:
            raise RuntimeHookError("复核边界类型无效")
        _boundary_label(boundary.get("last_label"), label="review boundary label")
        last_completed_plan_label = boundary.get("last_completed_plan_label")
        if last_completed_plan_label is not None:
            _boundary_label(
                last_completed_plan_label,
                label="last completed plan label",
            )
    return value


def _with_boundary(root: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Adopt pre-boundary v1 state without a migration subsystem."""
    existing_boundary = state.get("boundary")
    if existing_boundary is not None:
        if "last_completed_plan_label" in existing_boundary:
            return state
        return {
            **state,
            "boundary": {
                **existing_boundary,
                "last_completed_plan_label": (
                    existing_boundary["last_label"]
                    if existing_boundary["last_kind"] == "stage"
                    and existing_boundary["last_label"].startswith(("Plan checkpoint: ", "计划检查点："))
                    else None
                ),
            },
        }
    current_head = _git_head(root)
    review = state["semantic_review"]
    reviewed_head = review.get("reviewed_git_head")
    return {
        **state,
        "boundary": {
            "sequence": 1,
            "reviewed_sequence": 0 if review["result"] == "PENDING" else 1,
            "observed_git_head": reviewed_head or current_head,
            "last_kind": "activation",
            "last_label": "已读取原有 RuntimeHook 状态",
            "last_completed_plan_label": None,
        },
    }


def _load_state(root: Path, session_id: str) -> dict[str, Any] | None:
    state_root = _state_root(root, session_id, create=False)
    state_path = _session_dir(session_id) / "state.json"
    if not os.path.lexists(state_path):
        return None
    if state_root is None or _is_redirect(state_path) or not state_path.is_file():
        raise RuntimeHookError("RuntimeHook 状态不是直接文件")
    if state_path.stat().st_size > MAX_STATE_BYTES:
        raise RuntimeHookError("RuntimeHook 状态超过大小限制")
    try:
        value = json.loads(state_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeHookError("无法读取 RuntimeHook 状态") from exc
    if (not isinstance(value, dict) or value.get("schema") != SESSION_STATE_SCHEMA
            or value.get("session_id") != session_id
            or not isinstance(value.get("workspace"), str) or not Path(value["workspace"]).is_absolute()):
        raise RuntimeHookError("CONTEXT_MISMATCH：会话状态身份或工作区不匹配；不继承其他任务")
    if value["workspace"] != str(root):
        raise RuntimeHookError(
            "CONTEXT_MISMATCH：同一任务的已初始化工作区与当前请求不一致",
            context={"mismatch": "state_workspace", "registered_workspace": value["workspace"],
                     "requested_workspace": str(root)},
        )
    return _validate_state(_with_boundary(root, _validate_state(value)))


def _context(
    state: dict[str, Any],
    *,
    review_result: str | None = None,
    worktree: Path | None = None,
) -> str:
    temporary = state.get("temporary_task")
    intent = temporary or state["run_intent"]
    acceptance = "; ".join(
        f"{item['id']}={item['text']}" for item in intent["acceptance"]
    )
    non_goals = "；".join(state["run_intent"].get("non_goals", []))
    non_goal_text = f" 不做事项：{non_goals}。" if non_goals else ""
    episode = state.get("current_episode")
    episode_text = f" 当前阶段：{episode}。" if episode else ""
    review = state["semantic_review"]
    boundary = state["boundary"]
    effective_review = review_result or review["result"]
    jev_required = checkpoints.required()
    if jev_required and effective_review == "PASS" and not review.get("jev_request_sha256"):
        effective_review = "JEV_REQUIRED"
    if (
        effective_review == "PENDING"
        and boundary["last_kind"] == "git"
        and boundary["reviewed_sequence"] > 0
        and boundary["reviewed_sequence"] < boundary["sequence"]
    ):
        effective_review = "STALE"
    review_text = f" 语义复核状态：{effective_review}"
    if review["result"] != "PENDING":
        review_text += f" ({review['stage']}, {review['reference']})"
    boundary_due = boundary["reviewed_sequence"] < boundary["sequence"]
    boundary_text = (
        f" 最近边界 {boundary['sequence']}（{boundary['last_kind']}）："
        f"{boundary['last_label']}。边界复核：{'待完成 DUE' if boundary_due else '当前 CURRENT'}。"
    )
    worktree_text = f"工作树：{worktree}。" if worktree is not None else ""
    temporary_text = (
        f"临时任务进行中；保留主目标：{state['run_intent']['goal']}。"
        f"请求依据：{temporary['reference']}。完成后返回：{temporary['resume_objective']}。"
        "临时任务最终复核 PASS 后自动清除临时状态；未完成不可当作完成清除。"
        if temporary else ""
    )
    message = (
        f"RuntimeHook 语义上下文 [{state['activation_id']}]。"
        f"{worktree_text}{temporary_text}"
        f"当前目标 RUN_INTENT：{intent['goal']}。验收要求：{acceptance}。{non_goal_text}"
        f"复核强度：{state['internal_intensity']['level']}；理由："
        f"{state['internal_intensity']['reason']}。{episode_text}{review_text}。"
        f"{boundary_text} "
        "在阶段边界逐项检查实际产出：目标漂移、无依据硬编码、隐藏回退/过期状态、遗漏或未经要求的行为。"
        "测试、渲染或研究记录不能单独证明内容质量；在已有复核记录中说明未达要求和下一步。"
        "需机械证明的要求复用项目已有严格验证；独立安全与证据门禁不变。"
        "当前用户要求优先。由 Agent 判断是继续、临时插入、任务转移还是疑似偏移，不按关键词猜测。"
        "临时插入用 task --kind temporary 保留主目标；转移/偏移仅建议新工作树或新任务，不自动迁移或停止整项工作。"
        "中途回复/等待不是任务完成，记录阶段复核即可；宣称完成才做对应目标的最终复核。"
        + (JEV_REQUIRED_TEXT if jev_required else "")
    )
    if len(message) > MAX_CONTEXT_CHARS:
        raise RuntimeHookError("RUN_INTENT 过大，无法在原生 Hook 中完整注入")
    return message


def _stale_review_reasons(root: Path, state: dict[str, Any]) -> list[str]:
    review = state["semantic_review"]
    if review["result"] != "PASS" or review.get("stage") != "final":
        return []
    current_head, dirty = _git_checkpoint(root)
    reasons = []
    if checkpoints.required() and not review.get("jev_request_sha256"):
        reasons.append("尚无当前检查点的 Jev 必经复核")
    if current_head != review["reviewed_git_head"]:
        reasons.append("Git HEAD 已变化")
    if dirty:
        reasons.append("工作树存在未提交变化")
    boundary = state["boundary"]
    if boundary["reviewed_sequence"] < boundary["sequence"]:
        reasons.append("存在尚未复核的新边界")
    return reasons


def _mark_boundary(
    state: dict[str, Any],
    *,
    kind: str,
    label: str,
    observed_git_head: str | None,
) -> dict[str, Any]:
    if kind not in BOUNDARY_KINDS - {"activation"}:
        raise RuntimeHookError("只能新增 Git、阶段或 episode 边界")
    boundary = state["boundary"]
    return {
        **state,
        "boundary": {
            **boundary,
            "sequence": boundary["sequence"] + 1,
            "observed_git_head": observed_git_head,
            "last_kind": kind,
            "last_label": _boundary_label(label, label="review boundary label"),
        },
        "semantic_review": {
            "stage": None,
            "result": "PENDING",
            "reference": None,
            "reviewed_git_head": None,
        },
    }


def _sync_git_boundary(
    root: Path, state: dict[str, Any], session_id: str
) -> tuple[dict[str, Any], bool]:
    current_head = _git_head(root)
    previous_head = state["boundary"]["observed_git_head"]
    if current_head == previous_head:
        return state, False
    state = _mark_boundary(
        state,
        kind="git",
        label=f"Git HEAD {str(previous_head)[:12]} -> {str(current_head)[:12]}",
        observed_git_head=current_head,
    )
    _write_state(root, state, session_id)
    return state, True


def _write_state(root: Path, value: dict[str, Any], session_id: str, *, create_only: bool = False) -> None:
    value = {**value, "schema": SESSION_STATE_SCHEMA, "session_id": session_id,
             "workspace": str(root),
             "workspace_kind": "git" if os.path.lexists(root / ".git") else "directory"}
    value = _validate_state(value)
    _context(value)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if len(payload.encode("utf-8")) > MAX_STATE_BYTES:
        raise RuntimeHookError("RuntimeHook 状态超过大小限制")
    state_root = _state_root(root, session_id, create=True)
    assert state_root is not None
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".state.", suffix=".tmp", dir=str(state_root)
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _state_root(root, session_id, create=False)
        destination = _session_dir(session_id) / "state.json"
        if create_only:
            # Publish once, atomically; never replace a concurrent on/review.
            os.link(temporary_name, destination)
        else:
            os.replace(temporary_name, destination)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _acceptance(values: list[str]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in values:
        criterion_id, separator, text = raw.partition("=")
        criterion_id, text = criterion_id.strip(), text.strip()
        if (
            not separator
            or not ID_PATTERN.fullmatch(criterion_id)
            or not text
            or criterion_id in seen
        ):
            raise RuntimeHookError(
                "--acceptance 必须使用唯一的 STABLE-ID=可观察结果"
            )
        seen.add(criterion_id)
        result.append({"id": criterion_id, "text": text})
    if not result:
        raise RuntimeHookError("至少需要一个 --acceptance")
    return result


def activate(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    existing = _load_state(root, args.session_id)
    if existing and existing.get("temporary_task"):
        raise RuntimeHookError("TEMPORARY_TASK_ACTIVE：请先完成或按用户要求取消临时任务，不得覆盖主目标")
    local_exclude_added = False
    current_head = _git_head(root)
    activation_id = f"rh-{uuid.uuid4().hex[:16]}"
    state = {
        "schema": STATE_SCHEMA,
        "status": "active",
        "activation_id": activation_id,
        "run_intent": {
            "goal": _text(args.goal, label="goal"),
            "acceptance": _acceptance(args.acceptance),
            "non_goals": [_text(item, label="non-goal") for item in args.non_goal],
        },
        "internal_intensity": {
            "level": args.intensity,
            "reason": _text(args.reason, label="intensity reason"),
        },
        "current_episode": (
            _text(args.episode, label="episode objective")
            if args.episode.strip()
            else None
        ),
        "semantic_review": {
            "stage": None,
            "result": "PENDING",
            "reference": None,
            "reviewed_git_head": None,
        },
        "boundary": {
            "sequence": 1,
            "reviewed_sequence": 0,
            "observed_git_head": current_head,
            "last_kind": "activation",
            "last_label": "RuntimeHook 已启用",
            "last_completed_plan_label": None,
        },
    }
    _write_state(root, state, args.session_id)
    return {
        "ok": True,
        "status": "active",
        "activation_id": activation_id,
        "internal_intensity": state["internal_intensity"],
        "state_path": str(_session_dir(args.session_id) / "state.json"),
        "local_exclude_added": local_exclude_added,
    }


def disable(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if state is None:
        return {"ok": True, "status": "inactive", "changed": False}
    if state["status"] == "disabled_by_owner":
        return {"ok": True, "status": "disabled_by_owner", "changed": False}
    state = {
        **state,
        "status": "disabled_by_owner",
    }
    _write_state(root, state, args.session_id)
    return {
        "ok": True,
        "status": "disabled_by_owner",
        "changed": True,
        "state_retained": True,
    }


def _resume_main(state: dict[str, Any], reference: str, label: str) -> dict[str, Any]:
    state = dict(state)
    temporary = state.pop("temporary_task")
    state.pop("checkpoint", None)
    state = _mark_boundary(state, kind="episode", label=label,
                           observed_git_head=state["boundary"]["observed_git_head"])
    state["current_episode"] = temporary["resume_objective"]
    # Temporary success/cancellation never certifies the main task as complete.
    state["semantic_review"] = {
        "stage": "episode", "result": "PARTIAL", "reference": reference,
        "reviewed_git_head": None,
    }
    state["boundary"]["reviewed_sequence"] = state["boundary"]["sequence"]
    state["boundary"]["last_completed_plan_label"] = None
    return state


def task_relation(args: argparse.Namespace) -> dict[str, Any]:
    reference = _text(args.reference, label="当前用户请求/判断依据")
    if args.kind in {"transfer", "drift"}:
        return {
            "ok": True, "status": "ADVISORY", "kind": args.kind,
            "reference": reference, "semantic_state_changed": False,
            "message": "任务转移建议：源码或写入范围需要隔离时用新 worktree；仅上下文需要分开时用新任务。"
                       "新任务若会并行写同一工作树，仍需独立 worktree。疑似偏移先核对用户意图。"
                       "这里只给建议，不新建、不迁移、不停止安全工作，也不把用户明确追加的要求当作漂移。",
        }
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if state is None:
        raise RuntimeHookError("没有可关联的主任务，请先确认当前任务的 RuntimeHook 状态")
    if args.kind == "cancel":
        if not state.get("temporary_task"):
            return {"ok": True, "status": "NO_TEMPORARY_TASK", "changed": False}
        state = _resume_main(state, reference, "临时任务已按用户要求取消；恢复主任务")
        _write_state(root, state, args.session_id)
        return {"ok": True, "status": "CANCELLED", "temporary_cleared": True,
                "main_result": "PARTIAL", "next_objective": state["current_episode"]}
    if state["status"] != "active":
        raise RuntimeHookError("RuntimeHook 未启用，不创建临时任务")
    if state.get("temporary_task"):
        raise RuntimeHookError("TEMPORARY_TASK_ACTIVE：已有临时任务，不能嵌套或覆盖；先完成或明确取消")
    temporary = {
        "goal": _text(args.goal, label="临时目标"),
        "acceptance": _acceptance(args.acceptance),
        "reference": reference,
        "resume_objective": _text(args.resume_objective, label="返回主任务后的下一步"),
    }
    state = _mark_boundary(state, kind="episode", label="开始用户要求的临时任务；主目标保留",
                           observed_git_head=_git_head(root))
    state["temporary_task"] = temporary
    state.pop("checkpoint", None)
    state["current_episode"] = temporary["goal"]
    _write_state(root, state, args.session_id)
    return {"ok": True, "status": "TEMPORARY_ACTIVE", "activation_id": state["activation_id"],
            "main_intent_preserved": True, "resume_objective": temporary["resume_objective"]}


def record_review(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if state is None or state["status"] != "active":
        raise RuntimeHookError("没有启用中的 RuntimeHook 语义任务")
    if (args.scope == "temporary") != bool(state.get("temporary_task")):
        raise RuntimeHookError("REVIEW_SCOPE_MISMATCH：复核对象与当前主/临时任务不一致；状态未改变")
    state, _git_changed = _sync_git_boundary(root, state, args.session_id)
    next_objective = args.next_objective.strip()
    if args.stage == "episode" and not next_objective:
        raise RuntimeHookError("阶段复核必须指定 --next-objective")
    reference = _text(args.reference, label="semantic review reference")
    if args.stage == "final" and args.result == "PASS" and not state.get("temporary_task"):
        current_head, dirty = _git_checkpoint(root)
        if dirty or (state["workspace_kind"] == "git" and current_head is None):
            raise RuntimeHookError("主任务最终 PASS 需要干净 Git 检查点")
    jev_review = _review_checkpoint(args, root, state, reference)
    if jev_review and not jev_review["ok"]:
        return jev_review
    if state.get("temporary_task") and args.stage == "final" and args.result == "PASS":
        state = _resume_main(state, reference, "临时任务已复核完成并清除；恢复主任务")
        _write_state(root, state, args.session_id)
        return {"ok": True, "status": "review_recorded", "scope": "temporary",
                "result": "PASS", "reference": reference, "temporary_cleared": True,
                "main_result": "PARTIAL", "next_objective": state["current_episode"]}
    reviewed_git_head = None
    if args.stage == "final" and args.result == "PASS":
        reviewed_git_head, dirty = _git_checkpoint(root)
        if dirty:
            raise RuntimeHookError(
                "主任务最终 PASS 需要干净 Git 检查点"
            )
    state = {
        **state,
        "semantic_review": {
            "stage": args.stage,
            "result": args.result,
            "reference": reference,
            "reviewed_git_head": reviewed_git_head,
            **({"jev_request_sha256": jev_review["request_sha256"]}
               if jev_review and jev_review.get("request_sha256") else {}),
        },
        "boundary": {
            **state["boundary"],
            "reviewed_sequence": state["boundary"]["sequence"],
        },
        "current_episode": (
            next_objective if args.stage == "episode" else state.get("current_episode")
        ),
    }
    _write_state(root, state, args.session_id)
    return {
        "ok": True,
        "status": "review_recorded",
        "activation_id": state["activation_id"],
        "stage": args.stage,
        "result": args.result,
        "reference": state["semantic_review"]["reference"],
        "reviewed_git_head": reviewed_git_head,
        "reviewed_boundary_sequence": state["boundary"]["reviewed_sequence"],
        "jev": jev_review,
    }


def record_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    started = time.monotonic()
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if state is None or state["status"] != "active":
        raise RuntimeHookError("没有启用中的 RuntimeHook 语义任务")
    state, _git_changed = _sync_git_boundary(root, state, args.session_id)
    spec_path = getattr(args, "spec", None)
    checkpoint_id = getattr(args, "checkpoint_id", None)
    supplied = getattr(args, "packet", None)
    packet = spec = None
    intent = state.get("temporary_task") or state["run_intent"]
    if checkpoints.required() or any((spec_path, checkpoint_id, supplied)):
        if not args.native_cwd or not args.session_id:
            raise RuntimeHookError("检查点需要宿主实际 --native-cwd 和 --session-id")
        scoped, binding = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
        if scoped != root or binding.get("activation_id") != state["activation_id"]:
            raise RuntimeHookError("CONTEXT_MISMATCH：不记录其他任务检查点")
        if not all((spec_path, checkpoint_id, supplied)):
            raise RuntimeHookError("CHECKPOINT_SPEC_REQUIRED：提供 --spec、--id、--packet；说明和证据不能省略")
        spec_path = spec_path.resolve(strict=True)
        if not spec_path.is_relative_to(root):
            raise RuntimeHookError("检查点说明必须位于当前物理工作树内")
        spec = checkpoints.spec_from(spec_path, intent)
        packet = checkpoints.make_packet(spec, checkpoint_id, checkpoints.read_json(supplied), intent)
    current_head = _git_head(root)
    state = _mark_boundary(
        state,
        kind=args.kind,
        label=args.label or (packet["checkpoint"]["title"] if packet else ""),
        observed_git_head=current_head,
    )
    next_objective = args.next_objective.strip()
    if next_objective:
        state = {
            **state,
            "current_episode": _text(
                next_objective, label="next episode objective"
            ),
        }
    if packet:
        state["checkpoint"] = {"id": checkpoint_id, "spec": str(spec_path)}
        state["schema"] = CHECKPOINT_STATE_SCHEMA
        record = {
            "schema": checkpoints.RECORD_SCHEMA, "activation_id": state["activation_id"],
            "scope": "temporary" if state.get("temporary_task") else "main",
            "boundary_sequence": state["boundary"]["sequence"], "git_head": current_head,
            "intent_sha256": checkpoints.digest(intent), "spec_sha256": checkpoints.digest(spec),
            "observed_at": datetime.now(timezone.utc).isoformat(), "packet": packet,
            "packet_sha256": checkpoints.digest(packet),
            "review": None,
        }
        _state_root(root, args.session_id, create=False)
        checkpoints.save(checkpoints.record_path(_session_dir(args.session_id), checkpoint_id, record["scope"]), record)
    _write_state(root, state, args.session_id)
    return {
        "ok": True,
        "status": "review_due",
        "activation_id": state["activation_id"],
        "boundary": state["boundary"],
        "checkpoint_id": checkpoint_id,
        "local_elapsed_ms": round((time.monotonic() - started) * 1000, 3),
        "online_calls": 0,
    }


def connect_knowledge(args):
    """Bind durable project meaning, never another task's execution state."""
    settings = writeback_adapter.config()
    if not settings:
        raise RuntimeHookError("WRITEBACK_POLICY_NOT_ENABLED：先配置 Owner 授权的全局 3CAN 客户端")
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if not state or state["status"] != "active":
        raise RuntimeHookError("请先为当前任务启用 RuntimeHook")
    if not args.session_id or not args.native_cwd:
        raise RuntimeHookError("connect 需要当前任务 ID 和宿主实际目录")
    target, observed = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
    if target != root or observed.get("activation_id") != state["activation_id"]:
        raise RuntimeHookError("CONTEXT_MISMATCH：不得关联其他任务")
    binding = writeback_adapter.connection(settings, root, agent_id=args.agent_id,
                                   workorder_id=args.workorder_id, node_id=args.node_id)
    binding["session_id"] = args.session_id
    state["knowledge"] = binding
    _write_state(root, state, args.session_id)
    return {"ok": True, "status": "CONNECTED_LOCALLY", "knowledge": binding}


def _auto_writeback(args, output):
    """Called only by semantic commands, never by native lifecycle callbacks."""
    settings = writeback_adapter.config()
    if not settings:
        return {"status": "NOT_ENABLED", "local_work_blocked": False}
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if not state or not state.get("knowledge"):
        return {"status": "UNAVAILABLE", "error_code": "KNOWLEDGE_BINDING_REQUIRED",
                "message": "当前任务先 connect 到已核实的项目/模块节点；不猜测或借用其他任务节点。", "local_work_blocked": False}
    if state["status"] != "active":
        raise RuntimeHookError("RuntimeHook 未启用，不为旧任务自动回写")
    if args.command == "review" and not args.summary.strip():
        return {"status": "UNAVAILABLE", "error_code": "SEMANTIC_SUMMARY_REQUIRED",
                "message": "review --summary 需简述实际阶段变化与未完成项；不把目标原文冒充开发结果。", "local_work_blocked": False}
    if not args.session_id or not args.native_cwd:
        raise RuntimeHookError("自动回写必须核对当前任务和宿主目录")
    target, scope = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
    if target != root or scope.get("activation_id") != state["activation_id"]:
        raise RuntimeHookError("CONTEXT_MISMATCH：不得为其他任务回写")
    if state["knowledge"].get("session_id") != args.session_id:
        raise RuntimeHookError("CONTEXT_MISMATCH：回写绑定属于另一个任务")
    if args.command == "error" and ((args.scope == "temporary") != bool(state.get("temporary_task"))):
        return {"status": "UNAVAILABLE", "error_code": "REVIEW_SCOPE_MISMATCH",
                "component": "3can-runtimehook", "operation": "error", "local_work_blocked": False,
                "expected_scope": "temporary" if state.get("temporary_task") else "main",
                "requested_scope": args.scope,
                "message": "错误记录必须关联当前目标；核实后显式传入对应 --scope，未发起回写。"}
    intent = state.get("temporary_task") or state["run_intent"]
    event = {"event": "onboarding" if args.command == "connect" else "error" if args.command == "error" else "milestone",
             "session_id": args.session_id, "activation_id": state["activation_id"],
             "scope": getattr(args, "scope", "main"), "checkpoint": (state.get("checkpoint") or {}).get("id"),
             "summary": getattr(args, "summary", "") or intent["goal"],
             "reference": getattr(args, "reference", "") or "runtimehook:" + state["activation_id"],
             "result": output.get("result") if output.get("ok") else output.get("status", "UNAVAILABLE"),
             "next_objective": getattr(args, "next_objective", "")}
    if args.command == "connect":
        event["result"] = "observed"
    if args.command == "error":
        event["error"] = {"id": args.error_id, "state": args.error_state}
        event["result"] = args.error_state
    # For temporary completion the review already cleared the slot. Use the
    # caller's explicit scope and summary, never relabel it as main completion.
    receipt = writeback_adapter.deliver(settings, root, state["knowledge"], event)
    _state_root(root, args.session_id, create=False)
    event_id = receipt.get("event_id")
    if event_id:
        checkpoints.save(_session_dir(args.session_id) / (event_id.lower() + ".json"), receipt)
    return receipt


def notes_compaction(args: argparse.Namespace) -> dict[str, Any]:
    """Explicit maintenance, never an automatic capacity fallback."""
    adapter = writeback_adapter.notes_adapter
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if not state or state["status"] != "active" or not state.get("knowledge"):
        raise RuntimeHookError("KNOWLEDGE_BINDING_REQUIRED：维护使用当前任务自己的connect绑定")
    target, observed = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
    if target != root or observed.get("activation_id") != state["activation_id"] or state["knowledge"].get("session_id") != args.session_id:
        raise RuntimeHookError("CONTEXT_MISMATCH：不代其他任务维护")
    binding = state["knowledge"]
    settings = writeback_adapter.config()
    if not settings:
        raise RuntimeHookError("WRITEBACK_CONFIG_REQUIRED")
    client = writeback_adapter._client(settings, root)
    identity = writeback_adapter.context(client, root)
    if any(binding.get(k) != v for k, v in identity.items()):
        raise RuntimeHookError("PROJECT_WORKSPACE_CHANGED")
    url = writeback_adapter.urlparse(settings["base_url"])
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment or url.path:
        raise RuntimeHookError("LOCAL_3CAN_URL_REQUIRED")
    selector = checkpoints.read_json(Path(settings["selector_path"]))
    engine, graph = Path(selector["engine_root"]), Path(selector["graph_root"])
    if client._project_identity_gate(settings["base_url"], {"selected": str(engine)}, require_configured=True)["status"] != "pass":
        raise RuntimeHookError("PROJECT_IDENTITY_MISMATCH")
    deadline = time.monotonic() + 90
    receipt_path = _session_dir(args.session_id) / ("notes-maintenance-" + str(time.time_ns()) + ".json")
    receipt = {"status": "UNAVAILABLE", "binding": binding, "apply": args.apply, "requests": []}
    verified_runtime = False
    def request(path, *, payload=None, method="GET", timeout=8):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise adapter.NotesError("NOTES_MAINTENANCE_DEADLINE")
        attempt = {"method": method, "path": path, "ok": None}
        receipt["requests"].append(attempt)
        checkpoints.save(receipt_path, receipt)
        ok, result = client._try_json_request(settings["base_url"], path, payload=payload, method=method, timeout=min(timeout, remaining))
        attempt["ok"] = bool(ok)
        if not ok:
            status_code = result.get("http_status") if isinstance(result, dict) else None
            if status_code == 404 and method == "GET" and path.startswith("/api/nodes/SES-RHARCH-"):
                return None
            raise adapter.NotesError("CONFLICT" if status_code == 409 else f"HTTP_{status_code}" if status_code else "NOTES_MAINTENANCE_REQUEST_UNAVAILABLE")
        return result
    try:
        stats = request("/api/stats", timeout=4)
        valid, warning = client._validate_stats(stats, min_nodes=0, expected_engine_root=engine, expected_graph_root=graph)
        if not valid and warning and warning.get("kind") == "production_not_ready":
            stats = request("/api/stats?deep=true", timeout=30)
            valid, _ = client._validate_stats(stats, min_nodes=0, expected_engine_root=engine, expected_graph_root=graph)
        if not valid:
            raise RuntimeHookError("RUNTIME_IDENTITY_OR_READINESS_UNVERIFIED")
        verified_runtime = True
        plan_path = args.plan.resolve()
        if not plan_path.is_relative_to(root):
            raise RuntimeHookError("NOTES_PLAN_OUTSIDE_WORKSPACE")
        if args.apply:
            if not args.ticket_id:
                raise RuntimeHookError("NOTES_TICKET_REQUIRED")
            plan = checkpoints.read_json(plan_path, 2 * 1024 * 1024)
            receipt.update(adapter.apply(plan, binding, plan_path, args.ticket_id, request))
        else:
            if not args.summary_file or not args.summary_file.resolve().is_relative_to(root) or not args.summary_file.is_file() or args.summary_file.stat().st_size > 8000:
                raise RuntimeHookError("NOTES_SUMMARY_FILE_REQUIRED")
            if plan_path.exists():
                raise RuntimeHookError("NOTES_PLAN_EXISTS：保留既有计划，刷新时使用新的精确文件")
            summary = writeback_adapter._safe_text(client, args.summary_file.read_text(encoding="utf-8"), 2000)
            node = request("/api/nodes/" + writeback_adapter.quote(binding["node_id"], safe=""))
            plan = adapter.prepare(node, binding, summary)
            text = json.dumps(plan, ensure_ascii=False, indent=2)
            if len(text.encode("utf-8")) > 2 * 1024 * 1024:
                raise RuntimeHookError("NOTES_PLAN_TOO_LARGE")
            plan_path.parent.mkdir(parents=True, exist_ok=True)
            with plan_path.open("x", encoding="utf-8") as stream:
                stream.write(text)
            receipt.update(status="PREPARED_READ_ONLY", plan_sha256=plan["plan_sha256"],
                archive_id=plan["archive"]["id"], before_bytes=plan["before_bytes"], after_bytes=plan["after_bytes"],
                expected_updated_at=node["updated_at"], plan_path=str(plan_path))
        return {"ok": True, **{k: v for k, v in receipt.items() if k not in {"binding", "requests", "apply"}}, "receipt_path": str(receipt_path)}
    except (RuntimeHookError, adapter.NotesError, KeyError, TypeError, ValueError, OSError) as exc:
        code = str(exc) if isinstance(exc, (RuntimeHookError, adapter.NotesError)) else "NOTES_MAINTENANCE_CONTEXT_UNAVAILABLE"
        receipt.update(status="CONFLICT" if code == "CONFLICT" else "UNAVAILABLE", error_code=code)
        if verified_runtime and (code.startswith(("HTTP_", "ARCHIVE_")) or code in {"CONFLICT", "NOTES_MAINTENANCE_REQUEST_UNAVAILABLE", "NOTES_APPLIED_READBACK_MISMATCH"}):
            receipt["issue_intake"] = writeback_adapter.issue_observed(client, settings["base_url"], binding, code,
                operation="runtimehook_notes_compaction", evidence_ref=receipt.get("plan_sha256"))
        raise
    finally:
        checkpoints.save(receipt_path, receipt)


def _review_checkpoint(args, root, state, reference):
    """The existing review path owns mandatory invocation, never a second Hook."""
    if not checkpoints.required():
        if state.get("checkpoint"):
            raise RuntimeHookError("JEV_POLICY_NOT_ENABLED：先确认本机在线调用授权；不自动消费")
        return None
    descriptor = state.get("checkpoint")
    if not descriptor:
        if args.result != "PASS":
            return {"ok": True, "status": "NOT_CAPTURED", "request_sha256": None}
        raise RuntimeHookError("CHECKPOINT_REQUIRED：成功复核前必须记录项目检查点并调用 Jev")
    intent = state.get("temporary_task") or state["run_intent"]
    if not Path(descriptor["spec"]).resolve(strict=True).is_relative_to(root):
        raise RuntimeHookError("检查点说明不属于当前工作树")
    spec = checkpoints.spec_from(descriptor["spec"], intent)
    key = descriptor["id"]
    scope = "temporary" if state.get("temporary_task") else "main"
    path = checkpoints.record_path(_session_dir(args.session_id), key, scope)
    record = checkpoints.read_json(path)
    record_before = checkpoints.digest(record)
    binding = {"activation_id": state["activation_id"],
               "scope": "temporary" if state.get("temporary_task") else "main",
               "intent_sha256": checkpoints.digest(intent), "spec_sha256": checkpoints.digest(spec)}
    if (not isinstance(record, dict) or record.get("schema") != checkpoints.RECORD_SCHEMA
            or any(record.get(k) != v for k, v in binding.items())
            or record.get("boundary_sequence") != state["boundary"]["sequence"]
            or checkpoints.digest(record.get("packet")) != record.get("packet_sha256")
            or record.get("git_head") != _git_head(root)):
        raise RuntimeHookError("CHECKPOINT_STALE：重新取得当前检查点证据，不复用旧目标或旧代码意见")
    if not args.native_cwd or not args.session_id:
        raise RuntimeHookError("Jev 必经复核需要宿主实际 --native-cwd 和 --session-id")
    scoped, current_binding = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
    if scoped != root or current_binding.get("activation_id") != state["activation_id"]:
        raise RuntimeHookError("CONTEXT_MISMATCH：不代签其他任务检查点")
    if args.stage == "final" and args.result == "PASS":
        if key != spec["final_checkpoint"]:
            raise RuntimeHookError("FINAL_CHECKPOINT_REQUIRED：先完成说明中的最终检查点")
        for point in spec["checkpoints"]:
            if point["id"] == key:
                continue
            try:
                previous = checkpoints.read_json(checkpoints.record_path(_session_dir(args.session_id), point["id"], scope))
            except FileNotFoundError:
                previous = {}
            if (not isinstance(previous, dict) or any(previous.get(k) != v for k, v in binding.items())
                    or (previous.get("review") or {}).get("result") != "PASS"):
                raise RuntimeHookError(f"CHECKPOINT_COVERAGE_MISSING：{point['id']} 尚无同目标、同说明的成功复核")
    point = next(p for p in spec["checkpoints"] if p["id"] == key)
    opinion = record.get("opinion")
    if opinion is not None and opinion.get("schema") != checkpoints.jev.CONTRACT:
        raise RuntimeHookError("CHECKPOINT_PROTOCOL_STALE：判别协议已更新；保留旧意见，重新取得当前检查点证据，不自动收费重试")
    if opinion is None:
        judge_args = argparse.Namespace(root=root, native_cwd=args.native_cwd,
            session_id=args.session_id, packet=path, mode="advisory", timeout=args.timeout)
        opinion = assess(judge_args)
        record["opinion"] = opinion
    elif opinion.get("status") == "OBSERVED":
        request, mapping = checkpoints.jev.build_request(record["packet"], intent)
        opinion = {**opinion, **checkpoints.jev.parse_response(opinion, request["questions"], mapping), "reused": True}
    issues = checkpoints.concerns(opinion, point)
    if _load_state(root, args.session_id) != state or _git_head(root) != record["git_head"]:
        return {"ok": False, "status": "STALE", "message": "复核期间任务或 Git 变化；不写回旧状态"}
    if checkpoints.digest(checkpoints.read_json(path)) != record_before:
        return {"ok": False, "status": "STALE", "message": "复核期间检查点变化；不覆盖新记录"}
    allowed = args.result != "PASS" or not issues
    record["review"] = {"result": args.result if allowed else "PARTIAL", "reference": reference,
                        "issues": issues, "jev_request_sha256": opinion.get("request_sha256")}
    _state_root(root, args.session_id, create=False)
    checkpoints.save(path, record)
    return {"ok": allowed, "status": "REVIEWED" if allowed else "REVIEW_REQUIRED",
            "request_sha256": opinion.get("request_sha256"), "checkpoint_id": key,
            "issues": issues, "opinion": opinion,
            "message": "按具体异议补证据、调研或修复；低置信不等于已证明代码错误。不刷分、不自动改目标。"}


def status(args: argparse.Namespace) -> dict[str, Any]:
    root = _repository_root(args.root)
    state = _load_state(root, args.session_id)
    if state is None:
        return {"ok": True, "status": "inactive"}
    return {"ok": True, **state}


def assess(args: argparse.Namespace) -> dict[str, Any]:
    # Lifecycle callbacks never call the online adapter.
    import runtimehook_jev as jev
    if args.mode == "off":
        return {"ok": True, "status": "OFF", "semantic_state_changed": False}
    if args.packet is None or args.native_cwd is None or not args.session_id:
        raise RuntimeHookError("Jev 需要 --packet、宿主实际 --native-cwd 和 --session-id；不猜测任务归属")
    if not 1 <= args.timeout <= 30:
        raise RuntimeHookError("Jev timeout 必须在 1 到 30 秒之间")
    started = time.monotonic()
    root = _repository_root(args.root)

    def snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
        scoped, binding = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
        state = _load_state(root, args.session_id)
        if scoped != root or not state or state["status"] != "active" or binding.get("activation_id") != state["activation_id"]:
            raise RuntimeHookError("Jev CONTEXT_MISMATCH：不读取其他任务意图")
        # Narrow call-currentness only, not a candidate or artifact fingerprint.
        return state, {"session_id": args.session_id, "worktree": str(root), "state": state, "git_head": _git_head(root)}

    def packet_bytes() -> bytes:
        with args.packet.open("rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise jev.JevError("PACKET_TOO_LARGE")
        return raw

    base = {"schema": jev.CONTRACT, "mode": args.mode, "semantic_state_changed": False,
            "message": "Jev 仅复核所给片段，不证明文件真实性或任务完成；不授权、不清除复核债务、不停止任务。"}
    try:
        state, binding = snapshot()
        raw = packet_bytes()
        packet = json.loads(raw.decode("utf-8-sig"))
        if isinstance(packet, dict) and packet.get("schema") == checkpoints.RECORD_SCHEMA:
            packet = packet["packet"]
        if len(json.dumps(packet, ensure_ascii=False).encode()) > jev.MAX_PACKET_BYTES:
            raise jev.JevError("PACKET_TOO_LARGE")
        intent = state.get("temporary_task") or state["run_intent"]
        # Do not send the temporary task's local reference or resume pointers.
        public_intent = {k: intent[k] for k in ("goal", "acceptance", "non_goals") if k in intent}
        request, mapping = jev.build_request(packet, public_intent)
        digest = hashlib.sha256(json.dumps({"contract": jev.CONTRACT, "binding": binding, "request": request}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        receipt = _session_dir(args.session_id) / "jev-observation.json"
        _state_root(root, args.session_id, create=False)
        if os.path.lexists(receipt):
            if _is_redirect(receipt) or not receipt.is_file() or receipt.stat().st_size > MAX_STATE_BYTES:
                raise RuntimeHookError("Jev 观察记录不是大小受限的直接文件")
            cached = json.loads(receipt.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached.get("request_sha256") == digest and cached.get("status") == "OBSERVED":
                # Revalidate typed fields; a local cached opinion is not authenticated evidence.
                observed = jev.parse_response(cached, request["questions"], mapping)
                if snapshot()[1] != binding or packet_bytes() != raw:
                    return {**base, "ok": False, "status": "STALE", "error_code": "CONTEXT_CHANGED_DURING_CALL", "answers": {}}
                return {**base, **observed, "ok": True, "status": "OBSERVED", "request_sha256": digest,
                        "reused": True, "elapsed_ms": round((time.monotonic() - started) * 1000)}
        observed = jev.parse_response(jev.request_gateway(request, args.timeout), request["questions"], mapping)
        if snapshot()[1] != binding or packet_bytes() != raw:
            return {**base, "ok": False, "status": "STALE", "error_code": "CONTEXT_CHANGED_DURING_CALL", "answers": {}}
        result = {**base, "ok": True, "status": "OBSERVED", "request_sha256": digest,
                  "reused": False, "observed_at": datetime.now(timezone.utc).isoformat(),
                  "elapsed_ms": round((time.monotonic() - started) * 1000), **observed}
        # One replaceable result artifact, not execution state or a review ledger.
        descriptor, temporary = tempfile.mkstemp(prefix=".jev-", suffix=".tmp", dir=receipt.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            _state_root(root, args.session_id, create=False)
            os.replace(temporary, receipt)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return result
    except jev.JevError as exc:
        return {**base, "ok": False, "status": "UNAVAILABLE", "error_code": str(exc)}
    except (json.JSONDecodeError, UnicodeError):
        return {**base, "ok": False, "status": "UNAVAILABLE", "error_code": "INVALID_PACKET_OR_RECEIPT_JSON"}


def _completed_plan_label(payload: dict[str, Any]) -> str | None:
    if payload.get("tool_name") != "update_plan":
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    plan = tool_input.get("plan")
    if not isinstance(plan, list):
        return None
    completed = [
        item.get("step", "").strip()
        for item in plan
        if isinstance(item, dict)
        and item.get("status") == "completed"
        and isinstance(item.get("step"), str)
        and item.get("step", "").strip()
    ]
    if not completed:
        return None
    prefix = "计划检查点："
    return prefix + completed[-1][:(MAX_BOUNDARY_LABEL_CHARS - len(prefix))]


def _hook_error(message: str) -> int:
    print(
        json.dumps(
            {
                "systemMessage": (
                    f"RuntimeHook 语义上下文不可用 UNAVAILABLE：{message}。"
                    "可继续安全的本地工作；其他操作仍遵守各自授权与安全边界。"
                )
            },
            ensure_ascii=False,
        )
    )
    return 0


def _scope_path(session_id: str) -> Path:
    session_id = _text(session_id, label="native session ID")
    if not ID_PATTERN.fullmatch(session_id):
        raise RuntimeHookError("原生任务 ID 无效")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    if not home.is_absolute():
        raise RuntimeHookError("CODEX_HOME 必须是绝对路径")
    key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return home / "runtimehook" / "scopes" / f"{key}.json"


def _native_worktree(cwd: Any) -> Path | None:
    """Local marker lookup, not a Git subprocess or a Session transcript scan."""
    candidate = Path(_text(cwd, label="native cwd"))
    if not candidate.is_absolute():
        raise RuntimeHookError("原生 cwd 必须是绝对路径")
    candidate = candidate.resolve(strict=True)
    if not candidate.is_dir():
        raise RuntimeHookError("原生 cwd 必须是现有目录")
    return next(
        (p for p in (candidate, *candidate.parents) if os.path.lexists(p / ".git")),
        None,
    )


def _scope_marker(root: Path) -> list[int]:
    marker = root / ".git" if os.path.lexists(root / ".git") else root
    try:
        info = marker.stat()
    except OSError as exc:
        raise RuntimeHookError("CONTEXT_MISMATCH：已登记的 Git 标记不存在或不可读；先核实工作树") from exc
    # Commits change a .git directory's mtime, but not its identity. A linked
    # worktree's .git *file* changing invalidates its recorded Git binding.
    return [info.st_dev, info.st_ino] + (
        [info.st_size, info.st_mtime_ns] if marker.is_file() else []
    )


def _read_scope(session_id: str) -> dict[str, Any]:
    path = _scope_path(session_id)
    _managed_directory(path.parent, create=False)
    if not path.exists():
        raise RuntimeHookError("SCOPE_UNBOUND：当前原生任务尚未登记已观察的绑定")
    if _is_redirect(path) or not path.is_file() or path.stat().st_size > 8192:
        raise RuntimeHookError("范围缓存不是大小受限的直接文件")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeHookError("范围缓存不可读") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema") not in {SCOPE_SCHEMA, "3can.runtimehook-scope/v2", "3can.runtimehook-scope/v1"}
        or value.get("session_id") != session_id
        or not isinstance(value.get("worktree"), str)
        or not Path(value["worktree"]).is_absolute()
        or not isinstance(value.get("knowledge"), dict)
    ):
        raise RuntimeHookError("范围缓存身份无效")
    if value["schema"] in {SCOPE_SCHEMA, "3can.runtimehook-scope/v2"} and (
        not isinstance(value.get("native_cwd"), str)
        or not Path(value["native_cwd"]).is_absolute()
        or "native_worktree" not in value
        or (value["native_worktree"] is not None and (
            not isinstance(value["native_worktree"], str)
            or not Path(value["native_worktree"]).is_absolute()
        ))
    ):
        raise RuntimeHookError("范围缓存缺少已观察的宿主锚点")
    return value


def _check_scope(payload: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    """Read-only fast path. Cache records observations, never authorization."""
    binding = _read_scope(payload.get("session_id", ""))
    root = _native_worktree(payload.get("cwd"))
    expected = Path(binding["worktree"])
    if binding["schema"] in {SCOPE_SCHEMA, "3can.runtimehook-scope/v2"}:
        # The host anchor and the verified development target are distinct.
        # A non-Git host is matched exactly; Git hosts retain subdir support.
        anchor = Path(binding["native_worktree"]) if binding["native_worktree"] else None
        host_matches = root == anchor and (
            _scope_marker(root) == binding.get("native_git_marker") if root is not None
            else Path(payload["cwd"]).resolve(strict=True) == Path(binding["native_cwd"])
        )
    else:
        # Never silently opt a formerly mismatched v1 cache into cross-root use.
        host_matches = root == expected
    if not host_matches or _scope_marker(expected) != binding.get("git_marker"):
        raise RuntimeHookError(
            f"CONTEXT_MISMATCH：宿主锚点或开发工作树已变化；已登记开发工作树 {expected}。"
            "不得使用其他任务状态；报告不匹配并继续不受影响的安全工作",
            context={"mismatch": "native_host" if not host_matches else "workspace_marker",
                     "registered_workspace": str(expected), "requested_workspace": str(expected),
                     "registered_native_cwd": binding.get("native_cwd", str(expected)),
                     "observed_native_cwd": str(Path(payload["cwd"]).resolve(strict=True))},
        )
    return expected, binding


def _save_scope(binding: dict[str, Any], *, create_only: bool = False) -> None:
    path = _scope_path(binding["session_id"])
    _managed_directory(path.parent, create=True)
    if os.path.lexists(path) and (_is_redirect(path) or not path.is_file()):
        raise RuntimeHookError("范围缓存必须是直接文件")
    payload = json.dumps(binding, ensure_ascii=False, separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > 8192:
        raise RuntimeHookError("范围缓存超过大小限制")
    descriptor, temporary = tempfile.mkstemp(prefix=".scope-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
        if create_only:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _scope_observation(root: Path, cwd: Path, session_id: str, reference: str,
                       activation_id: str | None = None) -> dict[str, Any]:
    native_root = _native_worktree(str(cwd))
    return {
        "schema": SCOPE_SCHEMA,
        "session_id": session_id,
        "native_cwd": str(cwd.resolve(strict=True)),
        "native_worktree": str(native_root) if native_root else None,
        "native_git_marker": _scope_marker(native_root) if native_root else None,
        "worktree": str(root),
        "git_marker": _scope_marker(root),
        "activation_id": activation_id,
        "reference": reference,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "knowledge": {"status": "UNVERIFIED", "reference": None, "worktree": None},
    }


def _migrate_legacy(root: Path, session_id: str, binding: dict[str, Any]) -> None:
    """Import only a verified old owner, never consult legacy state for new chats."""
    if binding["schema"] == SCOPE_SCHEMA:
        return
    state = _load_state(root, session_id)
    if state is None and binding.get("activation_id"):
        legacy = root / STATE_PATH
        if _legacy_state_root(root) is None:
            raise RuntimeHookError("LEGACY_STATE_MISSING：旧绑定的状态不存在；不得自动新建目标")
        state = _validate_state(checkpoints.read_json(legacy, MAX_STATE_BYTES))
        if (state["activation_id"] != binding["activation_id"]
                or (state.get("knowledge", {}).get("session_id") not in {None, session_id})):
            raise RuntimeHookError("LEGACY_OWNER_MISMATCH：旧状态不属于当前会话；不得迁移或继承")
        records = list(legacy.parent.glob("checkpoint.*.json"))
        if len(records) > 128:
            raise RuntimeHookError("LEGACY_ARTIFACT_LIMIT：先核对旧检查点范围")
        owned = []
        for path in records:
            record = checkpoints.read_json(path)
            if record.get("activation_id") == state["activation_id"]:
                owned.append((path.name, record))
        directory = _state_root(root, session_id, create=True)
        for name, record in owned:
            checkpoints.save(directory / name, record)
        _write_state(root, _with_boundary(root, state), session_id, create_only=True)
    if state and state["activation_id"] != binding.get("activation_id"):
        raise RuntimeHookError("SCOPE_STALE：已有会话状态与旧绑定不一致；不自动覆盖")
    # Preserve source files and knowledge references for a reversible upgrade.
    migrated = _scope_observation(root, Path(binding.get("native_cwd") or root),
                                  session_id, "verified legacy owner migration",
                                  state["activation_id"] if state else None)
    migrated["knowledge"] = binding["knowledge"]
    _save_scope(migrated)


def bind_scope(args: argparse.Namespace) -> dict[str, Any]:
    """Explicit host/target observation for this session, never a peer activation."""
    root = _repository_root(args.root)
    if args.native_cwd is None:
        raise RuntimeHookError("bind-scope 需要宿主实际观察到的 --native-cwd")
    reference = _text(args.reference, label="host/Owner binding reference")
    state = _load_state(root, args.session_id)
    knowledge_root = getattr(args, "knowledge_worktree", None)
    knowledge_reference = getattr(args, "knowledge_reference", "")
    if knowledge_root is not None and (not knowledge_root.is_absolute() or not knowledge_reference):
        raise RuntimeHookError("3CAN 比对需要绝对工作树路径和证据引用")
    binding = {
        **_scope_observation(root, args.native_cwd, args.session_id, reference,
                             state["activation_id"] if state else None),
        "knowledge": {
            "status": (
                "UNVERIFIED" if knowledge_root is None else
                "MATCH" if knowledge_root.resolve() == root else "CONTRADICTS"
            ),
            "reference": knowledge_reference or None,
            "worktree": str(knowledge_root.resolve()) if knowledge_root else None,
        },
    }
    _save_scope(binding)
    return {
        "ok": True,
        "status": "MATCH",
        "binding": binding,
        "semantic_state_changed": False,
    }


def _migration_common_dir(root: Path) -> Path:
    if not os.path.lexists(root / ".git"):
        raise RuntimeHookError("MIGRATION_GIT_WORKSPACE_REQUIRED")
    result = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if result.returncode != 0:
        raise RuntimeHookError("MIGRATION_REPOSITORY_UNVERIFIED")
    return Path(result.stdout.strip()).resolve(strict=True)


def _migration_snapshot(args, subject: str, target: Path, native_cwd: Path,
                        reference: str, created_at: str) -> dict[str, Any]:
    binding = _read_scope(subject)
    source = _repository_root(Path(binding["worktree"]))
    target = _repository_root(target)
    state = _load_state(source, subject)
    if (not state or state["status"] != "active" or binding["schema"] != SCOPE_SCHEMA
            or state["activation_id"] != binding.get("activation_id")):
        raise RuntimeHookError("MIGRATION_OWNER_OR_ACTIVATION_MISMATCH")
    _check_scope({"cwd": binding["native_cwd"], "session_id": subject})
    if source == target:
        raise RuntimeHookError("MIGRATION_WORKSPACE_UNCHANGED：仅宿主锚点改变时使用 bind-scope")
    if _native_worktree(str(native_cwd)) != target:
        raise RuntimeHookError("MIGRATION_NATIVE_TARGET_MISMATCH")
    common = _migration_common_dir(source)
    if common != _migration_common_dir(target):
        raise RuntimeHookError("MIGRATION_REPOSITORY_MISMATCH")
    settings = writeback_adapter.config()
    if not settings:
        raise RuntimeHookError("MIGRATION_CANONICAL_CLIENT_REQUIRED")
    old_identity = writeback_adapter.context(writeback_adapter._client(settings, source), source)
    new_identity = writeback_adapter.context(writeback_adapter._client(settings, target), target)
    if any(old_identity[key] != new_identity[key] for key in ("project_id", "project_namespace")):
        raise RuntimeHookError("MIGRATION_PROJECT_MISMATCH")
    knowledge = state.get("knowledge")
    if knowledge and (knowledge.get("session_id") != subject
                      or any(knowledge.get(key) != value for key, value in old_identity.items())):
        raise RuntimeHookError("MIGRATION_KNOWLEDGE_OWNER_MISMATCH")
    moved = _mark_boundary(state, kind="stage", label="工作区已显式迁移；复核当前 Owner Intent、Workorder 与验收",
                           observed_git_head=_git_head(target))
    moved.update(workspace=str(target), workspace_kind="git")
    if knowledge:
        moved["knowledge"] = {**knowledge, **new_identity}
    _context(_validate_state(moved))
    observed = _scope_observation(target, native_cwd, subject, reference, state["activation_id"])
    observed["observed_at"] = created_at
    before = {
        "state": checkpoints.read_bytes(_session_dir(subject) / "state.json", MAX_STATE_BYTES).decode("utf-8"),
        "scope": checkpoints.read_bytes(_scope_path(subject), 8192).decode("utf-8"),
    }
    after = {"state": json.dumps(moved, ensure_ascii=False, indent=2) + "\n",
             "scope": json.dumps(observed, ensure_ascii=False, separators=(",", ":")) + "\n"}
    if len(after["state"].encode()) > MAX_STATE_BYTES or len(after["scope"].encode()) > 8192:
        raise RuntimeHookError("MIGRATION_STATE_TOO_LARGE")
    return {"schema": "3can.workspace-migration/v1", "actor_session_id": args.session_id,
            "actor_workspace": str(args.root), "target_session_id": subject,
            "activation_id": state["activation_id"], "source_workspace": str(source),
            "target_workspace": str(target), "target_native_cwd": str(native_cwd.resolve(strict=True)),
            "common_git_dir": str(common), "source_marker": _scope_marker(source),
            "target_marker": _scope_marker(target), "source_identity": old_identity,
            "target_identity": new_identity, "created_at": created_at, "reference": reference,
            "before": before, "after": after}


def _migration_replace(path: Path, raw: bytes) -> None:
    """Preserve exact bytes for transaction rollback, including old formatting."""
    _managed_directory(path.parent, create=False)
    if os.path.lexists(path):
        checkpoints.read_bytes(path, 512 * 1024)
    descriptor, temporary = tempfile.mkstemp(prefix=".migration-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _migration_idle_observation(args, plan) -> None:
    if plan["target_session_id"] == args.session_id:
        allowed = {Path(plan["target_workspace"])}
        if args.rollback:
            allowed.add(Path(plan["source_workspace"]))
        if args.native_cwd is None or _native_worktree(str(args.native_cwd)) not in allowed:
            raise RuntimeHookError("MIGRATION_NATIVE_TARGET_MISMATCH")
        return
    if args.observation is None:
        raise RuntimeHookError("MIGRATION_FRESH_IDLE_OBSERVATION_REQUIRED")
    observed = checkpoints.read_json(args.observation, 16 * 1024)
    try:
        age = time.time() - float(observed["observed_at_epoch"])
        response = observed["response"]
        valid = (observed["source"] == "mcp__codex_app.read_thread" and 0 <= age <= 120
                 and response["thread"]["id"] == plan["target_session_id"]
                 and Path(response["thread"]["cwd"]).resolve(strict=True) == Path(plan["target_native_cwd"])
                 and not response["thread"]["status"].get("activeFlags")
                 and response["turns"][0]["status"] == "completed")
    except (KeyError, TypeError, ValueError, IndexError):
        valid = False
    if not valid:
        raise RuntimeHookError("MIGRATION_TARGET_NOT_VERIFIED_IDLE")


def migrate_workspace(args: argparse.Namespace) -> dict[str, Any]:
    """Explicit bounded maintenance, never a native-event auto-adoption path."""
    if args.native_cwd is None or not args.native_cwd.is_absolute():
        raise RuntimeHookError("MIGRATION_ACTOR_NATIVE_CWD_REQUIRED")
    if args.target_session_id and args.target_session_id != args.session_id:
        actor = _load_state(args.root, args.session_id)
        if actor is None or actor["status"] != "active":
            raise RuntimeHookError("MIGRATION_ACTOR_INITIALIZATION_REQUIRED")
    directory = _state_root(args.root, args.session_id, create=False)
    if directory is None or args.plan.parent.resolve() != directory.resolve():
        raise RuntimeHookError("MIGRATION_PLAN_MUST_BELONG_TO_ACTOR")
    if not re.fullmatch(r"workspace-migration-[A-Za-z0-9._-]+\.json", args.plan.name):
        raise RuntimeHookError("MIGRATION_PLAN_NAME_INVALID")
    preparing = not args.apply and not args.rollback
    if preparing:
        if os.path.lexists(args.plan):
            raise RuntimeHookError("MIGRATION_PLAN_EXISTS：保留旧计划，用新文件记录新的观察")
        if args.to is None or args.target_native_cwd is None:
            raise RuntimeHookError("MIGRATION_TARGET_REQUIRED")
        subject = args.target_session_id or args.session_id
    else:
        plan = checkpoints.read_json(args.plan, 512 * 1024)
        if (not isinstance(plan, dict) or plan.get("schema") != "3can.workspace-migration/v1"
                or plan.get("actor_session_id") != args.session_id
                or (plan.get("actor_workspace") != str(args.root) and not (
                    args.rollback and plan.get("target_session_id") == args.session_id
                    and plan.get("source_workspace") == str(args.root)))
                or plan.get("target_session_id") != (args.target_session_id or args.session_id)):
            raise RuntimeHookError("MIGRATION_PLAN_ACTOR_OR_TARGET_MISMATCH")
        if (any(not isinstance(plan.get(key), str) or not plan[key] for key in
                ("created_at", "source_workspace", "target_workspace", "target_native_cwd", "common_git_dir", "activation_id", "reference"))
                or any(not isinstance(plan.get(side + "_marker"), list)
                       or len(plan[side + "_marker"]) not in {2, 4}
                       or any(type(value) is not int for value in plan[side + "_marker"])
                       or not isinstance(plan.get(side + "_identity"), dict) for side in ("source", "target"))
                or any(not isinstance(plan.get(stage), dict) or set(plan[stage]) != {"state", "scope"}
                       or any(not isinstance(raw, str) for raw in plan[stage].values()) for stage in ("before", "after"))):
            raise RuntimeHookError("MIGRATION_PLAN_INVALID")
        subject = plan["target_session_id"]
    # The operator keeps its own lock and identity; a peer is an explicit subject.
    with _session_lock(subject) if subject != args.session_id else nullcontext():
        if preparing:
            plan = _migration_snapshot(args, subject, args.to, args.target_native_cwd,
                                       _text(args.reference, label="Owner/host migration reference"),
                                       datetime.now(timezone.utc).isoformat())
            _migration_replace(args.plan, (json.dumps(plan, ensure_ascii=False, indent=2) + "\n").encode())
            return {"ok": True, "status": "MIGRATION_PREPARED", "plan": str(args.plan),
                    "plan_sha256": hashlib.sha256(args.plan.read_bytes()).hexdigest(),
                    "target_session_id": subject, "source_workspace": plan["source_workspace"],
                    "target_workspace": plan["target_workspace"], "semantic_state_changed": False}
        _migration_idle_observation(args, plan)
        if any(_scope_marker(Path(plan[side + "_workspace"])) != plan[side + "_marker"]
               for side in ("source", "target")):
            raise RuntimeHookError("MIGRATION_WORKSPACE_IDENTITY_CHANGED")
        paths = {"state": _session_dir(subject) / "state.json", "scope": _scope_path(subject)}
        current = {name: checkpoints.read_bytes(path, MAX_STATE_BYTES).decode("utf-8") for name, path in paths.items()}
        receipt_path = args.plan.with_name(args.plan.stem + ".receipt.json")
        plan_sha256 = hashlib.sha256(args.plan.read_bytes()).hexdigest()
        if args.rollback:
            recorded = checkpoints.read_json(receipt_path)
            if (recorded.get("plan_sha256") != plan_sha256
                    or recorded.get("actor_session_id") != args.session_id
                    or recorded.get("target_session_id") != subject):
                raise RuntimeHookError("MIGRATION_ROLLBACK_RECEIPT_MISMATCH")
            # Check both before writing either; never overwrite later task work.
            if any(current[name] not in {plan["before"][name], plan["after"][name]} for name in paths):
                raise RuntimeHookError("MIGRATION_ROLLBACK_CONFLICT：任务已有其他变化，未覆盖")
            for name, path in paths.items():
                if current[name] != plan["before"][name]:
                    _migration_replace(path, plan["before"][name].encode())
            if any(path.read_bytes() != plan["before"][name].encode() for name, path in paths.items()):
                raise RuntimeHookError("MIGRATION_ROLLBACK_READBACK_MISMATCH")
            result = "MIGRATION_ROLLED_BACK"
        elif current == plan["after"]:
            result = "MIGRATION_ALREADY_APPLIED"
        else:
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(plan["created_at"])).total_seconds()
            except (ValueError, TypeError):
                raise RuntimeHookError("MIGRATION_PLAN_INVALID_TIME") from None
            if not 0 <= age <= 300:
                raise RuntimeHookError("MIGRATION_PLAN_EXPIRED：重新核实尚未执行的迁移")
            if current != plan["before"]:
                raise RuntimeHookError("MIGRATION_STATE_CONFLICT：状态已变化，未覆盖")
            expected = _migration_snapshot(args, subject, Path(plan["target_workspace"]),
                                           Path(plan["target_native_cwd"]), plan["reference"], plan["created_at"])
            if expected != plan:
                raise RuntimeHookError("MIGRATION_PLAN_CHANGED：身份、目录或计划内容不匹配")
            pending = {"status": "MIGRATION_APPLYING", "actor_session_id": args.session_id,
                       "target_session_id": subject, "plan_sha256": plan_sha256}
            _migration_replace(receipt_path, (json.dumps(pending, indent=2) + "\n").encode())
            try:
                for name, path in paths.items():
                    _migration_replace(path, plan["after"][name].encode())
                if any(checkpoints.read_bytes(path, MAX_STATE_BYTES).decode("utf-8") != plan["after"][name]
                       for name, path in paths.items()):
                    raise RuntimeHookError("MIGRATION_READBACK_MISMATCH")
            except (OSError, RuntimeHookError, checkpoints.jev.JevError) as exc:
                observed = {name: checkpoints.read_bytes(path, MAX_STATE_BYTES).decode("utf-8") for name, path in paths.items()}
                if any(observed[name] not in {plan["before"][name], plan["after"][name]} for name in paths):
                    raise RuntimeHookError("MIGRATION_RECOVERY_REQUIRED：保留计划，不覆盖未知变化") from exc
                for name, path in paths.items():
                    if observed[name] != plan["before"][name]:
                        _migration_replace(path, plan["before"][name].encode())
                if any(path.read_bytes() != plan["before"][name].encode() for name, path in paths.items()):
                    raise RuntimeHookError("MIGRATION_ROLLBACK_READBACK_MISMATCH") from exc
                pending.update(status="MIGRATION_APPLY_FAILED_ROLLED_BACK", failure_type=type(exc).__name__)
                _migration_replace(receipt_path, (json.dumps(pending, indent=2) + "\n").encode())
                raise RuntimeHookError("MIGRATION_APPLY_FAILED_ROLLED_BACK：原始字节已恢复，计划保留") from exc
            result = "MIGRATION_APPLIED"
        receipt = {"ok": True, "status": result, "actor_session_id": args.session_id,
                   "target_session_id": subject, "activation_id": plan["activation_id"],
                   "plan": str(args.plan), "plan_sha256": plan_sha256,
                   "file_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
                   "history_files_rewritten": False, "graph_write_performed": False,
                   "observed_at": datetime.now(timezone.utc).isoformat()}
        _migration_replace(receipt_path, (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode())
        return receipt


def _native_session_id(payload: dict[str, Any]) -> str:
    """Installed Codex supplies the actual child thread ID as agent_id."""
    child = payload.get("agent_id")
    if payload.get("hook_event_name") in {"SubagentStart", "SubagentStop"} and not child:
        raise RuntimeHookError("CHILD_ID_REQUIRED：缺少原生子任务身份；不使用父任务状态")
    session_id = _text(child if child is not None else payload.get("session_id"),
                       label="native task ID")
    _scope_path(session_id)  # Same bounded identity validation as local commands.
    return session_id


def _onboarding_feedback(event: str, root: Path, payload: dict[str, Any]) -> int:
    text = (
        "RuntimeHook 全局必经 REQUIRED / INTENT_REQUIRED：已登记当前会话的独立工作区绑定。"
        f"任务 ID：{payload['session_id']}；工作区：{root}；实际宿主 cwd：{payload['cwd']}。"
        "立即读取已安装 RuntimeHook Skill，使用其控制器和上述 --root、--native-cwd、--session-id "
        "执行 on，按当前用户要求填写真实 goal/acceptance；不要要求用户重复开启。"
        "目标、检查点与回写状态按会话隔离；不得继承同目录或父任务的目标。"
        "同目录只读任务不需要新工作树；并行源码写入仍须独立工作树。"
        "随后按既有 checkpoint/review/connect/error 协议执行；此登记没有发起联网、Jev或3CAN回写。"
    )
    if event in {"Stop", "SubagentStop"}:
        output = ({"decision": "block", "reason": text}
                  if not payload.get("stop_hook_active") else {"systemMessage": text})
    else:
        output = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    print(json.dumps(output, ensure_ascii=False))
    return 0


def _scope_feedback(event: str, message: str, *, orientation: bool = False) -> int:
    text = (
        f"RuntimeHook 任务范围不可用 UNAVAILABLE：{message}。未使用或修改语义状态。"
        "报告此状态即可，不因此结束整项任务；继续独立安全工作。不得关闭其他任务钩子或绕过独立安全门禁。"
        "bind-scope 可记录已核实的宿主锚点关系；它不改变宿主目录，也不迁移已初始化的开发工作区。"
    )
    if event in {"SessionStart", "SubagentStart", "UserPromptSubmit"}:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": (SESSION_FAST_PATH + " " if orientation else "") + text,
        }}, ensure_ascii=False))
    elif event in {"Stop", "SubagentStop"}:
        print(json.dumps({"systemMessage": text}, ensure_ascii=False))
    # No PostToolUse spam, decision:block, continuation, network or cache writes.
    return 0


def _hook_root(
    requested_root: Path | None,
    payload: dict[str, Any],
) -> Path | None:
    if requested_root is not None:
        root = _repository_root(requested_root)
        if "cwd" in payload:
            native_root = _hook_root(None, payload)
            if native_root != root:
                raise RuntimeHookError(
                    f"CONTEXT_MISMATCH：原生 Hook 工作树 {native_root} 与指定工作树 {root} 不一致；"
                    "不得读取或替换其他任务的语义状态"
                )
        return root
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        raise RuntimeHookError("原生 Hook 输入缺少工作目录")
    candidate = Path(cwd)
    if _worktree_hint(candidate) is None:
        return candidate.resolve(strict=True)
    completed = _git(candidate, "rev-parse", "--show-toplevel")
    if completed.returncode != 0:
        return None
    return _repository_root(Path(completed.stdout.strip()))


def hook(args: argparse.Namespace) -> int:
    try:
        try:
            payload = json.load(sys.stdin)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeHookError("原生 Hook 输入不可读") from exc
        if not isinstance(payload, dict):
            raise RuntimeHookError("原生 Hook 输入必须是对象")
        event = payload.get("hook_event_name")
        if event not in {"SessionStart", "SubagentStart", "UserPromptSubmit", "PostToolUse", "Stop", "SubagentStop"}:
            return 0
        session_id = _native_session_id(payload)
        with _session_lock(session_id):
            return _hook_event(args, {**payload, "session_id": session_id})
    except (RuntimeHookError, checkpoints.jev.JevError, OSError, subprocess.SubprocessError) as exc:
        return _hook_error(str(exc))


def _hook_event(args: argparse.Namespace, payload: dict[str, Any]) -> int:
    try:
        event = payload["hook_event_name"]
        is_session_start = event == "SubagentStart" or (event == "SessionStart" and payload.get("source") in {
            "startup", "resume", "clear", "compact",
        })
        session_id = _native_session_id(payload)
        payload = {**payload, "session_id": session_id}
        mandatory = checkpoints.required()
        has_binding = os.path.lexists(_scope_path(session_id))
        if not has_binding:
            root = _hook_root(args.root, payload) if args.root is not None else (
                _native_worktree(payload.get("cwd")) or Path(payload["cwd"]).resolve(strict=True))
            if mandatory:
                own_state = _load_state(root, session_id)
                observation = _scope_observation(root, Path(payload["cwd"]), session_id,
                                                  "native lifecycle observed task identity and cwd",
                                                  own_state["activation_id"] if own_state else None)
                try:
                    _save_scope(observation, create_only=True)
                except FileExistsError:
                    pass  # Another event registered this same identity; validate it below.
                has_binding = True
            elif os.path.lexists(_session_dir(session_id) / "state.json"):
                return _scope_feedback(event, "SCOPE_UNBOUND：当前任务的原生绑定缺失")
        state = None
        knowledge_note = ""
        if has_binding:
            try:
                root, binding = _check_scope(payload)
                if args.root is not None and root != args.root.resolve(strict=True):
                    raise RuntimeHookError("CONTEXT_MISMATCH：指定目录与原生任务绑定不一致")
                _migrate_legacy(root, session_id, binding)
                binding = _read_scope(session_id)
                state = _load_state(root, session_id)
                if state is not None and binding.get("activation_id") != state["activation_id"]:
                    raise RuntimeHookError("SCOPE_STALE：activation 已变化；重新绑定前先核实当前任务")
                knowledge = binding.get("knowledge", {})
                if knowledge.get("status") == "CONTRADICTS":
                    knowledge_note = (
                        " 3CAN 关联记录待核对（CONTRADICTS）："
                        f"{knowledge.get('reference')}。保留已核实的当前任务绑定，"
                        "在有意义的交接/收口时更新历史记录；不因此停止开发或跳过 Jev。"
                    )
            except (RuntimeHookError, OSError, RuntimeError) as exc:
                return _scope_feedback(event, str(exc), orientation=is_session_start and args.session_orientation)
        if state is None and mandatory:
            return _onboarding_feedback(event, root, payload)
        if state is None or state["status"] != "active":
            if is_session_start and args.session_orientation:
                print(json.dumps({"hookSpecificOutput": {
                    "hookEventName": event,
                    "additionalContext": SESSION_FAST_PATH + (JEV_REQUIRED_TEXT if mandatory else "") + knowledge_note,
                }}, ensure_ascii=False))
            return 0
        state, git_changed = _sync_git_boundary(root, state, session_id)
        if is_session_start:
            stale_reasons = _stale_review_reasons(root, state)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": event,
                            "additionalContext": (
                                (f"{SESSION_FAST_PATH} " if args.session_orientation else "")
                                + _context(
                                    state,
                                    worktree=root,
                                    review_result=(
                                        "STALE" if stale_reasons else None
                                    ),
                                )
                                + knowledge_note
                            ),
                        }
                    },
                    ensure_ascii=False,
                )
            )
        elif event == "UserPromptSubmit":
            boundary = state["boundary"]
            if boundary["reviewed_sequence"] == boundary["sequence"]:
                state = _mark_boundary(
                    state,
                    kind="episode",
                    label="用户新要求开启了新对话阶段",
                    observed_git_head=boundary["observed_git_head"],
                )
                _write_state(root, state, session_id)
            stale_reasons = _stale_review_reasons(root, state)
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "UserPromptSubmit",
                            "additionalContext": _context(
                                state,
                                worktree=root,
                                review_result="STALE" if stale_reasons else None,
                            ) + knowledge_note,
                        }
                    },
                    ensure_ascii=False,
                )
            )
        elif event == "PostToolUse":
            plan_label = _completed_plan_label(payload)
            if (
                plan_label
                and state["boundary"].get("last_completed_plan_label") == plan_label
            ):
                plan_label = None
            if plan_label:
                state = _mark_boundary(
                    state,
                    kind="stage",
                    label=plan_label,
                    observed_git_head=state["boundary"]["observed_git_head"],
                )
                state["boundary"]["last_completed_plan_label"] = plan_label
                _write_state(root, state, session_id)
            if git_changed or plan_label:
                boundary = state["boundary"]
                reason = (
                    f"RuntimeHook 检测到阶段边界 {boundary['sequence']}（{boundary['last_kind']}）："
                    f"{boundary['last_label']}。请对照当前目标和验收要求，检查实际产出的目标漂移、"
                    "无依据硬编码、隐藏回退/过期状态、遗漏和未经要求的行为，记录真实阶段复核后继续。"
                )
                print(
                    json.dumps(
                        {
                            "hookSpecificOutput": {
                                "hookEventName": "PostToolUse",
                                "additionalContext": f"{reason} {_context(state, worktree=root)}",
                            },
                        },
                        ensure_ascii=False,
                    )
                )
        elif event in {"Stop", "SubagentStop"}:
            review = state["semantic_review"]
            boundary = state["boundary"]
            task_name = "临时任务" if state.get("temporary_task") else "主任务"
            continue_for_review = False
            if review["result"] == "PENDING" or boundary["reviewed_sequence"] < boundary["sequence"]:
                review_state = (
                    "STALE"
                    if boundary["last_kind"] == "git"
                    and boundary["reviewed_sequence"] > 0
                    else "DUE"
                )
                message = (
                    f"{task_name}的阶段复核待完成（{review_state}）；activation {state['activation_id']}，"
                    f"边界 {boundary['sequence']}（{boundary['last_kind']}）：{boundary['last_label']}。"
                    "请检查目标漂移、无依据硬编码、隐藏回退/过期状态和未经要求的行为。"
                    "中途回复或等待输入只需阶段复核；宣称完成才需对应任务的最终复核。"
                    "如受安全检查或必要输入阻塞，如实记录 PARTIAL/UNVERIFIABLE 并仅等待受影响步骤，不绕过限制。"
                )
                continue_for_review = True
            elif review.get("stage") == "episode":
                message = (
                    f"{task_name}阶段记录（{review['result']}），依据：{review['reference']}；"
                    f"复核范围：{boundary['last_label']}。"
                    f"这不表示整个任务完成。下一步：{state.get('current_episode')}。"
                    "有可继续的授权工作就继续；仅在确需输入或独立安全门禁处等待。"
                )
            elif review["result"] != "PASS":
                message = (
                    f"{task_name}最终复核为 {review['result']}，依据：{review['reference']}；尚未宣称完成。"
                    "若为临时任务，保留临时状态，完成后再清除；用户明确取消则使用 task --kind cancel。"
                )
            else:
                stale_reasons = _stale_review_reasons(root, state)
                if not stale_reasons:
                    return 0
                message = (
                    f"主任务最终复核已过期 STALE：{'；'.join(stale_reasons)}；原依据：{review['reference']}。"
                    "请重新核对实际结果，只有干净 Git 检查点才能记录主任务最终 PASS。"
                )
                continue_for_review = True
            message = f"RuntimeHook 工作树：{root}。{message} 独立项目安全与证据门禁不变。{knowledge_note}"
            if checkpoints.required():
                message += JEV_REQUIRED_TEXT
            if continue_for_review and not bool(payload.get("stop_hook_active")):
                print(
                    json.dumps(
                        {"decision": "block", "reason": message},
                        ensure_ascii=False,
                    )
                )
            else:
                print(json.dumps({"systemMessage": message}, ensure_ascii=False))
        return 0
    except (RuntimeHookError, checkpoints.jev.JevError, OSError, subprocess.SubprocessError) as exc:
        return _hook_error(str(exc))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="管理轻量的 3CAN RuntimeHook 语义监督。"
    )
    parser.add_argument("--root", type=Path)
    parser.add_argument("--session-id", default=os.environ.get("CODEX_THREAD_ID", ""))
    parser.add_argument(
        "--native-cwd", type=Path,
        help="本地命令前核对独立观察的原生任务 cwd；不改变任务绑定。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    binding = sub.add_parser("bind-scope", help="缓存已观察的宿主/任务/工作树关系；不修改任务状态。")
    binding.add_argument("--reference", required=True)
    binding.add_argument("--knowledge-worktree", type=Path)
    binding.add_argument("--knowledge-reference", default="")

    migration = sub.add_parser("migrate-workspace", help="显式同项目工作区迁移；默认准备，保留原始字节与历史。")
    migration.add_argument("--plan", type=Path, required=True)
    migration.add_argument("--to", type=Path)
    migration.add_argument("--target-native-cwd", type=Path)
    migration.add_argument("--target-session-id", default="", help="治理维护的明确目标；操作者仍用自己的全局 session-id。")
    migration.add_argument("--reference", default="")
    migration.add_argument("--observation", type=Path, help="跨任务 apply/rollback 前的最新原生空闲/cwd观察。")
    migration_action = migration.add_mutually_exclusive_group()
    migration_action.add_argument("--apply", action="store_true")
    migration_action.add_argument("--rollback", action="store_true")

    on = sub.add_parser("on", help="记录当前主目标 RUN_INTENT。")
    on.add_argument("--goal", required=True)
    on.add_argument("--acceptance", action="append", default=[])
    on.add_argument("--non-goal", action="append", default=[])
    on.add_argument("--intensity", choices=sorted(INTENSITIES), required=True)
    on.add_argument("--reason", required=True)
    on.add_argument("--episode", default="")

    connect = sub.add_parser("connect", help="一次绑定当前任务的 Agent/Workorder/已有知识节点并自动登记；不建新节点。")
    connect.add_argument("--agent-id", required=True)
    connect.add_argument("--workorder-id", required=True)
    connect.add_argument("--node-id", required=True)
    connect.add_argument("--reference", required=True)

    error = sub.add_parser("error", help="自动记录错误发生、调查、缓解或待验证修复；不擅改 ErrorCase。")
    error.add_argument("--id", dest="error_id", required=True)
    error.add_argument("--scope", choices=["main", "temporary"], default="main")
    error.add_argument("--state", dest="error_state", choices=["observed", "investigating", "mitigated", "resolution_claimed"], required=True)
    error.add_argument("--summary", required=True)
    error.add_argument("--reference", required=True)
    error.add_argument("--next-objective", default="")

    sub.add_parser("off", help="仅关闭 RuntimeHook 语义提醒。")

    relation = sub.add_parser("task", help="记录临时任务，或仅建议任务转移/纠偏；不迁移目录或新建任务。")
    relation.add_argument("--kind", choices=["temporary", "transfer", "drift", "cancel"], required=True)
    relation.add_argument("--reference", required=True)
    relation.add_argument("--goal", default="")
    relation.add_argument("--acceptance", action="append", default=[])
    relation.add_argument("--resume-objective", default="")

    review = sub.add_parser("review", help="记录一次主任务或临时任务的语义复核。")
    review.add_argument("--scope", choices=["main", "temporary"], default="main")
    review.add_argument("--stage", choices=["episode", "final"], required=True)
    review.add_argument("--result", choices=sorted(REVIEW_RESULTS), required=True)
    review.add_argument("--reference", required=True)
    review.add_argument("--summary", default="", help="脱敏的本阶段实际变化；不上传原始证据包。")
    review.add_argument("--next-objective", default="")
    review.add_argument("--timeout", type=float, default=10, help="Jev 单次调用超时秒数。")

    checkpoint = sub.add_parser(
        "checkpoint",
        help="声明一个已完成的阶段或 episode 边界。",
    )
    checkpoint.add_argument("--kind", choices=["stage", "episode"], default="stage")
    checkpoint.add_argument("--label", default="")
    checkpoint.add_argument("--next-objective", default="")
    checkpoint.add_argument("--spec", type=Path, help="当前项目的 JSON 检查点说明。")
    checkpoint.add_argument("--id", dest="checkpoint_id")
    checkpoint.add_argument("--packet", type=Path, help="实际参数、声明和脱敏证据片段。")

    sub.add_parser("status", help="查看当前本地 RuntimeHook 状态。")
    compact = sub.add_parser("notes-compact", help="显式同项目知识维护；默认只生成计划，Apply须精确新票据。")
    compact.add_argument("--plan", type=Path, required=True)
    compact.add_argument("--summary-file", type=Path)
    compact.add_argument("--apply", action="store_true")
    compact.add_argument("--ticket-id", default="")
    judge = sub.add_parser("assess", help="可选 Jev 片段复核；不改语义状态，不是 Stop 门禁。")
    judge.add_argument("--packet", type=Path)
    judge.add_argument("--mode", choices=["off", "observe", "advisory"], default="observe")
    judge.add_argument("--timeout", type=float, default=10)
    hook_parser = sub.add_parser(
        "hook", help="作为不持有业务控制权的 Codex 生命周期提醒运行。"
    )
    hook_parser.add_argument(
        "--session-orientation",
        action="store_true",
        help="在 SessionStart 提供无状态的 3CAN 快速指引。",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_utf8_stdio()
    args = build_parser().parse_args(argv)
    if args.command == "hook":
        if args.native_cwd is not None:
            return _hook_error(
                "--native-cwd 仅用于本地命令；原生 Hook 使用输入中的 cwd"
            )
        return hook(args)
    try:
        with _session_lock(args.session_id):
            return _local_command(args)
    except (RuntimeHookError, OSError) as exc:
        print(json.dumps({"ok": False, "status": "UNAVAILABLE", "error": str(exc)}, ensure_ascii=False))
        return 2


def _local_command(args: argparse.Namespace) -> int:
    if args.root is None:
        args.root = _native_worktree(str(Path.cwd())) or Path.cwd()
    try:
        args.root = _repository_root(args.root)
        scope = None
        own_migration = (args.command == "migrate-workspace"
                         and args.target_session_id in {"", args.session_id})
        _scope_path(args.session_id)  # All task state operations require a native identity.
        if args.command != "bind-scope" and not own_migration and args.session_id and _scope_path(args.session_id).exists():
            binding = _read_scope(args.session_id)
            if Path(binding["worktree"]) != args.root.resolve():
                raise RuntimeHookError(
                    "CONTEXT_MISMATCH：本地命令目录与当前任务的已登记绑定不一致",
                    context={"mismatch": "workspace", "registered_workspace": binding["worktree"],
                             "requested_workspace": str(args.root),
                             "registered_native_cwd": binding.get("native_cwd"),
                             "observed_native_cwd": str(args.native_cwd) if args.native_cwd else None},
                )
            observed_cwd = str(args.native_cwd or binding.get("native_cwd") or args.root)
            root, _ = _check_scope({"cwd": observed_cwd, "session_id": args.session_id})
            _migrate_legacy(root, args.session_id, binding)
            binding = _read_scope(args.session_id)
            if args.command in {"on", "off", "review", "checkpoint", "task", "connect", "error"}:
                existing = _load_state(args.root, args.session_id)
                if existing and existing["activation_id"] != binding.get("activation_id"):
                    raise RuntimeHookError("SCOPE_STALE：绑定或修改语义状态前请先核实当前任务")
        if args.native_cwd is not None and args.command != "bind-scope" and not own_migration:
            if not args.native_cwd.is_absolute():
                raise RuntimeHookError("实际观察的原生 cwd 必须是绝对路径")
            if args.session_id and _scope_path(args.session_id).exists():
                root, _ = _check_scope({"cwd": str(args.native_cwd), "session_id": args.session_id})
            else:
                # First cross-directory use requires explicit verified binding;
                # a command's workdir or --root is never an implicit handoff.
                root = _hook_root(args.root, {"cwd": str(args.native_cwd)})
            scope = {"status": "MATCH", "worktree": str(root)}
        if args.command == "bind-scope":
            output = bind_scope(args)
        elif args.command == "migrate-workspace":
            output = migrate_workspace(args)
        elif args.command == "on":
            output = activate(args)
            if args.native_cwd is not None and args.session_id:
                args.reference = "activation with independently observed host cwd"
                output["scope_binding"] = bind_scope(args)["status"]
        elif args.command == "off":
            output = disable(args)
        elif args.command == "review":
            output = record_review(args)
        elif args.command == "connect":
            output = connect_knowledge(args)
        elif args.command == "error":
            output = {"ok": True, "status": "ERROR_OBSERVATION", "result": args.error_state}
        elif args.command == "task":
            output = task_relation(args)
        elif args.command == "checkpoint":
            output = record_checkpoint(args)
        elif args.command == "status":
            output = status(args)
        elif args.command == "notes-compact":
            output = notes_compaction(args)
        elif args.command == "assess":
            output = assess(args)
        else:
            raise RuntimeHookError(f"不支持的命令： {args.command}")
        if scope is not None:
            output["native_scope"] = scope
        if args.command in {"connect", "review", "error"}:
            try:
                output["writeback"] = _auto_writeback(args, output)
            except (ValueError, OSError, subprocess.SubprocessError):
                output["writeback"] = {"status": "UNAVAILABLE", "error_code": "WRITEBACK_CONTEXT_UNAVAILABLE", "local_work_blocked": False}
        exit_code = 0 if output.get("ok", True) else 2
    except (RuntimeHookError, writeback_adapter.WritebackError, writeback_adapter.notes_adapter.NotesError, checkpoints.jev.JevError, OSError, subprocess.SubprocessError) as exc:
        output = {"ok": False, "status": "UNAVAILABLE", "error": str(exc)}
        if isinstance(exc, RuntimeHookError) and exc.context is not None:
            output.update({"error_code": "CONTEXT_MISMATCH", "component": "3can-runtimehook",
                           "operation": args.command, "local_work_blocked": False,
                           "context": exc.context, "recovery": exc.recovery})
        exit_code = 2
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
