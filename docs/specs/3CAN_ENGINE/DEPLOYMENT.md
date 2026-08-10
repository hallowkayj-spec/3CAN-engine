# 3CAN Engine — 部署与冷启动 (开源必读)

> **the maintainer 核心原则**: 开源的不是单独一个引擎, 是**完整一套构架**. 少一个组件, 部署效果 = 0. 这份文档讲清楚每个组件必须性 + 怎么装.

## 1. 3CAN 完整构架的 5 个必装组件

```
 ┌──────────────────────────────────────────────┐
 │  ① Engine (backend + proxy + frontend)      │   核心检索+API+可视化
 ├──────────────────────────────────────────────┤
 │  ② Hooks (~/.claude/scripts/hooks/*.js)     │   agent 事件 → 3CAN 自动回写闭环
 ├──────────────────────────────────────────────┤
 │  ③ Rules (.claude/rules/01-core.md)         │   agent 治理: 先查再建 / R1-R13 硬规则
 ├──────────────────────────────────────────────┤
 │  ④ Tools (neural-memory/tools/*.py)         │   维护向导: health / gdi / skill / leiden / archive / bootstrap
 ├──────────────────────────────────────────────┤
 │  ⑤ Docs (docs/specs/3CAN_ENGINE/*.md)       │   PRD / Architecture / Attribution / Limitations
 └──────────────────────────────────────────────┘
```

**任何一项缺失**:
- 没 ① → 没检索/记忆
- 没 ② → agent 产出不回写, 记忆断流
- 没 ③ → agent 无治理, 乱建节点 / 乱 grep
- 没 ④ → 老节点堆积, 数据腐化
- 没 ⑤ → 不知道哪里用错, 不知道局限

> 本文保留 v0.1 的完整 hook 部署方案供兼容参考。v0.2 默认路径见
> `docs/PROJECT_KIT.md`：backend 是核心，proxy、rules、tickets 和 hooks
> 都是按项目启用的适配层；agent wrapper 不拥有进程启停权。

## 1.5 组件分级 (用户可按需调整)

不是所有组件都"强制必装", the maintainer 明确: 用户可结合实际情况用自己的 agent 优化/关闭部分组件. 按重要性分 4 级:

| 级别 | 组件 | 缺了会怎样 | 用户可自主调整 |
|---|---|---|---|
| **L1 必装** | backend (app.py) | 无检索, 引擎废 | 不可关 |
| **L3 可选** | proxy (server.py) | 可直接连接 backend；缺少 proxy 管理面 | 仅需要稳定 ingress/管理面时启用 |
| **L3 可选** | rules/01-core.md | 不加载示例治理文案 | 项目用自己的 AGENTS/rules |
| **L2 强推荐** | SessionStart hook (cold-start) | 新 session 无 briefing, agent 从零开始 | 可关, agent 手动 `/api/briefing` |
| **L2 强推荐** | UserPromptSubmit (observer) | 无 the maintainer 纠错检测 + 新概念提醒 | 可关, 但反幻觉能力下降 |
| **L2 强推荐** | PreToolUse (behavioral-gate) | 无技术层 block 作弊/推脱/旧数据, 全靠 agent 自觉 | 可关但 agent 行为纠偏消失 |
| **L3 可选** | PostToolUse (post-tool-capture) | skill/file 变更不自动回写 | **the maintainer 明确: 不喜欢可关**, agent 自己调 /api/skills/invoke |
| **L3 可选** | PreCompact (pre-compact-writeback) | compact 前本 session 新文档不自动入库 | **the maintainer 明确: 不喜欢可关**, agent 自己手工 POST 节点 |
| **L4 按需** | tools/bootstrap_check.py | 没诊断向导 | 按需跑, 不跑不影响日常 |
| **L4 按需** | tools/leiden_community.py | 无 community boost | 按需跑 (R@3 会略降) |
| **L4 按需** | tools/llm_guided_health.py | 无 LLM 语义健康判定 | 按需跑 (替代方案: 用原版 housekeeping_audit 死指标, 次之) |
| **L4 按需** | tools/node_gdi_scorer.py | 无 5 维打分, route 不受影响 | 按需跑 |
| **L4 按需** | tools/skill_sync.py | SKILL.md 不入库 | 按需跑 |
| **L4 按需** | tools/session_aggregator.py | activity_log 不聚合成 SES-* | 按需跑 |
| **L4 按需** | tools/archive_manager.py | status=archived 节点不物理隔离 | 按需跑, 不跑节点还在 nodes/ 占空间 |

