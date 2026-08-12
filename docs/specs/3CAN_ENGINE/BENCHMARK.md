# 3CAN Engine — Benchmark & 评分方法 (X+3CAN)

> **评分协议**: 本文档按 [BENCHMARK_POLICY.md](./BENCHMARK_POLICY.md) v1.0 (S66g) 分层报告. LongMemEval 属于 **L1 Memory/Retrieval slice**, 不代表 3CAN 总分.
> **原则**: 不自建自评。"X" 表示任何**公开标准 benchmark**, 我们跑它, 报告分数 + judge 模型。行业无标准的维度, 用**严格自评卡**, 标明"仅供参考不对外宣传"。

## 1. X+3CAN 评分框架

按 [BENCHMARK_POLICY.md](./BENCHMARK_POLICY.md):
- **L1 Memory/Retrieval** (权重 20-25%): 公开 benchmark (LongMemEval / LoCoMo)
- **L2 Project Substrate** (权重 45-50%, 主战场): 自建 substrate-bench
- **L3 Governance/Harness** (权重 25-30%): 自建 harness-bench
- **UAT** (非权重, 封顶门槛): 真实任务实测

| 类别 | 做法 | 例子 |
|---|---|---|
| **A 类: 有公开 benchmark** (L1) | 跑 X, 同 judge 参数对齐公开数据, 多 caveat 齐报 | LongMemEval / LoCoMo / BEIR |
| **B 类: 有倡议无统一脚本** | 引用倡议框架, 自评时明标 "无统一脚本" | Stanford/NIST AI Agent Standards (2026-02) |
| **C 类: 全行业无标准** (L2/L3) | **自建 benchmark + 严格自评**, 0-10 打分卡 | 协同 / 项目管理 / 自纠错 |

## 2. A 类: 已跑过的公开 benchmark

### 2.1 LongMemEval (Wu et al. 2024, arxiv 2410.10813, ICLR 2025)

> **Layer**: L1 Memory/Retrieval. **占总分权重 20-25%, 不是总分.**
> **数据集**: `xiaowu0162/longmemeval` HF dataset, **oracle 变体** (已标注 answer session, 比 LongMemEvalS 容易一截).
> **我们的 judge**: DeepSeek-V3. **Self-judge bias 偏高 5-10%** — DeepSeek 答题 + DeepSeek 判, 与 paper GPT-4o judge 不完全可比.

#### 测试协议 (S66g)

每题: ingest haystack turns → route top-10 → DeepSeek 答 → DeepSeek 判 (binary). 运行器 `benchmark/longmemeval_runner.py` 现在支持三个独立 flag:

| Flag | 默认 | 作用 |
|---|---|---|
| `--eval-mode=slim/full/skeleton` | slim | slim=生产 agent 默认 (120-char summary), full=评测用 (完整 description) |
| `--reset-per-question` | OFF | ON 时每题 reset graph (严格隔离, Mem0/Letta 协议); OFF 时 cumulative ingest |
| `--no-str-fix` | (fix on) | 关闭 `str(gt)` 包装. 关闭时 multi-session 整数答案会崩 (int subscript) |

#### 实测数据 (S66g 变量 ablation, balanced 60 题 / 10 per type)

**原始 baseline (S66e 2026-04-18)** — 前三 bug 混合:
- slim + no-reset + no-str-fix → **accuracy 0.2333**
- by_type: temporal 0.5 / multi-session 0.0 (int 崩) / knowledge-update 0.3 / single-session-preference 0.2 / single-session-assistant 0.3 / single-session-user 0.1

**S66f 全修复** (三 fix 同上):
- full + reset-per-q + str-fix → **accuracy 0.7500** (45/60)
- by_type: temporal 0.9 / multi-session 0.5 / knowledge-update 0.8 / single-session-preference 0.7 / single-session-assistant 0.7 / single-session-user 0.9

**S66g 4 变量 ablation** (P0.1b, 进行中):
> 结果在 `benchmark/_longmemeval/longmemeval_*.json`, 包含 variant metadata (eval_mode / reset_per_question / apply_str_fix). 完成后填入下表.

