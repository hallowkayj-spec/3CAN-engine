# 3CAN source-available 发布前清单 (v0.1)

> **目标**: 2026-05 先进行项目组内开放和共建优化, 再做 broader public source-available release.
> **版本标识统一 (硬规则)**: tag 为 `v0.1.0`, 文案一律用 **"active prototype"** / **"experimental developer preview"**. **绝不使用 "alpha" 词** (避免措辞混乱). "prototype" 和 "preview" 是表达相同意思的正确词.
> **第一印象一致性硬原则**:
> - 不标榜 "第一 / 领先 / 最强"
> - 客观说现状; 坦承 vibecoding / 0 代码基础
> - 借鉴源全部点名 + 感谢
> - 所有数字带 caveat
> - **凡未落代码的 ≠ 已有能力**: README / LLM_POLICY / 任何对外文档里, 未实现的功能**必须标注 `planned v0.1.x`**, 不与"已实现"项混淆. 这是开源第一波最容易被审计挑出的 mismatch 点.

## 2026-05 节奏修正

公开发布前新增一个受控阶段:

1. **项目组内开放**: 面向上海理工大学老师和学生组成的小组, 用真实设备、真实开发习惯和真实任务压测 3CAN。
2. **共建优化**: 聚焦启动可靠性、shadow graph 防护、跨设备安装、学生 onboarding、多 agent 协作、回写纪律和发布包清理。
3. **收拢发布**: Ka 统一收拢贡献、规则和 release artifact。公开发布前仍需完成 H1-H10。

内部可简称"开源准备", 但对外文档仍使用 **source-available**。当前 license 是 PolyForm Noncommercial 1.0.0, 不是 OSI open source。

项目组协作细则见 `PROJECT_GROUP_COLLABORATION_2026_05.md`。

发布隔离规则见 `RELEASE_ISOLATION_POLICY.md`。核心口径: runtime 机制必须发布, 本项目 dogfood runtime state 必须隔离。

## 硬阻塞 (今天 — 发布当天必须 100% 完成, 否则不开源)

> GitHub community profile 会自动检测这些文件. 缺了 LICENSE = 访客没有合法复用权 (默认保留所有权). 缺 README = 第一眼空白.

### H1. LICENSE / NOTICE / LICENSING (根目录, 4 文件)

**License 决定: PolyForm Noncommercial 1.0.0** (source-available；允许许可证定义的非商业个人、研究、修改与分发用途；任何商业用途需单独书面授权；不是 OSI open source). 决策依据见 `LICENSING.md` FAQ.

- [x] 根 `LICENSE` — PolyForm Noncommercial 1.0.0 官方原文（原文不得修改，仓库内只保留这一份 canonical copy）
- [x] 根 `NOTICE` — 版权 + Required Notice 行（公开主体为 `hallowkayj-spec`）
- [x] `LICENSING.md` — 人话 FAQ (已落盘)
- [ ] 根 `README.md` License 段引用上述 3 份文件
- [ ] `pyproject.toml` license 字段设为 `{text = "PolyForm-Shield-1.0.0"}` (非 SPDX 标准标识, 也可用 `{file = "LICENSE"}`)

**对外话术硬规则**:
- 对外描述: **"source-available"**, **绝不**说 "open source" (OSI 定义明确排除)
- GitHub repo 描述: "Source-available graph-backed project substrate prototype"
- Release Notes 标 "source-available under PolyForm Noncommercial 1.0.0"

### H2. 根 `README.md`
- [ ] 一页简介, < 150 行
- [ ] 第一句: **"A graph-backed project substrate for multi-agent coding workflows"** (和 `docs/specs/3CAN_ENGINE/README.md` 定位一致, 不自相矛盾)
- [ ] 指向 `docs/specs/3CAN_ENGINE/README.md` 作主 README
- [ ] 明示: **active prototype / experimental developer preview** (不用 alpha)
- [ ] About the author 坦承段
- [ ] 5 个主文档链接 (README / PRD / EVIDENCE / LIMITATIONS / ATTRIBUTION)

### H3. `.gitignore`
- [ ] **`secrets.json`** (绝对不 commit)
- [ ] `graph/nodes/` (dogfood 数据, 非开源内容)
- [ ] release staging 不得包含真实或误导性 graph runtime: `graph/nodes/`, `activity_log.json`, `agents.json`, `embeddings.npz`, observer logs
- [ ] runtime state 目录只保留 `.gitkeep` / README, 样例图必须放在 `tests/fixtures/` 或 `examples/sample_graph/`
- [ ] `*.sqlite` / `*.db`
- [ ] `__pycache__/` / `*.pyc` / `node_modules/`
- [ ] `.env` / `*.env`
- [ ] `_archive/` / `_longmemeval/` / `_substrate/` / `_harness/` / `_wave2/` (benchmark 输出)
- [ ] `embeddings.npz` / `*.npz`
- [ ] `~/.claude/logs/` 引用 (绝不 commit)

