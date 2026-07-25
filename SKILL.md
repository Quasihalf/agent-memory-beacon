---
name: agent-memory-beacon
description: Use when setting up, operating, debugging, auditing, or extending Agent Memory Beacon; when Codex memories are missing, stale, unrelated, duplicated, or absent from Obsidian; or when working on transcript harvesting, dynamic recall, formal-memory lifecycle, Agent context, and macOS launchd integration.
---

# Agent Memory Beacon v0.5.0

## Overview

Agent Memory Beacon 是面向 Codex 的本地长期记忆层。它把可复用的决策、已解决错误、个人偏好、Skill 路由和工作流规则保存在用户拥有的 Obsidian Markdown 中，并在相关时机向 Codex 注入少量可追踪记忆。

它不是原始聊天备份工具，也不把每条消息都升级成长期记忆。

## Current Contract

| Agent | Capture | Dynamic recall | Direction |
|---|---|---|---|
| Codex | Automatic | Supported | Primary |
| Claude Code | Automatic | Not yet | Collection-only |
| ZCode | Compatibility | No | Maintenance only |

Obsidian Vault 是事实来源和控制面。正式记忆必须可审计、可回溯来源，并遵守以下边界：

- `[DECISION]`、`[ERROR]`、`[FAVOR]` 和 `[SESSION_SUMMARY]` 是提案，不是绕过质量门的指令。
- 信息不足或耐久性不确定的内容进入隐藏 candidate，不参与召回。
- Session 是证据，不直接进入运行时召回。
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
5. `recall-index.json` 是否重建且 candidate 数量为零。
6. UserPromptSubmit 是否满足首次、换题、索引变化、高风险或 30 分钟刷新条件。

统一检查入口：

```bash
.venv/bin/python doctor.py --profile quick
.venv/bin/python doctor.py --profile ci
```

源码 checkout 使用 `quick`/`ci`；stable runtime 使用 `quick`/`live`。`ci` 需要源码中的 tests、fixtures 和 Git 元数据；`live` 检查真实 Vault、Hook、launchd 和协议探针。Doctor 默认只读。

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
