# 3CAN Benchmark Policy — 三层评分体系 (唯一真相源)

> **版本**: v1.0 / 2026-04-19 (S66g)
> **作者**: the maintainer + Opus 主脑
> **锁定**: 本文档是 3CAN 所有评分活动的唯一协议. 与 [BENCHMARK.md](./BENCHMARK.md) 的历史数据并存, 但未来新评分一律遵循本 Policy.

## 1. 为什么拆层 — 背景

S66e 跑完 LongMemEval balanced 60 得 0.2333, 曾被当成"3CAN 真实分数". S66f 定位到是 runner 3 个 bug 混合 (int 崩溃 / slim 120-char 截断 / cumulative ingest 污染), 修完升到 0.7500. 但两个数字**都不能作为 "3CAN 总分"**, 原因:

- LongMemEval 只测 memory/retrieval slice (ingest → route → pack), 不测 3CAN 的平台价值 (gates / hooks / skills / agent 协作 / INTF / writeback / briefing / token 诊断).
- 一个 memory benchmark 把 "项目协同 substrate" 的总分定死, **赛道错配**.

the maintainer 明确 (S66g): "是否真的起作用来评估, 如果走了我们的 3CAN 引擎下的记忆回归和项目理解, 那么该多少分就多少分, 不用扣, 但是如果虚高, 那就下降".

## 2. 三层评分框架

### L1 — Memory / Retrieval (权重 20-25%)

**测的是**: 在给定 haystack/knowledge base 下, retrieval layer 能否找对 + pack 出足够 LLM 作答的片段.

**公开 benchmark**:
- LongMemEval-oracle (Wu 2024, ICLR 2025) — 已跑
- LongMemEval-S (non-oracle, 更难) — 未跑
- LoCoMo — 未跑
- BEIR / MTEB 子集 — 不在范围 (偏代码/通用 IR)

**必须标注的 caveat** (任何 L1 报告):
- oracle vs S (数据集难度)
- judge model (DeepSeek / GPT-4o / Claude / 人工)
- answer model
- runner 变量 (eval_mode / reset-per-q / str-fix)
- self-judge bias 估算 (+5-10% 如果同模型)

**2026 公开参考** (均 GPT-4o 或更强 judge):
- OMEGA (GPT-4.1): 0.954
- Mastra (GPT-5-mini): 0.949
- Mem0 token-efficient: 0.934
- Emergence AI RAG: 0.860
- Zep / Graphiti: 0.712
- GPT-4o oracle baseline: 0.87-0.92
- GPT-4o non-oracle: 0.606-0.640

### L2 — Project Substrate (权重 45-50% — 主战场)

**测的是**: 3CAN 独有价值 — 项目知识路由 / INTF 命中 / 跨 session 决策追溯 / 生命周期 / 多 agent 协作骨架.

**自建 benchmark**: `benchmark/substrate_bench.py` (P1 实施)
- 跨 session 决策追溯 (5+ 题)
- INTF 命中精度 (5+ 题)
- ERR 预警覆盖率 (5+ 题)
- Briefing token 前后差 (3+ 场景)
- 多 agent 信息共享 (3+ 场景)
- 生命周期召回 (dormant 复活, 3+ 场景)

**指标**:
- route top1/top3 精度
- briefing 压缩率 (target: token 压缩 ≥ 60%)
- 跨 session 恢复时间 (target: ≤ 5s per agent checkin)
- 重复犯错降低率 (ERR 命中 → 该 agent 未来同类错误发生率)

**对比**: 目前无直接同类 benchmark (Mem0/Zep/Letta 赛道不同). 对外用 "相对自己历史基线" 形式: 每次改动对比上一版本.

### L3 — Governance / Harness (权重 25-30%)

**测的是**: 规则是否进运行时回路 — gates 是否真拦截 / writeback 是否真发生 / skills 是否真调用 / ERR 是否真复查.

**自建 benchmark**: `benchmark/harness_bench.py` (P1 实施)
- 无 ticket 调高风险 tool → 应 deny (5+ 题)
- 过期 ticket → 应 deny (3+ 题)
- ticket scope 不匹配当前改动 → 应 deny (3+ 题)
- PostToolUse 自动 writeback 发生率 (Edit/Write 后 activity 是否进 log)
- ERR 命中场景下 agent 是否先 route ERR (通过 gate 日志审计)
- skills 按 ticket 动态调用率 (不是全量注入)

**指标**:
- gate 真触发率 (target: ≥ 95% of high-risk tool calls)
- writeback 自动完成率 (target: ≥ 90% of mutating ops)
- gate 日志完整率 (无 silent drop)
- ERR 前置命中率 (target: ≥ 80% of相关 task)

**现状 (诚实)**: 过去 2 sessions L3 事实上是 0 — gates 装了不跑. S66g P0.3 目标是让 L3 从 0 起步.

