# Agent Memory Vault for Obsidian v0.3.0-personal

面向 macOS、Codex 和 Claude Code 的个人自动化记忆系统。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Version](https://img.shields.io/badge/version-0.3.0--personal-orange)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Codex%20%7C%20Claude%20Code-lightgrey)

这个仓库是从 `obsidian-knowledge-brain` 改出来的个人自动化版本。
我保留了它“把对话变成 Obsidian 记忆”的核心思路，但重点改成了我自己的使用场景：

- 在 macOS 上自动读取 Codex 和 Claude Code 的 JSONL 对话记录。
- 用 hook 在对话结束或新对话开始时自动收割，不依赖手动复制。
- 把有价值的 `[DECISION]`、`[ERROR]`、`[SESSION_SUMMARY]` 写入 Obsidian vault。
- 为每个项目生成可读 session 标题，减少 UUID 文件名和图谱污染。
- 保留英文机器标签，内容可以自然写中文，方便人读，也方便程序解析。

---

## 这个分支和原版的区别

| 方向 | 原版/上游 | 这个个人分支 |
|---|---|---|
| 主要目标 | 通用 Obsidian 知识大脑 | 我的 Codex/Claude Code 自动记忆层 |
| 运行平台 | 泛平台说明较多 | macOS 优先，围绕 `~/.codex` 和 `~/.claude` |
| 使用方式 | 偏框架化、可手动触发 | 默认自动化，Stop/SessionStart hook 双保险 |
| Obsidian 输出 | 规则、周报、项目记忆 | 更强调可读 session、索引入口、图谱防污染 |
| 标注格式 | 英文机器标签 | 标签/字段英文，正文默认可用中文 |
| v3 取舍 | 上游 v3 偏 skill-only | 只吸收显式项目路由和最小有效记录，不放弃自动化 |

---

## 工作模式

```
Codex / Claude Code 对话
    ↓
模型在回复里写 [DECISION] / [ERROR] / [SESSION_SUMMARY]
    ↓
Stop hook 或 SessionStart hook 读取 JSONL transcript
    ↓
session_harvester.py 去重、识别项目、清洗 Obsidian 链接
    ↓
写入 ~/ObsidianBrain/01-Projects/<project>/Memory/
    ↓
刷新 00-Inbox/Agent Memory Index.md
```

这不是一个“把全部聊天记录塞进 Obsidian”的工具。它更像一个过滤器：
只把可复用的决策、已经解决的问题、会影响下次工作的总结留下来。

---

## 快速安装 / Quick Install

### macOS + Codex

```bash
git clone https://github.com/2731350936/obsidian-knowledge-brain.git
cd obsidian-knowledge-brain/scripts
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # PyYAML + requests (LLM mode optional)
.venv/bin/python setup.py
```

脚本会默认使用:

- Codex transcript: `~/.codex/sessions`
- Vault: `~/ObsidianBrain`
- Agent Memory markdown: `~/ObsidianBrain/05-Agent-Memory`

然后安装 Codex 集成。这个命令会非破坏性合并 `~/.codex/hooks.json`，并把 `patches/CLAUDE.md.patch` 追加到当前目录的 `AGENTS.md`。写入前会自动生成 `.bak-*` 备份。

```bash
.venv/bin/python install_codex.py
```

也可以先预览:

```bash
.venv/bin/python install_codex.py --dry-run
```

对 Codex 来说，`AGENTS.md` 里的标注规则是 L1 感官系统；`session_harvester.py` 会从 Codex JSONL transcript 中读取这些标注。
每次成功收割后，harvester 还会刷新 `00-Inbox/Agent Memory Index.md`，把最近的 session、decision 和 error 汇总成一个 Obsidian 入口。

### 与上游 v3 的关系

这个分支保留 v2 的自动化工作流: Codex / Claude Code 对话结束后由 hook 自动收割，
开新对话时由 SessionStart 补收割，不要求用户手动运行 skill。

从 v3 吸收的部分是更明确的“最小有效记录”和防污染思路:

- 标注内容保持短句，只记录能复用的技术决策和已解决错误。
- 默认使用 `[DECISION:...| context:...]` / `[ERROR:type=...| resolution=...]`。
- 需要跨项目路由时，可以额外写 `project:<project-slug>` 和 `scope:project`。

示例:

```text
[DECISION:保留 hook 自动收割作为主路径| context:用户目标是自动化记录，不是手动 skill 调用| project:github-obsidian-knowledge-brain| scope:project]
[ERROR:type=path-filesystem| resolution=修正 Obsidian 对绝对路径 Markdown 链接的误识别| project:github-obsidian-knowledge-brain]
```

如果只是想手动重建这个入口，不处理 transcript:

```bash
.venv/bin/python session_harvester.py --mode index
```

手动配置时，在 `~/.codex/hooks.json` 里加入 Stop + SessionStart hook。如果已有 hooks，不要覆盖原有项，把下面的 command 追加到对应数组里:

```json
{
  "hooks": {
    "Stop": [{
      "hooks": [{
        "type": "command",
        "command": "path/to/scripts/.venv/bin/python path/to/scripts/session_harvester.py --mode stop",
        "timeout": 120
      }]
    }],
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "path/to/scripts/.venv/bin/python path/to/scripts/session_harvester.py --mode start",
        "timeout": 120
      }]
    }]
  }
}
```

最后配定时任务:

```bash
cd path/to/scripts && .venv/bin/python runner.py --full
```

### Claude Code compatibility

Claude Code 仍然可以使用同一套脚本。把 `config.yaml` 里的 `agent` 设为 `claude`，并填 `claude_project_path`。

**然后必须做两件事**:
1. 把 `patches/CLAUDE.md.patch` 的内容加到你的 `CLAUDE.md`（这是 L1 — AI 的"感官系统"）
2. 在 `~/.claude/settings.json` 配好 Stop + SessionStart hook（这是 L2 — 自动收割）

```json
{
  "hooks": {
    "Stop": [{"matcher": "", "hooks": [{"type": "command", "command": "python path/to/scripts/session_harvester.py --mode stop"}]}],
    "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": "python path/to/scripts/session_harvester.py --mode start"}]}]
  }
}
```

**最后配定时任务**:
```
/cron 7 14 * * * durable=true "cd path/to/scripts && python runner.py --full"
```

---

## 记录什么 / What Gets Captured

这个分支默认只收割三类结构化内容，不把完整聊天记录直接塞进项目笔记:

```text
[DECISION:保留 hook 自动收割作为主路径| context:用户目标是自动化记录，不是手动 skill 调用| project:github-obsidian-knowledge-brain| scope:project]
[ERROR:type=path-filesystem| resolution=修正 Obsidian 对绝对路径 Markdown 链接的误识别| project:github-obsidian-knowledge-brain]
[SESSION_SUMMARY]
projects: [github-obsidian-knowledge-brain]
primary: github-obsidian-knowledge-brain
summary: "本轮完成 macOS Codex/Claude Code 自动采集与 Obsidian 写入验证。"
[/SESSION_SUMMARY]
```

其中:

- `DECISION` 记录以后还会复用的技术取舍。
- `ERROR` 记录已经解决的问题，避免下次重复踩坑。
- `SESSION_SUMMARY` 记录一次对话的收束信息。
- `project` 是可选字段，用来在跨项目对话里明确归档位置。

---

## 实际效果 / What You'll See

### Obsidian 里会看到

```markdown
01-Projects/github-obsidian-knowledge-brain/Memory/
├── sessions/
│   └── 2026-06-22-保留 hook 自动收割作为主路径.md
├── decisions.md
└── pitfalls.md

00-Inbox/
└── Agent Memory Index.md
```

### Session 文件里会看到

```markdown
# 保留 hook 自动收割作为主路径

Session: test-session | Date: 2026-06-22 | Project: github-obsidian-knowledge-brain

## Decisions

1. **保留 hook 自动收割作为主路径**
   - Context: 用户目标是自动化记录，不是手动 skill 调用

## Errors Encountered

1. `path-filesystem`
   - Resolution: 修正 Obsidian 对绝对路径 Markdown 链接的误识别
```

### 个人化改动

```text
上游思路: AI 对话应该沉淀成 Obsidian 知识。
这个分支: 我的 Codex/Claude Code 对话应该自动沉淀，且不能污染 vault。
```

---

## 为什么不用... / Why Not Just Use...

| 方案 | 问题 |
|------|------|
| **纯 CLAUDE.md** | 单文件膨胀、无自动发现跨项目模式、无时间线/主题索引 |
| **数据库** | 需要 schema 维护、AI 不能直接读写、Obsidian 打不开 |
| **Notion/Confluence** | API 限流、需联网、知识锁在 SaaS、不跨 AI 工具 |
| **纯 Agent Memory** | 只对当前 AI 有效。换工具不认。Markdown vault 任何工具都能读 |
| **只保留原始 JSONL** | 信息太多，Obsidian 图谱会乱，下一轮对话也不一定会读 |

---

## 目录结构 / What's Inside

```
obsidian-knowledge-brain/
├── SKILL.md                         ← Agent Memory Vault 技能定义
├── README.md                        ← 本文件 / This file
│
├── scripts/                         ← 11 个 Python 脚本
│   ├── setup.py                     ← 一键初始化
│   ├── session_harvester.py         ← Codex/Claude Code Hook 收割器
│   ├── runner.py                    ← 管道编排 (5 步)
│   ├── backup.py                    ← JSONL 备份 + Nutstore
│   ├── analyzer.py                  ← 根因分析
│   ├── maintainer.py                ← 智能卡片 + 合并检测
│   ├── reporter.py                  ← 周报 + 学习叙事
│   ├── compiler.py                  ← CLAUDE.md + Agent Memory
│   ├── config.py / config.example.yaml
│   └── requirements.txt             ← PyYAML + requests
│
├── references/                      ← 深入文档
│   ├── architecture.md              ← 8 个设计决策 + Anti-Patterns
│   └── workflow.md                  ← 四层工作流 + Hook 配置 + 管道
│
├── templates/vault/                 ← Vault 模板
└── patches/CLAUDE.md.patch         ← Codex/Claude Code 标注规则
```

---

## 常见问题 / FAQ

**Q: 离网能用吗？/ Works offline?**
A: 完全离网工作。启发式根因分析不需要网络。LLM 深度分析是可选的增强。

**Q: 必须装 Obsidian？**
A: 不必须。Vault 是纯 Markdown 文件夹，任何编辑器都能看。Obsidian 提供更好的可视化。

**Q: 漏扫了怎么办？**
A: runner.py 启动时自动检测上次扫描时间。超过 7 天自动切换全量模式补跑。

**Q: 会改坏文件吗？**
A: 所有写操作是原子写入 (.tmp → os.replace)。崩溃不损坏原文件。有 `--dry-run` 预览模式。

---

## 许可证 / License

MIT — 随便用、改、分发。
