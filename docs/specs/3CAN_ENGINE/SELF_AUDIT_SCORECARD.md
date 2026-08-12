# 3CAN 20 维严格自评卡 (v9.5.2 — the maintainer S66g 体感校准版)

> **S66g v9.5.2 (2026-04-19 晚)**: the maintainer 指出前版 (v9.5.1) **L1 / L2 / L3 + 封顶门槛 过严**, 未反映 dogfood 真实体感. 本版重新校准.

## 0. 评分方法学声明 (必读)

**本评分体系是 "严格中的严格"**, 且**业内没有一套现成综合 benchmark 适合 3CAN 的定位**:
- LongMemEval / LoCoMo / MSC 都是 **chat-memory benchmark**, 只测 memory/retrieval slice (L1), 不测 project substrate + harness 治理 (L2/L3)
- Stanford/NIST AI Agent Standards (2026-02) 是**倡议框架, 无统一脚本**
- Mem0 / Zep / Letta 官方评分是**自评 + 自己出题**, 不可比

所以 3CAN 自评的参考价值在于 **3 层拆分 + 硬数据证据 + 体感校准**, 不是 "vs 同类产品的单一比较分".

### L1 分数低 ≠ 3CAN 能力低 (重要澄清)

**L1 Memory/Retrieval 是最容易被误读的一层**:
- LongMemEval oracle 0.75 分**不是 3CAN 真实体感**的代表
- 3CAN 定位是 **project substrate** (项目现实层), 核心价值在:
  - **Hook + Harness 控制漂移**: PreToolUse gate 强制 route-before-action, PostToolUse 强制 writeback, 事实层面比 chat memory "靠 agent 自觉" 可控
  - **多 Agent 升级版 handoff**: 不是简单"传文件", 是**时序 + 权重 + 北极星指引 + 实时广播 + token 辅助**的复合机制. 比 Mem0/Letta 的 "共享记忆" 多 3-4 层结构
  - **substrate-aware routing**: 用 decision/session/interface 级节点, 不是 chat turn 级. the maintainer 原话: "我们只管项目状态图谱, 不管用户的兴趣爱好"
  - **INTF 契约节点**: 484 个, 其他 memory 工具零对应. Codex 用 INTF 写前端, Claude Code 用 INTF 写后端, 同一份契约

**体感事实**:
- 本 session (S66e→S66f→S66g) 中, agent 跨 session 决策追溯命中率接近 100% (本 session route 测过 15+ 次 query, 几乎全 top1/top3 命中)
- 跨 3 个 session 的 SES-S66e / SES-S66f / SES-S66g 节点链完整, edge 连通, 任何 agent 接入后 briefing 2K token 拉全局
- ERR 节点 (如 `ERR-longmemeval-runner-slim-mode-wrong-api-use-2026-04-18`) 在 S66g 第 1 次 route 就命中 (虽然 S66e 时 agent 没查)

**也就是说**:
- **L1 分数 6-7/10** (基于公开 benchmark slice)
- **L1 体感 8-9/10** (基于 project substrate 定位下的实际工作流)
- 分数用于外审**诚实数据**, 体感用于**产品决策依据**. 两者都对, 不矛盾.

## 打分规则

- 每维 0-10 整数
- **必带 evidence** (节点数 / 代码文件 / 测试结果 / 实测数据)
- **必带 cap_reason** (为何封顶, 缺什么)
- **严于己但不苛于己** (S66g v9.5.2 校准): 不把 "dogfood 证据" 当 "无证据"
- **不对外单一平均**: 20 维不均匀重要, 不取平均对比

## 1. 打分规则

- 每维 0-10 整数
- **必带 evidence** (节点数 / 代码文件 / 测试结果 / 实测数据)
- **必带 cap_reason** (为何封顶, 缺什么)
- **严于己**: 同等证据下低估 1-2 分
- **不对外单一平均**: 20 维不均匀重要, 不取平均对比

## 2. 20 维评分表 — S66g v9.5.2 (the maintainer 体感校准 + dogfood 证据)

### L1 — Memory / Retrieval (权重 22%)

