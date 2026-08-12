# 3CAN 命名讨论

> **the maintainer 倾向保留 3CAN, 可加副标如 `3CAN-Graph`**。此文档整理思考, 供 GPT-5.4 外审复核。

## 1. "3CAN" 来源

PRD.md 原文:
> **3CAN** = **C**ustom · **C**ompact · **C**omposite · **A**ugmented · **N**eurosymbolic

5 个 C-word 的缩写 (有重复, 有点勉强但可解释):
- Custom — 定制化 (为具体项目定制知识图谱)
- Compact — 紧凑 (skeleton 模式省 token)
- Composite — 组合 (dense + sparse + reranker 复合)
- Augmented — 增强 (RAG 增强 agent 上下文)
- Neurosymbolic — 神经符号 (BGE-M3 神经 + 图谱符号)

**前身**: 内部曾有 "C³AN" (C-cubed-AN) 的写法, 对应 L0-L5 层级知识架构。3CAN 是简化后的品牌名。

## 2. 对内评价

✅ **优点**:
- 工程师/竞赛评审能理解 (缩写解码合理)
- 描述了技术路线核心 (定制 + 紧凑 + 组合 + 增强 + 神经符号)
- 匹配北极星 "记忆精确指引 + 项目协作管理"
- 没撞其他开源项目名 (Mem0 / Zep / Letta / Graphify / Entroly 都不冲突)

⚠️ **不足**:
- 5 个 C 里有 3 个语义接近 (Custom / Compact / Composite) 勉强凑
- "Augmented" 实际是 RAG 通用术语, 不是 3CAN 独特
- 对外 PR 不友好 — 普通人看到 "3CAN" 会问 "三罐? 三扫描? 3D-CAN 扫描仪?"

## 3. 对外 (开源 + PR) 的风险

### 3.1 语义噪声
- 搜索 "3CAN" 会撞: 3D-SCAN (3D 扫描仪) / CAN bus (工控总线) / Three Cans (某个开源玩具项目) / 3 can of beer (生活用语)
- 不像 Mem0 (Memory zero) / Zep (短快响亮) / Letta (人名感) 那样清晰

### 3.2 发音
- 中文: 三-can (能) / 三-canc (惨)?
- 英文: "three-can" / "triple-can"?
- 读起来不如 2-3 音节响亮 (Mem0/Zep/Letta)

### 3.3 记忆性
- 不到一年社区知名 memory 工具: Mem0, Zep, Letta, Cognee, Graphiti, Supermemory, LangMem, ReMe
- 都是 1-3 音节 + 语义暗示
- "3CAN" 在这堆里不易记住

## 4. 备选方案 (按 the maintainer 喜好排序)

### 方案 A (the maintainer 推荐): **3CAN** + 副品牌 `3CAN-Graph`
- 仓库名: `3can-graph` (github.com/zeven/3can-graph)
- 对外 README 标题: "3CAN-Graph: Project Substrate for Multi-Agent Development"
- 对内: 保留 `3CAN` 技术名
- **优点**: the maintainer 已认同, 不破坏现有文档/节点 ID
- **缺点**: 两个名字容易混 (用户是记 3CAN 还是 3CAN-Graph?)

### 方案 B: 保持 `3CAN` 不变
- 当前节点 ID 都带 "3CAN" 或基于这个命名 (比如 DEC-3can-*)
- README 首行写明 "3CAN = 5C acronym for Custom/Compact/Composite/Augmented/Neurosymbolic"
- **优点**: 最简单, 零改动
- **缺点**: 对外传播不利

### 方案 C: 改名 `ProjectGraph` / `AgentSubstrate` / `Nexus` / `Pax`
- **优点**: 对外友好, 语义直接
- **缺点**: 大量节点 ID / CLAUDE.md / rules 都要改, 工程成本高 + 搜索历史难追溯
- **不推荐** (the maintainer 也没想走这条)

### 方案 D: 区分品牌层次
- **项目内部**: 3CAN (保留)
- **SaaS 品牌**: Zeven (已有, 柒木沐)
- **开源仓库**: `zeven-memory` 或 `zeven-graph`
- **技术协议/底层**: 3CAN (作为引擎名字写在 README)
- 类比: OpenAI (公司) + GPT (技术); Zep (产品) + Graphiti (开源底层)
- **优点**: 品牌分层清晰, 符合行业惯例
- **缺点**: Zeven 作为 SaaS 品牌面向电商运营, 开源工具叫 zeven-xxx 对程序员可能不直观

## 5. 我的判断 (Opus 主脑)

**方案 A (`3CAN-Graph`) 实用可行**:
- 保留"3CAN"技术名不动
- 对外加"-Graph"说明是图谱型 (区别于 agent runtime / memory layer)
- 社区看到 `3CAN-Graph` 会正确理解 "基于图的 3CAN 引擎"

但**建议在 README 首行额外加一句通俗 tagline**, 比如:
```
# 3CAN-Graph

> A project substrate for multi-agent developers. Knowledge graph + agent registry +
> error memory + bi-directional skills. Think of it as "a shared memory brain for
> your Claude Code / Codex / Gemini CLI agents".
```

这样即使"3CAN"不好记, 打开 README 3 秒能懂干什么的。

## 6. 开源前 checklist (命名相关)

- [ ] 仓库名决策: `3can-graph` vs 保留 `3can`
- [ ] README 首行 tagline
- [ ] 节点 ID 约定保持不变 (DEC-3can-* 等)
- [x] 许可证: PolyForm Noncommercial License 1.0.0
      (source-available, 非 OSI open source；以仓库根 `LICENSE` 为准)
- [ ] 域名: zeven.ai / 3can.dev / 3can-graph.dev 都看着还可以

---

**the maintainer 决策权**: 以上仅分析, 最终名字由 the maintainer 定。

供 GPT-5.4 外审: 你的视角有没有更 savvy 的命名?