### H4. `SECURITY.md` (根目录)
- [ ] 漏洞上报方式 (email / GitHub Security Advisory)
- [ ] 披露窗口政策 (建议 90 天)
- [ ] **localhost-only 默认绑定**明示: backend 仅监听 `127.0.0.1`, 不绑 `0.0.0.0`
- [ ] 用户不要把 `secrets.json` / API key / 图谱数据 提交进 git
- [ ] 明示 live dogfood graph / activity logs / agent state 不属于 release artifact

### H5. `CONTRIBUTING.md` (根目录)
- [ ] Issue template (bug / feature / question)
- [ ] PR 规范 (小而聚焦 / 带测试 / 过 ruff)
- [ ] 提交节奏 (维护者少, 响应时间预期)
- [ ] 诚实段: "作者 vibecoding 出身, 代码质量会有专业工程师能发现的问题, 特别欢迎 hard criticism"

### H6. 最小 install 路径 (可跑)
- [ ] `install.sh` (Linux / Mac / Git Bash on Windows) — 最小单命令:
  - `pip install -r requirements.txt`
  - 启动 backend (`python neural-memory/backend/app.py --port 9701`)
  - 启动 proxy (`python neural-memory/proxy/server.py`)
  - 打印 "check http://localhost:9700/api/stats"
- [ ] `requirements.txt` 或 `pyproject.toml` 依赖清单
- [ ] 依赖清单覆盖 route/writeback 硬依赖: `sentence-transformers`, `numpy`, `scikit-learn`
- [ ] smoke test 不只测 `/api/stats`; 必须覆盖 `/api/route` 或 `/api/route/simple`, 避免发布一个只能 liveness 不能检索的半健康包

### H7. 最小 CI (GitHub Actions)
- [ ] `.github/workflows/ci.yml` 做 3 件事:
  - `ruff check` 过全仓
  - `python -m py_compile` 所有 .py 文件 (语法检查)
  - 冷启动 smoke test (spawn backend + curl /api/stats + 断言 200)
  - release isolation scan: 阻止 live graph/logs/embeddings/secrets 进入 release artifact

### H8. Secrets 快扫 (发布当日)
- [ ] `git ls-files | xargs grep -iE "api[_-]?key|password|secret|token.*=.*['\"][a-z0-9]{20}" | wc -l` 应返 0
- [ ] 检查 AutoDL / RPA / provider 脚本没有明文账号、密码、API key
- [ ] 目测 `git log --all --oneline | head` 确认无意外 commit

### H9. Backend 默认安全基线
- [ ] **确认 `app.py` 默认 `host=127.0.0.1`**, 不是 `0.0.0.0`
- [ ] 如果必须 `0.0.0.0`, 加启动时 warn + 文档明示安全风险

### H10. CHANGELOG.md (根目录)
- [ ] v9.0 → v9.5 简要历史
- [ ] v0.1.0 首发内容
- [ ] 标注从 v9.x 内部版本 → v0.1.0 公开发布的对应关系

## 软阻塞 — 可滑到 v0.1.1 / v0.1.2 (开源后陆续补)

### S0. 项目组内开放反馈闭环
- [ ] 2 名核心大三学生 + 2 名辅助大二学生的初始职责写回 3CAN
- [ ] 老师/学生环境矩阵记录: OS, shell, Python, IDE, coding agent
- [ ] 至少 5 个新设备完成 `doctor -> bootstrap -> route -> writeback` 闭环
- [ ] 每个重复 onboarding 问题都形成 `ERR-*` 或 `PRO-*` 节点
- [ ] 成员加入/退出/换责均写回 3CAN, 不靠聊天记忆

### S1. 容器化
- [ ] `docker-compose.yml` — backend + proxy 容器化
- [ ] `Dockerfile`

### S2. 跨平台安装
- [ ] `install.ps1` (Windows PowerShell)
- [ ] `Makefile` (Linux/Mac 可选)

### S3. LLM 工具链
- [ ] `tools/llm_provider.py` 多 provider 抽象层
- [ ] 每个 LLM 工具加 `--estimate-cost` 参数
- [ ] llama.cpp 本地接入实测 (当前是设计, 未实测, 需标 planned)

### S4. Benchmark 扩题
- [ ] substrate-bench v2 扩到 25 题, 使用 prefix-匹配而非具体 node ID (让社区可复用)
- [ ] harness-bench v2 加 valid-ticket 场景
- [ ] Ablation C (full + cumulative + str-fix) 补跑

### P0.1 核心文档 (已完成 / 需小改)

