# 3CAN Evidence — S66g Compliant Hard Facts

> **版本**: v1.0 / 2026-04-19 (S66g)
> **编制**: 按 [BENCHMARK_POLICY.md](./BENCHMARK_POLICY.md) v1.0 严格分层
> **定位**: **3CAN 对外 (竞赛 / 开源 / PR / 演讲) 可引用的唯一 evidence 文档**. 取代 2026-04-14 的 `3CAN_BENCHMARK_REPORT_2026Q2.md` (已归档至 `_archive/2026Q2_marketing_snapshot.md`).
> **话术硬规则**: 本文档不做"vs X 赢/输", 不报单数字总分, 不使用 "独创/唯一/首个", 不声明未跑的 benchmark.
>
> **历史快照说明**: 本文的节点数与 600 秒内存票据描述只证明
> 2026-04-19 的 v9.5/S66g 基线，不代表未发布的 v0.2 候选。
> v0.2 的 SQLite/WAL 票据与 Error Knowledge 契约见
> [`../../ERROR_KNOWLEDGE_LIFECYCLE.md`](../../ERROR_KNOWLEDGE_LIFECYCLE.md)；
> 新结果必须另附当次可复现证据，不能回填进本历史快照。

## 1. 存在性事实 (可立即 curl 核验)

| 指标 | 当前值 (2026-04-19) | 核验 |
|---|---|---|
| 节点总数 | 1408 | `curl http://localhost:9700/api/stats` → `total_nodes` |
| 边总数 | 1023 | 同上 → `total_edges` |
| Active 节点 | 744 (status=active) | 同上 → `active_nodes`. **注**: `status=active` 是 lifecycle 标签, 不等于 "近期被 route 命中". 真 route 命中需看 `activation_count > 0`. |
| 注册 Agent | 10 | `GET /api/agents` |
| Hash chain audit | valid=True / breaks=0 **within current 500-entry window** | `GET /api/audit/verify`. **注**: activity_log 仅保留最近 500 条, 窗口外前序 hash chain 断链. 不是全量 append-only 审计 (LIMITATIONS §3.4). |
| 节点类型分布 | knowledge(570) + session(375) + feedback(334) + reference(40) + process(25) + decision(25) + tool(14) + skill(12) + secret(10) + config(3) | `models.py NodeType enum` + `GET /api/stats`. schema 9 类枚举, 实际分布极不均. |
| 节点 ID 前缀 | 20+ 语义分类 | CONTRACTS.md §2 |
| 技术文档 | 17 份 (3CAN_ENGINE/) + PROTOCOL.yaml v9.4 (OpenAPI 3.0.3) | 本目录 |

## 2. 架构与协议事实 (代码可查)

- **HTTP API**: 唯一对外入口 `localhost:9700` (single-writer slot proxy), 36+ 端点 (见 PROTOCOL.yaml + ARCHITECTURE.md §6).
- **Route pipeline**: 4-signal RRF (Cormack 2009) + cross-encoder 精排 (bge-reranker-v2-m3). 详见 ARCHITECTURE.md §4.
- **Pack mode 3 档**: skeleton (~20 tok/节点) / slim (~50) / full (~500-800). `_pack_skeleton` / `_pack_slim` / full dump.
- **V9.5 Route Ticket Gate (S66g)**:
  - `POST /api/route/ticket` → 返回 ticket_id + TTL 600s + err_warnings + intf_anchors + api_usage_hints
  - PreToolUse hook 校验 ticket 存在 + 未过期 + scope 匹配, 不通过 deny
  - PostToolUse 强制 `/api/activity/log`, 失败写 `~/.claude/logs/3can-writeback-fail.jsonl`
  - Gate 决策写 `~/.claude/logs/3can-gate.jsonl`
- **单写 slot 代理**: proxy 9700 + green 9701 / blue 9702；共享图锁下
  一次只运行一个 writable backend，不宣称零停机 standby。
- **生命周期**: 30d 未 route 命中 → dormant / 60d → archived. 支持复活.

### 2.1 解决证据的信任边界

