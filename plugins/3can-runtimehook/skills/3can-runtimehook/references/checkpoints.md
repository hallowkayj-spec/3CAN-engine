# 开发检查点与 Jev 必经复核

检查点是关键工程位置的参数、实际输出与验收记录，不是暂停进程的 debugger、全盘快照或新的任务调度器。
复用四个原生事件、一个语义状态和既有 `checkpoint → review`。没有网络服务、第二队列或 9700 依赖。

## 必经位置

本机启用 required 后，开发任务必须使用本 Skill；不可用的范围如实报告，不能借其他任务状态。
在项目现有文档目录建立一个小 JSON 说明（标准 JSON，无新的 YAML 依赖），由 Agent 根据真实链路选择位置：

- 架构、技术栈、外部开源组件或 API 契约定案前：比较已打开的当前证据、已有实现和替代方案。
- 关键模块实现和接口/数据交接后：记录实际参数、异常分支与测试输出。
- 重复失败后改变路线、隐式 fallback 或需求/范围变化时：先核对最新要求和根因。
- 交付前：核对声明、未完成项和 UAT/E2E 真实结果。测试通过、部署成功、业务验收分别判断。

一个小修复可合并相关位置；不能为了省事省略实际适用的环节。内部循环不逐工具调用模型。
配置必须覆盖当前主/临时目标的全部 acceptance ID。所有配置点均必经，最终点不能跳过前面未成功复核的点。
这不自动发现项目真实的所有关键位置：Agent 必须对照调用链设计，架构检查点的 Jev 应同时审查覆盖是否遗漏。

## 说明格式

路径示例 `docs/runtimehook-checkpoints.json`；参数名、验收关联和评分量表由项目决定，不硬编码视频镜头或 RPA 主机。
下面是最小单点示例，实际多模块任务应展开适用的关键点：

```json
{
  "schema": "3can.checkpoints/v1",
  "final_checkpoint": "parser-delivery",
  "checkpoints": [{
    "id": "parser-delivery",
    "title": "解析修复、异常分支与交付声明核对",
    "criteria": ["A01"],
    "parameters": ["passed", "failed"],
    "rubric": [
      "缺少真实证据，或结果明确违背本检查点验收",
      "有实现依据，但本检查点仍有重要未验证或未解决问题",
      "实际证据覆盖本检查点全部验收，声明与验证范围一致"
    ],
    "minimum_score": 1.5,
    "minimum_confidence": 0.65,
    "minimum_probability": 0.7
  }]
}
```

阈值是项目筛查策略示例，不是已经校准的可靠性标准。不要为得到 PASS 降低阈值；有理由调整时审查并提交配置变更。
量表从 0 开始；minimum_probability 指达到量表门槛的概率总和，以及各声明/下一步的支持概率。
confidence 保留 Provider 原字段，表示分布集中程度，不冒称判断正确概率或整体产品质量百分比。
多 criterion 的检查点在同一次请求中逐项独立评分，并返回 criterion/evidence 映射；每项均须满足原门槛，不用平均分盖过失败项。
量表应描述当前环节的证据支持程度，不同时评代码质量、下一步相关性和未来部署。未属于本检查点的后续验收仍保留，不提前宣称完成。
最多 32 点、量表 2–7 级、每次证据包 32 KiB；参数只保存少量标量，原始日志/大媒体留在原有产物目录并提取相关片段。

## 执行闭环

完整阅读 [Jev 指引](jev.md)，使用同一控制器，所有命令都带真实 `--root --native-cwd --session-id`。
按 Jev 原有 packet 格式准备最新用户要求、claims、实际 evidence、next_step，另加 `parameters`：
每条 claim 限定一个结论及其直接证据；不要让一个“全部正常”摘要代理多个模块的审查。没有必要评判下一步时可设 `next_step: null`，
但不能用它删除已经发现的异议或逃避范围检查。Jev 只做片段级第二意见，主审仍须打开真实源码、检查调用链和业务结果。

