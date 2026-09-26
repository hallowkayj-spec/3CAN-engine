# 自动回写：接入、关键阶段、错误进展

Owner 启用后，`connect` 自动登记 Agent 并写入关联；已有 `review` 自动回写阶段摘要与真实复核状态。
原生四个 Hook 仍离线，不扫描会话全文，不额外调用模型，不新建 Runtime、队列或图谱节点。

## 机器配置一次，任务关联一次

在 `CODEX_HOME/runtimehook/writeback.json` 配置经 Owner 授权的本机客户端，和 Jev policy 分开，旧插件继续兼容：

```json
{
  "schema": "3can.runtimehook-writeback/v1",
  "client_path": "<版本固定的3can_codex.py绝对路径>",
  "selector_path": "<Canonical Supervisor的runtime-selector.json绝对路径>",
  "base_url": "http://127.0.0.1:9700"
}
```

客户端用已安装的发布副本，不指向其他任务正在编辑的脚本。公共安装默认不联网。
本适配器仅支持本机回环服务；不猜测远端租户和凭据协议。

任务首次接入 3CAN 时，核实当前宿主关系、唯一 AgentId、模块 Workorder，以及一个**已有**的项目/模块知识节点：

```text
<controller + --root --native-cwd --session-id> connect --agent-id <本Agent> --workorder-id <模块Workorder> --node-id <已有知识节点> --reference <当前归属依据>
```

Agent 从当前项目已有 route/精确读取核实知识落点后自行执行，不反复要求用户输入技术字段。
`.agents/project.json`、Git 物理工作树、节点 project/namespace、Runtime identity/deep readiness 必须一致。
缺失绑定明确 `UNAVAILABLE`，不能猜节点、复制别的项目胶囊或借用其他任务身份。
新主目标的 `on` 建立新 activation，须重新 connect；普通继续不重复 on。
子 Agent 若只有父任务 ID，不得改写父 scope cache；由父任务汇总子任务证据，或使用独立宿主身份的受支持客户端。
安装插件不等于所有外部 Agent 已自动接入；未遵循该协议的 Agent 不在可证明覆盖内。

## 关键开发和交付

架构/依赖决策、核心模块/接口完成、失败改路、交付沿用 checkpoint → review：

```text
review --stage episode --result PARTIAL --summary "实际改变；已验证项；尚未验证项" --reference "已有证据或Git/CI引用" --next-objective "下一步"
```

最终用 `--stage final`。摘要必须是实际语义变化，不能拿目标原文、自写 PASS 或测试成功冒充产品完成。
PARTIAL/FAIL 和 Jev 要求继续复核也自动回写真实状态。低分不会变成 PASS。
只写 ≤2000 字摘要、结果、引用、下一步及必要身份，不上传 Jev 原始证据包。
临时完成仍标明 temporary，不把临时成功当作主任务成功；务必提供明确摘要。

## 错误出现、进展和结果

识别到真实故障，以及调查/缓解/修复证据发生实质变化时，立即调用：

```text
error --id <已有ErrorCase-ID或稳定模块故障键> --state observed --summary "现象与影响" --reference <证据>
error --id <同一ID> --state investigating --summary "确认原因和排除项" --reference <证据>
error --id <同一ID> --state mitigated --summary "缓解与剩余风险" --reference <证据>
error --id <同一ID> --state resolution_claimed --summary "修复、验证结果、未验证边界" --reference <证据>
```

临时任务加 `--scope temporary`。同一 ID 串联进展；预期负例测试不是生产故障。
自动的是传输和持久化，Agent 仍须识别真实故障、提供证据；不靠解析任意命令猜测错误。
`resolution_claimed` 是修复声明，不是 canonical `resolved`。
已有 ErrorCase 正式解决继续用 canonical `done` + exact ticket + 服务端验证回执，再将回执写入阶段摘要。
重复故障的提升、去重和回归沿用 ErrorKnowledge；不直接修改 ERR/FIX/EVD 节点，不自建错误状态机。

## 成功、失败和重放

`writeback.status=WRITTEN_AND_READBACK_VERIFIED` 才是本次写入并精确读回；`ALREADY_RECORDED` 表示同一增量已存在。
仅本地记录或 Jev 意见不能声称远端成功。标记由完整作用域和增量内容生成，重放不重复追加；原 notes 保留。
版本冲突不覆盖、不自动重试。HTTP 成功但 count=0、identity/项目不匹配、缺失读回均不算成功。

脱敏回执保存在当前 `.codex/runtimehook/runtimehook_delta_<摘要>.json`，失败也保留原待写增量和 typed 状态。
不后台重试；诊断/恢复后可显式重放原参数，不能盲重试。身份校验失败时可能没有可安全保存的 packet，先修正绑定。
服务拒绝只尝试一次脱敏 `3can_issue_observed`，该上报失败不递归。
网络约 10 秒预算、单次请求至多 2 秒（拒绝上报另至多 1 秒）；OS/DNS 调度不是严格进程 SLA。
节点 notes 超过 256 KiB 时要求治理归纳，绝不截断旧证据。

本地交付与回写分别报告；`UNAVAILABLE/CONFLICT` 不阻塞安全开发，治理票据门禁保持原样。
回滚移除本机 writeback 配置并恢复已保存插件版本，保留回执和远端历史。无需重启 9700/9711。