`verified: true` 只是调用方声明，不是解决权限。自动把 ErrorCase 标记为
resolved 必须同时满足:

1. 目标文件位于 `THREECAN_PROJECT_DIR` 或
   `THREECAN_TARGET_ROOTS` 的允许根下；
2. evidence artifact 位于显式 `THREECAN_EVIDENCE_ROOTS` 内且不超过
   `THREECAN_EVIDENCE_MAX_BYTES`（默认 4 MiB）；
3. artifact SHA-256 与 receipt 相符；
4. JSON schema 为 `3can.verification-attestation/v1`，其 `kind`、
   `verifier`、`ticket_id`、`target_digest`、`scope_digest` 与当前
   receipt/ticket 一致，`command` 非空、`exit_code` 为 0、`outcome`
   为 pass/passed/success；
5. 除 `signature` 外的完整对象按 UTF-8、Unicode 不转义、key 排序、
   紧凑 `,`/`:` 分隔进行 canonical JSON 编码，并由运行时注入的
   `THREECAN_EVIDENCE_HMAC_KEY` 做 HMAC-SHA256；secret 至少含 32 个
   随机 bytes，不写入仓库。

未配置 evidence roots/key、key 过短、文件越界、digest/signature 不符，
或只提供 activity self-hash 时，结果必须保持 `review_required`。本发布
包的 `.env.example` 只给空占位符，不含真实密钥。

## 3. 性能事实 (内部实测, 非跨工具对比)

| 指标 | 值 | 条件 |
|---|---|---|
| Route P50 latency | 4.4s | **历史记录，非 v0.2 验收值**。1385 节点, Windows CPU, BGE-M3 + bge-reranker-v2-m3；记录于 2026-04-15 v9.2，发布包不含当时私有 graph corpus 与原始结果票据，不能独立复现。 |
| Route P95 latency | 5.6s | 同上；只保留作历史审计线索。 |
| skeleton vs full | -86.5% token | 单 query `RRF fusion routing`, 6 节点. **单次实测, 不是跨工具对比**. |
| Budget tokens | 400-1200 常见 | `_enforce_budget` 尾部截断 |

## 3.5 L1 — 历史内部 46-query Benchmark 记录

> 本节保留 2026-04-15 v9.2 的内部记录，供审计历史使用。它**不是
> v0.2 当前结果，也不能从发布包独立复现**：当次私有 graph corpus 与
> 原始逐题结果票据未发布，且 `route_benchmark_v1.json` 在记录分数后
> 已做可移植性修改。行业标准公式本身可检查，但旧分数没有完整的
> 可复现证据链。

### 留存材料与复现边界

- **当前 fixture**: `neural-memory/benchmark/route_benchmark_v1.json`
  (46 题, 8 类)，已替换为只指向通用 seed graph 的
  `synthetic_public` fixture。它可用于新实验，但不能复现旧分数。
- **Runner**: `neural-memory/benchmark/run_benchmark.py`，调用当前
  `/api/route`；可用于用户自有图谱实验，不能恢复 2026-04-15 的私有语料状态。
- **缺失证据**: 冻结 graph corpus、运行环境/模型摘要、逐题原始响应和
  内容寻址的结果票据均未随发布包提供。
- **Fixture 漂移**: 当前 fixture 与记录分数时的题集并非字节级冻结版本，
  因此不能把新运行或旧分数归因于同一测试输入。
- **公式** (Wikipedia 标准 IR 定义, 无 3CAN 魔改):
  - `MRR = (1/n) × Σ(1/rank_i)`
  - `R@k = top-k 中命中 expected 的比例`
  - `nDCG@k = DCG@k / IDCG@k` (Järvelin & Kekäläinen 2002)

### v9.2 历史记录 (2026-04-15，未附可复现票据)

| 指标 | 值 |
|---|---|
| **MRR** | **0.9239** (历史记录；不得作为 v0.2 PASS) |
| **R@1** | **0.7826** (78.3% top-1 精准) |
| R@3 | 0.9130 |
| P@3 | 0.3551 |
| nDCG@3 | 0.8695 |
| Latency P50 / P95 | 4.4s / 5.6s |
| grep_replacement_ratio | 0.9348 (93.5% route 替代 grep) |

