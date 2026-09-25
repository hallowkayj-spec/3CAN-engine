---
name: 3can-runtimehook
description: Keep Owner Intent and semantic review timing stable across long, multi-stage, cross-module, drift-prone Codex work. Use implicitly when this lightweight supervision materially helps, or when the user says RuntimeHook, 开启 RuntimeHook, 按 RuntimeHook 执行, 这个任务用 RuntimeHook, /3CAN, or asks to turn it off. Reviews goal drift, unjustified hardcoding, hidden fallback or stale state, and unrequested behavior without replacing Git or project evidence gates.
---

# 3CAN RuntimeHook

RuntimeHook is a semantic supervisor, not an evidence kernel. Apply it without
asking the user to choose a mode when the task is long, multi-stage,
cross-module, historically drift-prone, or explicitly requests RuntimeHook. Do
not invoke it for every small edit merely because it is installed.

## Locate and inspect

Resolve the physical root with `git rev-parse --show-toplevel`. Use the absolute
`<root>/scripts/3can_runtimehook.py` path and pass that same root through
`--root`. If the script or native project hooks are absent, report
`UNAVAILABLE`; do not create a daemon, parser, graph call, task registry service, or
replacement Hook.

RuntimeHook state is current-task state for one physical Git worktree, not
per-chat state. Never run concurrent RuntimeHook tasks in the same worktree;
use a separate worktree for each concurrent task. A disposable scope cache is
an observation, not a writer lease or task scheduler.

Observe the host's task ID and native cwd; command workdir is not native cwd.
Pass `--native-cwd <observed-cwd> --session-id <host-task-id>` with `--root` to
`on`, which also caches that relation. For an existing verified task or an
Owner-authorized handoff, use `bind-scope --reference <host-and-intent-evidence>`
with those same global arguments. Do not adopt an existing activation merely
because its directory matches. Unknown/stale/mismatched scope emits advisory
feedback without a Stop block or peer-state write. Continue independent safe
work; never overwrite another task's goal to silence a reminder. Native events
use the small `CODEX_HOME/runtimehook/scopes/` cache, not session transcripts or
9700. Optional `--knowledge-worktree` plus `--knowledge-reference` compare a
3CAN handoff at registration; missing knowledge is `UNVERIFIED`, not a gate.
Subagent Hooks may carry the parent session ID; never rebind the parent's cache
to a child worktree. A cache hit proves neither authentication nor a lease.

Run `status`. If the active RUN_INTENT still matches the current Owner task,
reuse it. Before changing Intent, distinguish a temporary insertion from an
Owner-authorized transfer or unrequested drift. Never overwrite a peer or
temporary task with `on`. Git/PR artifacts retain durable engineering history.

## 同一任务中的临时插入

由 Agent 根据用户明确要求和交付范围判断，不按关键词/时长猜测。用户追加相关工作不是擅自漂移。
所有反馈、目标和验收阐述使用中文，保留机器字段、路径和用户原文。

临时插入并预期返回主任务时，复用本控制器：

```text
task --kind temporary --goal "临时目标" --acceptance "T01=可核验产出" --reference "用户要求依据" --resume-objective "返回主任务后的下一步"
```

临时阶段复核用 `review --scope temporary --stage episode`。实际产出达标后用
`review --scope temporary --stage final --result PASS --reference "交付复核依据"`。
该命令原子清除临时状态并恢复主目标；随后 `status` 确认 `temporary_task` 不存在。
不要另行 `off` 或重建主任务。主任务保持阶段 `PARTIAL`，不能继承临时 PASS。
临时产出验收不要求提交主任务未完成代码，但不能绕过任何独立安全/证据门禁。

失败或等待输入保留临时状态并如实记录非成功结果；不得宣称完成或自动取消。
用户明确取消才用 `task --kind cancel --reference "取消依据"`，不把取消记为成功。
一次仅允许一个临时任务，中断/恢复不能遗失；不得嵌套或通过 `on` 覆盖。

任务转移/疑似偏移用 `task --kind transfer|drift --reference "判断依据"` 只给中文建议，
不改状态、不自动新建或迁移，也不强制结束整项任务。写入范围需要隔离时建议独立 worktree；
仅上下文需分开时建议新任务，但并行写同一树仍不允许。语义不清楚才作必要澄清。
主任务复核使用默认 `--scope main`。中途回复或等待输入只需当前阶段复核，
不能把每轮回答结束等同于整体完成；临时完成必须调用对应的最终复核，不能只口头说完成。

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
generic boundary:

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
existing review reference, map applicable acceptance criteria to observed
results, name unmet/untested criteria, and state the next bounded objective.
Rendering success, a source ledger, hook invocation and a self-assigned PASS do
not establish content, design, business execution or end-to-end quality.

Record an honest result (`PASS`, `PARTIAL`, `FAIL`, `UNVERIFIABLE`,
`CONTRADICTS`, or `UNREQUESTED`) and a durable reference with `review`. For an
episode review, include the next bounded objective. A reference may be a Git
commit, PR review, or project evidence path; do not create a duplicate proof
format. Before recording main-task final `PASS`, create the task's normal Git checkpoint
and verify the worktree is clean. RuntimeHook records that HEAD only to make the
semantic review stale after a later commit or dirty edit; it does not fingerprint
the candidate or artifact.

`SessionStart` reasserts current Intent. `UserPromptSubmit` also creates at most
one coalesced episode debt when the prior boundary was reviewed. If review debt
is still due at `Stop`, the native Hook requests one continuation so the Agent
must review or report an honest typed non-success state; it does not loop
forever or override an independent project gate.

If a criterion needs mechanical proof, use the project's existing convergence
or Task Oracle path separately and reference that result. RuntimeHook never
creates or owns its selector, candidate freshness, receipt lifecycle, bindings,
fallback policy, history ledger, or project evidence Stop decision.

## Disable only this layer

When the Owner says `关闭 RuntimeHook`, `这个任务不用 RuntimeHook`, or equivalent,
run `off`. This retains the current local semantic state and makes subsequent
RuntimeHook hooks silent. It does not disable independent credentials,
deployment, publication, security, or PR15 convergence gates.

## 可选 Jev 局部复核（OpenRouter）

当用户启用 Jev 后，在已有的重要阶段/最终复核中，先准备脱敏的实际证据片段，
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

Jev 输出只是片段级意见，不是新的 PASS、Stop 门禁或证据真实性保证。
Agent 必须自行核对其指出的具体 claim/evidence，再用原有 `review` 记录真实结果；
不可机械把 SUPPORTED 映射成 PASS。Ponytail 负责实现简洁但不削减验收，
Codex 代码 review 负责代码问题，3CAN 负责有意义的历史与协调；均不由 Jev 替代。

项目 Kit 的命令在 `<root>/scripts/` 下；Jev 指引中 `<Skill>/scripts/` 路径在此替换为该目录。

## Real invocation boundary

The explicit Codex route is `$3can-runtimehook` (or `/skills`), and natural
language may invoke it implicitly. `/3CAN` is product shorthand only; Codex does
not currently expose a reliable custom slash-command registration path, so do
not implement a slash parser.