- [x] `README.md` (中英双语) — S66g 已重写, 加 "About the author" 诚实段
- [x] `PRD.md` (产品定义) — S66g 已更新路线图
- [x] `ARCHITECTURE.md` — 稳定, 无需改
- [x] `FEATURES.md` — 稳定
- [x] `API_USAGE.md` — 加了 route_ticket / activity_log 端点
- [x] `LIMITATIONS.md` — §2bis 加了 S66g 新增诚实项
- [x] `BENCHMARK_POLICY.md` — 三层评分框架落地
- [x] `BENCHMARK.md` — §2.1 重写 + Ablation 表
- [x] `SELF_AUDIT_SCORECARD.md` — v9.5.2 按 3 层重排 + 体感校准
- [x] `LLM_POLICY.md` — 2026-04-28/29 收束为 release-facing LLM 接入地图: retrieval model / tokenizer / generative LLM 三层分离, BYOK, route-time LLM 低频触发, no-key 降级, shipped/partial/planned 显式标注
- [x] `RELEASE_CONSISTENCY_AUDIT_20260429.md` — staging / README / LLM_POLICY / checklist 口径一致性审计与剩余 gate
- [x] `ATTRIBUTION.md` — 借鉴源全点名 + 感谢, 加 B10 Obsidian
- [x] `STABILITY_TIERS.md` — Stable / Experimental / Research 分级
- [x] `DEPLOYMENT.md` — §1.7 sentinel bootstrap 文档化
- [x] `AGENT_BINDING.md` — 稳定
- [x] `CONTRACTS.md` — 稳定
- [x] `NAMING.md` — 稳定
- [x] `PROTOCOL.yaml` — v9.4.0
- [x] `REAL_UAT_PLAN.md` — 稳定
- [x] `EVIDENCE.md` — S66g 新建 + §3.5/3.6/3.7 MRR/dogfood/GH
- [x] `recipes/CLAUDE_CODE_INTEGRATION.md` — S66g 新建
- [x] `recipes/CODEX_CLI_INTEGRATION.md` — S66g 新建
- [x] `OPEN_SOURCE_CHECKLIST.md` — 本文件

## 中期 — v0.1.1 / v0.1.2 (开源后 1-6 周内推)

- [ ] `CODE_OF_CONDUCT.md` (Contributor Covenant 模板即可)
- [ ] `ROADMAP.md` 短中长期
- [ ] `pyproject.toml` Python 包元数据
- [ ] substrate-bench v2 扩到 20-25 cases (通用 prefix 匹配, 不绑 Ka 节点 ID)
- [ ] harness-bench v2 加 valid-ticket 场景 3-5 题
- [ ] Ablation C 补跑
- [ ] bi-temporal validity 架构占位 (`valid_from` / `valid_until`)
- [ ] 58 个 ruff style-only 残留清理
- [ ] Docker Compose 完整

## 长期 — v0.2 目标 (3-6 月)

- [ ] bi-temporal validity 完整实现
- [ ] Hierarchical Leiden
- [ ] Online IDF 重算
- [ ] 跨 IDE 实测 (Zed / Cursor / Continue.dev)
- [ ] Real UAT 累积 ≥20 scenarios closed
- [ ] pgvector + HNSW 迁移 (10K+ 节点后)
- [ ] v0.2 tag 门槛: substrate v3 top1 ≥ 0.80 + harness v3 生产触发率 ≥ 50% + Real UAT ≥ 20

## 发布当天 SOP

1. 最后一次全仓 `ruff check` — 过
2. `python -m py_compile $(git ls-files '*.py')` — 0 error
3. `git log --all --oneline | head` — 确认无意外 commit
4. `git ls-files | xargs grep -iE "api[_-]?key|password|secret|token.*=.*['\"][a-z0-9]{20}"` — 应返 0
5. 根 `README.md` 最后一次校对 (第一屏没用 "alpha", 没打 stars 排名, 没标榜第一)
6. H1-H10 全 check 过
7. Release tag **`v0.1.0`** (不带 `-alpha` 后缀)
8. Release Notes 措辞: **"active prototype / experimental developer preview. Built vibecoding by a non-traditional developer. Hard criticism warmly welcome."** (不用 alpha 词)
9. 简短发布说明 (可选, HN / r/LocalLLaMA / 知乎 / 小红书)

## 不做的

- ❌ 不使用 "alpha" 词 (改用 prototype / preview)
- ❌ 不写营销软文
- ❌ 不标榜 "第一 / 最强 / 领先"
- ❌ 不对比某家具体产品的高低
- ❌ 不公开任何 benchmark 数字 without 4 caveat
- ❌ 不承诺 v1.0 时间表 (等数据说话)
- ❌ **不把未落代码的能力在 README 写成"已有"** (第 1 印象 mismatch 雷区)

## 第一周回应准则 (开源后 7 天)

- 所有 issue 在 48 小时内至少回复 1 次 (哪怕是 "看到了, 3 天内回").
- 任何 "你 README 写的 X 实际是 planned" 类 issue → 立即更新 README + thank submitter, 不辩护.
- 任何 "你跟 Y 比如何" 类 issue → 一律引到 LIMITATIONS §0 的 "不做的场景" 清单, 不做跨产品比较.
- 任何安全相关 issue → SECURITY.md 流程处理, 不公开 debug.
