# 3CAN RuntimeHook

RuntimeHook is a thin semantic supervisor for Codex tasks. It remembers the
current Owner Intent across resume/clear/compaction and prompts an Agent to
review four general failure modes: goal drift, unjustified hardcoding, hidden
fallback or stale state, and unrequested behavior.

The Plugin also emits one bounded 3CAN fast-path orientation at `SessionStart`,
including outside Git repositories. That orientation tells a Session to start
safe local work immediately, use fresh ticket context only just in time for a
ticket-governed operation, and write durable meaning only at a real checkpoint.
It does not activate RuntimeHook or contact 9700.

It is not a second Task Oracle. Semantic execution state remains in one ignored file:

```text
.codex/runtimehook/state.json
```

That file contains enabled/disabled state, an activation ID, RUN_INTENT,
Agent-selected internal intensity, an optional current episode, the latest
semantic review result/reference, and one bounded review-boundary epoch. The
epoch retains only its sequence, reviewed sequence, last generic kind/label,
the last completed-plan label used for de-duplication, and observed Git HEAD; it
is not an event log. A final `PASS` also records the clean Git HEAD that was
reviewed. These narrow anchors answer only whether a new review is due and
whether the reviewed code is still current; they are not artifact hashes,
candidate fingerprints, proof receipts, or a history ledger.
RuntimeHook does not own a convergence selector, binding policy, or project
evidence decision. Git remains engineering truth; the existing
`3can_convergence.py` and Task Oracle remain the evidence kernel.

The state is scoped to one physical Git worktree, not to one chat. At most one
current RuntimeHook task may use that worktree; concurrent tasks require
separate worktrees so one task cannot replace another task's Intent.

### Native task directory must match

An explicit command workdir or controller `--root` does not change the native
task cwd supplied to Hooks. A legacy chat can therefore execute code in one
repository while its native events still address another repository's state.
The optional `--native-cwd` check accepts an independently observed host task
directory and refuses a mismatch before reading or writing semantic state:

```text
<controller> --root <physical-worktree> --native-cwd <host-task-cwd> status
```

The same check can precede `on`, `review`, `checkpoint`, or `off`. A matching
subdirectory resolves to its Git worktree. It is a scope diagnostic, not a
Session registry or authentication boundary; omission does not mechanically
certify native scope. Native Hooks also reject a conflicting explicit `--root`
when their payload contains cwd, and active context/reminders identify the
worktree they actually use. The lightweight scope cache below prevents an
unregistered chat from inheriting that worktree's activation; the host binding
itself still needs correction.

### Once-observed relationship, local fast path

Use the host-provided task ID and cwd, not transcript parsing or task-title
heuristics. The controller's `bind-scope` command records an existing verified
task/root/activation relation without changing semantic state. `on` also does
this when passed `--native-cwd` and a native `--session-id` (the latter defaults
to `CODEX_THREAD_ID`). A bulk initial inventory can call the same command per
verified task; there is no second importer, daemon, polling service or database.

```text
<controller> --root <worktree> --native-cwd <host-cwd> --session-id <host-id> bind-scope --reference <host-observation-and-current-intent-evidence>
```

The cache is one bounded JSON observation per native task under
`CODEX_HOME/runtimehook/scopes/`, keyed by a hash of the ID for a portable file
name. It contains no goal, priority, token, ticket or execution history. Atomic
per-task replacement avoids a shared registry lock. Deleting it loses no
engineering evidence: the next relevant event reports `SCOPE_UNBOUND` until a
new observation. It never automatically adopts an existing activation.

A hit checks the physical root and local Git marker identity using filesystem
operations only. Native events then reuse the existing semantic review logic;
that logic still queries Git for boundary/freshness and safe state access. Thus
local relationship-lookup latency and full native Hook latency are different
measurements. No milliseconds claim includes a network request or a model review.

New task IDs, moved/nested worktrees, changed Git markers or changed activation
IDs cannot silently reuse an old binding. A same-worktree subdirectory remains
valid. Codex subagent hooks may share the parent session ID; a child cannot
rebind the parent's entry to its own root. This adapter is not a complete
subagent ownership service and does not grant concurrent writer permission.

