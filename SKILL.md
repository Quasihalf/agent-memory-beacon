---
name: agent-memory-vault
description: Maintain a personal Obsidian-based memory vault for macOS Codex and Claude Code. This fork focuses on automatic transcript harvesting, Chinese-readable decision/error notes, project-aware session files, and a visible Obsidian index. Use this when setting up or debugging Codex/Claude Code conversation capture, Obsidian agent memory, session summaries, project memory, or automatic DECISION/ERROR harvesting.
---

# Agent Memory Vault v0.3.0-personal

> macOS-first personal memory layer for Codex, Claude Code, and Obsidian.

这个 skill 文档保留原版知识大脑的核心结构，但当前分支的实际重点是：

- 自动读取 Codex / Claude Code 本地 transcript。
- 把有价值的 `[DECISION]`、`[ERROR]`、`[SESSION_SUMMARY]` 写入 Obsidian。
- 让机器标签保持英文，正文保留中文表达。
- 用可读 session 标题和 ignore filters 降低 Obsidian 图谱污染。

## 个人分支重点 / Personal Fork Focus

| Area | This fork |
|------|-----------|
| Runtime | macOS, Codex Desktop/CLI, Claude Code |
| Capture | Hook-based automatic harvesting, not manual copy/paste |
| Storage | `~/ObsidianBrain/01-Projects/<project>/Memory/` |
| Format | English machine tags, Chinese-friendly content |
| Routing | Optional `project:<slug>` / `scope:project` fields |
| Safety | Obsidian link sanitization and graph ignore filters |

## 这是什么 / What Is This

你和 AI 聊了几百次天。每次的决策、踩坑、解决方案——这些知识是最宝贵的。但如果没有系统化管理，它们就在聊天记录里烂掉。

这个个人分支做一件更具体的事：**让 Codex 和 Claude Code 的关键对话自动沉淀进我的 Obsidian vault。**

It keeps the useful parts of agent conversations close to the projects they came from, without turning the vault into a raw chat dump.

## 四层进化架构 / Four-Layer Evolution Architecture

```
┌──────────────────────────────────────────────────────────┐
│ L1: 即时标注 (Instant Annotation)                          │
│     每次决策 → [DECISION: ...]  每次错误 → [ERROR: ...]     │
│     CLAUDE.md Priority 0 强制执行                          │
│     可靠性: 最高 (事件驱动，不需要等任何东西)                    │
├──────────────────────────────────────────────────────────┤
│ L2: 自动收割 (Auto-Harvest)                               │
│     Stop hook: 关窗口 → 收割当前 transcript → 写 vault       │
│     SessionStart hook: 开窗口 → 扫 48h 漏网 transcript →    │
│     补收割 → Agent Memory 更新 → AI 初始化时已是聪明版         │
│     可靠性: 高 (系统信号 + 双保险)                            │
├──────────────────────────────────────────────────────────┤
│ L3: 深度分析 (Deep Analysis)                               │
│     每天 14:07 cron → 全量扫描 → 根因分析 → 规则维护 → 周报    │
│     不是数错误，是找根因。不是堆规则，是合并泛化。                │
│     可靠性: 中 (电脑得开着)                                   │
├──────────────────────────────────────────────────────────┤
│ L4: 手动收尾 (Manual Sync)                                 │
│     你说 "收尾/整理/sync up" → neat-freak 全量审计           │
│     可靠性: 中 (靠人记得)                                     │
└──────────────────────────────────────────────────────────┘
```

### 实时进化闭环 / The Real-Time Evolution Loop

```
Session 进行中
    ↓ AI 输出 [DECISION:] [ERROR:] (L1 — 实时)
Session 结束
    ↓ Stop hook → session_harvester.py (L2 — 秒级)
    ↓ 提取标注 → 写入 vault → 触发增量扫描
    ↓ 新模式 → 规则注入 Agent Memory
下一个 Session 开始
    ↓ SessionStart hook → 补收割漏网之鱼 (L2 — 秒级)
    ↓ AI 加载 CLAUDE.md + Agent Memory → 已经更聪明
每天 14:07
    ↓ 全量深度扫描 → 根因分析 → 规则合并 → 周报 (L3)
```

## 快速开始 / Quick Start

### 1. 一键初始化 / One-Click Setup

```bash
cd scripts/
python setup.py
```

回答三个问题：vault 放哪、项目根在哪、有哪些项目。自动建好全部目录。

### 2. 配置 CLAUDE.md / Configure CLAUDE.md

把 `patches/CLAUDE.md.patch` 的内容加到你的项目 `CLAUDE.md`。这部分是 **Priority 0 标注规则** —— AI 必须在每次决策和错误时输出结构化标注。这是整个大脑的"感官系统"。

### 3. 配置 Hooks / Configure Hooks

