# 3CAN Engine — 文档索引 + README

> **版本**: v0.2 release candidate (未发布, 2026-07-29)
> **作者**: the original maintainer + contributors
> **状态**: active prototype / experimental developer preview. Source-available under PolyForm Noncommercial 1.0.0.
> **2026-05 cadence**: controlled project-group prerelease before broader public source-available release.

## License

This repository is **source-available, not OSI-open-source**.

The code in this repository is licensed under the **PolyForm Noncommercial License 1.0.0**.

In plain language:
- Noncommercial personal use, research, learning, modification, and sharing are allowed under the license terms
- Forks, experiments, and contributions are welcome
- Commercial use requires separate written permission

See the canonical [`LICENSE`](../../../LICENSE) for the verbatim license text,
[`NOTICE`](../../../NOTICE) for required attribution, and
[`LICENSING.md`](./LICENSING.md) for a plain-language FAQ.

Contact the maintainer before any commercial use.

## One-line positioning (external)

> 3CAN is a source-available graph-backed project substrate prototype for coding-agent workflows, with explicit routing, writeback, and harness-level evaluation.

---

## English TL;DR (for external reviewers)

**A graph-backed project substrate for multi-agent coding workflows.**

3CAN is designed for OPC (One-Person Company) and small teams working on medium-sized projects that run multiple AI coding agents over weeks or months. It provides shared project reality — not generic chat memory — so agents find the right place faster, avoid repeated mistakes, and restore context with lower token cost.

HTTP-only API at `localhost:9700`. BYOK for every LLM touchpoint. No vendor lock-in.

It targets one specific problem:

> when multiple AI agents work on the same medium-sized project over weeks/months, the hardest part is not "remembering more text". It is sharing the same project reality, enforcing route-before-action discipline, and preventing agents from re-discovering history that has already been written down.

### The 5 layers

Shipped layers describe the v0.2 candidate. Planned layers remain explicitly
labeled and are not release claims.

1. **Memory layer** — `shipped` — graph-backed project memory (nodes + edges + activation decay, BGE-M3 embedding, 4-signal RRF + cross-encoder rerank)
2. **Project-management layer** — `shipped` — `INTF` contract nodes + `DEC` / `SES` / `ERR` / `FEE` typed nodes + multi-agent registry + hash-chained activity log + lifecycle sweep
3. **Optional enforcement layer** — `shipped (basic)` — PreToolUse Route Ticket Gate (`route → read ERR/INTF → ticket → act`), PostToolUse writeback hook. Can be turned off per user preference.
4. **LLM integration at specific points** — `shipped / partial / planned must be labeled explicitly`:
   - `core`: route can run without generative LLMs, using local retrieval models such as BGE-M3 and rerankers
   - `optional`: project bootstrap, keyword/alias repair, summary enrichment, node-health checks, edge suggestions, behavior gates, and benchmark judges can use BYOK or local LLMs
   - `planned after v0.2`: consistent provider abstraction, `--estimate-cost` / `--dry-run` for every LLM tool, provider-specific tokenizers, and a per-agent usage ledger
5. **Modular / decoupled** — `shipped` — the core engine (backend / proxy /
   route / retrieve / writeback) has **zero generative-LLM dependency**.
   Minimal installs use a deterministic hashing embedding fallback; the full
   profile adds BGE-M3 and rerankers. Every generative-LLM tool and hook module
   can be enabled or disabled independently.

### Chinese-friendly

Most node content (names, descriptions, keywords) in the current dogfood graph is in **Chinese**. BGE-M3 is multilingual; query and storage both support Chinese natively. English-only projects also work. Mixed-language is the common case in practice.

### What 3CAN is NOT

- Not a standalone coding model
- Not a replacement for any coding CLI or IDE
- Not a generic long-chat memory bot
- Not a full autonomous agent runtime
- Not a finished enterprise platform
- No multi-tenant permission model, no SaaS admin panel

### Historical evidence and current candidate status

- **Historical private graph snapshot**: an internal 2026-04 note recorded
  1407 nodes / 1023 edges / 10 registered agents. The graph is excluded from
  this package and those counts are not a v0.2 acceptance result.
- **Historical private 46-query record** (2026-04-15): MRR **0.9239** /
  R@1 **0.7826**. The frozen graph and raw receipt are absent, so the score is
  not independently reproducible. The included 46-query fixture is a new,
  synthetic seed-graph suite and does not reproduce that score.
