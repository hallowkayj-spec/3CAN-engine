# 回滚与卸载 3CAN-engine

> **3CAN 是加一层, 不是替换你原有工作流**. 如果你不喜欢用 / 项目不合适 / 想换方案, **可以完全回滚, 你的 CLAUDE.md / memo.md / handoffs 等原有文件原封不动**.

本文档讲三种级别的回滚:
- **A. 临时停用** (保留数据, 下次还能用)
- **B. 完全卸载** (删干净, 回到装 3CAN 前状态)
- **C. 导出图谱数据** (卸载前备份, 未来想再用可导回)

---

## A. 临时停用 (最轻)

只停服务, 保留数据. 下次想用再启动即可, 图谱数据不丢.

### 1. 停止 backend 和 proxy

如果是前台跑:
```bash
# 在 backend / proxy 的终端窗口按 Ctrl+C
```

如果是后台跑:
```bash
# Linux / Mac
pkill -f "neural-memory/backend/app.py"
pkill -f "neural-memory/proxy/server.py"

# Windows (PowerShell 或 cmd)
# 使用最初启动 3CAN 的同一个终端、Scheduled Task 或 service manager 停止
# 不要按 PID/进程名批量结束 python.exe；先核对命令行、可执行路径和监听端口
```

### 2. 停用 Claude Code hooks (可选)

编辑 `~/.claude/settings.json`, 把 `3can-*` 相关 hook 配置注释掉或删除:
```jsonc
{
  "hooks": {
    // "PreToolUse": [{"matcher": "...", "hooks": [{"command": "node ~/.claude/scripts/hooks/3can-behavioral-gate.js"}]}],
    // "PostToolUse": [...],
    // 其他 3can-* hook 类似注释
  }
}
```

或者在**项目级** `.claude/settings.json` 关闭, 不动全局.

### 3. 让你的 agent 停止调用 3CAN

编辑项目 `CLAUDE.md`, 注释掉 "3CAN 接入规则" 段. agent 下次 session 就不会再访问 `localhost:9700`.

**到此为止, 3CAN 完全不运行, 但代码 + 图谱数据保留, 随时可恢复**.

---

## B. 完全卸载

把所有 3CAN 相关文件删除, 回到装 3CAN 前状态.

### 1. 先做 A 步所有操作 (停服务)

### 2. 备份图谱数据 (如果想将来再用)

```bash
# 把你的 3CAN 图谱数据打包备份, 存到别的位置
cd /path/to/3CAN-engine
tar -czf ~/my-3can-graph-backup-$(date +%Y%m%d).tar.gz graph/
# 或 Windows:
# Compress-Archive -Path graph -DestinationPath ~/my-3can-graph-backup.zip
```

备份文件是 JSON 明文, 任何时候可导回 (或者用其他工具读).

### 3. 删除仓库和虚拟环境

```bash
# 删 3CAN-engine 仓库目录
cd ..
rm -rf 3CAN-engine/

# 如果你在 venv 里装的依赖, 直接删 venv
rm -rf /path/to/your/venv/

# 如果是全局 pip 装的, 可选择卸载 3CAN 依赖 (注意: 其他项目可能也用这些, 谨慎):
# pip uninstall fastapi uvicorn sentence-transformers leidenalg python-igraph flashrank
# (通常不建议, 因为其他项目可能需要)
```

### 4. 删除 Claude Code hooks

```bash
# 删除 3CAN 提供的 hook 脚本
rm ~/.claude/scripts/hooks/3can-*.js

# 编辑 ~/.claude/settings.json, 删除所有 3can-* hook 配置
```

### 5. 删除本地缓存 (可选)

```bash
# BGE-M3 模型缓存 (占 2-3 GB, 你可能其他项目也用它, 谨慎)
# Linux / Mac: rm -rf ~/.cache/huggingface/hub/models--BAAI--bge-m3
# Windows: 在 %USERPROFILE%\.cache\huggingface\hub\ 里找 models--BAAI--bge-m3

# 3CAN 独立的 log 文件
rm -rf ~/.claude/logs/3can-*.jsonl
rm -f ~/.claude/logs/3can-gate-bootstrap
```

### 6. 移除项目 CLAUDE.md 中的 3CAN 段