### 非权重项 — Real UAT (单独列, 不折分但必须有)

**测的是**: 实际项目任务中 3CAN 的体感贡献 (token 省 / 恢复速度 / 错误不重犯 / 体感满意度).

**详见**: [REAL_UAT_PLAN.md](./REAL_UAT_PLAN.md)

**硬规则**:
- UAT 不能替代 L1/L2/L3 — 但 **UAT 为空时, L1/L2/L3 总分不超过 6/10**.
- 原因: 一个"只在 benchmark 里好看"的系统, 没有 real-world 证据, 上限就是这样.

## 3. 评分体系 (S66g v1.1 门槛制, 不做加权总分)

**GPT 外审指正 (已采纳)**: 加权综合分把 memory / substrate / harness 压成一个数字, 会把 3CAN 真实定位冲掉 (memory 单一高分稀释 harness, 或 harness 低分拖低整体). 改用**门槛制 (release gate)**: 三层并列展示, 每层单独过线.

### 公开评测表 (对外只展示三层并列)

| 层 | 展示什么 | 不展示什么 |
|---|---|---|
| **A. Memory / Retrieval** | LongMemEval 原始分 + 4 caveat; 内部 46-query MRR + IR 公式定义 | 加权综合分; 跨工具胜负 |
| **B. Substrate Tasks** | route-before-action pass rate / briefing 压缩率 / schema anchoring (INTF) 命中 / writeback completeness / ERR recall | 同上 |
| **C. Harness / Gates** | policy gate pass rate / structured output pass rate / budget enforcement / writeback after action / benchmark judge consistency | 同上 |

三层独立显示, 读者自行判断对他们场景哪层重要.

### 内部 Release Gate (门槛制)

**不做加权平均, 改三项全过线判定**:

| 层 | Release gate | 当前状态 |
|---|---|---|
| A. Memory | frozen public or private OPC protocol + content-addressed receipt | 🟡 `validating`: public 16-node synthetic seed fixture has a reproducible 46-query receipt; historical LongMemEval 0.75 / MRR 0.92 are not current PASS evidence |
| B. Substrate | ≥ 0.80 on substrate-bench v2+ (20+ cases) | 🟡 pilot v1 (10 cases) top1 0.70, v2 未跑 |
| C. Harness | ≥ 0.85 on harness-bench v2+ (生产触发率 + valid-ticket 场景) | 🟡 pilot v1 (8 cases) 100% PASS, v2 未跑 |
| No critical regression | — | 🟡 focused tests pass; real frozen-graph OPC before/after still required |
| Real UAT scenarios closed | ≥ 5 for v0.1.x, ≥ 20 for v0.2 | 🟡 recorder ready, 实际 closed = 0 |

**Release-ready 判定**: 所有硬门槛都过才算 release-ready. **当前状态**:
A/B/C 与 real OPC 尚未全部闭合 → **"active prototype / experimental
developer preview"**, 不叫 "release-ready".

### 不做

- ❌ 不给单一"3CAN 总分 X/10"对外宣传
- ❌ 不说"beats Mem0" / "matches GPT-4o" 等跨工具/跨数据集直接比较
- ❌ 不把某一层高分稀释另一层低分 (正是门槛制要阻止的)
- ❌ 不把 harness engineering 标榜成"全新身份"; harness 是能力层, 不是品牌

## 4. 与历史数字的关系

| 历史数字 | 层 | 新解读 |
|---|---|---|
| 0.2333 (S66e) | L1 | variant=slim+no-reset+no-str-fix, runner 3 bug 混合, 仅参考不对外 |
| 0.7500 (S66f) | L1 | variant=full+reset-per-q+str-fix, 无 ablation 拆分贡献, 仅参考 |
| MRR 0.92 (46-query 内部) | L2 (部分) | 自建测试集, 不公开对比 |
| skeleton -86.5% token | L2 (部分) | 内部对比, 不对外宣传 "vs 其他工具" |
| 8.6 自评 | - | 已撤回 |
| 5.75/10 (9 维自评 S66e) | - | 旧版, 将被 S66g 20 维 3 层归类替换 |

## 5. 后续动作

- P0.1b: L1 ablation 4 runs → 拆出每个 fix 单独贡献
- P0.2b: 重写 BENCHMARK.md §2.1 按本 Policy
- P1.1: substrate-bench 出题 + 跑首轮
- P1.2: harness-bench 出题 + 跑首轮
- P1.3: Real UAT 3-5 场景

## 6. 外审提问 (GPT-5.4)

1. L1/L2/L3 权重 0.22/0.48/0.30 是否合理 (对 "project substrate" 类产品)
2. UAT 封顶 6.0 是否过严/过松
3. L3 真触发率门槛 95% 是否现实
4. 有没有漏掉的第 4 层 (如 security / multi-tenancy, 当前 out of scope)
