# RuntimeHook / Jev Gateway：最小接入复核

日期：2026-09-25。Workorder：RUNTIMEHOOK-JEV-GATEWAY-20260925。
Owner：3CAN RuntimeHook。起点 `9e5d7dacea10b37dc191ccc9980fe01d1c19ee17`，
分支 `feat/foundation-convergence-20260910`。候选插件 `0.1.8-rc.1`。

## 结论与边界

附件指出的限制真实存在：Agent 自填 PASS 不是独立审查；现有 Hook 不理解连续失败和
最终回答中的夸大。此次没有把它们伪装为“已经由更严格的状态机解决”。
仅增加可选 `assess`：按明确目标、最新请求和真实片段做局部第二意见，
检查声明—证据、下一步—目标。原生四个事件仍离线，原 semantic state schema、
临时任务收尾、Stop 和项目证据门禁不变。Jev 结果不能直接签署 PASS 或下发工具动作。

范围不包括：全会话读取、自动取证、RPA 执行、视频审美验收、3CAN rerank、
重复失败监控、daemon、模型路由器或第二套 candidate kernel。

## 从研究到选择

问题：在 TypeSafe 注册受限、现有控制器为 Python 的本机条件下，如何合法接入并控制延迟和权限？

- [Vercel Evaluation HTTP](https://vercel.com/docs/ai-gateway/modalities/evaluation)：
  新代码直接用 `/v1/evaluate`，choice 契约足够。不采用 Chat Completions，不引入 Node/AI SDK。
- [TypeSafe 兼容 API](https://vercel.com/docs/ai-gateway/sdks-and-apis/typesafe)：
  适合迁移旧 SDK；本项目没有旧 SDK，故不并存两条传输路径。
- [官方 API Key](https://vercel.com/docs/ai-gateway/authentication-and-byok/api-keys) 与
  [额度](https://vercel.com/docs/ai-gateway/pricing)：用专用 Gateway Key、账户原生预算，
  不把账户管理 Token、BYOK 或 Coding Agent 全局改路由作为前置要求。
- [Jev jaggedness](https://docs.typesafe.ai/model-jaggedness/jev-1.13)：
  typed choice 不能证明语义正确，文本内注入、缺上下文和复杂推理仍有风险。
  选择小片段、预定义选项；不据概率自动授予权限。
- [Codex PostToolUse](https://learn.chatgpt.com/docs/hooks#posttooluse)：
  宿主支持生命周期反馈，但这不要求每次工具调用都联网。本轮不更改事件 matcher。

反例：测试成功但声称线上部署完成；无字幕证据却声称视觉合格；用户明确插入的视频任务；
评判中 activation/HEAD/证据变化；HTTP 429；返回任意命令；Agent-only PASS。
验证：离线传输/隔离测试、实际 Windows DPAPI 假密钥往返、既有组件回归；
真实模型准确性必须用安全配置后的独立标注样本另验，不用 mock 的 SUPPORTED 冒充。

## 实现与简化审查

- 一个延迟导入的标准库适配器，一个已有 CLI 子命令。没有新依赖、服务、后台进程或 native Hook。
- `state.json` 完全不写；一份 `jev-observation.json` 是可替换意见缓存，不是执行状态。
- 本地任务/工作树身份不上传。仅发送经 Agent 脱敏的目标、criterion、最新请求和所选片段。
- 相同输入/绑定/HEAD 复用意见；调用中变化丢弃结果。不证明未包含的文件或遗漏证据。
- 单次网络尝试，有限输入/输出、socket timeout、无重定向或自行回退；失败只报告 typed 状态。
- 输出引用由本地映射，模型不能增添引用/命令；概率不是真实性保证。
- Windows 凭据使用系统 DPAPI，通用环境变量由用户自己的 Secret Manager 注入。
  Python 从 PowerShell 7 启动 Windows PowerShell 5 时隔离继承的 PSModulePath，
  防止实际复现的模块类型数据重复加载错误；不修改宿主全局环境。
- 按 Ponytail 检查：未引入 provider interface、通用凭据管理器、自动重试、数据库或新状态机。

全局 AGENTS.md 与 3CAN.md 的现有“输出对目标复核、PASS 不等于证据、按需取证、
安全本地工作不被 unavailable 阻塞”已经覆盖本功能；此次不追加全局研究/模型调用硬门槛。
具体用法留在插件 Skill 与按需读取的 `references/jev.md`，避免全局提示词继续膨胀。

## 验证状态

- 既有 RuntimeHook + 新增初始适配器组件回归：97 passed，2 skipped，323.21 秒。
- 新增对抗性用例覆盖：同输入复用、上下文中途变化、临时意图、负面意见不阻塞、
  缺密钥、单次失败、非法结构、原生事件无网络、大小限制和加密凭据往返。
- 初测发现并修正：Windows PowerShell 模块路径继承问题；JevError 被宽泛 ValueError
  捕获导致 RESPONSE_TOO_LARGE 错标 JSON；大 bytes 参数被 pytest 展开为过长临时目录名。
  这些是本轮集成/测试问题，不属于 Jev 语义准确性结果。
- 源码/插件一致性、最终专项计数和安装回执保留在项目 `output/` 的本机交付证据。
- 当前任务原生 cwd 与已登记工作树不匹配，保留 `UNAVAILABLE / CONTEXT_MISMATCH`；
  没有改绑、冒用其他任务 state，或把手动组件测试当作 App 原生事件验收。

实际 Gateway 认证/费用、模型误报漏报、生产任务自然触发与原生事件均须分别验收。
在成功获取真实响应前，在线能力状态为 `PENDING_LIVE_CREDENTIAL_TEST`，不能写“全面上线有效”。
账户注册、API Key 创建与本地安全保存也不是 Jev 调用成功的证据。

## 回滚

在线层 off 不影响现有监督；回到 0.1.7 无语义状态迁移。保留旧缓存包以免影响已加载任务。
独立任务不能被重启/暂停；本轮不动 9700/9711/17890，不合并远端 PR、不改现有业务工作树。
如要删除本地凭据，只删除指定加密文件；在 Vercel 撤销 Key 才取消远端权限。