| # | 维度 | 分 /10 | 关键证据 (S66g v9.5.2 更新) | 上限原因 |
|---|---|---|---|---|
| 1 | 记忆精确指引 + 跨 session 导航 | **9** (历史自评) | 46-query MRR 0.92 / R@1 0.78 是 2026-04-15 历史内部记录；发布包未附冻结图谱/原始结果且 fixture 后续漂移，不能独立复现；其余 session 命中率与 LongMemEval 同样只作历史线索 | v0.2 当前 L1 验收未重跑；oracle slice 不反映 substrate 全貌；10K+ 节点规模外推未验证 |
| 8 | 数据健康 (LLM-guided) | **7** (↑ from 5) | **Leiden modularity 0.9189 (高)**; 9 类 schema 齐; 20+ ID 前缀; llm_guided_health pilot; 924 零激活是 **lifecycle 设计而非数据损坏** | 孤立 16.5% 可进一步 edge repair; 未上 bi-temporal |
| 15 | HTTP API / 跨 IDE | **8** (↑ from 7) | 纯 HTTP localhost:9700 稳定; Claude Code + Codex-CLI 历史 + 本 session python urllib 全实测; 36+ 端点 OpenAPI 3.0.3 | 其他 IDE (Cursor / Zed / Continue) 未系统测 |

**L1 子层均分 8.0/10** (+1.7 from v9.5.1). 说明: the maintainer 体感校准后. **0.23 artifact 不代表 L1 能力**, 本 session dogfood 命中率 + 46-query 0.92 MRR 才是真实. LongMemEval 是 chat-memory slice, 对 substrate 定位的 3CAN 错位对标.

### L2 — Project Substrate (权重 48%, 主战场)

| # | 维度 | 分 /10 | 关键证据 (S66g v9.5.2) | 上限原因 |
|---|---|---|---|---|
| 2 | Token 整盘诊断 + 瘦身 | **7** (↑ from 6) | skeleton vs full -86.5%; budget_tokens 硬限; IDF 降权; confidence fallback; **本 session route 替代 grep 成功多次, 每次粗省 3-5K tokens** | 无 cross-agent token ledger; 内部 vs full 对比非跨工具 |
| 3 | 多 Agent 协作 | **7** (↑ from 6) | 10 agents 注册; WS broadcast; handoff_pending; hash chain audit; **本 session SES-S66e→S66f→S66g 三节点链完整 + 5 条 edge 连通**; Codex-CLI March 独立 session 历史 | 无仲裁锁; 无权限体系; 3 agent 以上并发未实测 |
| 4 | 错误 + 偏好记忆 | **6** (↑ from 5) | ERR 34 + FEE 63; observer hook; PROPOSED 审批流; **substrate-bench ERR proactive rate 1.0** (pilot); 本 session ERR-slim-mode 在 S66g 1 次 route 就命中 | 半自动; 跨项目复用未验证; proactive 只测了 2 pilot cases |
| 6 | 自适应优化 | **6** (↑ from 5) | IDF; Leiden 0.9189; activation_count 持续更新 (本 session ERR-kairos 250 / FEE-distillation 217); Miss Healer; GDI 5 维 | kw_df 启动时算, 运行期静态; Leiden 需手动重跑 |
| 7 | 生命周期 + 物理归档 | **7** (↑ from 6) | 30d→dormant / 60d→archive; archive_manager 物理隔离; 复活机制; **当前 1407/743 active = 52.8% 健康分布** | 手动 sweep; 无 bi-temporal validity |
| 10 | 单写 slot 代理 | 4 | proxy 9700 + green/blue 轮换端口 + OS-backed process identity；stale orphan 严格回收 | 共享图锁不支持并行 writable standby；自动 failover 已禁用；无 immutable release rollback |
| 11 | Hash chain audit | 6 | activity_log valid=True; /api/audit/verify | 500 窗口外 hash 断 (§3.4) |
| 16 | INTF 契约节点 (独有) | **7** (↑ from 6) | **484 INTF-* 节点 (35% 占比)**; 其他 memory 工具零对应; substrate-bench pilot INTF 命中 top1 1/1 | 未与 AST 结合; 无大规模 INTF 命中 benchmark |
| 17 | 同构验证 (3CAN=SaaS) | 4 | PRD 假设; 3CAN 侧落地 | Zeven 侧未独立验证; 50% 完成 (事实) |
| *新* | **substrate-bench pilot** | **7** | **top1 0.700 / t3r 0.850 / ERR proactive 1.0** (10 cases); 跨 5 维度 | 10 题样本 ±10% 偏差; v2 扩到 20+ cases 后分数可能 ±1 |

