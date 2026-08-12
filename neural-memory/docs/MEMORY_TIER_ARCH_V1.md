# 3CAN 记忆分层架构 v1 (S66c 设计)

> **对应节点**: `ARCH-3can-memory-tier-4-layer-s66c`
> **目标**: 把 3CAN 从"一堆平级节点"进化成"按活跃度自动分层的动态图", 实现 Letta/MemGPT 的分层语义, 保持 3CAN 轻量 core.

---

## 1. 四层定义

| 层 | 英文 | 语义 | 判据 | Route 权重 | TTL |
|---|---|---|---|---|---|
| **Working** | 当前 session 热活 | 本 session 被 route ≥3 次 OR 被 writeback | activation_count ≥ 3 且 updated < 1h | **2.0x** | 1 session |
| **Recall** | 活跃常用 | 最近 7 天被 route 命中 OR activation_count ≥ 5 | last_route < 7d OR ac ≥ 5 | **1.5x** | 滚动 |
| **Archival** | 归档可查 | 60 天+ 未 route OR ac < 2 | last_route > 60d OR ac < 2 | **1.0x** | 180 天 |
| **Episodic** | 事件锚定 | SES-*/HO-* 节点, 时间 anchor | id prefix SES/HO/DOC-* 或 type=session | **0.8x** (不衰减) | 永久 |
| (Dormant) | 休眠 | Archival 持续 180 天未命中 | - | **0.3x** (默认不召回) | 1 年删除候选 |

## 2. 流转规则 (auto-transition, 每日 cron)

```
Working → Recall: session 结束后 (手动/定时)
Recall → Archival: last_route > 60 天 OR activation_count 7 天内 == 0
Archival → Dormant: last_route > 180 天
Dormant → [Delete candidate]: last_route > 365 天 (需 the maintainer 审批)

任何层被 route 命中 → 复活到 Recall (跳级复活)
Episodic 不流转 (时间 anchor 恒定)
```

## 3. Route 时 tier-weight 应用

现有 `graph_engine.py route()` 中的 `priority_bonus` 可扩展:

```python
TIER_WEIGHTS = {
    "working": 0.10,   # 加分
    "recall": 0.05,
    "archival": 0.0,
    "episodic": 0.02,  # 历史记录轻加
    "dormant": -0.10,  # 减分 (默认不浮上来)
}
# 在 emb_total / hybrid_score 里叠加
tier_bonus = TIER_WEIGHTS.get(node.tier, 0.0)
```

## 4. 节点 model 变更

`backend/models.py` NodeBase 加:
```python
tier: Literal["working", "recall", "archival", "episodic", "dormant"] = "recall"
last_route_at: Optional[str] = None   # ISO 时间
tier_transitions: list[dict] = []     # [{from, to, ts, reason}]
```

## 5. 初始化 (migration)

一次性脚本 `scripts/migrate_tier_v1.py` 按当前数据分配:
- `type=session` → `episodic`
- `id.startswith(("SES-","HO-","DOC-"))` → `episodic`
- `activation_count >= 5` OR `updated_at < 7d` → `recall`
- `activation_count < 2` AND `updated_at > 60d` → `archival`
- 其余 → `recall` (default)

## 6. Cron 实施

`scripts/cron/tier_transition_daily.py`:
- 扫所有非 episodic 节点
- 按规则 4 级流转
- 落盘 `tier_transitions` 历史
- 写 `DOC-tier-transition-log-{date}` 节点 (可选)

## 7. API 扩展

- `GET /api/nodes?tier=working` 按层过滤
- `POST /api/nodes/{id}/tier/promote` 手动升层 (the maintainer 调)
- `/api/stats` 增加 tier 分布字段

## 8. 对比 Letta (不照搬)

| Letta | 我们 | 差异 |
|---|---|---|
| core_memory (block) | Working | Letta 是 system prompt 插入, 我们是 route 加权 |
| archival_memory (vector) | Archival | 相似 |
| recall_memory (conversation) | Episodic | 我们用节点存 session, Letta 存 raw 对话 |
| (无) | Recall | 我们独有: 活跃常用区, 对标人脑"工作记忆-长时记忆"中间带 |

## 9. 预期收益

- Route latency: 默认不搜 dormant 节点, 可省 20-30% 搜索量
- 内存图整洁: 1 年自然分层, 老节点沉底不干扰
- 对应 the maintainer MoE 思路: 不同 tier = 不同 expert 加权

## 10. 实施阶段

- **Phase 1 (high effort, 本轮可做)**: model 加字段 + migration 脚本 + route 加 tier_bonus
- **Phase 2**: cron 自动流转 + UI tier 过滤
- **Phase 3**: 对外 API 暴露 + 文档
