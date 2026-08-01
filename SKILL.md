---
name: agent-memory-beacon
description: Use when setting up, operating, debugging, auditing, or extending Agent Memory Beacon; when Codex memories are missing, stale, unrelated, duplicated, or absent from Obsidian; or when working on transcript harvesting, dynamic recall, formal-memory lifecycle, Agent context, macOS launchd, or Windows/Mac evidence synchronization.
---

# Agent Memory Beacon v0.7.0

## Overview

Agent Memory Beacon 是面向 Codex 的本地长期记忆层。它把可复用的决策、已解决错误、个人偏好、Skill 路由和工作流规则保存在用户拥有的 Obsidian Markdown 中，并在相关时机向 Codex 注入少量可追踪记忆。

它不是原始聊天备份工具，也不把每条消息都升级成长期记忆。

## Current Contract

| Agent | Capture | Dynamic recall | Direction |
|---|---|---|---|
| Codex | Automatic | Supported | Primary |
| Claude Code | Automatic | Not yet | Collection-only |
| ZCode | Compatibility | No | Maintenance only |

Windows Codex/Claude can run as `producer-replica`: they publish immutable
transcript evidence and consume a verified read-only Vault replica. Only the
Mac `authority` may run the canonical harvester, publish formal memory, or
apply lifecycle transitions.

同步版本合同：

| State/document | Version | Contract |
|---|---:|---|
| `transcript.chunk` / `transcript.gap` event + ready | 1 | 保持 golden bytes 和 event ID |
| `attachment.blob` event + ready | 2 | `reference_id` 绑定 producer、stream、epoch、cursor、来源路径摘要、原名和 payload hash |
| Producer state | 3 | 安全迁移 v1/v2 queue 与 pending state |
| Authority ledger | 4 | 同时绑定 blob/metadata path、hash、bytes 与 sealed generation |

新附件的 canonical 路径是
`Attachments/Agent-Memory-Beacon/remote/objects/<sha[:2]>/<sha>.<ext>`，审计
metadata 位于
`04-Feedback/remote-attachments/<device>/<producer>/<seq>-<event-id>.md`。
receipt 只有在两个文件都精确属于同一 sealed generation 时才能发布并授权 GC。

Obsidian Vault 是事实来源和控制面。正式记忆必须可审计、可回溯来源，并遵守以下边界：

- `[DECISION]`、`[ERROR]`、`[FAVOR]` 和 `[SESSION_SUMMARY]` 是提案，不是绕过质量门的指令。
- 信息不足或耐久性不确定的内容进入隐藏 candidate，不参与召回。
- Session 正文是证据，不进入正式召回；每个 session 的最新有界摘要可派生为一条低优先级 `CONTEXT`。
- 正式记忆的 supersede、retract、expire、restore 必须先预览，并获得精确用户授权。
- 不保存密码、token、支付信息、原始推理或完整工具输出。

## Operating Workflow

1. 先读取 `scripts/config.yaml`，确认真实 Vault、Codex home 和稳定运行时路径。
2. 修改前运行 quick Doctor；涉及真实 Hook、Vault 或 launchd 时再运行 live Doctor。
3. 新安装先运行 `setup.py`，再验证并安装稳定运行时。
4. Codex 中审核并信任三个 Agent Memory Beacon Hook。
5. 用一条明确可复用的测试记忆验证采集，再用新话题验证 `[MEMORY_REFRESH]` 动态召回。

```bash
cd scripts
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python setup.py
.venv/bin/python install_runtime.py --dry-run
.venv/bin/python install_runtime.py --verify-release
.venv/bin/python install_runtime.py
```

