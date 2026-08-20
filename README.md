# 3CAN Engine

> 给 Codex、Claude Code、OpenCode、Gemini CLI、Kimi、DeepSeek 等 Coding Agent 使用的本地项目知识图谱与协作引擎。

[English](./README.en.md) · [中文用户指南](./docs/USER_GUIDE.md) · [项目接入手册](./docs/PROJECT_KIT.md) · [API 规范](./docs/specs/3CAN_ENGINE/PROTOCOL.yaml)

[![CI](https://github.com/hallowkayj-spec/3CAN-engine/actions/workflows/ci.yml/badge.svg)](https://github.com/hallowkayj-spec/3CAN-engine/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB)
![Local first](https://img.shields.io/badge/runtime-local--first-18a999)
![License](https://img.shields.io/badge/license-PolyForm%20Noncommercial-f59e0b)

![3CAN 图谱界面](./3CAN-engine-frontend.png)

## 一句话说明

3CAN 把项目里真正需要跨 Session 保留的内容——架构、决策、接口、流程、错误经验、证据和交接——组织成可检索、可追溯的图谱。Agent 开始任务时按需读取相关上下文，完成后把有长期价值的结论写回；Git 仍负责精确代码历史，CI、运行时和外部平台回执仍负责证明行为。

它主要解决这样的实际问题：

- 同一项目开了多个 Agent/Session，反复解释背景，仍会丢失关键约束；
- 非技术背景的 Owner 能说清产品、结构、设计哲学，却难以把这些意图稳定传递给每个 Agent；
- 历史错误、临时修复和取舍散落在聊天与文档里，下一次又重踩；
- 全量注入项目上下文太贵，普通搜索又缺少语义、适用范围和当前状态；
- 多 Worktree 并行时，需要共享项目事实，但不能共享某个 Session 的私有执行状态。

## 🌱 它不是“立项研发”出来的，而是从真实交付里长出来的

3CAN 一开始并不是一个准备商业化的“AI 记忆产品”。维护者是非专业技术背景的 OPC，真正目标一直是把自己的产品、平台、自动化和内容项目交付出来。早期与 Codex、Claude Code 协作时，项目主要依靠 PreToolUse hook、固定提示词、handoff 文档和人工 Session 交接维持轨道；这些办法在单任务里有效，但项目变多、周期变长、多个 Agent/Worktree 并行后，会出现重复解释、上下文漂移、历史错误复发和证据散落。

3CAN 因此在真实开发过程中逐步形成：先把会话经验变成节点，再增加语义路由、精确读取、活动哈希链、项目身份、Owner Intent、ticket/writeback、ErrorKnowledge、并发一致性和公开发布边界。它的功能不是从一张预设路线图里一次设计完，而是每次遇到真实阻碍后，寻找能长期复用且不会增加第二套真相源的最小结构。

概念层面上，3CAN 受到图结构、神经网络的分层表征，以及 Yann LeCun 的立场论文 [*A Path Towards Autonomous Machine Intelligence*](https://openreview.net/pdf?id=BZ5a1r-kVsf) 中“世界模型、记忆、目标与分层规划应形成一致整体”的启发。3CAN 没有复现 JEPA，也不是该论文的实现；它只是把“Agent 需要一个可查询的项目世界模型”转译为工程系统。

另一条独立思路来自约束满足（SAT）式的工程纪律：项目、Agent、Worktree、ticket、版本和证据都是显式约束；缺失或矛盾时返回 typed gate，而不是猜测后继续。这里的“SAT 式”是设计类比，3CAN 本身不是 SAT solver。

这段经历也决定了 3CAN 的定位：**先帮助 Owner 和 Agents 更顺畅地交付，再谈更宏大的自治。**

## 当前状态与许可证

- 版本线：**v0.2.0 release candidate**；GitHub Release 以仓库的 [Releases](https://github.com/hallowkayj-spec/3CAN-engine/releases) 页面为准。
- 许可证：**PolyForm Noncommercial License 1.0.0**。
- 这是源代码公开、可学习和非商业使用的 **source-available** 项目，不是 OSI 定义的开源许可证。
- 个人非商业学习、研究、修改与分享按许可证允许；公司内部、客户项目、SaaS、付费产品等商业用途需要另行取得书面许可。

完整条款见 [LICENSE](./LICENSE)、[中文授权说明](./LICENSING.md) 和 [English licensing note](./LICENSING.en.md)。

## 🧠 3CAN 能做什么

### 🧭 项目现实与语义路由

`POST /api/route` 根据当前任务、项目身份和工作区，返回一小组相关节点，而不是把整张图塞进提示词。节点可表达项目、接口、流程、环境、决策、文档、Session、反馈、ErrorKnowledge 等语义。

### 🧩 跨 Session 的耐久知识

Agent 可以读取精确节点、查看项目 briefing，并把完成的里程碑、决策、错误根因和验证证据做有边界的 writeback。3CAN 保存的是项目级意义，不是某个聊天窗口的临时思考或私有执行状态。

### 🧯 ErrorKnowledge

错误先以 occurrence 记录；只有兼容、确定性的重复错误才提升为 ErrorCase。修复完成后可以关联解决方案、验证证据、适用项目和 superseded lineage，避免把每次普通拒绝都变成永久噪声。

### 🧑‍🤝‍🧑 多 Agent / 多 Worktree 协作

每次有状态操作都可以绑定 `AgentId`、项目/命名空间、物理 Worktree/Workspace，以及需要时的 `WorkorderId`、`TicketId` 和 target/scope digest。这样多个客户端可以并行使用同一图谱，同时把冲突限制在真正相撞的节点或治理对象上。

### 🎯 Owner Intent

项目根目录可放一个人类可编辑的 `3CAN.md`，描述谨慎程度、上下文大小、外部变更确认、Review 和 writeback 偏好。它帮助非技术 Owner 稳定表达工作方式，但不会绕过凭据、项目隔离、Git、CI、生产发布或删除保护。

### 🔒 本地优先与可视化

默认只绑定 `127.0.0.1`。图谱、嵌入缓存、活动与票据状态保留在本机指定目录；Web 界面提供图谱检索、子图激活和状态查看。除非你自己配置外部模型或代理，核心 HTTP 服务不要求把项目资料上传到 3CAN 维护者。

## 3CAN 不是什么

- 不是聊天机器人，也不是 ChatGPT/Codex/Claude 的替代品；
- 不是自动写代码、自动发布或自动花费 API 额度的 Agent Runtime；
- 不是 Git、CI、Issue Tracker、数据库或凭据系统的替代品；
- 不是把所有日志永久收集起来的监控平台；
- 不是面向公网裸露的多租户 SaaS。本版本没有内建公网认证层。

## 非技术用户：10 分钟本地体验

你不需要单独开一个“3CAN 管理聊天 Session”。3CAN 是一个本机服务；任意支持 HTTP 或 MCP 的 Agent 都可以连接它。一个项目可以使用独立端口和独立图谱，多个 Session 也可以共享同一个受管理实例。

### Windows 11 / PowerShell

要求：Git、Python 3.11 或更高版本。

```powershell
git clone https://github.com/hallowkayj-spec/3CAN-engine.git
Set-Location 3CAN-engine
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-min.txt
.\scripts\init-project.ps1 -ProjectDir . -Port 9711 -StartServer
python .\scripts\verify_project.py --base-url http://127.0.0.1:9711 --min-nodes 10
```

### macOS / Linux

```bash
git clone https://github.com/hallowkayj-spec/3CAN-engine.git
cd 3CAN-engine
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-min.txt
./scripts/init-project.sh --project . --port 9711 --start-server
python scripts/verify_project.py --base-url http://127.0.0.1:9711 --min-nodes 10
```

验证通过后打开：

- 图谱首页：<http://127.0.0.1:9711>
- Token 面板：<http://127.0.0.1:9711/static/token_usage.html>
- API 文档：<http://127.0.0.1:9711/docs>

`9711` 只是项目本地示例端口，不是硬编码要求。若你已经有一个机器级共享实例，可以使用另一个端口，但同一个端口只能有一个明确的运行时 Owner。

## 给现有项目接入 Agent

最简单的接入顺序：

1. 先按上面的命令启动并验证一个项目本地实例；
2. 把 `examples/codex-cli-project-kit/` 复制到目标项目；
3. 将 `.agents/project.template.json` 改名为 `.agents/project.json`，填写项目 ID、命名空间、名称和 Git 仓库；
4. 将 `AGENTS.template.md` 改名为 `AGENTS.md`，并合并项目已有规则；
5. 复制根目录 `3CAN.md` 到目标项目，编辑支持的工作偏好；
6. 通过环境变量设置 `THREECAN_BASE_URL`，再运行项目 Kit 的 `doctor` 与只读 `route`；
7. 只有需要治理写入时才使用 ticket/prepare/done，不要把每次普通读操作都变成仪式。

`doctor` 必须报告 `project_identity.status=pass`，才能申请 mutation ticket。

详见 [docs/PROJECT_KIT.md](./docs/PROJECT_KIT.md)。Claude Code 示例见 [CLAUDE_CODE_INTEGRATION.md](./docs/specs/3CAN_ENGINE/recipes/CLAUDE_CODE_INTEGRATION.md)。任何 HTTP 客户端也可以直接使用：

```text
GET  /api/stats
POST /api/route
GET  /api/nodes/{node_id}
POST /api/activity/log
GET  /api/token-usage/overview
```

## 新项目会得到什么

发布包不携带任何维护者图谱。初始化时会生成一个通用、可重复运行的基础图谱，用于表达项目身份、环境、文档、接口、流程、决策、Session 和错误知识等基础类型。真实业务节点只来自你的项目扫描、明确导入或后续 writeback。

每个项目应至少隔离：

- `THREECAN_GRAPH_DIR`：图谱与运行时状态目录；
- `THREECAN_PROJECT_DIR`：目标项目根目录；
- `THREECAN_BASE_URL` / 端口；
- `.agents/project.json`：不含本机路径和端口的耐久项目身份。

不要把某个项目的 graph、SQLite、embedding cache、activity log 或本机日志复制成另一个用户的“默认数据”。

## 隐私与发布包边界

官方发布包由 Git 的精确提交构建，只包含已跟踪的公开仓库文件，不包含 `.git`、未跟踪文件或本机运行时数据。构建后会解包并再次执行严格扫描，拒绝：

- `.env`、API Key、Token、Cookie、密码、恢复码和凭据文件；
- 用户目录、维护者绝对路径、机器标识和私有项目重绑定信息；
- 真实 graph 节点、embedding、SQLite/WAL、activity、Agent 状态和日志；
- 私有 RPA、账号、业务数据、个人偏好和本机部署回执。

维护者构建命令：

```bash
python scripts/prerelease_scan.py --strict
python scripts/build_release.py --version v0.2.0-rc.1 --output-dir dist
```

输出包括 ZIP、SHA-256 文件和 JSON 构建回执。用户可校验：

```bash
python scripts/prerelease_scan.py --strict
```

注意：扫描只能证明已定义的公开包边界和高置信规则通过，不能数学上证明任何软件绝对没有隐私风险。发布前仍应人工审查变更与 Git 历史。

## 项目结构

```text
neural-memory/backend/       FastAPI 服务、图引擎、路由、票据与 ErrorKnowledge
neural-memory/frontend/      本地图谱与 Token 面板
neural-memory/expansions/    中文与领域词扩展
neural-memory/tests/         回归、并发、隔离、发布与安全测试
examples/                    Codex / Claude Code 接入示例
scripts/                     初始化、验证、隐私扫描与发布包构建
docs/                        用户指南、项目 Kit、协议、边界与证据说明
```

## 🧪 公开测试与自评

3CAN 不使用一个“综合总分”掩盖不同能力层。当前仓库可复现的公开证据如下：

| 验证面 | 公开候选结果 | 能证明什么 |
| --- | ---: | --- |
| Route benchmark | 46 queries；MRR `0.9783`；Recall@1 `0.8261`；Recall@3 / Hit@3 `1.0` | 在 16 节点公开合成 seed graph 上，任务路由能稳定找到预设相关节点 |
| Substrate benchmark | 10 cases；Top-1 `1.0`；Top-3 mean recall `0.8167`；ERR proactive@3 `1.0` | 公开 fixture 中的项目结构、接口和错误提示能按预设答案出现 |
| 本地发布验收 | `450 passed`；Ruff、严格隐私扫描、ZIP 解包扫描通过 | 当前候选的合同、并发、隔离、发布与安全回归在该测试环境通过 |
| GitHub clean clone | Ubuntu + Windows 独立 `9701` 冷启动、route/writeback、停止回收通过 | 一个不依赖维护者图谱的全新 checkout 能安装并运行 |

完整内容寻址回执见 [SEED_GRAPH_BENCHMARK_20260809.json](./docs/evidence/SEED_GRAPH_BENCHMARK_20260809.json)。这些数字是**官方自建 fixture 的能力证明，不是第三方排名**：它们不证明私有生产图质量、真实 OPC 长期收益、跨机器延迟，也不能直接与 Mem0、Graphiti、Letta 等不同赛道产品比较。仓库保留的历史 LongMemEval 试跑受 judge、runner 与 fixture 版本影响，不作为本候选的发布分数。

我们更关心后续 dogfood 是否减少重复解释、缩短恢复时间、避免重复错误，并让并行 Agent 真正完成交付；这些长期指标会继续按独立证据更新，而不会通过预估填满。

## 📐 证据与限制

- CI 运行 Ruff、Python 语法检查、完整测试、严格发布扫描，以及 Ubuntu/Windows 的隔离 9701 clean-clone 验收。
- 仓库包含合成 seed graph 的可复现候选评测；它不等于私有生产图、真实业务或跨机器性能保证。
- 最小依赖使用 hashing embedding，适合安装验证；BGE-M3 / reranker 属于可选完整语义栈。
- 当前主要面向单机 OPC、小团队和受控多 Agent 工作流；公网、多租户与企业级权限需要额外安全层。
- 3CAN 能改善上下文检索与协作纪律，但图谱质量、节点适用性和 Agent 行为仍需要 Owner 与工程 Review。

当前能力边界与证据状态见：

- [CURRENT_3CAN_CAPABILITY_BASELINE.md](./docs/evidence/CURRENT_3CAN_CAPABILITY_BASELINE.md)
- [RELEASE_VALIDATION_20260809.md](./docs/evidence/RELEASE_VALIDATION_20260809.md)
- [STABILITY_TIERS.md](./docs/specs/3CAN_ENGINE/STABILITY_TIERS.md)
- [ERROR_KNOWLEDGE_LIFECYCLE.md](./docs/ERROR_KNOWLEDGE_LIFECYCLE.md)

## 文档导航

| 你想做什么 | 从这里开始 |
| --- | --- |
| 第一次安装、理解术语 | [中文用户指南](./docs/USER_GUIDE.md) |
| 给现有项目接入 Codex/Claude 等 Agent | [Project Kit](./docs/PROJECT_KIT.md) |
| 查看 HTTP 请求示例 | [API Usage](./docs/specs/3CAN_ENGINE/API_USAGE.md) |
| 理解项目隔离、并发和 writeback | [Contracts](./docs/specs/3CAN_ENGINE/CONTRACTS.md) |
| 查看安全边界 | [SECURITY.md](./SECURITY.md) |
| 卸载且保留数据 | [UNINSTALL.md](./UNINSTALL.md) |
| 贡献代码 | [CONTRIBUTING.md](./CONTRIBUTING.md) |
| 查看版本变化 | [CHANGELOG.md](./CHANGELOG.md) |

## 维护者说明

3CAN 来自长期、多项目、多 Agent 的真实 dogfood，也大量使用 AI Agent 协助开发。这带来了实际工作流经验，也意味着我们更需要可复现的 Bug、失败测试、边界质疑和独立 Review。欢迎提交 Issue 与 PR；请不要上传自己的图谱、凭据、账号数据或包含私有路径的回执。