- **Historical LongMemEval note**: an internal oracle balanced-60 run recorded
  `0.23 → 0.75` after runner changes. The frozen inputs, environment, and raw
  receipt do not ship, so this is not independently reproducible and is not a
  v0.2 acceptance result.
- **Historical substrate pilot note**: top1 **0.70**, top3-recall **0.85**;
  no raw receipt ships, and the public 10-case fixture is synthetic.
- **Historical harness pilot note**: 8/8 denial-biased cases; it omitted a
  valid-ticket allow path and has no raw receipt.
- **Token efficiency (internal comparison only, not cross-tool)**: skeleton vs full mode saves **-86.5%** on one measured query; grep-replacement ratio **93.5%**
- **Dogfood observation, 2.5 months, single core user**: input-token bloat reduced approximately 30–40% versus pre-3CAN workflow (subjective, not a cross-tool benchmark)
- **Maturity**: v0.2 is an **unreleased active prototype / experimental
  developer preview**, not release-ready.

### About the author (honest note)

3CAN is an active prototype built with heavy AI-agent assistance, with heavy use of Claude Code and other AI coding agents during the whole development. Several parts of the system are expected to have bugs, suboptimal choices, or gaps that a professional software engineer would spot immediately.

**Contributions, corrections, hard criticism, and PR review are warmly welcome.** If you are a professional engineer and think a file / function / architectural choice is wrong, please open an issue — we will read every one, and the project will improve as a result.

### Source-available release reality

3CAN ships as an **engine + tools + docs**, not as a private pre-populated
knowledge graph. A new user starts with a small generic seed graph; the tools
(`project_bootstrapper` + hooks + gates) help bootstrap project-specific
knowledge. Projects with existing handoffs and contracts can seed useful nodes
sooner than fresh projects. Earlier percentage-based recovery curves were
subjective dogfood estimates, not measured release guarantees, and are excluded
from v0.2 evidence.

See [EVIDENCE.md](./EVIDENCE.md) for the full hard-facts document, [LIMITATIONS.md](./LIMITATIONS.md) for known gaps, [LLM_POLICY.md](./LLM_POLICY.md) for the BYOK / route / token-management map, and [ATTRIBUTION.md](./ATTRIBUTION.md) for every external idea we borrowed.

## 使用节奏诚实预期 (source-available 定位)

3CAN 不是 "一键安装立即见效" 的插件, 也不是 "要 2-4 周才有用" 的重量级平台。真实节奏 (针对核心用户 vibecoding 开发者):

- **Day 1 开箱**: 部署 + 冷启动向导 + 跑 project_bootstrapper 抽 20-50 种子节点 → 可以开始用
- **Day 1-3 高密度使用**: 真实项目任务暴露问题, 用户可自行调参数 (关 hook / 改阈值)
- **Day 3+**: 节点累积到 100+, 基座能力逐步显现
- **小白用户**: 跟随官方迭代 (自身 3-4 天从 v9.0→v9.4)

**不适合的用户**:
- 只想 10 分钟见效 AI 记忆 → 用 Mem0 / Cursor 内置
- 企业多部门权限管控 → 3CAN 无权限体系
- 想 AI 全自主干活 → 用 OpenClaw / Hermes autonomous agent

**适合的用户**:
- 维护中等偏大项目的个人 / 2-5 人小团队
- 愿意花 1-3 天调参，并持续积累可路由、可验证的错误解决方案
- 用 Claude Code / Codex / Gemini CLI 多 agent 协作

## 2026-05 项目组内开放

5 月公开发布前, 3CAN 会先进入项目组内开放和共建优化阶段。当前规划是与上海理工大学老师和学生共同推进:

- 学生获得 agent-assisted engineering、项目实践、比赛参赛素材和真实协作经验。
- 老师获得教学实践和项目成果沉淀。
- 项目侧获得多设备、多环境、多开发习惯下的真实反馈, 用于修 3CAN 启动、路由、回写、发布包和协同规则。

初始 SaaS 开发组规划为 2 名核心大三学生 + 2 名辅助大二学生。成员加入、退出、分工变化、任务线变化必须写回 3CAN, 不依赖聊天历史。