3CAN is consulted by the registering Agent only when its durable project or
handoff meaning is relevant. Pass the observed `--knowledge-worktree` and
`--knowledge-reference` for a comparison; otherwise it stays `UNVERIFIED`.
The saved comparison is explicitly dated evidence, not a live 9700 check or
authentication. Do not cache a ticket or infer business priority from it.

Scope problems emit a bounded advisory at SessionStart/UserPromptSubmit or a
Stop system message, never `decision:block` or `continue:false`. PostToolUse
does not repeat the same mismatch on every tool call. No foreign semantic state
is applied or changed; safe independent work continues and only a genuinely
unsafe affected operation remains deferred. Correctly scoped semantic review
timing below is unchanged, as are independent project safety gates.

Use the supported host path to repair the existing task. Codex App Server
documents cwd overrides on `turn/start`, but protocol support does not prove
that a particular Desktop client exposes a safe in-place rebind. Do not use a
Git-moving handoff as a metadata edit, raw-edit JSONL/SQLite, or attach a second
runtime to an owned thread. An already-loaded resume and a cold resume need
separate verification. Accept the repair only after actual native events and
reopen/resume agree with the intended root, without modifying the peer state.
Until then, report automatic supervision as `UNAVAILABLE`/unverified and keep
safe local development moving; do not clear foreign state to silence it.

## Use

The user need not choose a profile or run a controller command. Once the Plugin
is installed and its exact Hook definition is trusted, Codex may invoke
`$3can-runtimehook` implicitly for a long, multi-stage, drift-prone, or
explicitly requested task. Natural requests such as
`这个任务按 RuntimeHook 执行` and `关闭 RuntimeHook` are supported.