### 自定义开关 (示例: 关掉 PreCompact)

用户用惯了手工 /compact 自管节点, 不想让 hook 自动 writeback:

```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "SessionStart": [...],          // 保留
    "UserPromptSubmit": [...],      // 保留
    "PostToolUse": [...],           // 保留
    "PreCompact": [],               // ← 清空即可关闭
  }
}
```

或完全删除 PreCompact 那一段, 效果一样。

**用户也可以写自己的 hook 替代**:
```jsonc
{
  "PreCompact": [{
    "matcher": "*",
    "hooks": [{"command": "node my-custom-writeback.js"}]
  }]
}
```

---

## 1.7 可选 Route Ticket Gate Bootstrap (v9.5 S66g)

**场景**: 仅当项目主动安装 v9.5 ticket gate 时，`PreToolUse` hook
会要求配置范围内的 mutation 携带 `route_ticket_id`。普通 3CAN 使用和
read-only route/retrieve 不走该 gate。

**设计退路**: Sentinel 文件 bypass.

- **路径**: `~/.claude/logs/3can-gate-bootstrap`
- **作用**: 文件存在时, `3can-behavioral-gate.js` 在 main() 顶部立即 `process.exit(0)`, 所有工具放行, 每次 bypass 写一条 log 到 `~/.claude/logs/3can-gate.jsonl` 标记 `stage: bootstrap-bypass`.
- **仅用于**: Gate 初装 / Gate 代码紧急修复 / 跨重大版本升级这类"agent 改 Gate 自身"场景.
- **硬规则**:
  1. Sentinel 文件只在需要时手动创建 (不自动).
  2. 每个 bypass 事件都会被 gate 日志审计.
  3. **Bootstrap 完成后必须立即删除 sentinel**: `rm ~/.claude/logs/3can-gate-bootstrap`.
  4. 正常运行期间 sentinel 必须 **absent**. CI / 部署检查脚本应该验证这点.

**操作样例**:
```bash
# 初装 / 紧急修复时
mkdir -p ~/.claude/logs
echo "bootstrap opus-YYYYMMDD" > ~/.claude/logs/3can-gate-bootstrap

# ... agent 完成初装工作 ...

# 立即清除
rm ~/.claude/logs/3can-gate-bootstrap

# 验证 gate 正在跑
tail -20 ~/.claude/logs/3can-gate.jsonl
```

**为什么不用环境变量**: env var 在 Claude Code 已启动 session 里改不了 (inheritance 静态), sentinel 文件动态可检查且写 log.

**替代方案**: 如果允许较大停机, 可选择 (a) 临时注释掉 `~/.claude/settings.json` 里的 PreToolUse hook, (b) 用环境变量 `THREECAN_GATE_DISABLE=1` (未实现, 可扩展). 这些都更重, 通常 sentinel 就够.

---

## 2. 适用范围 (边界)

**适合**: vibecoding 下主流 coding AI + 多 CLI/终端
- Claude Code / Codex / Gemini CLI / Cursor (有 SKILL.md 或等价协议的)
- 2-5 人开发者小团队, 或个人维护中等偏大项目 (50+ 文件, 500+ 函数)

**不适合**:
- 多系统复杂环境 / 几百种 agents 编排 (超范围)
- 企业多团队权限管控 (3CAN 无权限体系)
- 追求 "一键让 AI 全自主干活" (那是 OpenClaw/Hermes 类 autonomous agent, 非 3CAN 赛道)

## 3. 一键部署流程 (Linux / Mac / Windows + Git Bash)

### 3.1 准备

```bash
# Python ≥ 3.11
python --version

# Clone
git clone <3can-repo>
cd 3can-engine

# 装依赖
# WSL/Linux CPU-only 推荐先装 CPU torch, 避免 pip 拉 CUDA wheel
pip install --index-url https://download.pytorch.org/whl/cpu torch
pip install -r neural-memory/backend/requirements.txt
pip install leidenalg python-igraph graspologic sentence-transformers transformers
```