| Variant | eval_mode | reset_per_q | str_fix | accuracy | 解读 |
|---|---|---|---|---|---|
| A-S66e (2026-04-18 历史) | slim | OFF | OFF | 0.2333 | S66e 原始 |
| A-S66g (2026-04-19 重跑验证) | slim | OFF | OFF | **0.2667** | +0.0334 漂移, 在 DeepSeek temperature=0.1 的噪声带 ±0.02-0.05 内. 两者都应报, 不说 0.2333 是 "真相" — 真相是 **0.25 ± 0.03 带宽** |
| B. +str-fix only | slim | OFF | ON | *Ablation B 进行中 Q~25/60* | 消除 multi-session int 崩 |
| C. +full +str-fix (no reset) | full | OFF | ON | *待启动* | 显式消除 pack-层截断 |
| D. +full +reset +str-fix (S66f) | full | ON | ON | **0.7500** | 全 fix |

**A→D 提升 +0.48-0.52 分 (从 ~0.25 到 0.75)**. 单独归因需 B/C 完成后计算 ΔA→B / ΔB→C / ΔC→D.

**B-A 差** = str(gt) 修 multi-session 崩的纯贡献 (预估 +0.05-0.08, 因为 multi-session 原 0/10 → 能答几个).
**C-B 差** = mode=full 对 retrieval 质量的独立贡献 (预估大头, +0.20-0.30).
**D-C 差** = reset-per-q 的隔离收益 (预估 +0.05-0.15).

#### 参考数据 (2026 SOTA on LongMemEval, GPT-4o+ judges)

| 系统 | Judge | Score | 数据集 |
|---|---|---|---|
| OMEGA | GPT-4.1 | 0.954 | LongMemEvalS |
| Mastra Observational Memory | GPT-5-mini | 0.949 | LongMemEvalS |
| Mem0 token-efficient (2026) | GPT-4o | 0.934 | LongMemEvalS |
| Emergence AI RAG | GPT-4o | 0.860 | LongMemEvalS |
| Zep / Graphiti | GPT-4o | 0.712 | LongMemEvalS |
| **GPT-4o oracle baseline** | GPT-4o | 0.87-0.92 | oracle (我们跑的) |
| GPT-4o full-context | GPT-4o | 0.606-0.640 | LongMemEvalS (non-oracle) |
| BM25 baseline | — | ~0.45 | LongMemEvalS |

#### 诚实归因 (多因合一)

S66e 的 0.2333 和 S66f 的 0.7500 **都不能作为 "3CAN 能力总分"**:

1. **LongMemEval 只测 memory/retrieval slice** — 3CAN 的平台价值 (gates / hooks / skills / agent 协作 / INTF / writeback / briefing / token 诊断) 一个都没走. 把 memory benchmark 当总分 = 赛道错配.
2. **oracle 比 LongMemEvalS 容易** — 我们跑的是 oracle, GPT-4o oracle 基线 0.87-0.92. S66f 的 0.75 低于 GPT-4o 同场. 不是 "matches GPT-4o".
3. **Self-judge bias** — DeepSeek 答 + DeepSeek 判, 论文用 GPT-4o 独立判. 同模型自判系统性偏高 5-10%.
4. **3 bug 混合** — S66e 的 0.23 是 runner 3 bug 共同作用; 不是 3CAN 架构天花板. S66f 诊断修复见 `SES-20260419-S66f-longmemeval-runner-debug-resolved`.
5. **无 bi-temporal** (Zep 护城河) — 我们节点无 valid_from/until, 对话矛盾事实无法按时间消解 (LIMITATIONS.md §1.1). 这条对 temporal-reasoning 类仍是真瓶颈.

#### S66e "不作弊声明" 的修订 (S66g 裁决)

**S66e 原文 (已撤回)**:
> "不作弊声明: 我们不修 runner 适配 3CAN (改考试答题纸 = 作弊). 保留现有 runner, 接受 23% 这个真实数字."

