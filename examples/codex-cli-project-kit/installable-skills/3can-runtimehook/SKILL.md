---
name: 3can-runtimehook
description: Keep Owner Intent and semantic review timing stable across long, multi-stage, cross-module, drift-prone Codex work. Use when machine policy requires engineering checkpoints, this supervision materially helps, or the user says RuntimeHook, 开启 RuntimeHook, 按 RuntimeHook 执行, 这个任务用 RuntimeHook, /3CAN, or asks to turn it off. Uses the plugin-bundled controller without a project kit; reviews drift, hardcoding, fallback and unrequested behavior without replacing Git or project evidence gates.
---

# 3CAN RuntimeHook

RuntimeHook is a semantic supervisor, not an evidence kernel. Apply it without
asking the user to choose a mode when the task is long, multi-stage,
cross-module, historically drift-prone, or explicitly requests RuntimeHook. Do
not invoke it for every small edit merely because it is installed. An
Owner-enabled `jev_required` machine policy requires this Skill in every new,
resumed or dispatched task, including small and non-Git tasks. Use a
proportionately small checkpoint; never wait for another Owner reminder.

## Apply the 3CAN fast path

The Plugin's `SessionStart` Hook provides a stateless orientation even before
RuntimeHook activation. Start safe local work immediately. Git owns exact
source state; use 3CAN route or retrieval only when durable project meaning
improves the decision. Obtain a fresh ticket just in time only for an operation
whose current project contract requires one, with exact Agent, project,
workspace/worktree, Workorder, target, and scope bindings. Honor the returned
TTL and completion deadline. Never blind-retry a typed refusal: refresh expired
state once only for the still-pending operation, reread a version conflict, and
stop that operation on an identity or digest mismatch. Durable writeback defaults
to a meaningful `AUTO_CLOSEOUT` or explicit `OWNER_REQUESTED` checkpoint. When the
Owner-enabled `CODEX_HOME/runtimehook/writeback.json` exists, read
[自动回写说明](references/writeback.md): at first 3CAN access `connect` the current
Agent/Workorder to an existing verified project node. Core development `review`
then writes its `--summary`, result and reference automatically; real errors and
progress use `error` with the same fault ID. Inspect the returned writeback status;
local success is not proof of remote persistence. Do not wait for another user reminder.

## Global entry and task-owned state

Use `scripts/3can_runtimehook.py` inside this loaded Skill directory as the
controller. Resolve its absolute path from the Skill location; do not require or
copy a controller into the target repository. Python 3 is required. For Git
work use the exact physical root from `git rev-parse --show-toplevel`; for a
non-Git task use its observed existing directory. Pass that path as `--root`.
Non-Git tasks have no fabricated Git HEAD or source-checkpoint acceptance.

With the Owner-enabled required policy, native SessionStart, SubagentStart and
subsequent events automatically register the observed task/cwd relationship.
`REQUIRED / INTENT_REQUIRED` means the entry ran and the Agent must initialize
its own goal and acceptance now, not ask the Owner to enable it again:

```text
<controller> --root <observed-workspace> --native-cwd <actual-host-cwd> --session-id <actual-task-id> on --goal <current-goal> --acceptance <ID=observable-result> --intensity light --reason <scope-based-reason>
```

Use the exact task ID and cwd shown by the native event or official host
metadata. The event's child `agent_id`, when present, is the child's actual
thread ID; use it as `--session-id`. Do not substitute the common parent
`session_id` or an inherited environment variable. Missing child identity is
`CHILD_ID_REQUIRED`, never permission to use the parent. Supported Codex versions
emit that child ID on tool events as well as SubagentStart/SubagentStop.

Each task owns its state, checkpoints and receipts under
`CODEX_HOME/runtimehook/sessions/<sha256(task-id)>/`. Every command and native
event selects the same task directory. A physical workspace is an observed
binding, not the semantic-state owner. Independent read-only tasks may share a
workspace; concurrent source writers still require separate worktrees/leases.
No activation, intent, checkpoint or 3CAN identity is inherited from a peer.