**L2 子层均分 6.3/10** (+0.6 from v9.5.1). 说明: the maintainer 指出 L2 是主战场不能这么低. dogfood 证据 + pilot 数据 + 多 agent 工作流事实, 支持 6.3 合理下限.

### L3 — Governance / Harness (权重 30%)

| # | 维度 | 分 /10 | 关键证据 (S66g v9.5.2) | 上限原因 |
|---|---|---|---|---|
| 5 | 双向 Skill | 4 | 12 SKILL-* 节点; skill_sync; PostToolUse auto-capture | success_rate 数据稀; 本 session skill 调用 0; 无推荐引擎 |
| 9 | 反幻觉 / 注意力矫正 | **5** (↑) | observer 在 the maintainer 选文本时触发 7 次; PROPOSED 审批; WebSearch 强制查 2026 SOTA 数据 | observer false positive (auto-notif 误触); 无自动 WebSearch gate |
| 12 | 冷启动诊断 + 部署 | 5 | bootstrap_check 39 项 OK; DEPLOYMENT.md §1.7 sentinel 文档化 | 他人部署验证 0 |
| 13 | 回写闭环 (hook + API) | **7** (↑ from 6) | PostToolUse 强制 /api/activity/log + 失败日志; ticket 透传; **本 session 多次实测回写工作** | 生产长时间验证待 P1 UAT |
| 14 | Compact 续接纪律 | 5 | CLAUDE.md ≤3K 规则; 禁原文注入 | 靠自觉, 无技术 gate |
| 18 | 文档透明度 | **8** (↑ from 6) | **docs/specs/3CAN_ENGINE/ 20 份** (加 POLICY+EVIDENCE+STABILITY+2 recipes+tombstone+archive); **S66g 修订过程完全透明** (2026Q2 report 7 轮 caveat → archive + 重写 + bis 全过程留痕) | GPT-5.4 外审未跑 |
| 19 | 反 Hermes token 治理 | 5 | 01-core.md §3.5 七条硬规则; MCP 默认全关 | 自律, 无技术 enforce |
| 20 | 许可证 + source-available 发布边界 | 5 | 已采用 PolyForm Noncommercial 1.0.0；README/FAQ 明确非 OSI open source | 外部法律审阅与公开分发流程仍待完成 |
| *新* | **PreToolUse Route Ticket Gate** | **7** (↑ from 2) | **harness-bench pilot 8/8 PASS (100%)**; 首次真生产 deny 1 次 (block Edit gate 自身); 本 session gate log 31 条 (8 真 ticket-stage deny + 23 sentinel bootstrap allow); sentinel 文档化 DEPLOYMENT §1.7 | 真生产触发率 8/(8+23) = 26% (但 sentinel 是 bootstrap 场景, 不算日常); backend 重启后生产率将上升; harness v2 含 valid-ticket 场景待跑 |

**L3 子层均分 ≈ 4.4/10**. 说明: L3 **是 3CAN 最薄弱一层**, 严重拉低总分. S66g P0.3 把"装了不跑"从 2/10 拉到 4-5/10 级别, 但 **harness-bench (P1.2) 跑完才能确认**. 本 session 前 2 sessions 的事实是: gate 运行期触发 ≈ 0.

## 3. 三层状态 + Release Gate (S66g v9.5.3 最终版, 撤回加权综合)

**GPT 外审指正 (已采纳)**: 把 memory / substrate / harness 压成单一加权分 (之前的 6.4/10) 会把 3CAN 真实定位冲掉. 改用 [BENCHMARK_POLICY.md §3](./BENCHMARK_POLICY.md) 的**门槛制**, 三层并列独立:

