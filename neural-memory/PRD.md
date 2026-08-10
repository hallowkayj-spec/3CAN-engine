# 3CAN: Neural Memory for AI Agents
## Product Requirements Document v0.1

> **3CAN** = Custom · Compact · Composite · Augmented · Neurosymbolic
> 一句话: 给AI coding agent装一个图结构的项目记忆，替代扁平文件。

---

## 1. 问题定义

### AI Agent当前记忆困境

| 问题 | 具体表现 | 代价 |
|------|---------|------|
| **Context丢失** | Session压缩后忘记关键决策和接口细节 | 反复犯同样的错（KAIROS事故） |
| **扁平检索** | MEMORY.md是线性索引，200行就截断 | 重要信息被淹没 |
| **手动交接** | Handoff文件靠人写人读，漏了就脱钩 | 多Agent并行时冲突、遗漏 |
| **无结构** | 知识点之间的关系（依赖、阻塞、触发）没有记录 | 改A不知道影响B |
| **不学习** | 犯过的错没有结构化沉淀到下次可查的地方 | 同类错误循环犯 |

### 现有方案的不足

| 方案 | 做了什么 | 缺什么 |
|------|---------|--------|
| Claude CLAUDE.md | 规则注入（25KB上限） | 无动态状态、无关系图 |
| Memory/*.md | 扁平文件+索引 | 无语义检索、无自动更新 |
| Handoff | 线性交接文档 | 写了没人读=0 |
| Mem0/Letta | 对话记忆 | 不解决项目状态路由 |
| Obsidian | 人类知识管理 | AI无法主动查询 |

---

## 2. 产品定位

**3CAN不是通用记忆产品。它是AI Agent的项目状态图谱。**

- **目标用户**: AI coding agent（Claude Code, Codex, Cursor, 任何CLI agent）
- **核心场景**: 复杂多session项目开发中的上下文管理
- **不做**: 对话记忆、个人笔记、通用知识图谱
- **做**: 项目知识路由 + 接口契约存储 + 错误教训沉淀 + 跨agent共享状态

---

## 3. 核心架构

```
┌─────────────────────────────────────────────────────┐
│                    3CAN Engine                       │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │
│  │ 节点层    │  │ 边层     │  │ Embedding层       │  │
│  │ JSON files│  │ typed    │  │ BGE-M3 1024d     │  │
│  │ 162+节点  │  │ 114+边   │  │ cosine similarity│  │
│  └────┬─────┘  └────┬─────┘  └────────┬─────────┘  │
│       └──────────────┴────────────────┘             │
│                      │                               │
│  ┌───────────────────┴───────────────────────────┐  │
│  │            Hybrid Router                       │  │
│  │  0.7 × embedding + 0.3 × keyword              │  │
│  │  + priority bonus + status penalty             │  │
│  └───────────────────┬───────────────────────────┘  │
│                      │                               │
│  ┌──────────┐  ┌─────┴────┐  ┌───────────────────┐  │
│  │ Writeback│  │ Sync     │  │ Discovery          │  │
│  │ 自动回写  │  │ 文件监听  │  │ INTF自动发现       │  │
│  └──────────┘  └──────────┘  └───────────────────┘  │
│                                                     │
│  API: FastAPI 14端点 + WebSocket                    │
└─────────────────────────────────────────────────────┘
```

### 节点类型（9种）

| 类型 | 前缀 | 数量 | 存什么 |
|------|------|------|--------|
| 模块 | MOD- | 13 | 项目各子系统状态、blocker、tech_stack |
| 密钥 | SEC- | 10 | API key变量名（不存明文）、状态 |
| 错误 | ERR- | 8 | 历史事故、根因、教训、防止重犯 |
| 接口 | INTF- | 7 | 函数签名、字段名、文件命名规则 |
| 反馈 | FEE- | 36 | 用户纠正、行为规则、永久约束 |
| 会话 | SES- | 40+ | Session摘要、交付物、持续性结论 |
| 用户 | USR- | 1 | 用户画像、偏好、沟通风格 |
| 决策 | DEC- | 3 | 架构决策、战略选择 |
| 引用 | REF- | 4 | 外部资源指针 |

### 边类型（8种）

`depends_on` · `feeds_into` · `blocks` · `informs` · `requires` · `updates` · `validates` · `triggers`

---

## 4. 验证数据

### 实验1: Memory Consolidation
```
Session token: 42K → 10.6K (-74%)
42个远古session → 2个归档文件 + 8个活跃
```

### 实验2: Knowledge Purifier（真实开发任务）
```
第一轮（无INTF节点）: 3.5/7 — 3个bug（字段名、文件名、KB delta）
加INTF节点后第二轮: 6/7 — 3个bug全修
提升: +71%
正循环: 错误 → 根因 → 回写节点 → 不再犯 ✓
```

### 对比: 3CAN vs 现有memo+handoff体系
```
精确检索: +15%（route比grep好一些）
上下文完整性: +25%（blocker/状态/依赖随节点返回）
依赖关系: +30%（114条typed edges，以前完全盲区）
自学习循环: +71%（单轮迭代验证）
```

---

## 5. API设计

### 核心端点

| 端点 | 方法 | 功能 | 谁调用 |
|------|------|------|--------|
| `/api/route` | POST | 任务→激活子图 | Agent每次开干前 |
| `/api/writeback` | POST | 批量回写节点变更 | Agent完成任务后 |
| `/api/preference` | POST | 沉淀用户偏好 | Agent识别到偏好时 |
| `/api/sync/rescan` | POST | 全量rescan memory目录 | Session启动时 |
| `/api/discover` | POST | 扫代码仓库发现INTF | 定期或手动触发 |
| `/api/reload` | POST | 热重载全图 | 部署更新后 |
| `/api/nodes` | CRUD | 节点增删改查 | 管理/调试 |
| `/api/edges` | CRUD | 边增删改查 | 管理/调试 |
| `/api/graph` | GET | 导出完整图（前端用） | Dashboard |
| `/api/stats` | GET | 图统计 | 监控 |

### 调用示例（Agent视角）

```bash
# Session开始: 查上下文
curl -X POST localhost:9700/api/route \
  -H "Content-Type: application/json" \
  -d '{"task":"写蒸馏提纯脚本","max_nodes":8}'

# 收到: MOD-distill + INTF-distill-output + SEC-volcengine + ...
# 每个节点含: current_state, blockers, key_files, notes(接口细节)

# Session结束: 回写
curl -X POST localhost:9700/api/writeback \
  -H "Content-Type: application/json" \
  -d '[{"node_id":"MOD-distill","field":"current_state","value":"purifier v2完成"}]'
```

---

## 6. 与运营教练SaaS的双向验证

### 为什么3CAN验证了运营教练的可能性

3CAN和运营教练SaaS共享同一套核心机制:

| 机制 | 3CAN(项目管理) | 运营教练(电商) |
|------|---------------|---------------|
| 图结构 | 162节点+114边(项目知识) | 105节点+116边(运营知识) |
| 路由 | BGE-M3 hybrid(任务→子图) | Ke-LinUCB(场景→节点) |
| 自学习 | 错误→回写→不再犯(+71%) | 建议→outcome→调权重 |
| typed edges | depends_on/feeds_into/blocks | informs/constrains/drives |
| 子图激活 | top-K节点+相关边 | 16/105节点并行推理 |

**如果图结构+路由+自学习在项目管理上work（已验证），同样的机制在电商运营上也应该work。** 因为两者的底层抽象相同:
- 给定一个场景描述
- 从知识图中路由出相关子图
- 基于子图内容做推理/行动
- 结果反馈回写到图中
- 下次遇到类似场景，路由更准

---

## 7. 开源路线图

### MVP (当前)
- [x] 图引擎 + BGE-M3 hybrid路由
- [x] CRUD API + WebSocket
- [x] Memory Consolidation
- [x] Session Writeback + Preference Learning
- [x] 同步层 (file watcher)
- [x] INTF自动发现
- [ ] 节点覆盖率41%→80%
- [ ] 连续3轮任务≥6/7

### v1.0 (开源发布)
- [ ] 一键安装 (`pip install 3can`)
- [ ] Claude Code hook自动集成
- [ ] 零配置启动（自动扫描项目目录建图）
- [ ] README + 演示视频
- [ ] GitHub Actions CI

### v1.x (社区驱动)
- [ ] Cursor/Windsurf集成
- [ ] Codex CLI集成
- [ ] 多项目支持
- [ ] 团队共享图（多人+多Agent）
- [ ] Web UI管理面板

---

## 8. 技术栈

| 组件 | 选型 | 理由 |
|------|------|------|
| 后端 | FastAPI | 异步+WebSocket+自动文档 |
| 存储 | JSON files | 零依赖、git-friendly、human-readable |
| Embedding | BAAI/bge-m3 (1024d) | 中文最强开源embedding之一 |
| 向量计算 | numpy + cosine | 162节点不需要ANN，精确计算<1ms |
| 前端 | 3d-force-graph (CDN) | 单HTML文件、零构建 |
| Python | 3.11+ | sentence-transformers兼容 |

---

## 9. 文件结构

```
neural-memory/
├── backend/
│   ├── app.py              # FastAPI 14端点
│   ├── graph_engine.py     # 图引擎核心 v4
│   ├── models.py           # Pydantic数据模型
│   ├── migrator.py         # Memory文件迁移器
│   ├── seed_nodes.py       # MOD/SEC/ERR节点生成
│   ├── seed_intf.py        # INTF接口节点生成
│   └── requirements.txt
├── frontend/
│   └── index.html          # 3D可视化 + Dashboard
├── graph/
│   ├── nodes/              # 162+ JSON节点文件
│   ├── edges.json          # 114+ 边定义
│   └── embeddings.npz      # BGE-M3向量缓存
├── hooks/
│   ├── session_loader.py   # Claude Code hook
│   └── neural_memory_hook.sh
├── PRD.md                  # 本文件
└── .gitignore
```

---

*3CAN v0.1 — S59 validated, 2026-04-13*
*"The graph remembers what the context window forgets."*