```json
{
  "latest_user_request": "修复解析并测试，不部署。",
  "claims": [{"id": "C1", "criterion_id": "A01", "text": "三个解析用例通过。", "evidence_ids": ["E1"]}],
  "evidence": [{"id": "E1", "kind": "tool_output", "excerpt": "3 passed, 0 failed"}],
  "next_step": "交付本地修复供审查，不声称部署完成。",
  "parameters": {"passed": 3, "failed": 0}
}
```

```text
<controller + scope> checkpoint --spec docs/runtimehook-checkpoints.json --id parser-delivery --packet output/parser-evidence.json --next-objective "复核解析修复"
<controller + scope> review --stage final --result PASS --reference "实际代码、测试和结论的既有复核文档"
```

`checkpoint` 只做本地校验与小文件记录，返回实际 `local_elapsed_ms`，不联网。
`review` 自动调用 Jev（不需要 Agent 先记得运行 assess）。每个新记录一次调用；重复复核复用该记录的意见。
记录保存实际评分、置信度、概率、模型版本、费用、异议和 review 引用。低分/异议不得登记 PASS。
在既有 review 文档中解释问题及所做修复；重新取得证据、记录修复后的检查点，再调用 review。
低置信/证据不足先补证据或调研，不能直接推断代码一定错，更不能无依据推倒重写。
不同意 Jev 时保留反证及 PARTIAL，交 Owner 决定；不以自写“已处理”字符串绕过门槛。
网络/余额/缺 Key 等失败保留 UNAVAILABLE；同一记录的重复 review 不循环请求。诊断原因后在新的真实检查点再尝试。
`PARTIAL/FAIL/UNVERIFIABLE` 可以诚实收口或等待输入，不代表验收完成；未成功的配置点仍阻止最终 PASS。

原生 Stop 在待复核时强制一次续接，复用宿主 `stop_hook_active` 防止无限循环；回调自身不联网。
它不是 OS 沙箱，不能阻止同权限程序篡改文件或绕过工具发话。最终仍要检查真实业务结果，不能把 Hook 通过当作 UAT。
本地记录和 Jev 在线请求分开计时，网络请求通常为秒级；不把本地毫秒记录宣传成毫秒级模型判断。

## 授权、状态与回滚

安装插件不等于获准向第三方上传或付费。Owner 确认后，在 `CODEX_HOME/runtimehook/policy.json` 设置：

```json
{"schema":"3can.runtimehook-policy/v1","jev_required":true}
```

这是一份小的机器配置，不保存 Session 优先级或项目状态。只有 Owner 可以要求停用或改变该策略。
配置错误不能静默降级；设为 false 后不再自动收费，既有检查点不能借此自动变成成功。
公开安装未配置策略仍保留原本的语义监督及显式 assess。已启用策略的本机不得用这个兼容入口逃避必经复核。

状态加入一个当前检查点 ID/说明引用，并使用 v3，旧控制器不能忽略新约束。
每个配置点在 `.codex/runtimehook/checkpoint.main.<id>.json` 留一份最新记录，临时任务使用 temporary 前缀，互不覆盖。
同一 ID 修复后替换最新记录；有价值的旧失败与最终结果另存既有 evidence/Git，再按 3CAN 规则回写持久含义。
这些是有界审计产物，不是 Git 源码、artifact freshness 或业务执行状态的替代品。
历史点是其当时的模块验证，不证明后续改动没有破坏它；最终检查点应跑相关回归/UAT并核对当前候选。

升级不改其他任务状态、目录或正在执行的旧版本路径。新/安全重开的任务核验实际加载新版本。
回滚保留现有记录；不要用旧控制器强行降级 v3。关闭 required 不会撤销远端 Key，也不关闭独立安全门禁。
源代码和 3CAN Runtime 没有变动依赖：此功能不要求重启 9700/9711。