稳定安装后使用：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/doctor.py --profile live
```

## Diagnosis

按链路定位，不要只看 Obsidian 是否出现新文件：

1. transcript 是否产生新内容。
2. heartbeat 高水位是否推进。
3. annotation 是否被判为 formal、candidate 或 rejected。
4. 项目 decisions/pitfalls/personal memory 是否更新。
5. `recall-index.json` 和 Graph v3 是否重建，图质量报告是否为 PASS，且 candidate 数量为零。
6. UserPromptSubmit 是否满足首次、换题、索引变化、高风险或 30 分钟刷新条件。
7. 跨设备模式再检查 producer global sequence、ready/object 哈希、authority
   ledger、sealed generation、receipt 和 replica active marker。

统一检查入口：

```bash
.venv/bin/python doctor.py --profile quick
.venv/bin/python doctor.py --profile ci
```

源码 checkout 使用 `quick`/`ci`；stable runtime 使用 `quick`/`live`。`ci` 需要源码中的 tests、fixtures 和 Git 元数据；`live` 检查真实 Vault、Hook、launchd 和协议探针。Doctor 默认只读。

同步统一入口：

```bash
cd /absolute/path/to/agent-memory-beacon
PY="$PWD/scripts/.venv/bin/python"
CFG="$PWD/scripts/config.yaml"
"$PY" -B scripts/beacon_sync.py --config "$CFG" doctor
"$PY" -B scripts/beacon_sync.py --config "$CFG" run
```

Syncthing 只允许两个单向 folder：Windows outbox Send Only 到 Mac inbox
Receive Only；Mac published Send Only 到 Windows received-published Receive Only。
不要同步 canonical Vault、Windows replica、任一 state directory 或
`attachment_roots`。

Windows 首次建立副本时人工运行一次：

```bash
"$PY" -B scripts/beacon_sync.py --config "$CFG" materialize --bootstrap
```

后台 `run` 不会隐式 bootstrap。Windows 端最低支持 Windows 10 1809 /
Windows Server 2019（build 17763）；安装前必须让 Doctor 通过。

Windows 正式安装必须从 PowerShell 源码根目录运行
`scripts/install_beacon_sync.py --runtime-root
"$env:LOCALAPPDATA\AgentMemoryBeacon\runtime"`。安装器先发布并校验
`releases/<release-id>`，再把当前用户 Task 和所选 `--codex-hooks` /
`--claude-hooks` 事务绑定到 release 内的 Python、脚本和配置；不得把正式任务
指向系统 Python 或 Git checkout。Mac authority 则由 `install_runtime.py` 在现有
stable runtime 事务中按配置安装可选 sync LaunchAgent。

不要把 canonical Vault 设成双向 Syncthing folder，不要手动改只读
replica，也不要因 sequence gap 删除较高序列事件。应先恢复缺失
bundle/object，再重跑 `run`。`collect --include-existing` 只用于明确批准的
历史 bootstrap。

## Memory Quality And Comparison

历史正式记忆审计默认只读，并将积压区分为证据不足、受 lifecycle 约束阻断和可执行建议：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/memory_quality_audit.py --json
```

Codex Memory 比较必须先运行只读 capability probe：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/evaluate_memory_comparison.py \
  --probe-only
```

feature 关闭、存储为空、证据无效或缺少同版本同 fixture 的两个 arm 时，只能报告 `N/A`。不得用 Beacon 的 Obsidian 可见性、来源审计或 lifecycle 能力代替行为评分，也不得自行开启 Codex experimental Memory。

## Formal Memory Lifecycle

使用稳定运行时 CLI 搜索和预览：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/memory_lifecycle.py \
  list --query "关键词" --json
```

只有用户明确授权某个 memory ID 和当前 revision 后，才可使用 `--expected-revision ... --apply` 执行正式转换。推断出的冲突只允许进入 proposal。

若用户批准的是 `memory_quality_audit.py --old-before` 生成的整份旧记忆计划，必须改用批量入口并引用计划中的 Canonical SHA256：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/memory_lifecycle_batch.py \
  --plan /absolute/path/to/old-memory-lifecycle-plan.md \
  --expected-sha256 <approved-canonical-sha256>
```

默认只预览；只有用户明确批准该精确 SHA256 时才加 `--apply`。不得把对单条记录的授权扩张为整批授权，也不得自动执行未批准的质量建议。

## Managed Context

Codex 使用全局 `AGENTS.md`，Claude Code 使用 `CLAUDE.md`。安装器维护的规则来源是：

```text
patches/AGENT_MEMORY_BEACON.md.patch
```

不要手工编辑 `COMPILED:RULES` 或 `COMPILED:PROJECTS` 块；`compiler.py` 会从 Vault 重建它们。

## References

- `README.md`: 安装、升级、兼容性和用户操作
- `references/architecture.md`: 数据模型、安全边界和运行时架构
- `references/workflow.md`: 采集、召回、维护和故障排除
- `scripts/doctor.py`: 可复现健康检查
- `scripts/install_runtime.py`: 事务安装、release 验证和回滚
