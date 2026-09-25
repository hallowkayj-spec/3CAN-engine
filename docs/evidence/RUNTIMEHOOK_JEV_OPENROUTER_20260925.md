# RuntimeHook / Jev：OpenRouter 接入替换

## 决策与边界

Owner 已改用 OpenRouter 并报告充值完成；本次不再诊断或修改付款网络。
换出口付款成功不能单独证明是某个 ASN 被 Stripe 封禁。

最小实现：替换现有 Jev 标准库 HTTP 适配器，不引入第二个 provider 路由、SDK、
后台进程、自动降级、任务状态机或新的 Stop 门禁。Ponytail full 用于缩小实现范围，
不削弱已有 scope、过期结果检测、凭据隔离和独立项目验收。

四个原生生命周期 Hook 和语义控制器未改；它们仍不调用在线模型。
Jev 仅在已有的显式 `assess` 边界读取调用方准备的脱敏片段，意见不能代签 PASS。
当前主任务的 native cwd 与已登记 worktree 不匹配，保持 `UNAVAILABLE / CONTEXT_MISMATCH`，
没有修改、冒用或关闭任何任务的语义状态；不影响这个源代码模块的安全工作。

## 官方证据到本地实现

2026-09-25 打开的主要证据：

- [OpenRouter Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)：
  原生请求为 model/state/questions；响应模型示例是带日期的 Jev 版本，usage 使用蛇形字段并报告美元 cost。
- [官方 Jev 教程](https://openrouter.ai/blog/tutorials/how-to-use-jev/)：
  用声明式选择题判断给定内容，不把它当成执行工具或任务主控。
- [Jev 1.13 模型页](https://openrouter.ai/typesafe/jev-1.13)：核对实际模型 ID 和当前定价；账单仍以真实请求为准。
- [Codex 插件本地安装](https://learn.chatgpt.com/docs/build-plugins#create-a-skills-only-plugin-manually)：
  使用已有 marketplace 和原生插件安装，不手写宿主配置注册项。

选定 `POST /api/alpha/decisions`、`typesafe/jev-1.13`，禁用 provider fallback。
这是 alpha API；不宣传稳定 GA，也不把模型名等同于不可变的内部权重。

反例：仅把旧 Vercel URL 换成 OpenRouter 会留下错误模型名、camelCase usage 和错误凭据来源。
本次同时对齐模型 ID、日期快照响应、input_tokens/output_tokens/cost、独立 OPENROUTER_API_KEY
和独立 Windows DPAPI 文件。既不把 Vercel Key 发往 OpenRouter，也不丢失实际模型或费用。

意见协议升级为 `3can.jev-opinion/v2` 以区分新回执格式；旧缓存不复用，主语义状态无迁移。
响应不完整、负数/非有限费用、版本不符、401/402/429、重定向和网络失败均保留 typed 状态；
不自动重试、购买或转用另一个模型。缓存里的 usage 是原请求消费，不是缓存命中的新增消费。

## 验证状态

- OpenRouter focused tests：51 passed（42.32 秒）。包括真实格式快照的解析/缓存往返、
  禁止旧 Key 跨 provider、余额不足单次失败、畸形账单拒收和既有 scope/并发变化反例。
- 首次完整 RuntimeHook 模块回归：106 passed、2 skipped、12 fixture setup errors。
  单独加载 Jev 测试时隐式注册共享测试模块有效；共同收集时共享 fixture 不可见。
  已改为显式复用两个既有 fixture，不改生产控制器。相关联合反例 8 passed；
  最终完整模块回归 **118 passed、2 skipped（363.91 秒）**。
  两项跳过分别是本机缺少创建目录符号链接权限、非 POSIX 宿主；不把跳过算作通过。
- Ruff、插件 manifest 验证、两份 Skill 验证通过。Skill 校验程序默认 GBK 读取中文曾失败，
  使用 Python 原生 UTF-8 模式后通过；没有为此修改全局编码或加入运行时兜底。
- live provider：`PENDING_CREDENTIAL`；没有真实 Jev 调用或质量通过结论。
- 本机插件安装：`LOCAL_INSTALLED_LIVE_PENDING`；版本 `0.1.8-rc.1+codex.20260925123828`，
  原生插件目录复查 enabled=true，来源仍是既有本地 marketplace。
  11 个安装文件逐个 SHA-256 一致；39 个旧文件保留，4 个旧版本可供已有任务使用。
  安装前后宿主 config.toml 哈希一致；不改网络、9700/9711、任务归属或任何语义状态。
- 原生宿主事件 E2E：`NOT_VERIFIED`；离线测试/插件 enabled 标签不等于原生事件已实际加载。

本地证据文件（仓库 output，未纳入公共源码）：

- `runtimehook-jev-openrouter-focused-20260925.xml`
- `runtimehook-openrouter-regression-20260925.xml`（首次错误保留）
- `runtimehook-openrouter-fixtures-20260925.xml`
- `runtimehook-openrouter-regression-fixed-20260925.xml`（最终完整回归）
- `runtimehook-openrouter-install-20260925.json`（安装、备份、哈希回执）
- `install-runtimehook-openrouter-20260925.ps1`
- `probe-runtimehook-openrouter-20260925.py`：默认 PLAN_ONLY，不联网；显式 --run 最多 7 个合成请求，
  首次传输/格式失败即停、不重试，结果不覆写；无实际项目内容或凭据日志。

## 接下来与回滚

保存专用小额限额 OpenRouter Key，再跑合成接入样本；分别报告类别分歧、真实延时与账单，
不把少量合成用例当成生产准确率、成本收益证明或新硬门禁的依据。
真实任务仍需核对当前宿主目录/任务绑定；新插件应在新任务或安全重开后确认实际加载，
不能宣称正在运行的旧任务已热更新。
本次没有修改 AGENTS.md 或 3CAN.md：外部 provider 切换属于插件接入说明，不应膨胀成全局强制研究/收费规则。
本机磁盘低于 20 GiB 安全线，已做小范围只读检查；未新建工作树、安装第三方依赖或进行重型构建。

停止 `assess` 即可关闭付费层，不停原生 Hook、3CAN 或其他工作。
保留旧插件文件供已有任务使用；需要回滚安装时使用备份和支持的插件流程，不直接改宿主数据库。
旧 Vercel 凭据不迁移、不读取、不删除；OpenRouter 远端撤销和本地加密文件删除是不同操作。