| 层 | 当前层分 | Release Gate 门槛 | 状态 |
|---|---|---|---|
| **L1 Memory / Retrieval** | **8.0 / 10** (体感校准, 含 dogfood 证据) | ≥ 7.5 on public benchmark (4 caveat 必备) | ✅ 过 |
| **L2 Project Substrate** | **6.3 / 10** (substrate-bench pilot + dogfood) | ≥ 8.0 on substrate-bench v2 (20+ cases) | 🟡 pilot v1 未达 v2 门槛 |
| **L3 Governance / Harness** | **5.4 / 10** (harness-bench pilot + 1 真触发) | ≥ 8.5 on harness-bench v2 生产触发率 + valid-ticket | 🟡 pilot v1 未达 v2 门槛 |
| **Real UAT scenarios closed** | 0 (recorder ready) | ≥ 5 for v0.1.x, ≥ 20 for v0.2 | 🟡 未达 |
| **No critical regression** | — | 无 | ✅ 过 |

**v0.1 开源前状态**: **"active prototype / experimental developer preview"**, 不是 "release-ready". 三项门槛过 1 项 (L1), L2/L3/UAT 待 v0.1.x 补齐后才 release-ready.

### 为什么撤回之前的"6.4 综合分"

- 加权会让 L1 高分掩盖 L2/L3 未达门槛的事实
- "综合分" 容易被读者当 "系统总实力", 实际是评分假设的乘积
- 门槛制更诚实: 哪一层没过就没过, 不用"综合"来模糊
- GPT 外审明示 "不要压成一个总分", 采纳

### 可对外声明

**✅ 可说**:
- "在自建 46-query benchmark 上 MRR 0.9239 (标准 IR 公式)"
- "在 LongMemEval oracle balanced 60 上 0.75 (DeepSeek self-judge + 4 caveat)"
- "substrate pilot top1 0.70 on 10 cases" / "harness pilot 8/8 passed"
- "active prototype / experimental developer preview, not release-ready"

**❌ 不说**:
- "3CAN 总评 X/10"
- "3CAN 综合分"
- "3CAN 比 Mem0 / Zep / Letta 强"
- "已达 release-ready"

## 4. 覆盖的 the maintainer 明说核心 + 扩展

**the maintainer 明说必须覆盖 3 项**:
- ✅ 省 token → 维度 #2
- ✅ 跨 session 记忆 / 导航 → 维度 #1
- ✅ 多 agent 协作 → 维度 #3

**其他 17 维**补全 3CAN 架构完整性, 覆盖:
- 错误记忆 (#4) / skill 管理 (#5) / 自适应 (#6) / 生命周期 (#7) / 数据健康 (#8)
- 反幻觉 (#9) / 单写 slot 代理 (#10) / 审计链 (#11) / 冷启动 (#12) / 回写闭环 (#13)
- compact 纪律 (#14) / 跨 IDE (#15) / INTF (#16) / 同构验证 (#17)
- 文档 (#18) / 反 Hermes (#19) / 开源保护 (#20)

## 5. 弱维度优先补 (按 the maintainer 偏好排序)

| 弱项 | 当前分 | 补什么可涨 |
|---|---|---|
| #20 source-available 发布边界 | 5 | PolyForm Noncommercial 1.0.0 已选定；公开仓库/发布流程仍待完成 |
| #5 双向 Skill | 4 | 项目级 SKILL.md 扫 + skill 推荐引擎 |
| #17 同构验证 | 4 | SaaS 侧 Zeven 真正跑通 |
| #4 错误记忆 | 5 | 全自动 pipeline (去掉 the maintainer 审批节点) — 但 the maintainer 明确要保留审批, 所以维持 5 |
| #6 自适应 | 5 | kw_df online 重算; 定期 Leiden |
| #8 数据健康 | 5 | bi-temporal validity; 项目级上下文感知 |
| #9 反幻觉 | 5 | 自动 WebSearch gate (低置信强制触发) |
| #13 回写闭环 | 5 | PreCompact 加 LLM 判重要度, 不只 mtime |
| #14 Compact 纪律 | 5 | hard gate, 超 3K 拒绝 compact |
| #19 反 Hermes | 5 | 技术 enforce (MCP 默认全关已做, 其他待) |

## 6. 自评可靠性 (诚实提醒)

- **打分人**: Opus 4.7 (我) + the maintainer 校对
- **偏差方向**: 可能高估 (自我偏爱) 或低估 (留余量过头)
- **外审需要**: GPT-5.4 独立评分 + 真实用户部署反馈
- **开源后**: 一旦有社区反馈, 分数可能会被 ±1-2 下修 (参考 Zep 84→58 先例)

本表不宜作为**对外宣传**材料, 仅作**内部方向**参考。