The separate `CODEX_HOME/runtimehook/scopes/` files retain only host/target
observations. An already-bound event checks that relationship locally. For a
new task, registration creates no semantic goal, network request or graph write.
The Agent provides the actual goal/acceptance with `on`, then performs the
required checkpoint/review and connect/error/writeback steps. Stop and
SubagentStop request one bounded continuation when initialization or review is
missing. Reporting a real limitation is allowed; claiming unperformed work is not.
An explicit Owner `off` affects only that task and retains its records.

A different command workdir does not change the native host cwd. Before a new
task initializes deliberate cross-directory development, verify the target and
writer context, then record that task's observed host/target relationship:

```text
<controller> --root <physical-target> --native-cwd <actual-host-cwd> --session-id <actual-task-id> bind-scope --reference <current-host-and-target-evidence>
```

An initialized task remains bound to its verified workspace. This command does
not move an existing task's state or silently retarget its intent.

Do not adopt an existing activation solely because its directory matches. A
new task always initializes its own intent, even when the target has legacy
state. Native callbacks neither parse transcripts nor contact 9700. Scope
mismatch or corruption remains typed unavailable; preserve state and continue
independent safe work. Verify real App lifecycle events separately from a
manual controller or launcher test.

The v4 state and v3 binding replace worktree-local execution state. A verified
v1/v2 binding may import only its matching legacy activation and owner, plus
bounded checkpoints. Original files remain for rollback; the new controller
never writes them. Foreign or mismatched legacy state is not adopted. Restore
the prior package and that exact task's saved scope for rollback, retaining new
receipts. Never downgrade or overwrite another task's records.

Run `status` for the current task. Reuse matching current intent; ordinary
continuation does not need another `on`. Classify changed user requests below.

## 同一任务中的临时插入与目标变化

对新增要求，由 Agent 根据用户原话、当前目标和产出范围判断，不按关键词、时长或领域机械分类。
用户明确追加的相关工作不是擅自漂移；普通继续和进度询问无需创建临时任务。反馈、目标、验收与阶段说明使用中文，保留机器字段、状态码、路径和用户原文。

- 临时插入，且做完应返回主目标：保留主 RUN_INTENT，使用一个临时任务槽；不新建进程、钩子、工作树或任务。
- 用户明确转移任务：说明新目标与原目标的区别，只建议新 worktree 或新任务；不能把建议当作已执行迁移。
- 疑似未经授权的偏移：指出与用户目标的具体差异并建议纠正；不擅自终止整项任务。语义确实不清楚时只问影响判断的一个问题。

临时开始（同一控制器和 `--root`，保留既有 scope 绑定）：

```text
task --kind temporary --goal "临时交付目标" --acceptance "T01=可核验结果" --reference "用户请求或既有证据引用" --resume-objective "完成后回到主任务的哪一步"
```

阶段复核仍用 `review --scope temporary --stage episode`，按原规范传结果、引用和下一步。
完成时必须检查实际产出并执行：

```text
review --scope temporary --stage final --result PASS --reference "实际交付与复核依据"
```

控制器在同一次原子保存中清除临时状态、恢复主目标与下一步；再用 `status` 确认
`temporary_task` 已不存在。无需另行 `off`，不能关闭主 Hook。临时成功不代表主任务成功：
主任务恢复为阶段 `PARTIAL`，旧最终 PASS 不复用。重复临时完成命令不得变成主任务 PASS。
临时视频/文档验收不要求把主任务未完的代码一起提交；仍必须满足临时产出自己的真实验收和独立门禁。

未完成、失败、等待必要输入时记录 `PARTIAL`/`UNVERIFIABLE` 等真实结果，保留临时状态，
不能当作已完成清除。只有用户明确取消才执行 `task --kind cancel --reference "取消依据"`；
取消也会清除临时状态、恢复主任务，但不记录临时成功。中断/恢复后继续同一临时状态；不按超时自动取消。
一次仅保留一个临时任务，不嵌套覆盖；已有临时任务需先完成或明确取消。