详见 [PROJECT_GROUP_COLLABORATION_2026_05.md](./PROJECT_GROUP_COLLABORATION_2026_05.md)。

## 一句话定位

**3CAN = 个人/小团队开发者的项目协同 substrate + 精确记忆指引引擎**。不是通用记忆产品, 不是自主 agent, 是**"给多个已有 agent (Claude Code / Codex / Gemini CLI) 提供共享知识图谱 + 多 agent 协同机制 + token 整盘诊断 + 错误不犯指引"** 的底座。

## 不是什么 (防误解)

不做**具体对标**, 只说 3CAN 不覆盖什么场景:

| 不是 | 为什么不是 |
|---|---|
| ❌ 通用对话记忆 | 只管项目状态图谱, 不管用户兴趣爱好 / 日常闲聊历史 |
| ❌ 自主 agent runtime | 不跑任务, agent (Claude Code / Codex / 其他) 才跑任务 |
| ❌ 跨项目能力市场 | 不做跨 agent 能力共享, 只管单项目协同 |
| ❌ 代码库索引器 | 不抽 AST, 不做代码结构; 做**决策 + 会话 + 接口契约 + 错误教训** 图谱 |
| ❌ 通用 RAG 替代品 | 是 RAG 的"精确索引层", 不是"答案生成器" |
| ❌ 大团队 / 企业多部门权限 | 无权限体系, 无多租户隔离; 目标是 OPC + 2-5 人小团队 |
| ❌ "一键自动化" 工具 | 所有 LLM 产出建议进 PROPOSED-* (审批流), 不自动 merge / 不自动执行 |

## 文档分卷

| 文档 | 内容 | 对谁重要 |
|---|---|---|
| [PRD.md](./PRD.md) | 产品定义 + 用户是谁 + 解决什么问题 | 外审 / 竞赛评审 |
| [ARCHITECTURE.md](./ARCHITECTURE.md) | 引擎结构, route pipeline 4 步 + 节点/边数据模型 | 开发者 / 接入方 |
| [FEATURES.md](./FEATURES.md) | 11 项已实现能力逐条详解 (含代码文件定位) | 开发者 / 外审 |
| [TOKEN_OPTIMIZATION.md](./TOKEN_OPTIMIZATION.md) | **核心差异化**: token 整盘诊断 + 瘦身 (the maintainer 明确 high priority) | 外审 / 个人开发者 |
| [PROJECT_GROUP_COLLABORATION_2026_05.md](./PROJECT_GROUP_COLLABORATION_2026_05.md) | 2026-05 项目组内开放、高校共建、多用户优化和回写规则 | 项目组 / 维护者 |
| [CHINESE_ROUTE_SEMANTICS.md](./CHINESE_ROUTE_SEMANTICS.md) | 中英双语说明中文 route 语义栈: BGE-M3, bge-reranker, 共现扩展, query_expander 边界 | 中文项目 / route 调优 |
| [LLM_POLICY.md](./LLM_POLICY.md) | LLM 接入地图: BYOK、route-time LLM、关键词/RAG 管理、token 诊断、无 key 降级、开源边界 | 开源用户 / 引擎开发者 |
| [BENCHMARK.md](./BENCHMARK.md) | X+3CAN 评分方法 + 真实跑过的数据 + 诚实局限 | 外审 |
| [LIMITATIONS.md](./LIMITATIONS.md) | 我们不擅长什么 / 哪些声称没有数据支撑 | 外审 / 社区 |
| [NAMING.md](./NAMING.md) | 3CAN 名字缩写解 + 是否改名讨论 | 开源决策 |
| [ATTRIBUTION.md](./ATTRIBUTION.md) | **所有借鉴来源清单**, 每条含 paper/repo + 我们的映射 | **对外开源必读** |

## 外审说明

本文档写法严格遵守三条:
1. **借鉴就是借鉴** — 不把 Cormack 2009 的 RRF 说成"我们发明"; 不把 Graphify 的 Leiden 说成"我们独创"
2. **没数据的不吹** — 凡说"更快/更准", 必带 benchmark 数据支撑; 没跑过的评测只写"未测", 不编
3. **局限先列** — LIMITATIONS.md 是第二个要读的文档, 不是附录

GPT-5.4 审阅请优先看 **ATTRIBUTION.md + LIMITATIONS.md + BENCHMARK.md**, 判断是否有夸大或漏归属。