**S66g 裁决 (the maintainer 确认 (c) 折中)**:
- `str(gt)` = **纯 bug 修复** (原 runner 对 int answer 崩 22/133 题). 不算作弊.
- `mode=full` = **API 用法修正**. 本文件 §2.1 第 4 条原本就已识别 slim 截断是错, `API_USAGE.md §1` 也写了 LongMemEval 不能用 slim. 不算作弊.
- `reset-per-question` = **改协议** (cumulative → strict isolation). 作为独立 flag 可控, 不强加默认. 单独报告.

**开源对外可报告的数字**: 必须 4 caveat 齐
1. dataset variant (oracle vs S)
2. judge model
3. runner variant flags
4. ablation 贡献拆分

**L1 分数本身不能作为 "3CAN 比 X 强/弱" 的直接依据.**

#### 这个数字的意义 (S66g 重写)

- 3CAN 作为 **memory/retrieval layer** 在 oracle 变体 + DeepSeek self-judge 条件下达到 0.75. 该分数**必须与 caveat 成套**.
- 3CAN 的核心价值 (L2 Project Substrate, L3 Harness/Governance) **LongMemEval 测不到**. 总分看 L2/L3 + UAT.
- "3CAN 不适合对话记忆任务" 的 S66e 判断现在需要修正: **在合适的 eval-mode 下, 单 session 级 QA 能达到 0.9**; 多 session 整合 (0.5) 确实仍弱, 主因是无 bi-temporal.

### 2.2 内部 46-query Route Benchmark (自建 — 不是公开, 明标)

- **性质**: **自建测试集, 自己判分, 仅内部方向用**
- **来源**: `benchmark/route_benchmark_v1.json` (opus-3can-4 2026-04-15 整理)
- **类别**: 8 类 (config-secret / cross-cluster / error-debug / interface / knowledge / project-mgmt / short-code / strategy)
- **指标**: MRR / R@1 / R@3 / P@3 / nDCG@3 / latency P50/P95 / grep_replacement_ratio
- **实测历程**:

| 版本 | MRR | R@1 | short-code | 延迟 P50/P95 | 备注 |
|---|---|---|---|---|---|
| v9.0 baseline | 0.9094 | 0.7609 | 0.556 | 4.8s/6.6s | 原始 |
| v9.1 (layer weight 错误) | 0.8841 | 0.7609 | 0.444 | 4.5s/5.5s | 撤回, short-code 被拖累 |
| **v9.2 (Path 0+2+4 + L2+skill+short_code)** | **0.9239** | **0.7826** | **0.667** | 4.4s/5.6s | 自审计通过 |
| v9.2 + 89 新边 + Leiden 重算 | 0.9022 (46题) | 0.7826 | 0.583 | 4.5s/5.6s | **无显著涨跌 (噪声 ±2%)** |
| v9.2.1 kw_audit 删 1856 kw | 0.8977 | 0.7727 | 0.583 | 5.2s/7.1s | **退化, 已回滚** |

- **噪声带宽**: 同配置跑 2 次 MRR 可能差 ±0.02, 差异小于此阈值不能声称 "涨/降"
- **Token 节省 (内部对比, 非跨工具)**:
  - skeleton vs full: **-86.5%**
  - skeleton vs slim: -27.9%
  - 6 节点测试: skeleton=574 tokens vs slim=796 vs full=4266
- **grep_replacement_ratio**: 0.9348 (93.5% 查询 route 替代 grep)

## 3. B 类: 倡议参考 (没实跑)

### 3.1 Stanford Digital Economy Lab + NIST AI Agent Standards Initiative (2026-02)
- 4 维评估框架 (通用性 / 可靠性 / 安全性 / 可解释性)
- **未有统一脚本**, 仅倡议框架
- 我们参考, 自评时对齐这 4 维 + 加 the maintainer 重点的 "token 经济性"

## 4. C 类: 无公开 benchmark — 严格自评卡