`task --kind transfer|drift --reference "判断依据"` 仅输出中文建议，不修改语义状态、不自动创建或迁移。
源码/写入范围需要隔离时建议新 worktree；仅上下文需分开时建议新任务。
新任务若会并行写同一工作树，仍必须使用独立 worktree；临时模式不增加写权限或绕过浏览器/凭据安全检查。

## Activate without ceremony

Derive one concise Goal and stable `ID=observable text` Acceptance list from the
Owner request. Keep implementation choices out of Acceptance unless the Owner
actually required them.

Choose and record the lightest useful review depth by semantic judgment:

- `light`: a short Goal/Acceptance check at each observed boundary;
- `medium`: inspect the completed episode output and relevant diff at each boundary;
- `max`: only when a named criterion needs an existing project-owned targeted
  strict Oracle in addition to semantic review.

When uncertain between light and medium, use medium. Do not classify by elapsed
minutes, file count, domain name, command string, or Oracle name. Do not ask the
user to choose the intensity. Run `on` with Goal, repeated Acceptance, selected
intensity, and a short semantic reason. Then work normally—understand, edit,
test, Git checkpoint, PR or delivery. Native Hooks observe Git HEAD changes,
completed `update_plan` stages, and the next Owner prompt after a reviewed
conversation episode; do not parse command text or add per-tool calls.

## Review semantically

Every observed Git, completed-plan, or new Owner-prompt boundary creates one
coalesced semantic review debt and immediately reinjects RUN_INTENT. A new
prompt closes the previously reviewed conversation episode automatically. When
a meaningful internal stage completes without one of those signals, run one
generic boundary (under required policy, capture the configured evidence packet
instead, as described in the mandatory checkpoint section):

```text
checkpoint --kind stage|episode --label "what completed" --next-objective "what is next"
```

Do not duplicate a boundary already observed through Git or `update_plan`, and
do not encode domain names, S-number lists, file paths, or command patterns in
the controller. At each debt and at the final boundary, inspect the actual
request, current diff or output, tests, and delivered result. Review:

1. Does the result still satisfy the Goal and every Acceptance criterion?
2. Is any decision hardcoded without a traceable Owner requirement, declared
   contract, or unavoidable platform constraint?
3. Is a hidden fallback, stale state, or superseded artifact being treated as
   current truth?
4. Was unrequested behavior added, or requested behavior silently dropped?

Inspect actual module outputs, not just the command that produced them. In the
existing review reference, map the applicable acceptance criteria to observed
results, name unmet/untested criteria, and state the next bounded objective.
Rendering success, a source ledger, hook invocation and a self-assigned PASS do
not establish content, design, business execution or end-to-end quality. An
early episode may pass its own scoped review while the overall task remains
incomplete; never silently promote that episode to final acceptance.

Record an honest result (`PASS`, `PARTIAL`, `FAIL`, `UNVERIFIABLE`,
`CONTRADICTS`, or `UNREQUESTED`) and a durable reference with `review`. For an
episode review, include the next bounded objective. A reference may be a Git
commit, PR review, or project evidence path; do not create a duplicate proof
format. Before recording main-task final `PASS`, create the task's normal Git checkpoint
and verify the worktree is clean. RuntimeHook records that HEAD only to make the
semantic review stale after a later commit or dirty edit; it does not fingerprint
the candidate or artifact.

Use `review --scope main` (the default) for the main task; use `--scope temporary`
while the temporary task is active. A scope mismatch cannot record a review.
`SessionStart` reasserts current Intent. `UserPromptSubmit` also creates at most
one coalesced episode debt when the prior boundary was reviewed. If review debt
is still due at `Stop`, the native Hook requests one continuation so the Agent
must review or report an honest typed non-success state; it does not loop
forever or override an independent project gate. A current episode review is
enough for an interim reply or necessary wait: Stop does not demand final PASS
merely because the turn ends. Claiming completion still requires the applicable
final review. A completed-plan signal alone never clears a temporary task.

