# Agent Memory Vault for Obsidian v0.3.0-personal

面向 macOS、Codex、Claude Code 和 ZCode 的个人自动化记忆系统。

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Version](https://img.shields.io/badge/version-0.3.0--personal-orange)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Codex%20%7C%20Claude%20Code%20%7C%20ZCode-lightgrey)

这个仓库是从 `obsidian-knowledge-brain` 改出来的个人自动化版本。
我保留了它“把对话变成 Obsidian 记忆”的核心思路，但重点改成了我自己的使用场景：

- 在 macOS 上自动读取 Codex、Claude Code 和 ZCode 的对话记录。
- 用 hook 在对话结束或新对话开始时自动收割，不依赖手动复制。
- 把有价值的 `[DECISION]`、`[ERROR]`、`[SESSION_SUMMARY]` 写入 Obsidian vault。
- 为每个项目生成可读 session 标题，减少 UUID 文件名和图谱污染。
- 保留英文机器标签，内容可以自然写中文，方便人读，也方便程序解析。

---

## 这个分支和原版的区别

| 方向 | 原版/上游 | 这个个人分支 |
|---|---|---|
| 主要目标 | 通用 Obsidian 知识大脑 | 我的 Codex/Claude Code 自动记忆层 |
| 运行平台 | 泛平台说明较多 | macOS 优先，围绕 `~/.codex`、`~/.claude` 和 `~/.zcode` |
| 使用方式 | 偏框架化、可手动触发 | 默认自动化，Stop/SessionStart hook 双保险 |
| Obsidian 输出 | 规则、周报、项目记忆 | 更强调可读 session、索引入口、图谱防污染 |
| 标注格式 | 英文机器标签 | 标签/字段英文，正文默认可用中文 |
| v3 取舍 | 上游 v3 偏 skill-only | 只吸收显式项目路由和最小有效记录，不放弃自动化 |

---

## 工作模式

```
Codex / Claude Code / ZCode 对话
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

除了显式机器标签，这个个人分支还会保守识别用户长期偏好和项目规则：

- 不确定但可能有用的内容先放到 `04-Feedback/_memory-candidates/`。
- 同类内容重复出现，或置信度足够高，再写入 `05-Agent-Memory/personal-memory.md`。
- 候选和正式个人记忆都会显示在 `00-Inbox/Agent Memory Index.md` 里。

这个分支也吸收了上游 v4 和 Cognee 的轻量思路，但不切换到 v4 的 project-local `.claude/` 存储，也不引入数据库、Docker 或向量服务:

- `05-Agent-Memory/keyword-index.json` / `.md`: 从 session、decision、pitfall、personal memory 生成关键词索引。
- `05-Agent-Memory/global-atoms.json` / `.md`: 只把跨项目重复出现的已解决错误提炼为全局经验原子。
- `05-Agent-Memory/recall-index.json`: 把 session、decision、error、personal memory 拆成可召回记忆单元。
- `05-Agent-Memory/memory-graph.json`: 记录项目、笔记、决策、错误和个人记忆之间的关系边。
- `05-Agent-Memory/recall-context.md`: 给人和 Agent 阅读的轻量召回入口。

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

### Codex Profile Sync

如果你在不同 Codex / ChatGPT 账号之间切换，skills 和插件启用状态可能不一致。
这个分支提供了一个安全的本地 profile 同步脚本，只同步可迁移的文件状态:

- `~/.codex/skills` 里的自定义 skills（跳过 `.system`）。
- `~/.codex/AGENTS.md` 的共享规则。
- `config.toml` 里安全的 `marketplaces.*` / `plugins.*` 启用配置。
- 插件 manifest，用来检查目标账号缺哪些插件。

它不会同步 `auth.json`、OAuth token、connector 授权、session、日志、插件 cache 内容，
也会过滤 skill 目录里的 `.env`、私钥、token、数据库和日志文件。

先在配置完整的账号下导出:

```bash
.venv/bin/python codex_profile_sync.py export --include-config
```

默认会写到:

```text
~/ObsidianBrain/05-Agent-Memory/codex-profile
```

切换到另一个账号后，先检查差异:

```bash
.venv/bin/python codex_profile_sync.py status
```

`status` 会区分三类情况:

- `Missing skills`: 目标账号没有这个 skill。
- `Changed skills`: 目标账号有同名 skill，但内容和导出的 profile 不一致。
- `Missing enabled plugins` / `Missing plugin cache`: 插件配置或本地缓存缺失。

正式应用前建议先预览:

```bash
.venv/bin/python codex_profile_sync.py apply --include-config --dry-run
```

确认后再应用本地 skills、共享 `AGENTS.md` 和安全插件配置:

```bash
.venv/bin/python codex_profile_sync.py apply --include-config
```

如果 `status` 仍提示插件缺失或授权缺失，需要在当前账号里重新安装/授权对应插件或 connector。
这是账号侧状态，不能靠复制本地文件安全解决。

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

### ZCode compatibility

ZCode 的运行日志在 `~/.zcode/cli/log/`，但完整正文主要在 SQLite 数据库:

```text
~/.zcode/cli/db/db.sqlite
```

这个分支会把 SQLite 里的每个 `session` 当成独立 transcript，读取 `message` / `part`
表中的 user 和 assistant 正文；不会读取 `~/.zcode/v2/credentials.json`、certs、运行日志或浏览器缓存。
`reasoning`、tool 调用和工具输出也不会进入 Obsidian 采集正文。

手动测试:

```bash
.venv/bin/python session_harvester.py --mode start --agent zcode
```

如果 ZCode 提供 hook 环境变量，可以传:

```text
ZCODE_SESSION_DB=~/.zcode/cli/db/db.sqlite
ZCODE_SESSION_ID=<session-id>
```

harvester 会把它解析成 `db.sqlite::<session-id>` 并只处理这一条会话。

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

### Personal Memory Candidates

当用户说出偏好、长期规则或自动化需求时，程序会额外做一次轻量判断。比如:

```text
我的想法是把不确定的内容先放到待确认文件夹。
如果以后重复出现类似内容，再把它加到正式记录里。
```

第一次通常会进入:

```text
04-Feedback/_memory-candidates/
```

第二次命中相同主题后，会升级到:

```text
05-Agent-Memory/personal-memory.md
```

harvester 运行时也会直接打印可见记录:

```text
[harvester]   [CANDIDATE] 项目规则: 以后个人化但不确定的内容先放到待确认文件夹 (confidence=0.68, seen=1)
[harvester]       我的想法是以后个人化但不确定的内容先放到待确认文件夹
[harvester]       -> 04-Feedback/_memory-candidates/项目规则 以后个人化但不确定的内容先放到待确认文件夹.md
```

其中 `[CANDIDATE]` 表示暂存待确认，`[PROMOTED]` 表示转入正式个人记忆，`[UPDATED]` 表示已转正记忆再次被提到。

相关阈值可以在 `scripts/config.yaml` 的 `personal_memory` 里调整:

```yaml
personal_memory:
  enabled: true
  candidate_threshold: 0.45
  direct_threshold: 0.85
  promote_seen_count: 2
  similarity_threshold: 0.5
```

### v4-lite Machine Indexes

每次 harvester 成功收割或手动执行索引重建时，也会刷新:

```text
05-Agent-Memory/keyword-index.json
05-Agent-Memory/keyword-index.md
05-Agent-Memory/global-atoms.json
05-Agent-Memory/global-atoms.md
05-Agent-Memory/recall-index.json
05-Agent-Memory/memory-graph.json
05-Agent-Memory/recall-context.md
```

`keyword-index` 是给 Agent 用的机器检索入口；`global-atoms` 借鉴上游 v4，只在同一个 resolved pitfall
出现在两个以上项目时才生成，避免把单项目经验过早升级成全局规则。
`recall-index` / `memory-graph` 借鉴 Cognee 的“结构化记忆 + 关系图 + 查询召回”思路，但保持本地 Markdown 为唯一主存储。

可以用一句话查询相关记忆:

```bash
.venv/bin/python memory_recall.py "Obsidian 中文 主存储" --project github-obsidian-knowledge-brain
```

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
