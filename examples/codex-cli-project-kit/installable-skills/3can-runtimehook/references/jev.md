# Jev：可选局部判别，不是另一个任务系统

维护者：3CAN RuntimeHook。协议 `3can.jev-opinion/v1`；首次采用 observe。
用户启用后在重要复核边界调用；不在 SessionStart / PostToolUse / Stop 中联网。
不需要 TypeSafe 账户、Vercel 项目或 9700/9711 服务。模型不是视觉模型。

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

1. 登录 [Vercel 控制台](https://vercel.com/dashboard)，选自己的 Team → AI Gateway。
   本机调用无需新建网站项目。Google 登录可以；是否需要额度以账户页面为准，不为此自动购买 Pro。
2. 在 AI Gateway → API Keys 创建专用 Key，并设置小额预算上限（若该账户提供）；
   AI Gateway 额度不足时才购买少量 credits。默认不启用 auto top-up，不把试用促销当永久价格。
3. 通用方式：由自己的 Secret Manager 注入进程环境变量 `AI_GATEWAY_API_KEY`。
   不在聊天、脚本参数、Git、`.env` 或明文配置中保存 Key。
4. Windows 可在自己可见的 PowerShell 中执行本 Skill 的脚本：

   ```text
   powershell -NoProfile -File "<Skill绝对路径>\scripts\configure_jev_key.ps1"
   ```

   在隐藏输入提示里粘贴 Key。它用 Windows DPAPI 保存加密 SecureString 到
   `CODEX_HOME/credentials/runtimehook-vercel.clixml`（CODEX_HOME 未设置则 `~/.codex`）。
   同一 Windows 用户可解密；这不是抵御该用户权限下恶意程序的边界。
   新 Key 文件无需重启 Codex；新插件版本/Skill 是否已被当前任务加载仍需另外验证。
   环境变量优先；过期环境 Key 不会偷偷改用其他 Key。轮换先撤销旧 Key，再仅删除此文件后重录。
5. 用一个无隐私的合成例子验证，再用于已获授权、完成脱敏的真实任务。

官方路径：`POST https://ai-gateway.vercel.sh/v1/evaluate`，model `typesafe-ai/jev`。
这是 Gateway 官方 Evaluation HTTP API，不是 chat/completions，也不需要绕过 TypeSafe 注册。
仅 Python 标准库，无新的 SDK、服务、浏览器自动化、后台轮询或模型自动回退。
网络单次尝试；socket timeout 默认 10 秒（1–30 可选），响应最多 64 KiB，并检查读取截止。
socket/DNS/系统调度不等于严格整进程墙钟 SLA；记录真实耗时，不宣传毫秒级模型调用。
模型别名由 Gateway 管理，未证明能固定具体 Jev 内部权重版本；记录返回 model，异常模型拒收。

权威参考（2026-09-25 已打开）：

- [Evaluation HTTP/choice 契约](https://vercel.com/docs/ai-gateway/modalities/evaluation)
- [API Key 与预算](https://vercel.com/docs/ai-gateway/authentication-and-byok/api-keys)
- [Gateway 额度与充值](https://vercel.com/docs/ai-gateway/pricing)
- [Jev 模型页：以账户实时价格为准](https://vercel.com/ai-gateway/models/jev)
- [TypeSafe 已知局限](https://docs.typesafe.ai/model-jaggedness/jev-1.13)

## 验收与回滚

离线测试只证明输入、传输、隔离、失败和结果处理，不能证明 Jev 真能识别幻觉或提高质量。
首次真实样本分别观察：中文过度宣称、证据缺失、明确反证、用户临时插入、真正无关扩展、注入式伪证。
先由人/主审独立标注，再比较模型输出、误报、漏报、延时、tokens/账单，不由 Jev 给自己评分。
不要因为几个合成测试通过就默认提升到硬门禁。重复失败/RPA/视频等第二批场景留待真实数据决定。

关闭在线层只需停止 `assess` 或选择 off，现有 Hook、状态和独立门禁不变。
卸载适配器可回到 0.1.7；无状态迁移。观察缓存可保留审计或仅删除该文件。
Key 撤销在 Vercel 完成；删除本地文件不等于撤销远端 Key。