Codex has a fixed built-in slash-command set. RuntimeHook does not register or
simulate `/3CAN`; it is only product shorthand. The real explicit route is
`$3can-runtimehook` or `/skills`. See the official
[Codex skills](https://learn.chatgpt.com/docs/build-skills),
[developer commands](https://learn.chatgpt.com/docs/developer-commands), and
[hooks](https://learn.chatgpt.com/docs/hooks) documentation.

The SessionStart orientation follows the repository [3CAN steering
contract](../3CAN.md): Git owns exact source state; 3CAN supplies durable
project meaning and relevant history. Read-only and safe local work needs no
ticket. A ticket is requested and consumed immediately before the operation
whose current project contract requires it, with the current Agent, project,
workspace/worktree, Workorder, target, and scope bindings. The Agent honors the
returned TTL and completion deadline and never blind-retries an expired,
conflicting, or mismatched request.

The Agent chooses the lightest useful review depth semantically:

- `light`: a concise Goal/Acceptance check at each observed boundary;
- `medium`: inspect completed output and relevant diff at each boundary;
- `max`: the same semantic reviews plus an existing targeted strict Oracle for
  only the criteria that need mechanical proof.

These are internal audit values, not user modes. RuntimeHook contains no
duration, file-count, domain, command-name, or Oracle-name classifier.

## Independence and OFF behavior

The Plugin bundles RuntimeHook only. Existing project-owned convergence,
credential, security, deployment, or publication gates remain independent and
all matching native Hooks continue to run. With no enabled RuntimeHook state,
only `SessionStart` emits the stateless 3CAN fast path; other RuntimeHook events
remain silent. When enabled, `SessionStart` emits that fast path plus RUN_INTENT.
After the current boundary is reviewed, the next `UserPromptSubmit`
automatically closes the prior conversation episode and creates one review debt
while reinjecting RUN_INTENT. A Git HEAD change observed after `Bash`, or an
`update_plan` call containing a completed step, also advances one review
boundary without parsing commands or project domains. For an internal semantic
stage with none of those signals, the Agent uses the same generic
`checkpoint --kind stage|episode --label ...` command. Multiple events before
review coalesce into one current debt; no history is retained.

Stop requests at most one native continuation while a boundary review is due
or a main-task final review is stale. A current episode review is enough for an
interim reply or a necessary wait; a turn ending is not task completion.
This owns semantic review timing only; it does not allow, deny, or
replace an independent evidence, credential, deployment, or publication gate.
On the already-continued Stop pass it reports the typed state instead of
looping. Recording final `PASS` requires a clean Git checkpoint. It is silent
only while HEAD still matches that checkpoint and the worktree remains clean;
later changes produce semantic-review debt. SessionStart uses the same currentness check and
reinjects the review as `STALE`, never as the persisted `PASS`, after either
change. SessionStart uses native
`hookSpecificOutput.additionalContext` with a bounded matching handler limit, so
the context reaches the model rather than only the UI event stream.

`off` changes the retained state to `disabled_by_owner`; later semantic
RuntimeHook reminders are silent while the stateless SessionStart orientation
remains available. It does not delete evidence and cannot disable credentials,
deployment, publication, security, or the independent PR15 convergence gate.

## 同一任务内临时插入（0.1.7 起）

四个原生 Hook 的说明、提示和错误阐述使用中文；机器字段、状态码、用户原文与历史引用保持原样。
临时请求由 Agent 依据用户当前要求判断，不能用关键词或耗时分类，也不能把明确追加的相关工作当作漂移。

```text
<controller> --root <worktree> task --kind temporary --goal "临时目标" --acceptance "T01=可验证产出" --reference "用户请求依据" --resume-objective "返回主任务后的下一步"
<controller> --root <worktree> review --scope temporary --stage final --result PASS --reference "实际产出复核依据"
<controller> --root <worktree> status
```

复用同一 activation、scope cache、复核强度与原子状态文件。一个可选 `temporary_task` 字段保存临时目标、
验收、用户请求引用和返回主任务的下一步；主 RUN_INTENT 原样保留。不创建子 Hook、任务栈、服务或执行历史。
一次仅一个临时任务，不支持嵌套或覆盖。中断恢复继续保留；Git/计划边界或一句“完成”不会自动删掉它。

实际临时产出复核通过后，`review --scope temporary --stage final --result PASS` 在一次保存中清除临时字段，
恢复主任务下一步，并将主任务保持为阶段 `PARTIAL`（不是主任务验收成功）。重复临时完成命令会拒绝范围不匹配，
不会把主任务变成 PASS。主任务原有最终 PASS 不恢复。临时视频/文档验收不要求提交主任务未完代码；
它仍需自身产出证据、项目严格验证（若适用）和独立安全门禁。主任务最终 PASS 的干净 Git 检查点要求不变。

`PARTIAL`、`FAIL`、`UNVERIFIABLE` 等非成功结果保留临时任务。仅用户明确取消时使用
`task --kind cancel --reference "取消依据"`，清除临时字段并恢复主目标，不记为成功。
`task --kind transfer|drift --reference "判断依据"` 只输出建议，不改变状态、新建任务、迁移或阻断工作。
新任务能隔离对话，但不能单独解决同一 worktree 的并行写入；需要时两者一起隔离。

状态兼容：没有临时任务仍写 v1；仅临时槽生效时写 `3can.runtimehook-state/v2`。
新控制器读两种版本；旧控制器必须报不可用而不是忽略临时槽。回滚前先用新控制器完成或明确取消临时任务，
不要强制降级/删除现场状态。升级不等于已运行任务热加载，不改它们的旧插件文件。

## Install and remove

### 可选 Jev 复核（0.1.8 起）

经用户启用，Agent 在重要阶段/最终复核中调用 `assess`，只检查当前片段的
“声明—证据”和“下一步—最新要求”。四个原生生命周期 Hook 保持离线，
没有新增 Stop gate，也不将模型意见写入语义状态或替代项目验收。
部署初期采用 observe；结构合法的 `OBSERVED` 不等于任务 PASS。

接口、证据 JSON、Windows 加密 Key 配置、费用与回滚详见
[随插件交付的 Jev 指引](../plugins/3can-runtimehook/skills/3can-runtimehook/references/jev.md)。
唯一额外本地产物是 ignored state root 中可替换的 `jev-observation.json`；
它是最近一次片段意见缓存，不是第二套执行状态。相同输入复用，变化/过期不得套用。
缺少有效 Key 时在线功能是 `UNAVAILABLE`；不能宣称实际 Jev 调用或准确率已经验收。

### Plugin 安装

RuntimeHook is distributed as the repository Plugin at
`plugins/3can-runtimehook` and is exposed by
`.agents/plugins/marketplace.json`. It requires Git and Python 3; the Windows
launcher supports `python.exe`, `python3.exe`, and the standard `py.exe -3`
launcher, while both platform launchers reject interpreters resolved inside the
current worktree. Non-SessionStart events exit before Python and Git discovery
when no RuntimeHook state exists. Windows commands run directly in the native
PowerShell hook host; there is no nested shell or batch wrapper. A custom cmd
or Git Bash hook shell on Windows is not a validated configuration. Launcher failures report typed
`UNAVAILABLE` instead of silently disabling Hooks. Native Hooks need no 3CAN Runtime,
graph, credentials, network service, or 9700 restart. Only the optional, explicitly
invoked Jev assessment uses a Gateway credential and network.

Add the public repository as a Codex marketplace and install the Plugin:

```text
codex plugin marketplace add hallowkayj-spec/3CAN-engine --ref main
```

Use an explicitly reviewed candidate ref instead of `main` when testing a
pending release. A local installation or an open PR does not update public main.

Restart the ChatGPT desktop app, open the Plugins Directory, choose the
`3CAN Engine` marketplace source, and install `3CAN RuntimeHook`. In Codex CLI,
open `/plugins` and install it from the configured marketplace, then start a new
session. Review the exact bundled Hook definition and trust it before use; in
Codex CLI, `/hooks` is the native inspection and trust surface. Installation
does not activate a task or create state. It only makes the bounded 3CAN
SessionStart orientation automatic; semantic supervision starts when Codex
selects the Skill or the Owner asks for RuntimeHook.

The first activation in a Git repository adds only
`/.codex/runtimehook/` to that repository's local Git exclude when an existing
ignore does not already cover it. This keeps state untracked without editing
`.gitignore` or any project source file. A tracked, redirected, or unsafe state
root remains typed `UNAVAILABLE`.

To update, upgrade the `3can-engine` marketplace and reinstall the Plugin from
that source. To remove it, first say `关闭 RuntimeHook` in active worktrees, then
disable or uninstall it through the Plugins browser. Uninstalling deliberately
does not delete retained project-local state or rewrite a repository's Git
exclude file.

After an update, use a new task or safely reopen a paused task to load the new
definition. An already-running task is not proven to hot-reload it. Check the
native event result, not only the enabled/trusted entry in Settings. An active
task's existing worktree state remains in place; do not activate over another
task's Intent merely to check installation.

The legacy project-kit copy remains a compatibility and clean-clone test fixture;
new users do not need to copy it into each repository. Distribution is governed
by the repository's PolyForm Noncommercial license. It is public
source-available software, not an OSI-approved open-source license.

## Short smoke

From a disposable Git worktree in a source checkout, the internal command path
is:

```powershell
$root = (git rev-parse --show-toplevel).Trim()
$cli = Join-Path $root 'plugins\3can-runtimehook\skills\3can-runtimehook\scripts\3can_runtimehook.py'
python $cli --root $root on --goal 'Deliver the requested bounded result.' --acceptance 'A01=The requested result is complete.' --intensity light --reason 'Small and clear.'
python $cli --root $root checkpoint --kind episode --label 'Implementation completed' --next-objective 'Review the result.'
python $cli --root $root review --stage final --result PASS --reference 'git:reviewed-commit'
python $cli --root $root off
```

This smoke tests semantic state only. Existing convergence tests separately
prove candidate freshness and Stop behavior.
