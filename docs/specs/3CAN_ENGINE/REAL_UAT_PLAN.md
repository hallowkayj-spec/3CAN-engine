# 3CAN 真实项目 UAT 评测框架

> **the maintainer 核心观点**: "测分前跑自身项目严格审计 + 模拟任务开发真实反馈" 比跑第三方 benchmark 重要 10 倍.
> 本文件定义: 怎么跑真实 UAT, 记录什么, 从哪些维度评, 最后怎么复盘到 3CAN 节点.

## 1. 为什么 UAT > benchmark

| 点 | 真实 UAT | 第三方 benchmark (LongMemEval 等) |
|---|---|---|
| 测的场景 | 我们目标用户真实做的事 | 学术评测集, 偏对话记忆 QA |
| 发现的问题 | 架构漏洞 + 体感问题 + 隐性需求 | 单一指标 (accuracy/MRR) |
| 可修 | 直接指向需要改的地方 | 改指标可能作弊 |
| 结果可用 | 写进 SES/ERR/DEC 节点, 持续沉淀 | 单次数字, 用完即弃 |
| 说服力 | 真用户能复现 | 靠 paper 引用 |

## 2. UAT 任务类型 (3 个 典型场景, the maintainer 选)

### 场景 A: 跨 session 决策追溯 (3CAN 核心价值)

**任务**: "上次我们讨论视频管线 bug 时决定用哪个方案?"

评测流程:
1. Claude Code 收到这个问题
2. agent 调 `/api/route task="视频管线 bug 决定 方案"`
3. 记录:
   - 返回的 top-3 节点 (是否含正确 DEC-video-pipeline-* 节点?)
   - confidence = ?
   - 用了多少 token (agent input tokens 前后对比)
   - agent 最终答对没 (the maintainer 评)

**成功标准**: top1 是正确 DEC/SES 节点 + confidence=high + 答案准确

### 场景 B: Bug 修复闭环 (错误不重犯)

**任务**: agent 帮修一个 bug, 看是否触发历史 ERR 节点提示

评测流程:
1. 引入一个已知 bug (例: route 用 slim mode 测 benchmark — 今天的 ERR-longmemeval-runner-slim-mode 先例)
2. 让 agent 诊断
3. 记录:
   - observer hook 有没有检测到?
   - agent 有没有 route 找 ERR 先例?
   - agent 有没有独立 rediscover 还是立刻从 ERR 借鉴?

**成功标准**: agent 开始诊断前就 route 到相关 ERR 节点, 不重犯

### 场景 C: 多 agent 协作 (Codex + Claude Code 双栈)

**任务**: Claude Code 写后端 API + Codex 写前端调用 + 共享 3CAN

评测流程:
1. Claude Code 建 INTF-* 节点描述 API 契约
2. Codex 读 INTF 节点写前端代码
3. 记录:
   - Codex 查到 INTF 用了多少 token?
   - Codex 写的代码和 API 契约一致吗?
   - Claude Code 改 API 后 Codex 有 WS 订阅感知?

**成功标准**: 两 agent 共享 INTF 后, Codex 写的前端一次对接成功, 无来回澄清

## 3. 记录模板

每次 UAT 任务跑完, 手工或自动生成一个 `SES-uat-{task}-{date}` 节点:

```json
{
  "id": "SES-uat-video-pipeline-20260418",
  "name": "[UAT] 跨 session 视频管线决策追溯",
  "cluster": "会话记录",
  "type": "session",
  "content": {
    "description": "UAT 任务 A: 测 3CAN 跨 session 决策找回",
    "current_state": "完成",
    "notes": "任务: 问 '视频管线 bug 决定用哪个方案'\n结果: agent route 到 DEC-video-scripts-physical-simplicity-s66c (top1 conf=high)\nagent 回答准确. token: 前 5k, 后 1.2k (省 76%)\n问题: 无",
    "extra": {
      "uat_task": "A",
      "route_top1_correct": true,
      "confidence": "high",
      "token_saved_pct": 0.76,
      "agent_errors": 0,
      "user_satisfaction": 4  // 1-5
    }
  },
  "activation_keywords": ["UAT", "真实测试", "跨 session 追溯", "视频管线", "场景A"],
  "primary_author": "ka"
}
```

## 4. 评分维度 (每场景都测)

| 维度 | 1 分 | 3 分 | 5 分 |
|---|---|---|---|
| Route 命中 | top3 都不对 | top3 含相关 | top1 就是正确 |
| Confidence 准 | 说 high 但错 / 说 low 但有答 | 基本对 | high/medium/low 分档准 |
| Token 省 | 没省或更费 | 省 30-50% | 省 70%+ |
| 错误不重犯 | 重犯 | 提过但没用 | 先查 ERR 再动 |
| 体感 | 卡 / 报错 | 能用 | 丝滑 |

## 5. 跑 UAT 的推荐方式

**手动 the maintainer 亲跑**:
1. 选一个真实任务 (不要临时造)
2. 开日志 (记录 Claude Code / Codex 会话)
3. 跑完人工复盘填模板
4. writeback 到 3CAN

**半自动 (工具支持)**:
- `tools/uat_recorder.py` (未实现, 待补) — 在 agent 调 route 前后自动记录 token 差, 让 the maintainer 只填"任务是否完成"

## 6. 样本数量建议

- the maintainer 自己跑 **3-5 个典型场景** 就够. 不用追求统计意义
- 关键是找出 "哪些任务 3CAN 真帮到 / 哪些帮倒忙"
- 数据进 3CAN, 持续沉淀 → 下次改进方向明确

## 7. 当前状态

**未跑任何真实 UAT**. 本 session 全在做基座 + benchmark, 没做真实任务测试。
**the maintainer 决定**: 优先跑哪个场景? 还是等更多基座补完再 UAT?

## 8. 关键诚实提醒

- 真实 UAT 结果可能非常负面 (agent 忘记查 3CAN / hook 没触发 / route 不准) — 这正是 UAT 价值, 暴露问题
- 不要**只跑 UAT 证明能用**, 要跑 UAT **找出不能用**
- 每次失败的 UAT 都应该写 ERR-* 节点, 这本身是 3CAN 价值验证 (真实使用中积累的错误教训 > 任何 benchmark 数字)
