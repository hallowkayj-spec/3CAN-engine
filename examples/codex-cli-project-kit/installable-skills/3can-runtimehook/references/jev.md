# Jev：可选局部判别，不是另一个任务系统

维护者：3CAN RuntimeHook。协议 `3can.jev-opinion/v2`；首次采用 observe。
用户启用后在重要复核边界调用；不在 SessionStart / PostToolUse / Stop 中联网。
当前唯一接入为 OpenRouter；不需要 TypeSafe/Vercel 账户或 9700/9711 服务。模型不是视觉模型。

## 最小使用

先确认宿主任务 ID、实际 cwd、物理 Git 根和激活目标匹配。`--root` 不改变宿主 cwd。
无法绑定当前任务时仅报告不匹配，不冒用别的 activation。继续安全本地工作。

准备一个 UTF-8 JSON，放在项目现有输出目录或 `.codex/runtimehook/` 中：

```json
{
  "latest_user_request": "修复这个解析错误并测试；不要部署。",
  "claims": [{
    "id": "C1", "criterion_id": "A01", "text": "这个解析用例通过了。",
    "evidence_ids": ["E1"]
  }],
  "evidence": [{
    "id": "E1", "kind": "tool_output", "excerpt": "test_parse_unicode PASSED; 1 passed"
  }],
  "next_step": "提交这次修复供用户审查。"
}
```

criterion 必须来自当前主任务或临时任务验收列表。一个 claim 只陈述一个可验证结果。
evidence kind 为 `tool_output`、`source_code`、`external_source` 或 `agent_summary`；
摘要不能冒充工具证据。来源类别是调用方声明，不是来源真实性证明。
可只检查下一步（claims/evidence 空列表）；不查下一步则设 `next_step: null`。
输入最多 32 KiB、8 个 claim、16 个片段；每个文本字段最多 4000 字符。拒绝过大输入，不静默截断。
先脱敏；不要把无关用户资料、会话全文、Cookie、密钥、凭据文件或整库代码送到第三方。
适用的数据跨境/保密要求仍由项目约束，不能因为用户装了插件就上传所有项目。

```text
python <Skill>/scripts/3can_runtimehook.py --root <物理工作树> --native-cwd <实际宿主cwd> --session-id <宿主任务ID> assess --packet <片段JSON> --mode observe --timeout 10
```

`off` 不读取任务内容、不联网；`observe` 保存供对照的模型观察，不据此改变工作流；
`advisory` 可以把异常选择作为建议交给 Agent 检查，但同样不阻塞、授权或自动记 PASS。
模式不是置信度阈值。概率是分类分布，不是可靠性或验收通过率。

只有声明/下一步、绑定状态、Git HEAD、片段或判别协议改变才重新判断；
同一输入复用 `.codex/runtimehook/jev-observation.json`，不重复请求到满意为止。
这里只保存一份最新观察产物，不新增语义状态、历史库或调度器。删除它只丢失意见缓存。
失败不自动重试；缺 Key 后先配置，限流后另一个合适边界再试，而不是本轮循环。

结果 `OBSERVED` 只表示拿到结构合法的意见；不等于 PASS。
`STALE` 表示调用期间任务/HEAD/输入发生变化，结果不可应用。
`UNAVAILABLE` 包括缺 Key、网络/超时、HTTP 拒绝或格式异常；继续原有复核，不阻塞安全工作。
调用前后检查范围、activation、临时目标、边界、HEAD 和片段；不扫描/哈希整个工作树或产物。
因此没有检测“片段未更新、真实文件却变化”的能力：Agent 必须重新取得实际证据。
片段被恶意改写、选择性遗漏或伪造，也不能由这个模型自动证明真实。

本地绑定/路径不上传；只上传当前目标、验收（及主任务 non-goals）、最新要求、声明、片段和下一步。
模型只返回预定义 choice/probabilities。中文解释和 claim/criterion/evidence 映射由本地生成，
不采纳模型给出的任意命令、路径、引用或自然语言指令。

## 账号、费用与安全配置