依赖完整性说明：

- `fastapi` / `uvicorn` / `pydantic` 只能保证 HTTP 服务能起来。
- `sentence-transformers` / `numpy` / `scikit-learn` 是 route、ticket、节点 embedding 写回的硬依赖。
- WSL CPU 环境不要直接让 `sentence-transformers` 首次解析 torch；先从 PyTorch CPU index 装 `torch`，否则 pip 可能尝试下载 CUDA toolkit 轮子，安装体积和耗时都会失控。
- `leidenalg` / `python-igraph` 是社区聚类增强依赖，装不上时可降级，但要在 bootstrap 报告中明确标记。

不要把 `/api/stats` 200 当成完整健康。若缺 embedding 依赖，常见表现是 stats 正常、agent check-in 正常，但 `/api/route`、`/api/route/ticket` 或 `POST /api/nodes` 500，backend log 出现 `ModuleNotFoundError: No module named 'sentence_transformers'`。这种状态只能算半健康，不能用于正式开发接盘。

### 3.2 配置

```bash
# 可选: DeepSeek API key (LLM-guided 工具需要, 无此则跳过它们)
# 推荐使用本机环境变量或本机 secret manager, 不提交任何密钥文件。
export DEEPSEEK_API_KEY="replace-with-your-own-local-key"
```

### 3.3 装 Hooks (必须)

```bash
# 把 hooks/ 复制到 Claude Code 全局
mkdir -p ~/.claude/scripts/hooks
cp hooks/*.js ~/.claude/scripts/hooks/
# 4 个必装: 3can-cold-start.js / 3can-prompt-observer.js /
#           3can-post-tool-capture.js / 3can-pre-compact-writeback.js
```

注册到 `~/.claude/settings.json`:
```json
{
  "hooks": {
    "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "node \"$HOME/.claude/scripts/hooks/3can-cold-start.js\"", "timeout": 8}]}],
    "UserPromptSubmit": [{"matcher": "*", "hooks": [{"type": "command", "command": "node \"$HOME/.claude/scripts/hooks/3can-prompt-observer.js\"", "timeout": 5}]}],
    "PostToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": "node \"$HOME/.claude/scripts/hooks/3can-post-tool-capture.js\"", "timeout": 3, "async": true}]}],
    "PreCompact": [{"matcher": "*", "hooks": [{"type": "command", "command": "node \"$HOME/.claude/scripts/hooks/3can-pre-compact-writeback.js\"", "timeout": 10}]}]
  }
}
```

### 3.4 装 Rules (必须)

复制 `.claude/rules/01-core.md` 到你的项目根 `.claude/rules/01-core.md`。Claude Code 会自动加载。

### 3.5 启 Engine (最后)

```bash
# 起 backend (green slot)
cd neural-memory/backend
python app.py --port 9701 &

# 起 proxy (对外入口 9700, 蓝绿切换)
cd ../proxy
python server.py &
```

### 3.6 验证

```bash
# 跑 bootstrap 冷启动诊断, 会交互式问你下一步做什么
python neural-memory/tools/bootstrap_check.py

# 或非交互模式, 只出报告
python neural-memory/tools/bootstrap_check.py --report-only
```

预期: 5 节 检查全 OK, 菜单让你选 "跑健康扫描 / Leiden / GDI / skill sync / benchmark"。

最低命令行验收：

```bash
curl -fsS http://localhost:9700/api/stats
curl -fsS 'http://localhost:9700/api/route/simple?q=deployment%20sanity&max_nodes=3'
```

Codex CLI 接入时，还要验证 wrapper 命令面和文档一致。若项目文档写的是 `prepare/done/compact`，但本地 `scripts/3can_codex.py` 只暴露 `ticket/activity-log/compact-note`，应在 release 前统一命令别名或更新文档，避免 agent 在伪 hook 流程里拿不到 route ticket。

### 3.7 WSL Ubuntu-24.04 直启注意

WSL 内访问 Windows `127.0.0.1:9700` 时要分清两种模式：

1. Windows 侧 3CAN 已启动，WSL 通过 `THREECAN_URL` 走 relay/portproxy。
2. WSL 侧直接启动 Desktop `neural-memory`，用 `THREECAN_ENGINE_ROOT` 指向真实引擎根目录，不能落到 release staging 或空样例目录。

