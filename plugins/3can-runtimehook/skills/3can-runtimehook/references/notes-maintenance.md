# 同项目 notes 维护

`KNOWLEDGE_NODE_COMPACTION_REQUIRED` 不自动截断、换节点或提高上限。维护者先审查当前意义摘要，再保存完整历史归档。该操作不关闭 ErrorCase，不产生任务 PASS。归档依赖新客户端；旧客户端对已归档事件返回 `DELTA_MARKER_CONFLICT`，不能宣称旧会话自动获得归档读取能力。

使用已经加载本版本的绝对控制器路径。当前任务先有自己的 `on`/`connect`、真实 native cwd/session、项目胶囊和 Workorder；目标节点 project/namespace 必须匹配。本机治理任务不能借其他项目的身份维护其节点。发布客户端属于单独的部署步骤，以下命令不安装或重启服务。

## 准备与复核

摘要文件须位于当前物理工作区，UTF-8、不超过 2000 字符，保留未解决/拒绝/未验证事实。计划含完整历史，不上传 Jev，不提交 Git；放在项目已有受保护输出目录。准备默认只有 GET 和本地写入：

```text
<controller> --root <physical-root> --native-cwd <actual-host-cwd> --session-id <actual-task-id> notes-compact --plan <new-exact-plan.json> --summary-file <reviewed-summary.txt>
```

保留返回的 plan_sha256、archive_id、before/after bytes 和 opaque expected_updated_at。审查源码、摘要与同项目边界；用既有 checkpoint/review 记录实际证据，只交摘要、哈希和证据引用。缺失或低置信意见不登记 PASS。计划文件不得在取得票据后修改。

## 即时票据与 Apply

复用本机 writeback.json 指定的 canonical client。直接运行其 CLI 会从脚本目录推导 PROJECT_ROOT，可能绑定错误；使用以下小型调用从当前 Hook 的已核验物理 root 加载原客户端，不复制项目身份或私有状态。路径/身份参数必须来自当前操作者，命令为一次即时票据请求，没有后台重试：

```python
import sys
from pathlib import Path
sys.path.insert(0, str(Path("<loaded-controller>").parent))
import runtimehook_writeback as wb
root = Path("<verified-physical-root>").resolve()
settings = wb.config()
client = wb._client(settings, root)
raise SystemExit(client.main([
    "--base-url", settings["base_url"], "--workorder-id", "<current-workorder>",
    "ticket", "--agent-id", "<current-agent>",
    "--task-description", "维护已绑定节点notes，完整保留历史与未解决事实",
    "--target-file", "<exact-absolute-plan.json>",
    "--scope-keyword", "<exact-bound-node-id>",
    "--scope-keyword", "runtimehook-notes-compaction",
    "--scope-keyword", "<verified-plan-sha256>",
    "--task-type", "NotesCompaction",
]))
```

检查票据的 Agent/project/namespace/workspace/Workorder、唯一 target 文件、三个 scope keyword、`project_identity_verified=true`、`target_digest_source=server_verified_snapshot_v1` 和摘要。不要先 consume；Apply 消费一次并遵守返回完成期限：

```text
<controller> --root <physical-root> --native-cwd <actual-host-cwd> --session-id <actual-task-id> notes-compact --plan <exact-plan.json> --apply --ticket-id <fresh-ticket>
```

客户端从同一份受限 bytes 解析计划并计算基线哈希。每次节点写前再检查文件哈希与完成期限。archive 从首次原生 create 起即 archived；完整 notes、可还原 API snapshot、项目身份和两项 SHA256 必须 GET 核验。目标仅更新 notes 和 manifest，使用原 opaque version CAS，保留其他元数据及 edges。原生读取改变访问计数，不代表语义变化；不覆盖读取统计。

`COMPACTED_AND_READBACK_VERIFIED` 才表示本次归档与目标读回成功。相同计划重复执行为 GET-only `ALREADY_COMPACTED`，保留随后新增 Delta。每次维护的脱敏请求阶段保存在当前 task 的 `notes-maintenance-*.json`；`ok=null` 表示请求结果未知，必须独立 GET，不能从超时断定未写入。实际拒绝只尝试一次同上下文 issue intake；上报失败 `UNAVAILABLE` 不递归。

## 失败、迁移与回滚

- 版本或语义冲突：保留计划/完整 archive，先重读；不得盲重试。需新候选时保存新精确文件、重新复核并取得新票据。
- 归档成功但目标写入失败：原目标未改时历史仍在原节点；保留 orphan archive，不自动删除。请求未知时先 GET 目标、manifest 与 archive，核对计划摘要/内容/版本。
- 目标已改变：不得用旧快照覆盖新 Delta。只有当前内容/版本与维护收据一致且没有新写入时，才能另立精确 rollback 计划/新票据，用已有 CAS 恢复；本命令不自动执行回滚。archive/原计划仍保留。
- 旧客户端回滚：保留历史记录与收据；旧事件重放仍可能 typed conflict。必须明确客户端版本边界。
- manifest 最多 32 个 archive，索引仍受 256 KiB notes 限制；达到边界显式拒绝。此维护不承诺无限历史容量，不移除旧 marker，不使用会让原生 GET 丢失归档的物理迁移工具。

测试、隔离 CLI/GraphEngine 验证和审查意见均不能代替真实宿主票据、安装、生产 Apply、原失败事件重放及 UAT。分别报告这些门禁。