编辑你项目的 `CLAUDE.md`, 删除或注释"3CAN 接入规则" 那段.

**到此为止, 3CAN 完全从你系统消失**, 你的 `CLAUDE.md` / `memo.md` / `handoffs/` / 其他 rules / MCP servers **完全不受影响**.

### 7. 验证

- 打开 `http://localhost:9700/` → 应该连不上 (backend 已停)
- `ls ~/.claude/scripts/hooks/ | grep 3can` → 应该无结果
- 你的 Claude Code / Codex 项目正常使用, 不提 3CAN

---

## C. 导出图谱数据 (卸载前保留知识)

如果你 dogfood 一段时间积累了图谱, 卸载前**导出**, 以后换工具或再装可以导回:

### 1. 全量导出 (JSON)

```bash
cd /path/to/3CAN-engine
# 图谱数据本身就是 JSON 文件, 直接打包即可
tar -czf ~/my-3can-backup-$(date +%Y%m%d).tar.gz graph/
```

### 2. 导出为 markdown (人类可读)

```bash
# 用 tool (如果你还想以后查阅, 而不是导回 3CAN)
python neural-memory/tools/export_to_markdown.py --out-dir ~/3can-export-md/
# 注意: 此脚本未必在 v0.1 ship, 如未实现可参考 graph/nodes/*.json 手动处理
```

### 3. 从备份导回 3CAN (未来想再用)

```bash
# 新装的 3CAN 目录下
cd /new/3CAN-engine/
tar -xzf ~/my-3can-backup-20260419.tar.gz
# graph/ 恢复了, 启动 backend 即可读取
python neural-memory/backend/app.py --port 9701
```

---

## D. 常见问题

### Q. 卸载后我的 CLAUDE.md 会受影响吗?

**不会**. `CLAUDE.md` 是你自己的文件, 3CAN 只是**建议**你在里面加一段"3CAN 接入规则". 卸载时把那段删掉就行, 其他保持原样.

### Q. 我装了 3CAN 的 hook, 现在 Claude Code 启动报错了?

可能是 hook 文件被删但 `~/.claude/settings.json` 里还引用着. 编辑该文件, 把引用 `3can-*.js` 的行**删除**或注释, 重启 Claude Code.

### Q. 图谱数据能带走吗? 别的工具能读吗?

能. `graph/nodes/*.json` 是普通 JSON, 任何工具都能读. Schema 见 `docs/specs/3CAN_ENGINE/CONTRACTS.md`. 你可以手动转成 Obsidian / Mem0 / 其他工具的格式.

### Q. 我装了依赖, 删 3CAN 时要卸载吗?

**不建议**全卸. 因为:
- `fastapi` / `uvicorn` 其他项目也可能用
- `sentence-transformers` + BGE-M3 模型 (2-3 GB) 其他 RAG 工具可能复用
- `leidenalg` / `python-igraph` 小众, 可以删

如果你用虚拟环境 (venv / conda), 直接删整个 venv 最干净.

### Q. 我 push 过图谱数据到 git, 怎么办?

**注意**: 图谱可能包含敏感信息 (项目决定 / 代码片段 / API schema). 你不该 commit graph/:
```bash
# 从 git 历史删除 graph/ (只改本地, 其他人仍看得到)
git rm -r --cached graph/
git commit -m "Remove graph data from tracking"
git push

# 如果要彻底清除 git history (高风险, 改写历史):
# git filter-branch 或 BFG Repo-Cleaner, 参考 https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/removing-sensitive-data-from-a-repository
```

默认的 `.gitignore` 已经排除 `graph/`, 你应该没这个问题, 除非你手动加了.

### Q. 卸载后, 本地有 3CAN-engine-frontend.png 这种截图怎么办?

那是 3CAN-engine 仓库里的文档资产, 和你项目无关. 卸载时删仓库就一并删了.

---

## 一句话收束

3CAN 的设计原则之一是**可逆**. 装上是加一层, 卸载是减一层, **你原有项目 / 工具链 / 数据完全不受影响**.

如果你用下来觉得不适合, 直接删, 没什么需要清理的副作用. 如果有疑问, 欢迎在 GitHub issue 问.