If a criterion needs mechanical proof, use the project's existing convergence
or Task Oracle path separately and reference that result. RuntimeHook never
creates or owns its selector, candidate freshness, receipt lifecycle, bindings,
fallback policy, history ledger, or project evidence Stop decision.

## Disable only this layer

When the Owner says `关闭 RuntimeHook`, `这个任务不用 RuntimeHook`, or equivalent,
run `off`. This retains the current local semantic state and makes subsequent
RuntimeHook hooks silent. It does not disable independent credentials,
deployment, publication, security, or PR15 convergence gates.

## 开发检查点与 Jev 必经复核（OpenRouter）

当本机 `CODEX_HOME/runtimehook/policy.json` 已由 Owner 配置 `jev_required: true`，所有新建、恢复及派发任务必须启用本 Skill，
完整读取 [检查点说明](references/checkpoints.md) 和 [Jev 指引](references/jev.md)。在项目已有文档目录配置关键点、
验收关联、要记录的参数和评分量表。架构/技术栈/开源组件选型、关键实现与集成、失败改路、交付都要覆盖。
用 `checkpoint --spec ... --id ... --packet ...` 记录实际证据，再用现有 `review`：它自动调用 Jev，
缺记录、漏掉配置点、旧意见、低分/低置信或未解决异议不能登记 PASS。不得靠 observe、off 或自写 PASS 绕开。
按异议补证据、调研或修复，不无依据重写，也不重复调用刷分；原有 review 引用须说明具体处理。
阶段 PARTIAL 和 API UNAVAILABLE 不是成功，不阻塞独立安全工作。临时任务沿用独立临时目标与检查点，完成后仍必须清除。
本机未启用策略时，下面的显式 assess 保留为兼容/观察入口；安装本身不授权付费或上传私密数据。

未启用 required 策略而单独试用 Jev 时，在已有的重要阶段/最终复核中，先准备脱敏的实际证据片段，
再用同一控制器的 `assess` 检查“声明是否有证据”和“下一步是否服务最新要求”。
调用、输入格式与安全配置见 [Jev 接入说明](references/jev.md)，使用前完整读取。
只在这些判断确有价值时调用，不按每次工具调用或每次小改动收费评判；无关任务不调用。
不要等用户每次提醒；已启用任务由 Agent 在这些边界执行。首次只用 observe，
advisory 也只能建议人工/Agent 复核，不能代替审查者作出验收或操作授权。

输入必须包括最新用户原话、适用 criterion、实际工具/代码片段和来源类别；
只给路径、哈希或自己写的 PASS 不够。不要上传整个会话/仓库、密钥或租户私密数据。
评判当前临时任务时沿用临时目标；明确的用户插入不应被误判为擅自漂移。
同一输入复用现有观察记录，不为了得到好结果反复调用。响应若已过期则弃用。
缺 Key、限流、不可用或上下文不足时标记真实状态，继续原有人工/Agent 复核和安全工作，
不能伪造 Jev 通过、自动反复重试或把整项工作卡住。

Jev 输出只是片段级意见，不是证据真实性保证或独立操作授权；required 策略让它成为成功复核的必要条件而非充分条件。
Agent 必须自行核对其指出的具体 claim/evidence，再用原有 `review` 记录真实结果；
不可机械把 SUPPORTED 映射成 PASS。Ponytail 负责实现简洁但不削减验收，
Codex 代码 review 负责代码问题，3CAN 负责有意义的历史与协调；均不由 Jev 替代。

## Real invocation boundary

The explicit Codex route is `$3can-runtimehook` (or `/skills`), and natural
language may invoke it implicitly. `/3CAN` is product shorthand only; Codex does
not currently expose a reliable custom slash-command registration path, so do
not implement a slash parser.

With required policy, native entry enrolls each task and requires its own
semantic initialization. Without that policy, public installation alone only
provides orientation and optional supervision; it does not authorize paid
uploads. Preserve task isolation and report unsupported host delivery honestly.
