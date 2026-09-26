# RuntimeHook：工程检查点与 Jev 必经复核

## 结论与模块边界

Owner 本轮明确要求开发检查点及 Jev 常态必经，不再只是可选建议。实现归属是
RuntimeHook 的既有 `checkpoint → review` 路径；没有增加后台服务、模型路由、任务调度器、
3CAN 执行状态或第二个 Stop Hook。源代码模块在 foundation-convergence 工作树开发，
不涉及 Runtime 9700/9711 的源码或生命周期，也没有修改其他任务的工作树/语义状态。

状态：`COMPONENT_VERIFIED / REAL_PROVIDER_SIMULATION_OBSERVED / NATIVE_BUSINESS_E2E_NOT_VERIFIED`。
目前主任务宿主 cwd 不是登记的源代码工作树，仍是 `UNAVAILABLE / CONTEXT_MISMATCH`。
没有伪造 native cwd 或用基准测试的临时任务冒充真实宿主任务。

## 第一性原理与已打开的外部证据

| 证据 | 对当前工程判断的影响 |
| --- | --- |
| [OpenAI Stop Hook](https://learn.chatgpt.com/docs/hooks#stop) | 复用一次 `decision:block` 续接以及 `stop_hook_active` 防循环；这是让 Agent 继续复核，不是操作系统写入禁令。 |
| [OpenRouter Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request) | 使用已有原生 choice/score、confidence、probabilities、usage；评分是连续期望值，不能把“等于满分”硬编码成成功。 |
| [TypeSafe Jev 1.13 jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13) | Jev 擅长给定材料的限定判断；数值/结构不变量由代码验证，外部真实性和复杂工程验收仍要独立证据。低置信不是代码已错的证明。 |
| [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | 其 checkpointer 用于图执行状态与恢复；本需求是工程边界证据，不引入另一套执行持久化框架。 |
| [OpenTelemetry span events](https://opentelemetry.io/docs/concepts/signals/traces/#span-events) | 借用关键时点附带少量结构化属性的思路；当前只需小记录，不引入 collector/遥测平台。 |

反例与验证：只有“pytest passed”不能证明上线；只有 render exit 0 不能证明设计；
清单 prepare 成功不是 RPA 已执行。除单元反例外，真实 Provider 模拟测试直接走
`activate → checkpoint → review 自动 Jev → 拒绝或记录 PASS`，不只测适配器能否返回 JSON。
Ponytail full 影响：使用标准 JSON 而不是加 YAML 依赖；只复用一个在线适配器和现有控制器。

## 实际落地内容

- 一份版本化项目说明：检查点 ID、验收 ID、实际参数名、评分量表与阈值。由具体项目的真实调用链选择位置，
  包括架构/依赖定案、关键模块/集成、失败改路、交付；小任务合并相关点，不逐工具调用。
- 配置必须覆盖当前全部 acceptance；最终成功复核不能跳过说明中尚未成功复核的点。
- 记录实际输入参数、证据片段、目标/说明摘要和窄 Git HEAD/boundary 关联；主/临时记录分开。
- `review` 自动调用 Jev，并保存评分、置信度、支持概率、实际模型版本和费用。
  没有检查点、跨任务/旧目标/旧代码、低分或异议不能签 PASS。API 不可用也不能假装通过。
- 同一记录反复 review 不重复收费；负面和不可用意见也保留。不通过就补证据/研究/修复后建新的真实记录，
  不是反复调用刷出好分。不要无依据重写正确代码，也不把模型结果当用户授权。
- 原生四个事件仍离线。进入复核前必须由 Agent 读取实际输出并提供脱敏包；Jev 不会自动看完整仓库、视频或电脑。
- 一份 Owner 授权的机器策略 `CODEX_HOME/runtimehook/policy.json` 设置 `jev_required=true`。
  对公开安装者，安装本身不代表同意上传或收费。既有 `assess` 是诊断入口，不清除必经复核。
- 全局 AGENTS.md 与 3CAN.md 替换原有相应条款，不添加另一个治理系统。

详细格式、命令及回滚见 [检查点使用说明](../../plugins/3can-runtimehook/skills/3can-runtimehook/references/checkpoints.md)。
目前最大 32 个配置点，每包最多 32 KiB，当前点记录最多 64 KiB；不保存原始大视频或全会话。
每 ID 保存最新记录。有价值的历史失败和最终结果进入原有 evidence/Git，再由 3CAN 保存持久含义。

## 测试：不隐藏首次失败

- 首轮新增 focused：19 passed，129.40 秒。
- 增加临时任务清除与旧 PASS 恢复反例后的全模块回归：**133 passed、6 failed、2 skipped**，496.53 秒。
- 六个失败均由新 sibling import 生成 `.pyc`，使干净 Project Kit 变 dirty；Git final gate 拒绝是正确行为。
  通过 `sys.dont_write_bytecode=True` 禁止 Hook 产生该副作用，没有放松干净工作树门槛或忽略用户文件。
- 修复后完整兼容模块：**22 passed、1 skipped**，136.72 秒；包含上述六个原失败。
- 后续插件 launcher/引导 focused：**5 passed**，15.02 秒。最初一个名称过滤没有选中测试，未计为通过。
- 新检查点测试涵盖自动调用、缓存不重复调用、低评分/置信/概率、API 不可用、漏点、证据缺失、跨目标、
  Git/说明/证据过期、复核期间并发变化、主/临时隔离和临时完成清除、旧 PASS 恢复不得免 Jev。
- Ruff、两份 Skill 校验、`git diff --check` 通过。跳过项属于本机 symlink 权限/POSIX 条件，不算通过。

## 真实 Provider 模拟：24 例，保留误判

代码：`neural-memory/benchmark/runtimehook_checkpoint_benchmark.py`。
默认 PLAN_ONLY；显式 --run 最多 24 次，首次传输/格式失败就停，无重试、无第二 provider，
费用达到 $0.01 后不再开始下一次（不是 Provider 侧硬预算上限）。
仅上传预先声明的合成片段，不上传真实任务会话、凭据或租户资料；临时 Git/Hook 状态独立创建并清除。

| 指标 | 本次观察 |
| --- | --- |
| 真实请求 / 格式有效 | 24 / 24 |
| 限定声明标签完全匹配 | 23 / 24（95.8%），不是生产准确率 |
| 不应通过的反例被签 PASS | 0 / 14 |
| 支持案例未签 PASS | 3 / 10 |
| 本机检查点捕获中位数 | 1,037.216 ms，包含 Git 和状态校验 |
| 自动 review 中位数 | 3,133.302 ms，包含 Git、凭据、网络和记录 |
| Provider 报告总费用 | $0.001556562 |
| 实际模型 | typesafe/jev-1.13-20260917 |

三个支持案例为准备态 RPA、显式零值保留、中文往返；均出现低置信或评分，不能宣传无误拦。
另一个标签差异为缺少用户验收证据：预设 INSUFFICIENT_CONTEXT，实际 CONTRADICTED；未被放行。
本轮未调低阈值、修改预设标签或重复付费请求来追求满分。

这验证了“会真实调用、负面意见影响 PASS”，没有证明 Jev 能独立判断所有架构/代码，更没有证明
它能取代单元测试、源码 review、人工视觉比较、UAT/E2E。阈值仍是筛查策略，未做生产校准。
完整操作尚未达到几毫秒；阶段级调用目前是秒级，不能用一个小 JSON 的写盘时延替代总体开销。

## Owner 要求提交前的再次测试（2026-09-25）

本轮不修改运行机制或降低阈值。完整复跑四个 RuntimeHook 模块：
**139 passed、2 skipped，378.53 秒**。这次是修复字节码副作用后的一次完整全绿运行；
范围是 RuntimeHook 四模块，不冒称整个 3CAN 仓库已经全部本地测试通过。
两个跳过项保留平台条件。发布必需文件清单补齐 Jev/检查点模块、文档和测试；清单测试 **17 passed**。
全仓 Ruff（含插件脚本）和 `git diff --check` 通过。

给既有模拟程序增加 `--plugin-root`，可指定实际安装包，每个进程只载入一份；
回执记录版本及五个关键文件的 SHA-256。默认 PLAN_ONLY，不会因为指定路径就安装、调用或重绑任务。
测试助手首次执行曾因读取控制器不存在的 `jev` 属性失败，发生在取 Key/网络请求之前；
改为从同一包导入适配器后执行，未重跑已经获得的负面意见。

从安装包 `0.1.9-rc.1+codex.20260925133943` 执行同一预声明的 24 例：

| 指标 | 安装版再次测试 |
| --- | --- |
| 真实请求 / 有效返回 | 24 / 24 |
| 限定声明标签完全匹配 | 23 / 24 |
| 不应通过的反例被签 PASS | 0 / 14 |
| 支持案例未签 PASS | 3 / 10 |
| 本机捕获中位数 | 832.020 ms |
| 自动 review 中位数 | 2,596.056 ms |
| 本轮 Provider 费用 | $0.001556562 |

支持但未通过的案例仍是准备态 RPA、显式零值保留和中文往返；缺验收证据的分类差异仍保留。
这不是新的独立生产数据集，不把重复同组模拟当泛化准确率验证。
13 个安装文件与源码逐一 SHA-256 匹配。
用实际安装包的 `commandWindows`、隔离 Git/配置和合成身份执行 SessionStart、UserPromptSubmit、
PostToolUse、Stop，以及已续接 Stop：五次输出均有效中文；未复核 Stop 续接一次，第二次不循环；
回调在无 Key 的隔离环境完成，工作树保持干净，临时夹具已清除。
这是 **INSTALLED_LAUNCHER_SIMULATION**，不是宿主自动发事件或真实业务 E2E。

本地回执：

- `output/runtimehook-checkpoints-retest-20260925.xml`
- `output/runtimehook-checkpoints-package-retest-20260925.xml`
- `output/runtimehook-checkpoints-installed-retest-20260925.json`，
  SHA-256 `fa52c99767a71e153dea3a7edd6ea0993c84847780dec6d7c1f1bc9c6769c597`。
- `output/runtimehook-installed-launcher-retest-20260925.json`，
  SHA-256 `35db4a76f149b4d3d4fe2a4ab78c381ed71cff72762ed12d5651bdd5badf3e48`。

工作目录直接 strict 扫描曾发现 ignored `output/` 中三个本机安装助手的用户路径，未将其当发布 PASS。
这些运维回执/助手保留在本地，不加入 Git。发布前从精确 commit 用现有 builder 导出并 strict 扫描归档；
归档和 CI 的最终结果以 PR 当前 HEAD 的实际回执为准，不删除本地证据或放松扫描规则。

## 本机安装与生效范围

- 已安装 `0.1.9-rc.1+codex.20260925133943`；13 个源码文件逐一 SHA-256 匹配。
- 保留 5 个旧版本共 52 个文件；宿主 config.toml 安装前后哈希相同。没有重启 App 或其他进程。
- Owner 策略已启用，全局指引已更新。已有任务不会被直接激活、迁移或改写；新/安全重开的任务需核验新版。
- 本任务 native binding 仍不匹配，自动监督 `UNAVAILABLE`，这是保留的部署/真实业务验收缺口。
  测试中传入的合成 native identity 仅是隔离模拟，不拿来宣称原生宿主 E2E。
- 9700 与 9711 不需要为了此插件更新而重启。生产运行时和插件是不同部署面。
- 首次安装时尚未 push；Owner 随后要求 commit/PR，本次更新沿用
  [Draft PR #17](https://github.com/hallowkayj-spec/3CAN-engine/pull/17)。以远端 HEAD/CI 为准；
  不 merge，不创建正式 release，本机安装不等于公共 main 更新。
- 本地插件更新后应在安全边界重启桌面 App，然后检查当前 Hook 信任和真实事件，见
  [官方本地安装指南](https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually)。
  App 重启不能修正任务保存的 cwd；当前任务 `CONTEXT_MISMATCH` 仍须单独处理，不能借他人状态。

## 证据与回滚

本地 `output/` 保留 focused/full/compatibility/launcher JUnit、真实 Jev JSON、安装 JSON 和部署脚本。
真实请求数据集摘要：`036096105d63381214eb786dfa6a7a1efaa835fcc5be3c6e0839704c934fccee`。
安装备份在 `CODEX_HOME/backups/runtimehook-checkpoints-20260925-095618`，包括旧插件、原 config/AGENTS/3CAN。
原 policy 不存在。若 Owner 明确决定回滚，先将 policy.jev_required 设 false 停止新付费要求，保留 v3 现场；
不得让旧控制器忽略/降级已有 v3，也不得改其他任务状态。全局文档只回滚本次条款，不能覆盖后续改动。
功能源码可按本模块提交 revert；公共分支/线上服务不在本次变更范围。

后续真实试用关注：是否实际调用、调用耗时、误拦、遗漏、可行动发现，以及 Agent 是否完整提供了证据。
这些数据未有新的 P0/P1 前，不扩建第二调度/审批状态机。
