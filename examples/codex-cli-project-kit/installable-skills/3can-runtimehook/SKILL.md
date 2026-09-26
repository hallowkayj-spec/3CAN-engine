---
name: 3can-runtimehook
description: Keep Owner Intent and semantic review timing stable across long, multi-stage, cross-module, drift-prone Codex work. Use when machine policy requires engineering checkpoints, this supervision materially helps, or the user says RuntimeHook, 开启 RuntimeHook, 按 RuntimeHook 执行, 这个任务用 RuntimeHook, /3CAN, or asks to turn it off. Uses the plugin-bundled controller without a project kit; reviews drift, hardcoding, fallback and unrequested behavior without replacing Git or project evidence gates.
---

# 3CAN RuntimeHook

RuntimeHook is a semantic supervisor, not an evidence kernel. Apply it without
asking the user to choose a mode when the task is long, multi-stage,
cross-module, historically drift-prone, or explicitly requests RuntimeHook. Do
not invoke it for every small edit merely because it is installed. An
Owner-enabled `jev_required` machine policy instead requires this Skill for
development, including small tasks with a proportionately small checkpoint.

## Apply the 3CAN fast path

The Plugin's `SessionStart` Hook provides a stateless orientation even before
RuntimeHook activation. Start safe local work immediately. Git owns exact
source state; use 3CAN route or retrieval only when durable project meaning
improves the decision. Obtain a fresh ticket just in time only for an operation
whose current project contract requires one, with exact Agent, project,
workspace/worktree, Workorder, target, and scope bindings. Honor the returned
TTL and completion deadline. Never blind-retry a typed refusal: refresh expired
state once only for the still-pending operation, reread a version conflict, and
stop on an identity or digest mismatch. Durable writeback defaults to a
meaningful `AUTO_CLOSEOUT` or explicit `OWNER_REQUESTED` checkpoint.

## Locate and inspect

Use `scripts/3can_runtimehook.py` inside this loaded Skill directory as the
controller. Resolve its absolute path from the Skill location; do not require or
copy a controller into the target repository. Resolve the physical worktree with
`git rev-parse --show-toplevel` and pass that exact root through `--root`.
If Git, Python 3, or the bundled controller is unavailable, report
`UNAVAILABLE`; do not create a daemon, transcript parser, graph call, task
registry service, or replacement Hook.

The first `on` in a repository adds only `/.codex/runtimehook/` to that
repository's local Git exclude when no existing ignore already covers it. It
does not edit tracked project files. A tracked, redirected, or unsafe state root
remains `UNAVAILABLE`.

RuntimeHook state is current-task state for one physical Git worktree, not
per-chat state. Parallel writers still require separate worktrees. The small
host-scope cache described below is an observation, not a lease or task scheduler.

Separate command workdirs do not change a task's native Hook cwd. Before first
activation, after moving project work, or when another task's Intent appears,
obtain the native task cwd from the host's task metadata (not the command's
workdir). The observed host cwd and the verified development worktree may
differ: do not require folder cleanup, move the App task, or copy state just to
make them equal. Pass `--root <physical-root> --native-cwd <observed-cwd>` to `on`;
`--session-id` defaults to the host's `CODEX_THREAD_ID` when available. This
records a same-worktree scope as part of activation. For cross-directory use,
first verify the target Intent/writer and run `bind-scope` below, then `on`
only if a new main Intent is actually needed. For an existing verified task or
an Owner-authorized handoff, retain its state and record only the relationship:

```text
<controller> --root <physical-root> --native-cwd <observed-cwd> --session-id <host-task-id> bind-scope --reference <host-observation-and-current-intent-evidence>
```

Do not adopt an existing activation solely because its directory matches;
verify that its current Intent belongs to this task. A new task ID must not
automatically inherit the old task's activation. Subagent Hook payloads may
carry the parent session ID; never replace the parent's cache with a child
worktree. Report unsupported automatic supervision and continue scoped work.

The disposable cache under `CODEX_HOME/runtimehook/scopes/` contains only the
observed task/root/activation relation and evidence pointers, not goals,
priorities, tickets or execution state. Already-bound native events do a local
lookup without Git discovery, transcript scanning or 9700. New tasks, changed
host anchors, worktrees or activations require a new observation. A non-Git
host cwd matches exactly; a Git host also permits subdirectories of its same
observed worktree. Native events then use only the verified target, never the
host peer's semantic state. At registration only,
relevant 3CAN handoff evidence may be compared using `--knowledge-worktree` and
`--knowledge-reference`; absent knowledge stays `UNVERIFIED`, not a failure.
A historical `CONTRADICTS` is advisory, not an identity authority or a reason
to skip Jev. Verify the current mapping, then update durable meaning through
the normal bounded 3CAN writeback at handoff/closeout, not on every Hook event.

Unknown/stale/mismatched scope gives advisory feedback without a Stop block,
without adopting or changing peer state. Do not finish the entire task merely
because of that feedback: report it and continue independent safe work. A
cached match is not authentication, a live 3CAN check or a writer lease. The
existing `--native-cwd` check also protects pending local state writes; neither
check rebinds the native task.
If the native cwd cannot be verified, keep automatic supervision unverified
and continue safe local work. Do not guess it, edit Session databases, start a
second writer, or switch off/replace foreign Intent. Use a supported host
binding repair, then confirm the actual native event's Worktree and activation
after resume. Manual controller success is not native Hook acceptance.
Old v1 scope caches remain same-worktree-only until explicitly rebound; v2
separates host and target. Older plugin versions cannot read v2: keep them for
already-running tasks, but reload the updated plugin for a newly rebound task.

Run `status`. If the active RUN_INTENT still matches the current Owner task,
reuse it. Classify an intervening request with the rules below before replacing
Intent. Use `on` for a verified main-task change only, never to overwrite a
temporary or peer task. Git/PR artifacts retain durable engineering history.

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

当本机 `CODEX_HOME/runtimehook/policy.json` 已由 Owner 配置 `jev_required: true`，开发任务必须启用本 Skill，
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

Before activation the stateless 3CAN fast path also reports an enabled Jev
requirement; no online call or task state is created by that event. Installing
the Plugin alone does not authorize paid uploads. Activate when supervision
materially helps, the Owner asks, or an Owner-enabled machine policy requires
development checkpoints. Preserve scope isolation in every case.