在 `~/.claude/settings.json` 中添加：

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python path/to/scripts/session_harvester.py --mode stop"
      }]
    }],
    "SessionStart": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python path/to/scripts/session_harvester.py --mode start"
      }]
    }]
  }
}
```

**这两个 hook 是把"被动存储"变成"主动学习"的关键。** Stop hook 在每次关窗口时收割，SessionStart hook 在每次开窗口时补收割漏网之鱼。

### 4. 配定时任务 / Schedule Daily Deep Scan

Claude Code cron (推荐):
```
/cron 7 14 * * * durable=true "cd path/to/scripts && python runner.py --full"
```

### 5. 跑一次 / Run Once

```bash
python runner.py --full
```

## Vault 结构 / Vault Structure

```
vault/
├── README.md
├── 用户手册.md
│
├── 00-Rules/                    ← 你审批的规则
│   ├── RULE-API-001.md          ← 例如: cBioPortal API 规则
│   ├── RULE-FIG-002.md          ← 例如: identity-fill 规则
│   ├── _TEMPLATE.md
│   ├── _inbox/                  ← AI 提案 (待审批)
│   │   └── _rejected/           ← 已拒绝 (30天自动清)
│   └── _archive/                ← 已归档
│
├── 01-Projects/                 ← AI 自动写
│   └── {project}/
│       └── Memory/
│           ├── sessions/        ← 每次对话的结构化总结
│           ├── decisions.md     ← 决策日志
│           └── pitfalls.md      ← 踩坑记录
│
├── 03-Maps/                     ← 自动重建 (每次扫描)
│   ├── topic-index.md           ← 按主题索引
│   └── timeline.md              ← 按时间线
│
└── 04-Feedback/                 ← 自动生成
    ├── weekly-reports/          ← 周报 (含"学到了什么")
    ├── growth-metrics.md        ← 成长指标
    ├── error-taxonomy.md        ← 错误分类词典
    ├── heartbeat.md             ← 扫描心跳
    ├── _raw-sessions/           ← JSONL 备份
    └── _logs/                   ← 运行日志
```

## 脚本说明 / Scripts Reference

| 脚本 | 干什么 | 关键特性 |
|------|--------|----------|
| `session_harvester.py` | Codex/Claude Code Hook 收割器。`--mode stop` 收割当前 session，`--mode start` 补收割漏网 transcript | 原子写入、幂等、离网工作、Obsidian 链接清洗 |
| `runner.py` | 管道编排器。5 步: backup → analyze → maintain → report → compile | UTF-8 强制、lock 防并发 |
| `backup.py` | JSONL transcript → vault 备份。过滤 agent sub-session | 原子复制、增量检测 |
| `analyzer.py` | 关键词筛选 + LLM 根因分析 + 启发式兜底 | 可离线运行，LLM 是增强 |
| `maintainer.py` | 智能审批卡 + 合并检测 + 规则 reinforce/touch | 减少重复规则 |
| `reporter.py` | 周报 + 学习叙事 + growth-metrics + 搜索索引 | 给 Obsidian 一个可读入口 |
| `compiler.py` | CLAUDE.md/AGENTS.md 规则表 + Agent Memory 同步 | 项目记忆和跨 session 记忆并行 |

## 个人版记录重点 / Personal Capture Focus

这个分支首先服务我的日常使用，而不是做一个泛化演示仓库。

它优先记录:

- Codex/Claude Code 适配过程里的技术决策。
- Obsidian vault 路径、图谱污染、链接清洗这类真实踩坑。
- 对新会话有帮助的 session summary。
- 跨项目对话里的显式 `project:<slug>` 路由。

它不会优先记录:

- 没有复用价值的中间聊天。
- 原始 JSONL 的完整内容。
- 只为了凑周报数量的重复错误。

### 示例 / Example

```text
[DECISION:保留机器标签英文，内容使用中文| context:英文标签便于程序稳定解析，中文内容便于人工在 Obsidian 中阅读]
[ERROR:type=path-filesystem| resolution=修正 Obsidian 对绝对路径 Markdown 链接的误识别，避免生成 Users/... 空文件]
```

### 规则生命周期 / Rule Lifecycle

```
启发式/LLM 发现模式
    ↓
action=new_rule → 生成审批卡 (带具体规则文本)
action=reinforce → 更新已有规则的 last_triggered
action=merge → 生成合并建议卡 (≥2 条规则重叠)
action=review → 标记需人工审查的未知模式
    ↓
你审批 (在 Obsidian 里或聊天里)
    ↓
beta (30天观察期) → active (正式规则)
    ↓
60天未触发 → 归档 (不是删除，移到 _archive/)
```

## 日常使用场景 / Daily Usage Scenarios

### 场景 1: 正常关窗口 (知识不丢)

你干完活 → 说 "收尾" → AI 输出 [SESSION_SUMMARY] → 你关窗口 → Stop hook 收割 → vault + Agent Memory 更新 → 完成

### 场景 2: 换窗口继续 (知识秒传)

你在窗口 A 做了交接 → X 掉 → 开窗口 B → SessionStart hook 收割窗口 A 的 transcript → Agent Memory 更新 → 窗口 B 的 AI 已经拿到窗口 A 的教训

### 场景 3: 突然关掉 (知识等下次)

你 Ctrl+C 两次强杀 → transcript 在磁盘上 → 下次你开窗口时 SessionStart hook 收割 → 或者等下午 14:07 cron

### 场景 4: 电脑关机过夜 (知识等明天)

你今天干完关机 → transcript 在磁盘 → 明天开机开 Claude Code → SessionStart hook 收割 → 下午 14:07 深度分析

## 注意事项 / Important Notes

- **Python 3.10+**，依赖: PyYAML, requests (LLM 模式)
- **macOS 优先**: 当前重点是 Codex Desktop/CLI 和 Claude Code 的本地 transcript
- **不联网也能跑**: LLM 聚类是可选增强，启发式分析离线工作
- **Obsidian 只是查看器**: vault 是纯 Markdown 文件夹，不需要 Obsidian 运行
- **原子写入**: 所有写操作是 .tmp → os.replace，崩溃不损坏文件
- **非破坏性**: 脚本有 --dry-run 模式
- **Idempotent**: 收割器跑两次不会重复写入

## 引用文件 / Bundled Resources

- `references/architecture.md` — 设计原理和决策记录
- `references/workflow.md` — 详细使用流程和故障排除
- `scripts/` — Python 脚本
- `templates/vault/` — Vault 模板文件
- `patches/CLAUDE.md.patch` — CLAUDE.md Priority 0 标注规则补丁