### 分类别

| 类别 | n | MRR | R@1 |
|---|---|---|---|
| strategy | 6 | 0.98 | 0.83 |
| interface | 6 | 0.96 | 0.83 |
| config-secret | 6 | 0.96 | 0.83 |
| error-debug | 6 | 0.94 | 0.83 |
| project-mgmt | 6 | 0.93 | 0.83 |
| knowledge | 6 | 0.92 | 0.83 |
| cross-cluster | 6 | 0.91 | 0.67 |
| **short-code** | **4** | **0.667 (弱项)** | 0.5 |

### 必读 caveat

- **不是跨工具对比**: Mem0/Zep/Letta 没跑我们 46-query, 我们没跑他们 benchmark. 数字不可换.
- **不可独立复现**: 发布包不含当次 graph corpus、逐题原始输出和结果票据。
- **Fixture 已漂移**: 当前 46 题文件只能作历史/实验输入，不能证明旧分数。
- **样本数 46**: IR 学界显著性需 ≥100, P1 扩.
- **版本已变化**: v9.3/v9.4/v9.5/v0.2 后未在冻结语料与冻结模型上重跑。

### 对外可说 / 不可说

**✅ 可说**: "2026-04-15 的内部记录写下 MRR 0.9239 / R@1
0.7826；该记录缺少随发布包提供的私有 graph corpus 和原始结果票据，
当前 fixture 也已漂移，所以它是历史线索，不是可独立复现的 v0.2
benchmark。"

**❌ 不可说**: "v0.2 MRR 0.9239" / "当前 benchmark 已通过" /
"按仓库即可复现旧分数" / "vs Mem0/Zep 检索强" / "检索 SOTA".

---

## 3.6 历史 dogfood 观察的证据边界

2026-02 至 04 的单用户 dogfood 笔记曾记录 route 命中、briefing 载荷、
跨 session handoff 与少量 token 估算。发布包没有附带完整的私有图谱、
逐次 query/response、统一 tokenizer 或内容寻址 receipt，因此这些观察
只能解释设计动机，**不能作为 v0.2 的命中率、节省比例、错误不重犯率或
多 agent 成功率**。

v0.2 要建立这些声明，必须使用去隐私化冻结任务集，记录 commit、模型与
tokenizer、每次 route 输出、人工判定和原始 receipt。未完成前，对外仅
说明系统提供 route、briefing、writeback 与 Error Knowledge 契约，不给
效果百分比。

---

## 3.7 已撤回的 2026-04 市场排名草案

早期内部草案曾按 GitHub stars、主观功能映射和不同协议下的分数，
给 3CAN 与若干记忆/图谱项目排位。该比较不满足同语料、同模型、
同协议、同时间点和可复现结果票据的基本条件，**不得作为证据或发布
声明**。v0.2 候选不发布“Top N”、胜负、SOTA 或 stars 增长预测。

外部项目只在 `ATTRIBUTION.md` 和 Error Knowledge 生命周期研究表中按
“借鉴了什么 / 没复制什么”出现，不构成产品排名。

---

## 4. L1 Memory/Retrieval — 历史 LongMemEval 记录

一份 2026-04 内部笔记记录了 LongMemEval oracle balanced-60 在 runner
修正前后的 `0.2333 → 0.7500`。当次冻结输入、运行环境、逐题响应与
content-addressed receipt 没有随发布包提供；同一模型参与回答与判定，
runner 还同时改变了字符串转换、pack mode 和 per-question reset。

因此该记录只保留为历史调试线索，**不是 v0.2 benchmark，也不能与论文
或其他产品的结果直接比较**。重新发布分数前应冻结 dataset variant、
runner、模型、judge、环境和逐题 receipt，并把各项 runner 变化拆成独立
ablation。

## 5. L2 Project Substrate — 历史 pilot 与公开 fixture 的边界