WSL 中长期运行进程建议用 `setsid -f` 写日志到 `logs/wsl_3can/`，不要依赖被 shell 生命周期清理的临时后台任务。完整验收必须包含 stats、session-start、route/ticket、节点写回四项。

## 4. 冷启动诊断 (bootstrap_check.py)

**这一工具本身就是 3CAN 引擎的一部分**. 用户每次部署新项目或环境变动后跑一次, 检查:

1. **环境**: Python 版本 / 包依赖 / 端口占用 / secrets 文件
2. **组件**: 5 个必装组件是否齐全 (backend/proxy/tools/hooks/settings 注册)
3. **图谱**: 节点数 / 活跃率 / 孤立率 / hash chain 完整性
4. **Token 基线**: skeleton vs slim vs full 测量, 确认省 token 生效
5. **互动菜单**: 让用户选接下来跑什么, 而不是我们替他决定

**检测到问题时**: 给出具体修复指令, 不报空"失败"。

## 5. 持续健康管理 (周 / 月 跑)

按需跑这些工具维护图谱健康, bootstrap_check 的菜单会建议:

```bash
# 新节点的描述补全 (LLM)
python tools/llm_summary_enrichment.py

# 短码歧义消解 (LLM)
python tools/short_code_curator.py

# 社区自动聚类 (Leiden, 无需 LLM)
python tools/leiden_community.py

# 5 维资产打分 (GDI)
python tools/node_gdi_scorer.py

# LLM 语义判旧节点健康 (替代死指标)
python tools/llm_guided_health.py --limit 50

# Session 聚合 (从 activity_log 自动生成 SES-auto-*)
python tools/session_aggregator.py

# 物理归档隔离 (status=archived 节点移到 graph/archive/)
python tools/archive_manager.py --migrate

# Skill 同步 (SKILL.md ↔ 3CAN 节点)
python tools/skill_sync.py
```

## 6. 数据保护原则

- **永不删除**: 任何 lifecycle/housekeeping/LLM 判定的结果都只改 status, 不 rm 文件
- **物理归档**: status=archived 节点可用 `archive_manager.py --migrate` 移到 `graph/archive/nodes/`, 引擎不加载但 `restore` 可还原
- **节点 cache**: embeddings.npz / click_log.json / pending_keywords.json 都支持重建, 删了不丢节点数据
- **hash chain audit**: `activity_log.json` 每条带 prev_hash+self_hash, `/api/audit/verify` 随时校验

## 7. 常见问题 (FAQ)

**Q: 我只想测试 route, 不想装 hooks?**
A: 可以, 但会退化成 "一次性检索工具", 不具备"跨 session 记忆闭环"。3CAN 价值的 70% 在 hooks 闭环, 建议装全。

**Q: Windows 不支持?**
A: 支持. 但 BGE-M3 CPU encoding 在 Windows 比 Linux/Mac 慢 3-5x. 1000+ 节点首次冷启动 build 可能 10-20 分钟 (之后走 cache 秒级)。

**Q: 我不用 Claude Code 行吗?**
A: 任何能识别 SKILL.md + hook script 的 vibecoding CLI 都能接. 核心就是能调 `localhost:9700` 的 HTTP API + 能挂 JS hooks。Codex / Gemini CLI / Cursor (装 shell 扩展) 都行。

**Q: 多台机器共用一套 3CAN?**
A: 目前设计是单机. 远程分享 `graph/` 目录 (git 托管) 可以, 但并发写有竞争, 不推荐直接跨机器。企业场景建议挂 nginx 反代 + 定期 git push。

## 8. 验收清单 (部署后自检)

- [ ] `python tools/bootstrap_check.py` 所有检查通过
- [ ] `curl http://localhost:9700/api/stats` 返回节点统计
- [ ] `~/.claude/settings.json` 里 4 个 hook 已注册
- [ ] 打开 `http://localhost:9700` 能看到 3d-force-graph 可视化
- [ ] 跑一次 `curl 'http://localhost:9700/api/route/simple?q=test&max_nodes=3'` 有返回
- [ ] Claude Code 里打 `/help`, 观察 PostToolUse hook 是否触发 (看 `/api/skills/invoke` 活动)
