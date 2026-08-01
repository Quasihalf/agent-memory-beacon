# Agent Memory Beacon v0.7.0

面向 Codex 和本地 Agent 的 Obsidian 长期记忆系统。

Agent Memory Beacon 会把经过筛选的决策、错误、偏好、Skill 路由和工作流规则保存在用户拥有的 Obsidian Markdown 中，并在需要时向 Agent 提供可见、可追踪的相关记忆。

本项目源自 [Tubo2333/obsidian-knowledge-brain](https://github.com/Tubo2333/obsidian-knowledge-brain)，现在采用独立产品名称和以自动化长期记忆为中心的方向。

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![Version](https://img.shields.io/badge/version-0.7.0-orange)
![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Codex%20%7C%20Claude%20Code%20%7C%20ZCode-lightgrey)

## 核心能力

- 在 macOS 上自动读取 Codex、Claude Code 和 ZCode 的对话记录。
- 用 hook 在对话结束或新对话开始时自动收割，不依赖手动复制。
- Codex 长对话会在首次消息、换题、正式索引更新和高风险场景自动刷新多条相关记忆。
- Codex 长对话会静默生成滚动会话摘要；同一 session 只保留最新一版，后续相关任务可把它作为低优先级 `[CONTEXT]` 召回。
- 把有价值的 `[DECISION]`、`[ERROR]`、`[FAVOR]`、`[SESSION_SUMMARY]` 写入 Obsidian vault。
- 对显式标签执行语义质量门：耐久内容进入正式记忆，不确定内容进入隐藏待确认，明确噪声被拒绝。
- 把未解决工具失败先放入私有候选，不把测试 RED、成功重试或不可信通知正文直接升级成正式错误。
- 为每个项目生成可读 session 标题，减少 UUID 文件名和图谱污染。
- 学习用户反复纠正过的工作流程，比如“分析 GitHub 项目前先查源码”“pensive 审查后可直接修复”。
- 学习用户提出的一次性高价值启发，在设计、类比、替代方案或卡住时按需注入，拓展 Agent 的解题思路。
- 把同一任务中共同出现的 Decision、Error、Workflow 和 Insight 组成轻量经验链；只有明确询问类似经历且带具体内容时才补充同任务记忆。
- 为每次召回显示 `why_recalled` 和 `authority`，区分内容命中、图谱关系、经验链以及事实由谁拥有、在哪里执行。
- 记录不含提示词和记忆正文的效果事件，生成可读效果账本；重复来源或正向使用证据只会生成隔离的转化建议，不会自动改代码或正式记忆。
- 保留英文机器标签，内容可以自然写中文，方便人读，也方便程序解析。
- 支持 Windows 与 Mac 同时产生 Codex/Claude 对话：Windows 只上传不可变 transcript evidence，Mac 仍是唯一 Vault writer，并向 Windows 发布经过校验的只读副本。

---

## 与上游的区别

| 方向 | 原版/上游 | Agent Memory Beacon |
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
模型在回复里写 [DECISION] / [ERROR] / [FAVOR] / [SESSION_SUMMARY]
    ↓
Stop hook、SessionStart hook 或 launchd 读取 JSONL / ZCode SQLite transcript
    ↓
session_harvester.py 去重、识别项目、执行标签质量分流、协调工具失败/重试
    ↓
写入 ~/AgentMemoryBeacon/01-Projects/<project>/Memory/
    ↓
刷新 00-Inbox/Agent Memory Index.md

Codex 每条实质用户消息
    ↓
UserPromptSubmit 按触发条件读取 recall-index
    ↓
只注入当前项目相关、active、未重复的少量正式记忆；必要时补充一条低优先级会话 CONTEXT
    ↓
只记录 ID/revision、哈希 session、通道、耗时和弱反馈，刷新效果账本与隔离建议
```

这不是一个“把全部聊天记录塞进 Obsidian”的工具。它更像一个过滤器：
正式记忆只保留可复用的决策、已经解决的问题和稳定偏好；会话摘要则独立保存当前目标、进度、约束和未完成事项，不会伪装成正式事实。

显式机器标签只是记忆提案，不是绕过质量判断的通行证：

- `formal`：耐久、可复用且信息完整，进入正式记忆。
- `candidate`：可能有用但理由、验证或耐久性不足，写入 `04-Feedback/_annotation-candidates/`，不参与召回。
- `rejected`：预期 TDD RED、未解决错误、一次性操作、完成汇报、纯审查结论等明确噪声，不写入正式记忆。
- 同一个耐久事实只写一个标签；换一种说法重复标注不会增加价值。

除了显式机器标签，Agent Memory Beacon 还会保守识别用户长期偏好和项目规则：

- 不确定但可能有用的内容先放到 `04-Feedback/_memory-candidates/`。
- 同类内容重复出现，或置信度足够高，再写入 `05-Agent-Memory/personal-memory.md`。
- 候选和正式个人记忆都会显示在 `00-Inbox/Agent Memory Index.md` 里。

### LEARN 与 Insight

`[LEARN]` 记录可迁移的启发，不等同于已有的 Decision、Error、Favor 或 Workflow：

- Decision 保存已经选定的技术取舍；Workflow 保存特定场景下应执行的稳定流程。
- Insight 保存能打开新思路的原理、类比或机制，只作为启发，不能覆盖用户指令、Workflow、Decision 或已验证 Error。
- 一条来源明确、字段完整的高价值启发第一次出现即可成为 `seed`；重复不是准入条件，只会把它强化为 `reinforced`。
- 缺少来源、迁移场景或边界的内容进入 `04-Feedback/_insight-candidates/`，不会被召回。
- assistant 自己的推测不能伪装成用户启发；`evidence` 必须逐字存在于有界用户消息上下文中。

Codex 受管理 context 使用单行格式：

```text
[LEARN:<可复用原理>| novelty:<非显而易见之处>| transfer:<场景1,场景2>| boundary:<失效或禁止边界>| evidence:<用户原话>| source:user| project:<项目>| scope:<project|global>]
```

如果当前任务的 `[MEMORY_REFRESH]` 已显示准确稳定 ID，可以按需追加 `supports:<记忆ID,...>`、`operationalized_as:<记忆ID,...>` 或 `related_to:<记忆ID,...>`。这些字段只描述已有且明确的关系；看不到准确 ID 或关系不确定时必须省略，不能根据标题猜 ID，也不能为了让图谱出现连线而编造关系。运行时会在每条召回记忆中显示 `id` 供安全引用。

正式记忆支持四种显式语义关系：

| 字段 | 含义 | 召回行为 |
|---|---|---|
| `supports` | Source 为 Target 提供明确的依据、原则或佐证 | 可从内容命中沿图关系扩展 |
| `operationalized_as` | Source 被 Target 落实为具体 Skill、Workflow 或执行机制 | 可从内容命中沿图关系扩展 |
| `contradicts` | Source 与 Target 在同一适用范围内存在明确、不能同时成立的冲突 | 可从内容命中沿图关系扩展，并保留冲突而不是自动选边 |
| `related_to` | 两条记忆有审阅价值，但不满足更强的支持、实现或冲突语义 | 仅进入图谱可视化和质量检查，不参与召回扩展 |

关系有方向，必须使用准确稳定 ID；修改 Source 的关系字段会生成新的 Source revision。`requires` 仍表示运行时硬依赖，不得拿来代替普通关联。图谱只是正式 Markdown 的派生视图，不能反向创造关系。

正式记录位于 `05-Agent-Memory/insights.md`。只有设计新功能、寻找替代方案、做类比、比较借鉴或当前路径卡住，并且查询含有具体内容锚点时，运行时才自动注入 `[INSIGHT]`；每次最多 2 条、默认合计不超过 400 tokens。显式“我有哪些启发”可查看清单，“给我一个思路”这类无具体对象的请求保持静默。

自适应学习还有三层防污染边界:

- Codex/Claude 注入的 `AGENTS.md`、`CLAUDE.md`、`<INSTRUCTIONS>`、插件推荐和环境上下文不会被当作用户原话。
- Codex sub-agent 和 Claude sidechain 可以产出结构化 session，但不会参与个人偏好、Skill 或 Workflow 学习。
- 首次安装只建立 transcript 高水位；长对话继续追加时，自适应学习只读取高水位之后的新消息，不回放旧内容。

Codex 的工具/审查证据使用独立候选层：

- 测试先失败后通过、同操作失败后成功会被视为预期或暂时失败，不保留为正式记忆。
- 终态工具失败以固定诊断类别和退出码进入 `04-Feedback/_error-candidates/`，不保存原始命令、输出或用户提示词。
- Codex 当前没有给 sub-agent 通知提供可验证来源，因此自动审查正文采集默认关闭；审查结论应由 Agent 输出显式 `[ERROR]`，以后只有可信结构化事件才能重新启用自动采集。
- 只有带根因、解决办法和验证结果的显式 `[ERROR]` 才能进入项目 `pitfalls.md`；候选永不进入运行时召回。
- 候选变化和索引重建通过 generation marker 协调；候选、heartbeat 和索引都使用随机排他临时文件、固定父目录、文件/目录 `fsync`，中断后会在下一次 harvest 自动修复索引。

Agent Memory Beacon 也吸收了上游 v4、Cognee、Hindsight 和 graph-engineering 的轻量思路，但不切换到 v4 的 project-local `.claude/` 存储，也不引入数据库、Docker、向量服务或 LLM 自动建边:

- `05-Agent-Memory/keyword-index.json` / `.md`: 从 session、decision、pitfall、personal memory 生成关键词索引。
- `05-Agent-Memory/global-atoms.json` / `.md`: 只把跨项目重复出现的已解决错误提炼为全局经验原子。
- `05-Agent-Memory/recall-index.json`: `units` 只收录 `active` 的正式 decision、error、personal memory、skill preference、workflow rule 和 insight；另有隔离的 `conversation_summaries` 集合保存每个 session 的最新派生摘要。
- `05-Agent-Memory/memory-graph.json`: Graph v3 派生索引；节点使用稳定类型，关系遵守 domain/range 合同，每条边绑定来源、revision、观察时间、推导方式和置信度。正式记忆之间的语义边还必须能从 source unit 的 `requires`、`supports`、`operationalized_as`、`superseded_by` 等字段反向证明，图不能自行创造关系。它与 recall index 共用确定性 `generation_id`，任一正式正文、revision 或笔记链接变化都会生成新代次。
- `05-Agent-Memory/memory-graph-quality.md`: 人类可读的图质量报告，显示非法边、缺失证据、过期 revision、悬空引用和孤立节点。
- `05-Agent-Memory/recall-context.md`: 给人和 Agent 阅读的轻量召回入口。
- `memory_recall.py`: 分别运行词项、结构化名称、记忆类型、时间和显式图关系检索，再用带权 RRF 融合；结果保留每个通道的排名证据。

类型和时间不会成为宽松的“猜测入口”。查询会先把“错误、失败、修复、问题、最近、一次、哪些”等意图词与具体内容词分开，这些弱词本身不能建立词项或结构化名称锚点。只有明确的清单问题，例如“我有哪些个人偏好”，或明确的时间问题，例如“最近一次错误”，才允许类型/时间通道在没有内容命中的情况下建立候选；带单项语义的时间查询只返回相关候选中的一条，普通“修复这个问题”则返回空。这样借用了 Hindsight 的多路召回结构，同时保留 Beacon 优先避免误召回的边界。

### 效果、权威和任务经验链

- `04-Feedback/memory-effectiveness.md` 是效果账本。事件只保存记忆 ID/revision、哈希后的任务身份、召回通道、耗时、估算 token 和保守反馈，不保存用户 prompt、记忆正文或真实 session ID。
- 正式记忆可声明 `authority_role`、`authority_owner`、`canonical_source`、`enforced_by`、验证引用和新鲜度策略。相关性始终先于权威性；只有内容相关性相同时，权威等级才参与排序。
- `recall-index.json` 的 `experience_bundles` 只保存共享 `session:` 来源的正式记录 ID、精确 revision、类型、项目和日期。它不复制 session 正文，也不是可执行 lifecycle 的正式记忆。
- 只有“以前怎么处理下载断点续传的完整过程”这类明确经验意图并带具体内容锚点的请求才展开经验链；companion 还必须与已命中记忆存在具体词汇桥接，不能只因为处于同一个长 session 就注入。清单问题、模糊“以前怎么处理”和普通任务不会展开；一次最多补 2 条，只使用 1 个经验包。
- `04-Feedback/_promotion-proposals/` 保存把稳定 Decision、Error 或 Workflow 转移到更强执行面的建议。至少需要 3 个独立来源，或 2 次召回加正向信号；出现纠正/误导证据、已有 `enforced_by` 或来自候选路径时不会生成。
- harvest 和 weekly maintenance 会幂等刷新效果报告与转化建议。建议永远不会自动修改源码、AGENTS、测试或正式记忆。

新版记忆采用三层数据模型:

1. `01-Projects/*/Memory/sessions/` 保存历史证据；session 正文不进入正式召回，但最新有效摘要可派生为一条低优先级 `CONTEXT`。
2. 项目 `decisions.md` / `pitfalls.md` 与 `05-Agent-Memory/` 保存 schema `2.0` 正式记忆，包含稳定 ID、revision、status、scope 和来源。
3. `04-Feedback/_memory-candidates/`、`_annotation-candidates/`、`_skill-preferences/`、`_workflow-candidates/`、`_error-candidates/`、`_insight-candidates/` 保存待确认材料，candidate、rejected 和非 active 内容不会注入 Agent。

已有 Vault 可以先只读预览旧记忆升级。预览不会写文件:

```bash
cd scripts
.venv/bin/python migrate_memory_v2.py --vault /path/to/your/vault
```

确认 `writes`、项目路由和候选清理结果后再应用。应用前会在
`04-Feedback/_rollback/memory-v2/<migration-id>/` 创建并验证完整备份:

```bash
.venv/bin/python migrate_memory_v2.py \
  --vault /path/to/your/vault \
  --apply \
  --migration-id 20260712-memory-v2
```

需要回退时使用应用结果中的 `manifest` 路径:

```bash
.venv/bin/python migrate_memory_v2.py \
  --vault /path/to/your/vault \
  --rollback /path/to/your/vault/04-Feedback/_rollback/memory-v2/20260712-memory-v2/manifest.json
```

迁移器把已经符合 schema `2.0` 的项目聚合记录视为权威来源，即使某条历史记录内容不完整，也不会再从 session 猜测并覆盖它。它只分类真正的 legacy 记录；已升级的 Personal、Skill、Workflow 和 candidate 文件不会被重复处理。记录自身的 `project` 决定最终聚合路径，跨项目归位不会改变 ID、revision、status 或内容。一次成功迁移后再次预览应得到 `planned_writes: 0`，可用这个零写入结果确认迁移幂等。

---

## 快速安装 / Quick Install

### macOS + Codex

```bash
git clone https://github.com/Quasihalf/agent-memory-beacon.git
cd agent-memory-beacon/scripts
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python setup.py
```

新安装会默认使用:

- Codex transcript: `~/.codex/sessions`
- 新安装默认 Vault：`~/AgentMemoryBeacon`
- 已有安装继续使用配置中的路径，例如 existing `~/ObsidianBrain`；升级不会移动目录。
- Agent Memory markdown: `~/AgentMemoryBeacon/05-Agent-Memory`

`setup.py` 会为全新 Vault 生成有效的空召回索引，因此无需先运行一次深度扫描才能安装稳定运行时。

然后使用稳定运行时安装器。它先运行源码 CI，再把 allowlist 内容复制到
`~/.local/share/agent-memory-beacon/runtime` 的隔离 venv，运行 staging quick
检查，最后按当前配置事务切换 Codex/Claude Hook、Agent context、harvest、weekly
以及可选的 authority sync LaunchAgent：

```bash
.venv/bin/python install_runtime.py --dry-run
.venv/bin/python install_runtime.py --verify-release
.venv/bin/python install_runtime.py
```

`--verify-release` 会真实执行源码 CI、创建全新 staging venv、安装依赖并运行 quick Doctor，随后删除 staging；它不会修改 Hook、AGENTS、launchd 或稳定运行时。建议在首次安装和正式发布前运行。

安装器不会复制 `.git`、tests、planning、缓存、日志、数据库、凭据或任意仓库文件；稳定运行时使用经过审计的 `requirements.lock`，发布后的 `config.yaml` 会清除内联 secret，并只保留运行所需路径。安装事务会从当前配置动态枚举并逐字节快照所有受管 Hook、context 和 LaunchAgent；任一步失败都会恢复旧运行时、原文件和原服务状态。成功输出中的 `manifest_path` 是手动回滚入口：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/install_runtime.py \
  --rollback-manifest /absolute/path/from/manifest_path
```

手动回滚会先检查安装后是否有人继续修改 Hook、AGENTS 或 plist；检测到漂移时拒绝覆盖。新增或更新 command hook 后，在 Codex 中打开 `/hooks`，确认并信任 Agent Memory Beacon 的三个命令。Codex 的 hook trust 独立于 `hooks.json`；安装器只报告 `trust_review_required`，不会伪造或覆盖 trust 状态。

`install_codex.py` 和 `install_launchd.py` 仍保留用于组件级开发、排障和测试，但正式运行不应继续指向可移动的开发 checkout。

同一台 Mac 上使用 Claude Code 时，可单独安装 collection-only hook。ZCode 保留已有采集兼容性，但当前阶段不再扩展其行为：

```bash
.venv/bin/python install_claude.py
.venv/bin/python install_zcode.py --context-only  # 仅维护已有兼容配置
```

稳定运行时事务会安装按配置启用的短任务，而不是常驻 Python 进程:

- `io.agent-memory-beacon.harvest`: 每 300 秒检查新增/变化的 transcript，使用 launchd `Standard` 调度并在运行完后退出，避免索引构建被后台限速拖过下一周期。
- `io.agent-memory-beacon.weekly`: 按 `config.yaml` 的 `scan.day/hour/minute` 使用 `Background` 调度执行深度管道。
- `io.agent-memory-beacon.sync`: 仅在 `beacon_sync.role: authority` 且已启用时安装，按短周期执行 reduce、publish 和 receipt 发布。

在源码 checkout 中，安装、升级或发布前使用：

```bash
.venv/bin/python doctor.py --profile quick
.venv/bin/python doctor.py --profile ci
```

完成稳定迁移后，运行时只使用 `quick` 或 `live`：

```bash
RUNTIME="$HOME/.local/share/agent-memory-beacon/runtime"
"$RUNTIME/.venv/bin/python" "$RUNTIME/scripts/doctor.py" --profile quick
"$RUNTIME/.venv/bin/python" "$RUNTIME/scripts/doctor.py" --profile live
```

- `quick`: 检查配置、模块导入和全部 Python 脚本语法。
- `ci`: 仅供包含 tests、fixtures 和 Git 元数据的源码 checkout；在 quick 基础上运行全量单元测试、版本化召回评估和 `git diff --check`。
- `live`: 检查真实 Vault frontmatter/wikilink、候选隔离、Codex Hook、launchd 路径/服务和只读 Hook 协议探针。

需要给自动化工具消费时增加 `--json`。doctor 默认只读；它不会修复索引、修改 Hook、重载 launchd 或写入召回状态。

首次安装后台收割时，程序会把现存 JSONL 的文件字节位置和 ZCode session 的消息数写入 `heartbeat.md`，但不会读取并导入安装前历史。之后所有正式注解和候选学习只处理新建会话或已建会话的新增部分；Codex 工具错误关联只回看游标前最多 4 MiB，不会反复扫描整段长会话。

SessionStart/harvest launchd 默认每批最多处理 32 个 transcript，并在 180 秒软预算后停止领取新项。一个批次内的所有变更只触发一次完整索引重建；发生写入的 transcript 只有在该次重建成功后才提交高水位游标，失败或中断时会在下一轮幂等重试。批次日志会分别输出 processing、index 和 total 耗时；索引日志还会细分 repair、collect、knowledge、render_write 和 dirty_clear，便于定位真实 Vault 中的慢阶段。

对 Codex 来说，`AGENTS.md` 里的标注规则是 L1 感官系统；`session_harvester.py` 会从 Codex JSONL transcript 中读取这些标注。
每次成功收割后，harvester 还会刷新 `00-Inbox/Agent Memory Index.md`，把最近的 session、decision 和 error 汇总成一个 Obsidian 入口。

`UserPromptSubmit` 使用独立的 `codex_prompt_hook.py`：每条实质消息先做低成本索引版本和会话状态检查，只在首次消息、正式索引更新、重要错误/操作、明显换题或超过 30 分钟时检索。成功时会在当前轮看到 `[MEMORY_REFRESH]`；无相关记忆、短确认、锁争用、超时或任何异常都返回空 JSON，不阻断消息。

### 后台滚动会话摘要

滚动摘要复用当前正在回答的 Codex 模型，不启动第二个模型、不发起额外 API 请求，也没有常驻摘要进程。`UserPromptSubmit` 只在检查点到期时加入一段私有指令，让当前回答末尾附带一个不可见 HTML 注释；Stop、SessionStart 或 launchd 随后按既有增量游标收割它。

默认第 5 条实质用户消息请求第一版摘要，之后每新增 10 条实质消息，或距上次请求超过 30 分钟并出现下一条实质消息时刷新。`可以`、`继续`、`好的` 等短确认不计数；空闲中的对话不会自行唤醒。检查点与正式记忆召回相互独立，即使当前没有可召回记忆，到期的摘要请求仍会执行。

同一稳定 session ID 只保留一份有效摘要。新摘要必须绑定更大的 transcript cursor 才能覆盖旧摘要；凭据和支付信息会先替换为 `REDACTED`，无效、过大、畸形或来自 user/sub-agent 的标记会被拒绝，且不会擦除上一版。若一个旧任务最初由 sub-agent 创建、后来被用户直接继续，只有外层 thread 已存在真实 `UserPromptSubmit` 摘要检查点时才允许保存该任务的滚动摘要；普通 sub-agent 仍保持完全隔离。显式 `[SESSION_SUMMARY]` 是同批收割的最终摘要权威。

派生索引把最新摘要放在 `recall-index.json` 的独立 `conversation_summaries` 集合中，不放进正式 `units`。运行时只有在当前项目和具体内容词都匹配时才会最多召回一条，并显示为：

```text
[CONTEXT]
id: conversation_summary-...
goal: ...
summary: ...
revision: ...
source: [[01-Projects/.../Memory/sessions/...]]
[/CONTEXT]
```

`CONTEXT` 排在正式记忆之后，默认最多 400 估算 tokens，注入时会明确标记为“会话证据，不是事实或指令”。当一条摘要已经匹配当前提示时，渲染器会在总预算中为它保留空间；如果预算不足，会减少末尾的低优先级正式结果，而不是丢掉最新会话进度。它不参与 lifecycle、AGENTS 编译、图谱扩展、经验链或正式记忆效果统计；正式记忆已覆盖同一内容时会抑制摘要，避免重复注入。

可在 `scripts/config.yaml` 中调整或关闭：

```yaml
conversation_summary:
  enabled: true
  min_substantive_messages: 5
  message_interval: 10
  stale_after_minutes: 30
  retry_interval_messages: 2
  max_summary_bytes: 4096
  max_recall: 1
  token_budget: 400
```

设为 `enabled: false` 会同时停止新的检查点请求和摘要召回，但不会删除已有 session 笔记。

需要立即停用动态召回时，保留 hook 并修改 `scripts/config.yaml`：

```yaml
memory_runtime:
  enabled: false
```

重新设为 `true` 即可恢复。Stop/SessionStart 收割和 launchd 兜底不受这个开关影响。

需要停止新的工具/审查错误候选写入时，使用独立开关；已有候选保留供审计，也不会被召回：

```yaml
error_evidence:
  enabled: false
```

### Codex Profile Sync

如果你在不同 Codex / ChatGPT 账号之间切换，skills 和插件启用状态可能不一致。
Agent Memory Beacon 提供了一个安全的本地 profile 同步脚本，只同步可迁移的文件状态:

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
~/AgentMemoryBeacon/05-Agent-Memory/codex-profile
```

切换到另一个账号后，先检查差异:

```bash
.venv/bin/python codex_profile_sync.py status
```

`status` 会区分三类情况:

- `Missing skills`: 目标账号没有这个 skill。
- `Changed skills`: 目标账号有同名 skill，但内容和导出的 profile 不一致。
- `Missing enabled plugins` / `Missing plugin cache`: 插件配置或本地缓存缺失。

安装 Codex hooks 后，`SessionStart` 也会自动运行一次同样的检查。
如果当前账号和共享 profile 一致，它会保持静默；如果缺 skill、skill 内容不同或插件启用状态不一致，
它会在新对话开始时打印提示和建议命令。它不会自动 apply，避免后台覆盖另一个账号自己的配置。
compiler 更新个人记忆时，会同步刷新 profile 里 `AGENTS.shared.md` 的受管理规则块，避免以后执行 apply 时把新记忆回写成旧版；Skills、插件清单和安全配置仍只在显式 `export` 时更新。

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

Agent Memory Beacon 保留 v2 的自动化工作流: Codex / Claude Code 对话结束后由 hook 自动收割，
开新对话时由 SessionStart 补收割，不要求用户手动运行 skill。

从 v3 吸收的部分是更明确的“最小有效记录”和防污染思路:

- 标注内容保持短句，只记录能复用的技术决策和已解决错误。
- 默认使用 `[DECISION:...| context:...]` / `[ERROR:type=...| resolution=...]`。
- 需要跨项目路由时，可以额外写 `project:<project-slug>` 和 `scope:project`。

示例:

```text
[DECISION:保留 hook 自动收割作为主路径| context:用户目标是自动化记录，不是手动 skill 调用| project:agent-memory-beacon| scope:project]
[ERROR:type=path-filesystem| resolution=修正 Obsidian 对绝对路径 Markdown 链接的误识别| project:agent-memory-beacon]
```

如果只是想手动重建这个入口，不处理 transcript:

```bash
.venv/bin/python session_harvester.py --mode index
```

手动配置时，在 `~/.codex/hooks.json` 里加入 Stop、SessionStart 和 UserPromptSubmit hook。如果已有 hooks，不要覆盖或重排原有项，把下面的 command 作为独立 group 追加到对应数组末尾:

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
    }],
    "UserPromptSubmit": [{
      "hooks": [{
        "type": "command",
        "command": "path/to/scripts/.venv/bin/python path/to/scripts/codex_prompt_hook.py",
        "timeout": 2
      }]
    }]
  }
}
```

手动修改后同样需要在 Codex `/hooks` 中审核新命令。

macOS 后台任务统一用 launchd 安装:

```bash
.venv/bin/python install_launchd.py
launchctl print gui/$(id -u)/io.agent-memory-beacon.harvest
launchctl print gui/$(id -u)/io.agent-memory-beacon.weekly
```

### 迁移兼容性

升级会识别旧安装中的 `KNOWLEDGE_BRAIN` 受管理标记、
`com.obsidian-knowledge-brain.harvest` / `com.obsidian-knowledge-brain.weekly` launchd 标签，
以及历史项目 slug `github-obsidian-knowledge-brain`。这些兼容标识保留用于迁移；新安装使用
`AGENT_MEMORY_BEACON` 和 `io.agent-memory-beacon.*`。

### ZCode compatibility

ZCode 的运行日志在 `~/.zcode/cli/log/`，但完整正文主要在 SQLite 数据库:

```text
~/.zcode/cli/db/db.sqlite
```

Agent Memory Beacon 会把 SQLite 里的每个 `session` 当成独立 transcript，读取 `message` / `part`
表中的 user 和 assistant 正文；不会读取 `~/.zcode/v2/credentials.json`、certs、运行日志或浏览器缓存。
`reasoning`、tool 调用和工具输出也不会进入 Obsidian 采集正文。

安装 ZCode 上下文和后台任务:

```bash
.venv/bin/python install_zcode.py
```

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

Claude Code 使用同一套脚本和 Vault。安装器会保留 `~/.claude/settings.json` 的其他字段，只合并 Stop/SessionStart hook，并维护 `~/.claude/CLAUDE.md` 中自己的标记块:

```bash
.venv/bin/python install_claude.py --dry-run
.venv/bin/python install_claude.py
```

Stop 事件只扫描 Claude 当前会话；SessionStart 和 launchd 仍按 `transcript_agents` 检查三端遗漏，避免某个平台的启动事件屏蔽另外两个来源。

### 显式导入历史会话

首次后台安装不会自动回放旧对话。需要导入历史时，先评分，再明确选择文件:

```bash
.venv/bin/python score_sessions.py ~/.codex/sessions --top 20
CODEX_TRANSCRIPT_PATH="/absolute/path/to/selected.jsonl" \
  .venv/bin/python session_harvester.py --mode stop --agent codex
```

这条命令只导入所选会话里的结构化 decision/error/summary；已建立高水位的旧用户消息不会顺带污染自适应偏好。ZCode 可以用 `ZCODE_SESSION_DB` 和 `ZCODE_SESSION_ID` 明确选择一个 session。

---

### Windows 对话同步到 Mac

同步是“对话生产 active-active、正式记忆 single-writer”，不是双向复制
Obsidian Vault：

```text
Windows Codex/Claude JSONL
  -> immutable outbox -> Syncthing 单向 -> Mac authority inbox
  -> 真实 session_harvester -> canonical Vault
  -> sealed generation + receipt -> Syncthing 单向 -> Windows received-published
  -> verified read-only replica
```

事件协议按内容类型独立演进：`transcript.chunk` / `transcript.gap` 的 event 和
ready 保持 schema v1 的既有字节与 ID；`attachment.blob` 的 event 和 ready 使用
schema v2。Producer state 为 v3，Mac authority ledger 为 v4。已有且已经 durable
的 v1 attachment bundle 只作为兼容输入读取，新附件不会再写成 v1。

附件不是扫描目录后全量上传。Producer 只处理 transcript 中明确出现、且真实文件
位于 `attachment_roots` 白名单内的引用；默认单个附件上限为 32 MiB，并且不能
超过 `max_object_bytes` 或 `max_replica_object_bytes`。Mac 按内容签名决定扩展名，
写入：

```text
Attachments/Agent-Memory-Beacon/remote/objects/<sha[:2]>/<sha>.<ext>
04-Feedback/remote-attachments/<device>/<producer>/<seq20>-<event-id>.md
```

ledger v4 同时保存 blob 与 metadata 的路径、SHA-256 和字节数。只有两个文件都以
相同路径、哈希和大小出现在已封存 generation manifest 中，事件才会绑定该
generation 并发布可用于 GC 的 receipt；不能只凭“文件已经写进 Vault”确认成功。

#### Syncthing 的两个单向目录

只配置两个传输目录：

| Syncthing folder | Windows | Mac |
|---|---|---|
| `amb-windows-outbox` | `%USERPROFILE%\AgentMemoryBeaconSync\outbox`，Send Only | `~/AgentMemoryBeaconSync/inbox/windows-main`，Receive Only，绑定 `device_id: windows-main` |
| `amb-mac-published` | `%USERPROFILE%\AgentMemoryBeaconSync\published`，Receive Only | `~/AgentMemoryBeaconSync/published`，Send Only |

`replica_path` 是 Windows 本地派生目录，不直接交给 Syncthing。Mac 的
`vault_path`、两端 `state_dir` 和 `attachment_roots` 也不能加入这两个 folder。
所有非空同步路径必须在环境变量展开后成为绝对路径；state、outbox、published、
received-published、replica、每个 inbox、attachment root 和 canonical Vault
之间不得相等或互相包含。程序也会按 realpath / Windows 大小写规则拒绝通过
符号链接或路径别名隐藏的重叠目录。

Mac `beacon_sync` 示例：

```yaml
beacon_sync:
  enabled: true
  role: authority
  state_dir: "~/.local/share/agent-memory-beacon/sync/authority"
  published_dir: "~/AgentMemoryBeaconSync/published"
  attachment_roots: []
  inboxes:
    - device_id: "windows-main"
      path: "~/AgentMemoryBeaconSync/inbox/windows-main"
```

Windows 示例：

```yaml
codex_sessions_path: "%USERPROFILE%/.codex/sessions"
claude_project_path: "%USERPROFILE%/.claude/projects"
beacon_sync:
  enabled: true
  role: producer-replica
  device_id: "windows-main"
  state_dir: "%LOCALAPPDATA%/AgentMemoryBeacon/sync"
  outbox_dir: "%USERPROFILE%/AgentMemoryBeaconSync/outbox"
  received_published_dir: "%USERPROFILE%/AgentMemoryBeaconSync/published"
  replica_path: "%USERPROFILE%/AgentMemoryBeaconReplica"
  attachment_roots:
    - "%USERPROFILE%/.codex/attachments"
    - "%USERPROFILE%/Downloads"
  max_attachment_bytes: 33554432
```

Windows 副本要求 Windows 10 1809 / Windows Server 2019（build 17763）或更高
版本。物化器依赖该版本开始支持的按句柄只读覆盖语义；安装器和 Doctor 会在旧
系统上直接拒绝，而不是安装后等待后台任务失败。

#### Mac authority：Bash 初始化与稳定运行时

在 Mac 的源码根目录执行。先完成 Syncthing folder 配对并把上述配置写入
`scripts/config.yaml`：

```bash
cd /absolute/path/to/agent-memory-beacon
PY="$PWD/scripts/.venv/bin/python"
CFG="$PWD/scripts/config.yaml"

"$PY" -B scripts/beacon_sync.py --config "$CFG" doctor
"$PY" -B scripts/install_runtime.py --verify-release
"$PY" -B scripts/install_runtime.py --dry-run
"$PY" -B scripts/install_runtime.py

RUNTIME="$HOME/.local/share/agent-memory-beacon/runtime"
"$RUNTIME/.venv/bin/python" -B \
  "$RUNTIME/scripts/beacon_sync.py" \
  --config "$RUNTIME/scripts/config.yaml" run
"$RUNTIME/.venv/bin/python" -B \
  "$RUNTIME/scripts/doctor.py" --profile live
```

Mac 使用 stable runtime 安装器时，如果配置已经启用 authority sync，同步
LaunchAgent 会与 harvest、weekly、hooks 和 Agent context 一起安装、验证和
回滚，不需要先单独安装同步任务。安装后 `io.agent-memory-beacon.sync` 的命令必须
指向 stable runtime，不能指回源码 checkout。

#### Windows producer-replica：PowerShell 初始化、bootstrap 与安装

先在 Windows 源码根目录创建 venv，并从这里运行首次 identity/baseline。命令的
工作目录不可省略，避免相对路径落到错误 checkout：

```powershell
Set-Location C:\absolute\path\to\agent-memory-beacon
py -3.11 -m venv .\scripts\.venv
$Python = (Resolve-Path .\scripts\.venv\Scripts\python.exe).Path
$Config = (Resolve-Path .\scripts\config.yaml).Path
& $Python -m pip install --requirement .\scripts\requirements.lock
& $Python -m pip check

& $Python -B .\scripts\beacon_sync.py --config $Config init
& $Python -B .\scripts\beacon_sync.py --config $Config collect
```

首次普通 `collect` 只建立高水位，不上传旧对话。只有明确需要历史导入时，用下面
这条替代首次普通 `collect`：

```powershell
& $Python -B .\scripts\beacon_sync.py --config $Config collect --include-existing
```

等待 Windows outbox 到达 Mac、Mac authority 至少运行一次、再等待 published
完整到达 Windows。确认 `replica_path` 是预期的空派生目录后，只执行一次显式
bootstrap：

```powershell
& $Python -B .\scripts\beacon_sync.py --config $Config materialize --bootstrap
& $Python -B .\scripts\beacon_sync.py --config $Config doctor
```

然后发布并绑定版本化 stable runtime。每个 release 位于
`%LOCALAPPDATA%\AgentMemoryBeacon\runtime\releases\<release-id>`；Task 与 hook
都使用该 release 内的 `.venv\Scripts\python.exe`、`beacon_sync.py` 和脱敏配置，
不使用系统 Python 或 Git checkout。只启用实际使用的 hook 参数：

```powershell
$RuntimeRoot = "$env:LOCALAPPDATA\AgentMemoryBeacon\runtime"

& $Python -B .\scripts\install_beacon_sync.py --config $Config --runtime-root $RuntimeRoot --codex-hooks --claude-hooks --dry-run

$Installed = (& $Python -B .\scripts\install_beacon_sync.py --config $Config --runtime-root $RuntimeRoot --codex-hooks --claude-hooks | ConvertFrom-Json)

$Binding = $Installed.actions[0].runtime
$RuntimePython = [string]$Binding.python_path
$RuntimeScript = [string]$Binding.script_path
$RuntimeConfig = [string]$Binding.config_path
$RuntimeDoctor = Join-Path (Split-Path $RuntimeScript) "doctor.py"

& $RuntimePython -B $RuntimeScript --config $RuntimeConfig doctor
& $RuntimePython -B $RuntimeDoctor --profile live
```

安装器先 staging、运行 Windows 同步测试、安装 lock 依赖、执行 `pip check`、初始化
producer、运行 quick Doctor 并校验 release manifest；全部通过后才更新当前用户
Task Scheduler 和所选 collector hooks。Task/hook 作为一组事务更新，同名但无
ownership 标记的任务会被拒绝；旧 release 保留，首版不自动清理。

`materialize --bootstrap` 是空副本首次接入已发布 generation 的唯一入口。后台
`run` 永远不会隐式 bootstrap；没有 active generation 时会停止并保留现场，避免
误把错误目录当成新副本接管。首次成功后，Windows Task 才按 collect、逐代
materialize、receipt GC 自动推进。

#### 运行顺序与恢复

Mac authority 的 `run` 严格按 reduce、seal generation 并原子绑定当前 pending
事件、publish receipt 执行；Windows 严格按 collect、materialize、receipt GC
执行，只有副本已经达到 receipt 绑定的 sealed generation 后才允许清理 outbox。
绑定先于 receipt 文件写入，因此崩溃重试不会漂移到新 generation；seal 后才
reduce 的事件保持未绑定，留给下一代。序列缺口、
对象哈希冲突、未知 schema、本地 replica 漂移都会停止推进，不会猜测或
覆盖。检查状态：

```bash
cd /absolute/path/to/agent-memory-beacon
scripts/.venv/bin/python -B scripts/beacon_sync.py \
  --config scripts/config.yaml doctor
scripts/.venv/bin/python -B scripts/doctor.py --profile quick
```

Doctor 把 24 小时在线交付目标与 7 天默认 GC retention 分开检查；received
`current.json` 已到达但副本未物化或仍落后时会直接失败。macOS live profile
检查 launchd 的磁盘 plist 和已加载命令，Windows live profile 检查 Task XML
中的 ownership、用户、触发器、动作及任务状态，不会调用 launchd。任何 receipt
只写入 generation 或 generation ID 一半的损坏绑定也会立即报告，不等待 SLO。

v1 没有 Windows 向 Mac 回报“已应用到第几代”的独立确认协议，因此 authority
不会猜测删除已封存 generation。它只清理不被任何保留 generation 引用且摘要
可验证的孤立对象；Windows 只保留当前 active snapshot，apply/rollback journal
存在时不清理。这个取舍优先保证离线设备仍能逐代追上。

发生故障时不要删除 ledger、伪造 receipt/current/active marker、跳 sequence 或
关闭路径检查。先保留现场，再按状态恢复：

- `missing object` / bundle 未完成：让 Syncthing 重新扫描并补齐原 bundle 或
  generation；ready/current 保留不动，补齐后重跑对应端 `run`。
- `sequence gap`：从 Windows outbox 或备份恢复缺失的较低 seq；较高 seq 继续等待。
- attachment 未绑定 receipt：确认 blob 与 metadata 两个 canonical 路径都存在，
  再在 Mac 重跑 authority `run`；保留 inbox，reducer 可重建未绑定 effect。
- `partial generation binding`：这是 ledger 损坏，不手填字段；停止发布，从完整
  ledger 备份恢复或人工审计后再运行 Doctor。
- `parent mismatch` / replica behind：等待缺失的中间 generation 到达，再运行
  `materialize`；不要重新 bootstrap 一个已有 active marker 的副本。
- `replica drift`：先把本地编辑另存到非 managed 目录，并从已验证的 active
  generation 或备份恢复原字节；物化器不会覆盖第三种状态。
- `bootstrap required`：只在确认是目标空 replica 后运行一次
  `materialize --bootstrap`，不要给后台 `run` 增加该参数。
- Task/hook 漂移：先运行同一安装命令的 `--dry-run`，再幂等重装；不要使用
  Task Scheduler `/F` 覆盖无 ownership 的同名任务。

Windows 只卸载受管 Task/hook，不删除 state、outbox、replica 或 release：

```powershell
Set-Location C:\absolute\path\to\agent-memory-beacon
$Python = (Resolve-Path .\scripts\.venv\Scripts\python.exe).Path
$Config = (Resolve-Path .\scripts\config.yaml).Path
& $Python -B .\scripts\install_beacon_sync.py --config $Config --uninstall --codex-hooks --claude-hooks
```

Mac stable runtime 需要整体回退时，使用安装成功回执中的精确 manifest：

```bash
RUNTIME="$HOME/.local/share/agent-memory-beacon/runtime"
"$RUNTIME/.venv/bin/python" -B \
  "$RUNTIME/scripts/install_runtime.py" \
  --rollback-manifest /absolute/path/from/manifest_path
```

同步不会远程执行 lifecycle 命令，也不会自动安装副本中的 Skill。正式记忆
的 retract、supersede、expire、restore 仍只能在 Mac 上按精确 ID、revision
和显式审批执行。

---

## 记录什么 / What Gets Captured

Agent Memory Beacon 默认收割结构化机器标签，不把完整聊天记录直接塞进项目笔记:

```text
[DECISION:保留 hook 自动收割作为主路径| context:用户目标是自动化记录，不是手动 skill 调用| project:agent-memory-beacon| scope:project]
[ERROR:type=path-filesystem| resolution=修正 Obsidian 对绝对路径 Markdown 链接的误识别| project:agent-memory-beacon]
[FAVOR:保留机器标签英文，内容用中文| context:用户希望自己能直接读懂 Obsidian 记忆内容| type:preference| project:agent-memory-beacon]
[SESSION_SUMMARY]
projects: [agent-memory-beacon]
primary: agent-memory-beacon
summary: "本轮完成 macOS Codex/Claude Code 自动采集与 Obsidian 写入验证。"
[/SESSION_SUMMARY]
```

其中:

- `DECISION` 记录以后还会复用的技术取舍。
- `ERROR` 只记录包含根因、修复动作和验证结果的可复用已解决问题，避免下次重复踩坑。
- `FAVOR` 记录明确、可复用的个人偏好、项目规则或环境事实；会在回复末尾可见，并进入 personal memory 流程。
- `SESSION_SUMMARY` 记录一次对话的收束信息。
- `project` 是可选字段，用来在跨项目对话里明确归档位置。

不要把完成状态、临时步骤、普通观察、问题句、预期失败测试、未修复 finding 或一次性界面误操作写成正式标签。解析器会保留格式正确的标签，质量门再决定它属于正式、待确认还是拒绝路径。

### 历史记忆质量审计

旧版本已经写入的正式记忆不会被新质量门静默删除。可以先运行只读审计：

```bash
.venv/bin/python memory_quality_audit.py
```

需要在 Obsidian 中查看报告并生成精确生命周期提案时运行：

```bash
.venv/bin/python memory_quality_audit.py --write-report --propose
```

只处理某个日期以前的旧正式记忆时，先生成独立审批计划。例如 schema `2.0` 上线前的记录使用排他截止日 `2026-07-13`：

```bash
.venv/bin/python memory_quality_audit.py \
  --old-before 2026-07-13 \
  --propose \
  --json
```

该模式按正式记录自身的 `date` 筛选，跳过无日期或日期无效记录；它会在 `_lifecycle-proposals/` 写入带 Canonical SHA256 的只读计划，并只为这批建议创建 pending 提案。计划使用 `path#key[id=...]` 稳定定位器和单记录规范摘要，因此同文件追加无关记忆不会使审批失效，目标记录自身的字段或来源变化仍会改变哈希。子集模式只淘汰所选 ID 的旧提案，不会把其他质量审计提案标为 stale。计划分别统计通过质量门、低质量、近重复操作和证据不足未生成操作的记录；生成提案仍不代表批准，更不会改变正式 lifecycle 状态。

用户明确批准整份旧记忆计划的 Canonical SHA256 后，先只读预览，再显式应用：

```bash
.venv/bin/python memory_lifecycle_batch.py \
  --plan /path/to/vault/04-Feedback/_lifecycle-proposals/old-memory-lifecycle-plan-before-2026-07-13.md \
  --expected-sha256 <approved-canonical-sha256>

.venv/bin/python memory_lifecycle_batch.py \
  --plan /path/to/vault/04-Feedback/_lifecycle-proposals/old-memory-lifecycle-plan-before-2026-07-13.md \
  --expected-sha256 <approved-canonical-sha256> \
  --apply
```

批量执行器会在共享 writer lock 内复核整个规范载荷、每条记录和替代项的 revision、稳定 locator 与 `canonical-record-v1` digest。它禁止目标与替代项重叠，并按整批结束后的依赖状态验证替代项仍可召回。同一源文件的多项变更会先在内存合并，所有源只发布一次，派生索引和 Agent context 只重建一次；计划、提案、源、派生输出和外部 context 都进入同一个 rollback manifest。成功后精确提案标为 `applied`，同目标的其他 pending 提案标为 `stale`，审批计划和 lifecycle audit 绑定同一个 Canonical SHA256。

旧正式记忆补充语义关系时，候选发现和正式写入必须分开。先人工核验 Source/Target 的语义和证据，再用结构化 JSON 生成只读计划：

```json
[
  {
    "source_id": "project_rule-source-first",
    "relation": "operationalized_as",
    "target_id": "workflow-source-first",
    "reason": "该 Workflow 是项目规则的明确执行形式",
    "evidence_refs": [
      "memory:project_rule-source-first",
      "memory:workflow-source-first"
    ]
  }
]
```

```bash
.venv/bin/python memory_relation_batch.py plan \
  --proposals-json /path/to/relation-proposals.json \
  --output /path/to/vault/04-Feedback/_relation-proposals/semantic-plan.md

.venv/bin/python memory_relation_batch.py preview \
  --plan /path/to/vault/04-Feedback/_relation-proposals/semantic-plan.md \
  --expected-sha256 <approved-canonical-sha256>

.venv/bin/python memory_relation_batch.py apply \
  --plan /path/to/vault/04-Feedback/_relation-proposals/semantic-plan.md \
  --expected-sha256 <approved-canonical-sha256> \
  --apply
```

计划冻结两端的 ID、revision、类型、项目、稳定 locator、`canonical-record-v1` digest、证据摘录和理由。计划正文、任一端记录或批准哈希发生漂移都会在写前拒绝；成功时同一源文件的关系合并写入并只重建一次派生索引，失败时恢复正式源、计划、recall index、memory graph、图质量报告和其他生成索引。文本相似度只能用于发现候选，不能授权正式关系。

报告会把积压明确分为三类：`evidence_insufficient` 表示质量不足但没有足够证据建议改变状态；`blocked_lifecycle_actions` 表示已经形成保守建议、但被 active alias owner 等生命周期约束阻断；`executable_recommendations` 才是可以生成 pending 提案的精确操作。报告同时列出近重复组、正式 ID 身份冲突、精确 ID 和当前 revision。身份冲突只进入报告，并同步生成 `04-Feedback/memory-quality-conflicts.md` 逐来源复核计划，不会生成普通生命周期动作；提案批次会在写入前完成完整身份和 revision 预检。提案保存在 `04-Feedback/_lifecycle-proposals/`，不会进入召回，也不会自动执行 `retract` 或 `supersede`。运行时索引会先抑制高置信噪声并折叠可信近重复，但正式源记录保持可审计状态，直到用户明确批准具体生命周期操作。

冲突复核计划会区分“同一事实的重复副本”“不同事实误用同一 ID”和“低质量占位内容”，为每组列出建议保留的 `Source + Revision + Source Digest`、其余来源的确定性新 ID、目标项目、预期状态和证据保留方式；同文件同 revision 的记录还会给出精确 `Source Locator`。同一事实只在同项目或“路由宿主 + 唯一业务项目”之间归并；不同业务项目默认保留为独立事实。所有建议均为 `pending` 只读方案，仍需用户按精确 ID、来源、revision 和源文件 digest 明确批准后才能执行。

用户批准完整计划哈希后，先运行只读预览；只有显式增加 `--apply` 才会写入：

```bash
.venv/bin/python memory_identity_repair.py \
  --plan /path/to/vault/04-Feedback/memory-quality-conflicts.md \
  --expected-sha256 <approved-plan-sha256>

.venv/bin/python memory_identity_repair.py \
  --plan /path/to/vault/04-Feedback/memory-quality-conflicts.md \
  --expected-sha256 <approved-plan-sha256> \
  --apply
```

执行器会在共享 `harvester.lock` 内重新核对计划哈希、全部冲突 ID、Owner、Revision、Source Digest 和 Source Locator。任一字段漂移都会在写入前停止；成功批次会保留批准计划副本、所有源文件与派生输出的 rollback manifest，最后只重建一次索引和 Agent context，并写入 lifecycle audit。

### 与 Codex Memory 的可复现比较

先只读检查本机 Codex Memory 是否具备公平比较条件：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/evaluate_memory_comparison.py \
  --probe-only
```

probe 只运行 `codex --version`、`codex features list`，并用 SQLite read-only URI 读取 `jobs` 和 `stage1_outputs` 数量；不会开启实验 feature、训练 Memory、修改账号或写数据库。feature 关闭、存储为空、版本未知或 schema 不可读时，比较基线不可用。

当同一版本的 Codex Memory 已启用且有真实历史学习结果后，分别用隔离的 Beacon arm 和 Codex Memory arm 运行同一黑盒 fixture，生成 schema `1.0` JSON 报告，再执行：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/evaluate_memory_comparison.py \
  --beacon-report /absolute/path/beacon-arm.json \
  --codex-report /absolute/path/codex-memory-arm.json
```

每个 arm 必须绑定 `codex_version`、`fixture_id`、`fixture_sha256`、`evidence_status: valid`、非空 `evidence_refs`，并且只提供七个固定指标：`precision_at_k`、`critical_error_recall`、`irrelevant_trigger_rate`、`contamination_count`、`long_task_freshness_rate`、`recall_p95_ms` 和 `max_estimated_tokens`。两个 arm 的版本或 fixture 不一致、证据无效、Codex Memory 不可用或缺少任一 arm 时，结果都是 `N/A`；Beacon 单臂分数可以用于自身回归，但 `claim_allowed` 必须为 `false`，不会产生虚假的胜负结论。

统一评分为相关准确率 30 分、关键错误召回 25 分、无关触发与污染 20 分、长任务新鲜度 15 分、延迟与上下文成本 10 分。只有 Beacon 至少 `85/100` 且领先同版本 Codex Memory 至少 `15` 分，才允许使用“效果超过 Codex Memory”的行为结论。Obsidian 所有权、candidate 隔离、来源审计、撤回/替代和跨 Agent 可移植性单独列为 `non_scored_beacon_capabilities`，不参与这 100 分。

`evaluate_memory_runtime.py` 仍是 Beacon 自身的确定性回归评测，不是公平 A/B 的替代品。

### Personal Memory Candidates

当用户说出偏好、长期规则或自动化需求时，程序会额外做一次轻量判断。比如:

```text
我的想法是把不确定的内容先放到待确认文件夹。
如果以后重复出现类似内容，再把它加到正式记录里。
```

如果模型在回复末尾显式写出:

```text
[FAVOR:默认用中文解释复杂功能| context:用户看不懂英文输出| type:preference| project:agent-memory-beacon]
```

harvester 会把它当作高置信个人记忆处理，并在 hook 输出中显示 `[PROMOTED]` / `[UPDATED]` 等结果。
不要为一次性任务、普通提问、清单字段、密钥或用户明确不想记录的内容写 `[FAVOR]`。
平台注入上下文和子代理内部对话不会进入这套判断。

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

### Adaptive Skill Preference Learner

当你在对话里手动调用 skill，例如:

```text
这段中文太像 AI 了，用 $humanizer 改自然一点。
这个 bug 反复出现，调用 $superpowers:systematic-debugging 排查。
[@pensive](plugin://pensive@claude-night-market) 检查一下刚才新增的功能。
这条偏好要额外记录，用 $manual-memory-capture。
```

harvester 会学习“什么具体场景下你会主动想到这个 skill”。它不会只存关键词，而是生成场景画像:

- `task_intent`: 用户真正想完成什么
- `artifact_type`: 作用对象，比如中文段落、代码 bug、测试失败、Obsidian 记忆
- `pain_point`: 为什么普通处理不够
- `why_skill_fits`: 为什么这个 skill 适合
- `positive_signals`: 以后看到哪些信号应考虑这个 skill
- `negative_signals`: 哪些相似场景不应该调用，避免误触发

第一次出现会进入候选:

```text
04-Feedback/_skill-preferences/
```

不同 session 中重复出现相似场景后，会晋升为正式规则:

```text
05-Agent-Memory/skill-routing-rules.md
```

运行时会直接打印:

```text
[skill-learner] CANDIDATE humanizer 技能偏好: humanizer - 中文表达自然化 (confidence=0.58, seen=1)
[skill-learner]     -> 04-Feedback/_skill-preferences/技能偏好 humanizer - 中文表达自然化.md
```

晋升后，规则会进入 `00-Inbox/Agent Memory Index.md` 和
`05-Agent-Memory/recall-index.json`，新 Codex 对话可以通过 Obsidian 记忆和 AGENTS/compiled memory
间接提高主动考虑 `humanizer`、`superpowers`、`pensive` 等 skill 的概率。

注意: 这个程序不会直接修改 Codex 内部模型概率，因为没有公开接口；它是通过可读规则和可召回记忆来影响未来对话。

相关阈值可以在 `scripts/config.yaml` 的 `skill_preferences` 里调整:

```yaml
skill_preferences:
  enabled: true
  candidate_dir: "04-Feedback/_skill-preferences"
  formal_path: "05-Agent-Memory/skill-routing-rules.md"
  promote_seen_count: 2
  similarity_threshold: 0.5
  initial_confidence: 0.58
  repeat_increment: 0.18
```

### Adaptive Workflow Memory

Skill preference 解决“这个场景该更常考虑哪个 skill”。Workflow memory 解决更高一层的问题:
“这个场景下 Codex 应该主动采用什么工作流程”。

典型会学习两类反复纠正:

- GitHub 源码优先: 当用户提供 GitHub skill、插件、仓库、项目截图或名称，并要求解释、评估或借鉴时，先打开 upstream GitHub，阅读 README、目录结构、关键源码或 manifest，再给结论。不要只根据名称猜。
- pensive 审查后修复: 当用户让 pensive 或代码审查流程检查本地项目，且发现的是可验证、可测试的代码问题时，先报告关键问题，再继续修复并运行测试。

第一次出现会进入候选:

```text
04-Feedback/_workflow-candidates/
```

不同 session 中重复出现相似纠正后，会晋升为正式流程规则:

```text
05-Agent-Memory/workflow-rules.md
```

每条 workflow candidate 会保存:

- `rule_name`: 规则名
- `trigger_scene`: 什么场景触发
- `user_correction`: 用户纠正了什么
- `desired_behavior`: 以后应主动怎么做
- `why_it_matters`: 为什么符合用户目标
- `positive_signals`: 看到哪些信号应考虑这条流程
- `negative_signals`: 哪些场景不该自动执行
- `evidence_excerpt`: 短证据，不保存完整 transcript
- `seen_count` / `confidence` / `source_session` / `project` / `last_seen`

运行时会直接打印:

```text
[workflow-learner] CANDIDATE github_source_first 流程记忆: GitHub 项目先查源码 (confidence=0.58, seen=1)
[workflow-learner]     -> 04-Feedback/_workflow-candidates/流程记忆 GitHub 项目先查源码.md
```

晋升后，规则会进入 `00-Inbox/Agent Memory Index.md`、
`05-Agent-Memory/recall-index.json` 和 `05-Agent-Memory/memory-graph.json`。
新对话可以通过 Obsidian 记忆和 compiled memory 更容易读到这些流程规则。

安全边界:

- 当前用户本轮明确指令永远优先于 workflow memory。
- 用户说“先别操作”“只讨论”“只审查”“不要改”时，不自动修复。
- 用户明确说不要联网、只根据本地文件分析时，不自动查 GitHub。
- 涉及 destructive 操作、上传 GitHub、安装、账号、付款、凭据或隐私数据时仍需谨慎或确认。
- 不保存密码、token、API key、OAuth、付款信息或完整 transcript。

相关阈值可以在 `scripts/config.yaml` 的 `workflow_memory` 里调整:

```yaml
workflow_memory:
  enabled: true
  candidate_dir: "04-Feedback/_workflow-candidates"
  formal_path: "05-Agent-Memory/workflow-rules.md"
  promote_seen_count: 2
  similarity_threshold: 0.5
  initial_confidence: 0.58
  repeat_increment: 0.18
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
05-Agent-Memory/memory-graph-quality.md
05-Agent-Memory/recall-context.md
```

`keyword-index` 是给 Agent 用的机器检索入口；`global-atoms` 借鉴上游 v4，只在同一个 resolved pitfall
出现在两个以上项目时才生成，避免把单项目经验过早升级成全局规则。
`recall-index` / `memory-graph` 借鉴 Cognee 的“结构化记忆 + 关系图 + 查询召回”思路；Graph v3 借鉴 graph-engineering 的 schema-first、关系约束、provenance 和保守融合；运行时排序借鉴 Hindsight 的多路检索与 RRF 融合。实现仍使用确定性的词项、结构化名称、类型、时间和显式图关系通道，正式事实只来自本地 Markdown，图和混合检索都是可重建的派生视图。

如果 `memory_runtime.index_path` 改到自定义目录，`memory-graph.json` 会自动写到该 index 的同一目录；质量报告和人类可读的 `recall-context.md` 仍位于 `05-Agent-Memory/`。品牌迁移会把这两个自定义路径纳入同一冻结、回滚和重建合同，不会退回默认目录。quick/CI Doctor 可以识别端点和关系均自洽的旧 Graph v2、通过完整校验但尚无 `generation_id` 的旧 Graph v3，以及只缺 note/experience source revision 的上一版 Graph v3，以便安装流程先完成派生索引重建；兼容入口会用当前严格校验验证除该已知缺项外的全部合同。Codex 运行时和 live Doctor 只接受代次匹配的严格 Graph v3，非对象图和旧图都会 fail closed。

Obsidian 笔记的 `links_to` 关系只保留给知识图谱可视化和派生质量检查，不参与正式记忆扩展。运行时必须先有内容锚点，随后最多沿正式 unit 明示的五类语义关系走两跳；关联笔记、聚合 `decisions.md` / `pitfalls.md`、项目节点和共享 session 都不能把整篇内容带入召回。

可以用一句话查询相关记忆:

```bash
.venv/bin/python memory_recall.py "Obsidian 中文 主存储" --project agent-memory-beacon
```

普通输出会显示 `channels`、`why_recalled` 和 `authority`；使用 `--json` 还可以查看每个通道的 rank、原始分数、时间模式、图关系或经验包定位。运行时注入也保留精简的 `why_recalled` 与权威执行/来源路径。

---

## 实际效果 / What You'll See

### Obsidian 里会看到

```markdown
01-Projects/agent-memory-beacon/Memory/
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

Session: test-session | Date: 2026-06-22 | Project: agent-memory-beacon

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
Agent Memory Beacon: Codex/Claude Code 对话应该自动沉淀，且不能污染 vault。
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
agent-memory-beacon/
├── SKILL.md                         ← Agent Memory Beacon 技能定义
├── README.md                        ← 本文件 / This file
│
├── scripts/                         ← 采集、学习、索引、安装和维护脚本
│   ├── setup.py                     ← 一键初始化
│   ├── session_harvester.py         ← Codex/Claude/ZCode 增量收割器
│   ├── runner.py                    ← 管道编排 (5 步)
│   ├── backup.py                    ← transcript 指纹 + 可选脱敏归档
│   ├── analyzer.py                  ← 根因分析
│   ├── maintainer.py                ← 智能卡片 + 合并检测
│   ├── reporter.py                  ← 周报 + 学习叙事
│   ├── compiler.py                  ← 三端上下文 + Agent Memory
│   ├── install_codex.py / install_claude.py / install_zcode.py
│   ├── install_launchd.py           ← macOS 后台任务
│   ├── install_runtime.py           ← allowlist 发布 + Hook/launchd 事务切换与回滚
│   ├── doctor.py                    ← quick / ci / live 统一只读健康检查
│   ├── config.py / config.example.yaml
│   └── requirements.lock            ← 经审计的稳定运行时依赖
│
├── references/                      ← 深入文档
│   ├── architecture.md              ← 9 个设计决策 + Anti-Patterns
│   └── workflow.md                  ← 四层工作流 + Hook 配置 + 管道
│
├── templates/vault/                 ← Vault 模板
└── patches/AGENT_MEMORY_BEACON.md.patch ← Codex/Claude/ZCode 标注规则
```

---

## 常见问题 / FAQ

**Q: 离网能用吗？/ Works offline?**
A: 完全离网工作。启发式根因分析不需要网络。LLM 深度分析是可选的增强。

**Q: 必须装 Obsidian？**
A: 不必须。Vault 是纯 Markdown 文件夹，任何编辑器都能看。Obsidian 提供更好的可视化。

**Q: 漏扫了怎么办？**
A: runner.py 启动时自动检测上次扫描时间。距上次成功扫描达到 8 个完整日时自动切换全量模式补跑。

**Q: 会把旧聊天或完整原文复制进 Vault 吗？**
A: 默认不会。首次安装只记高水位；`privacy.store_raw_transcripts` 默认是 `false`，`_raw-sessions` 最多保存脱敏元数据。历史导入必须显式选择。

**Q: 为什么日志、raw session 和 profile 不在知识图谱里？**
A: `04-Feedback/_raw-sessions`、`_logs`、`_rollback`、`_cleanup-backups`、`05-Agent-Memory/codex-profile` 和误生成的 `Users/` 都是内部目录，会从 Obsidian 图谱、召回索引和链接校验中排除。

**Q: 会改坏文件吗？**
A: 所有写操作是原子写入 (.tmp → os.replace)。崩溃不损坏原文件。有 `--dry-run` 预览模式。

---

## 许可证 / License

[MIT License](LICENSE) - 可使用、修改和分发，但须保留许可证与版权声明。