### 4.1 打分规则
- 每维 0-10 分
- **必带证据** (节点数 / 代码文件 / 测试结果), 不带证据不给分
- **必带 cap reason** (为何封顶, 哪里还有缺口)
- 给自己打的分 **比真实体感低 1-2 分** 留余量 (参考 Zep 84% → 58.44% 第三方下修先例)

### 4.2 自评表 (v9.3, Wave 2 Scorecard 产出)

| 维度 | 得分/10 | 证据 | 上限原因 |
|---|---|---|---|
| **记忆精确指引** | 7/10 | 46-query 内部 MRR 0.92, R@1 0.78, skeleton -86% token | 未跑 LongMemEval 全集, 10 题 pilot 仅 40% |
| **项目协作管理** | 7/10 | 11 agents 注册, activity_log 500 条 + hash chain audit, handoff_pending | 并发冲突无仲裁机制 |
| **Token 整盘诊断** | 6/10 | skeleton/slim/full 3 档, budget_tokens, IDF, confidence fallback | 没有跨 agent token ledger |
| **错误不重犯** | 5/10 | 33 ERR-* + 62 FEE-* + observer hook + WebSearch 强制 | 半自动 (LLM 分析 → PROPOSED → the maintainer 审批) |
| **双向 skill** | 4/10 | 12 SKILL-* 入库, /api/skills/invoke 统计 | 真实调用数据尚未累积; 项目级 skills 未扫 |
| **自适应优化** | 5/10 | activation_count 热度, IDF kw, Miss Healer, Leiden community | kw_df 静态, 无在线学习 |
| **生命周期管理** | 7/10 | 30d dormant / 60d archive, 721 active/664 dormant | 手动 sweep, 不做 bi-temporal 事实有效期 |
| **数据健康度** | 5/10 | 1385 节点, 228 孤立 (16.5%), 924 零激活 (66.7%) | 孤立率中等, 可优化 |
| **自评均分** | **5.75/10** | 9 维平均 | 对外讲 4-5 分区间, 不吹 |

### 4.3 对外话术

**对内 / 竞赛评审**:
- "3CAN 在内部 46-query benchmark MRR 0.92, R@1 0.78, skeleton mode 省 86% token。9 维自评平均 5.75 (严于己, 留余量)。"

**对外 / 开源 README** (谨慎):
- "We provide memory accuracy (MRR 0.92 on self-built 46-query benchmark, DeepSeek-V3.2 judge), token efficiency (skeleton mode saves 86.5% vs full), and project coordination primitives. We do NOT claim superiority over Mem0/Zep/Letta — different target users, different benchmarks."

## 5. 评分伦理 (the maintainer 明确要求)

1. **不自建自评当外部数据**: 46-query 只对内, 对外必标"自建"
2. **同 judge 才能比**: 跨模型比较必标 judge 差异
3. **分数低于真实 1-2 分**: 防止社区实测下修 (EvoMap 被 Mem0 复核、Zep 84→58 都是先例)
4. **功能列出 ≠ 功能有效**: 列了 11 项, 不代表每项实测好用。真实验证要等 UAT (下一步)
5. **撤回 8.6**: 自动化自评综合分不对外用

## 6. 未来 benchmark 计划

| benchmark | 优先级 | 预计工作量 |
|---|---|---|
| LongMemEval 均衡 60 题 (Wave 2) | **P0, 进行中** | 30-40 分钟 |
| LongMemEval 全 500 题 | P1 | 4-6 小时 |
| LoCoMo 子集 | P2 | 需另下载数据 |
| BEIR 部分任务 (评 RAG 层) | P3 | 低优先 |
| 真实项目 UAT 1-2 周 | **P0, the maintainer 最看重** | 人工跑 + 日志分析 |

## 7. Wave 2 Scorecard 文件位置

- 脚本: `benchmark/wave2_scorecard.py`
- 输出: `benchmark/_wave2/wave2_report_*.json` + `*.md`
- 运行: `python benchmark/wave2_scorecard.py --lme-n-per-type 10`