一份 2026-04 私有图 pilot 笔记记录 10 cases、top-1 0.70、
top-3 recall 0.85。发布包不含当次冻结 graph、原始逐题响应或
content-addressed 结果票据，旧的私有派生 fixture 也已移除。因此这些
数字只能是历史线索，**不是 v0.2 PASS，也不能从当前仓库复现**。

发布包现在附带 `benchmark/substrate_bench_v1.json` 的 10-case
**synthetic_public** fixture，只引用 `seed_nodes.py` 创建的通用节点。
当前候选已在 fresh seed/development profile 上记录内容寻址结果票据：
[`SEED_GRAPH_BENCHMARK_20260809.json`](../../evidence/SEED_GRAPH_BENCHMARK_20260809.json)
报告 10 cases、top-1 `1.0`、mean top-3 recall `0.8167`。它只证明公开
synthetic fixture 可复现，不证明私有生产图、多 agent 协作、错误不重犯
或跨 session 稳定，也不替代 20+ case 的发布门槛。

## 6. L3 Governance/Harness — 历史 denial pilot，当前未验收

早期 8-case harness pilot 笔记记录 8/8，但主要验证无票据/伪票据的
deny 和正则分支，未覆盖“有效票据 + scope match = allow”，也没有随
发布包提供原始结果票据。它不是 v0.2 验收结果。

当前仍不可声明“高风险工具 95% 拦截率 / writeback 90% 自动化率 /
ERR 前置命中率 80%”。任何新结果必须记录 commit、fixture hash、
环境、完整 allow/deny 路径与内容寻址 receipt。

## 7. 当前**不能**声明 (P1 黑名单)

对外任何材料**禁止**出现以下表述, 直到 P1 substrate-bench / harness-bench / Real UAT 通过:

- ❌ "3CAN 比 Mem0 / Zep / Letta 强" (未跨工具复测)
- ❌ "多 Agent 协作有效" (仅 dogfood, 无 benchmark)
- ❌ "错误不重犯 / 自学习正循环" (harness-bench 未跑; 单次 n=7 不是证据)
- ❌ "Token 跨工具节省 90%+" (内部 skeleton vs full, 非跨工具)
- ❌ "12 次 route 全中" / "3.5→6/7 自学习" (单次手工 task, 样本极小)
- ❌ "架构已在开发与另一个业务领域双场景实证" (没有冻结数据、
  同协议 UAT 与结果 receipt)
- ❌ "综合评分 X/10 超过 Y/10" (8.6 / 5.9 / 6.6 全是 2026-04-14 我们自评, 不是第三方)
- ❌ "业内独创 / 唯一 / 首个" (2026-04-14 公开开源调研范围内 "未发现"; 不是"不存在")

## 8. 对外话术 5 条硬规则

1. 任何分数 / 百分比声明**必须带 caveat** (数据集 + judge + protocol + sample size).
2. **不使用 "vs X 赢/输"** 除非跨工具复测有 paper-trail.
3. **未跑的 benchmark 不报数**; 单次跑的数据明标 "单次, 无复现协议".
4. 跨领域同构只作为**产品假设**, 不作为**实证结论**.
5. 对外总分**≤ 5.0/10** (BENCHMARK_POLICY 封顶: UAT 空 + L3 gate 触发率未 substrate-bench / harness-bench 验证).

## 9. 结语

3CAN 在 S66g 的真实状态:
- **已有**: 代码 + 协议 + 17 份文档 + 基础 benchmark caveat 齐 + Gate 首次真触发 + sentinel 文档化 bootstrap.
- **待验**: substrate-bench / harness-bench / Real UAT.
- **不说**: 无证据的营销话术 + 跨工具排名 + 独创声明.

**本文档同时作为评估门槛**: 外部读者可以在 clean clone 上验证启动、
协议、synthetic fixture 和测试结果。历史私有图统计、46-query 分数、
LongMemEval、substrate pilot 与 harness pilot 均因缺少冻结语料或原始
receipt 而不可独立复现，不得列入当前验收。

本文档在 P1 (substrate-bench + harness-bench + UAT) 完成后重大更新. 之前任何版本以本文最新时间戳为准.