1. 登录自己的 OpenRouter 账户，不需要新建网站或注册 TypeSafe。
2. 在 [API Keys](https://openrouter.ai/settings/keys) 创建 RuntimeHook 专用 Key，先设置小额限额（如 1 美元）。
   已有账户额度可用；不要为接入重复充值或开启 auto top-up。Key 限额是上限，不是自动消费目标。
3. 通用方式：由自己的 Secret Manager 注入进程环境变量 `OPENROUTER_API_KEY`。
   不在聊天、脚本参数、Git、`.env` 或明文配置中保存 Key。
4. Windows 可在自己可见的 PowerShell 中执行本 Skill 的脚本：

   ```text
   powershell -NoProfile -File "<Skill绝对路径>\scripts\configure_jev_key.ps1"
   ```

   在隐藏输入提示里粘贴 Key。它用 Windows DPAPI 保存加密 SecureString 到
   `CODEX_HOME/credentials/runtimehook-openrouter.clixml`（CODEX_HOME 未设置则 `~/.codex`）。
   同一 Windows 用户可解密；这不是抵御该用户权限下恶意程序的边界。
   新 Key 文件无需重启 Codex；新插件版本/Skill 是否已被当前任务加载仍需另外验证。
   环境变量优先；过期环境 Key 不会偷偷改用其他 Key。轮换先撤销旧 Key，再仅删除此文件后重录。
5. 用一个无隐私的合成例子验证，再用于已获授权、完成脱敏的真实任务。

官方路径：`POST https://openrouter.ai/api/alpha/decisions`，model `typesafe/jev-1.13`。
这是 OpenRouter 官方 Decisions API，仍为 alpha；不是 chat/completions，也不绕过 TypeSafe 注册。
用版本名而非 latest；响应可为该版本或带日期的快照（例如 `typesafe/jev-1.13-20260917`），
记录实际返回名称，拒绝其他版本。版本名不能证明内部权重永远不变。
保留官方 `usage.input_tokens`、`output_tokens` 和 `cost`（美元），缺失/非法用量不记为零。
相同输入的缓存复用不发请求；其中 usage 是原请求的账单，不是此次新增消费。
官方模型页当前列输入 $0.042/百万 tokens、输出 $0；以实际回执和账户账单为准，不承诺永久价格。
本版替换 Vercel 路径，不回退、不读取或发送旧的 `AI_GATEWAY_API_KEY`/Vercel 加密文件。
旧观察协议不会被新请求复用；不需要迁移 RuntimeHook 主状态。
仅 Python 标准库，无新的 SDK、服务、浏览器自动化、后台轮询或模型自动回退。
网络单次尝试，`provider.allow_fallbacks=false`；socket timeout 默认 10 秒（1–30 可选），响应最多 64 KiB，并检查读取截止。
socket/DNS/系统调度不等于严格整进程墙钟 SLA；记录真实耗时，不宣传毫秒级模型调用。
HTTP 401/402/429 等只返回脱敏错误码；检查凭据/余额/限流后再决定下次请求，不循环重试或自动购买。

权威参考（2026-09-25 已打开）：

- [Decisions HTTP/choice 契约及用量](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request)
- [官方 Jev 使用示例](https://openrouter.ai/blog/tutorials/how-to-use-jev/)
- [专用 API Key](https://openrouter.ai/settings/keys)
- [Jev 模型页：以账户实时价格为准](https://openrouter.ai/typesafe/jev-1.13)
- [TypeSafe 已知局限](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

## 验收与回滚

离线测试只证明输入、传输、隔离、失败和结果处理，不能证明 Jev 真能识别幻觉或提高质量。
首次真实样本分别观察：中文过度宣称、证据缺失、明确反证、用户临时插入、真正无关扩展、注入式伪证。
先由人/主审独立标注，再比较模型输出、误报、漏报、延时、tokens/账单，不由 Jev 给自己评分。
不要因为几个合成测试通过就默认提升到硬门禁。重复失败/RPA/视频等第二批场景留待真实数据决定。

关闭在线层只需停止 `assess` 或选择 off，现有 Hook、状态和独立门禁不变。
卸载适配器可回到 0.1.7；无状态迁移。观察缓存可保留审计或仅删除该文件。
本次更换接入的回滚可保留旧插件用于已有任务，或停止 `assess`；不要隐式恢复 Vercel 调用。
Key 撤销在 OpenRouter 完成；删除本地文件不等于撤销远端 Key。
