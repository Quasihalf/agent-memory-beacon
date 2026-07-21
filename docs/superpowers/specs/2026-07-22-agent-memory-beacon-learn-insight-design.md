# Agent Memory Beacon Learn / Insight 设计

> 日期：2026-07-22
> 状态：对话设计已确认，等待书面规范审阅
> 第一阶段运行端：Codex on macOS
> 权威存储：Obsidian Markdown

## 1. 摘要

Agent Memory Beacon 新增 `LEARN` 能力，用于从用户提出的新项目、新观点、机制、类比和解题视角中提炼可迁移的启发。它不替代现有 Decision、Error、Favor、Skill 或 Workflow，而是补充当前系统缺少的“生成性记忆”：未来遇到相似但不完全相同的问题时，Codex 可以从中寻找思路、建立类比和拓展方案空间。

本设计采用“新语义类型、复用现有底座、受限动态召回”方案：

- 对话中的机器采集标签为 `[LEARN:...]`。
- 正式运行时记忆类型为 `insight`。
- 首次出现且价值明确的启发立即成为正式 `seed`，不要求重复。
- 重复、实践成功或正式证据支持只负责将 `seed` 强化为 `reinforced`。
- 内容不完整、来源不能验证或疑似普通聊天时才进入候选区。
- 候选仍然绝对禁止进入运行时召回。
- Insight 只在探索、设计、类比、替代方案或卡住换路时受限召回，不覆盖事实、用户指令和正式行为规则。

运行时不增加外部 LLM 调用，不引入数据库、向量服务、Docker 或常驻进程。Codex 使用当前对话中已有的推理能力生成结构化 `[LEARN]`；收割、校验、去重、索引和召回保持确定性。

## 2. 问题定义

当前正式记忆主要回答以下问题：

| 类型 | 回答的问题 |
|---|---|
| Decision | 最终选择了什么，为什么 |
| Error | 什么失败过，如何修复 |
| Favor | 用户长期偏好、项目规则或环境事实是什么 |
| Skill | 什么场景考虑调用哪个 Skill |
| Workflow | 某类场景中 Agent 应按什么流程行动 |

这些记忆擅长保持一致性和避免重复错误，但不能完整表达：

- 一个新观点背后的可迁移原理。
- 某个项目机制对其他问题的启发。
- 尚未成为强制规则、但值得在未来探索时重新考虑的类比。
- 一次出现、以后可能不再重复的高价值灵感。
- 一个思路在哪些边界下会失效。

把这些内容塞进 Decision 会把启发误当成已选方案；塞进 Workflow 会把可选思路误当成必须行为；塞进 Favor 会污染个人偏好。因此需要独立的 `insight` 类型。

## 3. 目标和非目标

### 3.1 目标

1. Codex 能自动识别用户提出的高价值、非显然、可迁移思路。
2. 一次性但价值明确的启发可以立即保存并在未来受限召回。
3. 新颖性、启发价值和可信度分开建模，重复次数不决定是否值得记住。
4. 每条 Insight 保留来源、适用场景、迁移方向和失效边界。
5. Insight 与来源 session、项目、Decision 和 Workflow 建立可审计关系。
6. 未来召回能拓展方案空间，但不能改变正式事实和行为约束的优先级。
7. 用户能从回复末尾、收割日志、Obsidian 文件和召回上下文确认功能是否生效。

### 3.2 非目标

- 不自动学习 Codex 未经验证的自行猜想。
- 不把新闻、普通事实、任务进度、结论复述或临时待办当作 Insight。
- 不要求同一观点重复出现后才保存。
- 不让候选 Insight 进入召回。
- 不自动把 Insight 转成 Decision、Workflow 或用户偏好。
- 不批量把旧 Decision、Error 或 session 改写成 Insight。
- 第一阶段不为 Claude Code 增加动态 Insight 召回，也不扩展 ZCode。
- 不把 Insight 描述为事实、证明或确定结论。

## 4. 核心领域模型

### 4.1 正式类型和成熟度

`type` 使用现有运行时类型体系中的新值 `insight`。正式记录继续使用生命周期 `status: active|superseded|retracted|expired`；启发成熟度使用独立字段，不能挤占正式生命周期状态：

| maturity | 含义 | 是否召回 |
|---|---|---|
| `seed` | 首次出现、价值明确、来源可验证，但尚未获得独立强化证据 | 是，受限且标明启发性假设 |
| `reinforced` | 被不同 session 再次支持，或获得可验证实践证据 | 是，排序优先级略高 |

候选记录使用 `status: candidate` 和 `type: insight-candidate`，不具有 `maturity` 的运行时语义，也不进入正式索引。

### 4.2 新颖性和可信度分离

每条记录至少区分：

- `novelty_score`：相对现有正式记忆是否提供了新机制、新视角或新组合。
- `transfer_score`：是否能迁移到当前具体任务以外。
- `utility_score`：未来能否帮助产生方案、诊断方向或设计权衡。
- `confidence`：来源、验证和重复证据的可信程度。

正式准入主要看前三项和来源完整性。`confidence` 影响显示和排序，但首次只有一个来源不会阻止高价值 Insight 成为 `seed`。

### 4.3 来源类型

第一阶段只允许：

- `user`：思路明确来自当前或前序用户消息。
- `jointly_validated`：思路来自共同推导，并在同一任务中有可验证的成功证据或正式 Decision/Error/Workflow 证据。

纯 assistant speculation 不得直接成为正式 Insight。无法确定来源时进入候选或拒绝。

### 4.4 与现有记忆的分类优先级

一段内容先判断其主要用途：

1. 已解决失败及复用解法：Error。
2. 对未来行为具有约束力：Decision、Favor、Skill 或 Workflow。
3. 提供可选、可迁移、生成性思路：Insight。

同一事件可以同时形成不同层次的记录，但不得复制同一句话：Decision 记录本项目的已选方案，Insight 提炼跨任务原理和边界。两者通过关系字段连接。

## 5. 采集协议

### 5.1 `[LEARN]` 格式

推荐单行格式：

```text
[LEARN:<可迁移原理>| novelty:<它与已有做法的关键不同>| transfer:<可迁移到哪些场景>| boundary:<何时不适用>| evidence:<用户原话中的短证据>| source:<user|jointly_validated>| project:<project-slug>| scope:<project|global>]
```

机器标签和字段名保持英文，正文可使用中文。每条标签只表达一个原子 Insight。

最小正式准入字段：

- Insight 正文。
- `transfer`。
- `boundary`。
- `source`。
- 可验证的 `evidence` 或正式证据引用。
- 项目和 scope 绑定。

`novelty` 缺失时可以进入候选，但不能仅凭“很新颖”“值得学习”等空泛措辞成为正式记录。

### 5.2 Codex 何时自动输出

同时满足以下条件时，Codex 应自动输出 `[LEARN]`：

- 用户提供了 Codex 当前方案中未包含的机制、约束关系、类比或视角。
- 内容不只是具体操作指令，而能抽象为其他任务可复用的原理。
- 能说明至少一个迁移方向。
- 能说明至少一个失效边界或适用条件。
- 来源可以在本轮可见用户消息或正式验证证据中定位。

以下情况不输出：

- 普通事实、链接、项目名称或功能列表。
- “继续”“可以”“改一下”等操作消息。
- 仅表达喜欢或不喜欢。
- 已完整存在于正式 Insight 的同义复述。
- Codex 自己临时提出、没有用户来源或实践验证的猜想。
- 密钥、凭据、支付、认证信息或其他敏感内容。

### 5.3 来源验证

`source:user` 的 `evidence` 必须是先前用户消息中可验证的短片段。收割器使用去标记、归一化后的精确包含检查，不接受只存在于 assistant 回复中的证据。

收割器只解析 assistant 在受管理协议下输出的 `[LEARN]`。用户消息中直接出现的标签只作为普通对话内容，不能绕过 assistant 判断和质量门；当用户明确要求记录一个启发时，assistant 仍需根据可见原话生成来源可验证的标签。

`source:jointly_validated` 必须带稳定证据引用，例如同一 session 中已通过质量门的 Decision、Error 或验证事件。第一阶段若无法确定性验证，降级为候选，不进行猜测性准入。

这项来源验证防止 assistant 错误地把自己的构想归因给用户。

## 6. 质量门和候选规则

质量判断分成两层：当前 Codex 在生成 `[LEARN]` 时负责判断语义新颖性、迁移价值和边界；确定性收割器负责验证字段、来源、隐私、类型冲突和与现有记录的保守重复。收割器不声称能够单靠关键词证明一个观点在世界知识中绝对新颖，因此首次正式记录仍明确标为 `seed`，而不是已验证事实。

### 6.1 正式 `seed` 准入

高质量首次记录可以直接成为正式 `seed`。准入条件包括：

- 字段结构完整且单行长度受限。
- 来源证据通过确定性验证。
- Insight 正文与 transfer、boundary 不是重复句。
- 不是问题、进度、选择结果、偏好或流程命令。
- 相对现有正式 Insight 不构成高相似重复。
- 不包含敏感信息或不安全路径。

### 6.2 候选

以下情况进入 `04-Feedback/_insight-candidates/`：

- 思路可能有价值，但缺少 transfer 或 boundary。
- 来源意图明确，但 evidence 无法验证。
- 与现有 Insight 高度相似但不能确定是重复还是扩展。
- 内容过于抽象，尚不足以稳定复用。

候选的存在不代表价值低，也不要求通过重复才能转正。后续任一完整、来源可验证的 `[LEARN]` 可以补齐候选并建立正式 `seed`。

### 6.3 拒绝

已知非记忆噪声、敏感内容、伪造来源、普通进度和空泛自评直接拒绝，不写候选。

## 7. 强化、冲突和生命周期

### 7.1 强化

以下任一证据可以将 `seed` 强化为 `reinforced`：

- 不同正式 session 独立出现语义相近且来源可验证的 Insight。
- 实际任务成功使用该思路，并存在稳定 Decision、Error 或 Workflow 引用。
- 用户明确确认该 Insight 在另一个场景中有效，且来源可验证。

强化只增加证据、成熟度和轻量排序权重。它不能改变原 Insight 的核心含义。

`reinforced` 是根据追加证据派生的认识成熟度，不是正式生命周期状态迁移。自动强化只允许追加稳定 source refs、验证关系和派生成熟度；不得改写 insight、transfer、boundary 或 scope。任何核心字段变化必须创建新记录或生命周期 proposal，不能借强化绕过正式记忆治理。

### 7.2 冲突

新证据若改变核心含义、适用边界或否定旧 Insight，不自动覆盖正式记录。系统生成生命周期 proposal，包含旧 ID、revision、新建议、来源和原因，等待现有正式生命周期流程处理。

### 7.3 保留

一次性 `seed` 不因未重复而自动过期或降级。只有显式生命周期操作或已经预设并到期的 `expires_at` 可以改变正式状态。

## 8. 存储契约

### 8.1 路径

- 候选：`04-Feedback/_insight-candidates/`
- 正式：`05-Agent-Memory/insights.md`
- 质量报告：复用 `04-Feedback/memory-quality-report.md`
- 运行时索引：复用 `05-Agent-Memory/recall-index.json`
- 机器图谱：复用 `05-Agent-Memory/memory-graph.json`

### 8.2 正式记录字段

每条正式 Insight 至少包含：

```yaml
id: insight-<stable-digest>
revision: <sha256>
type: insight
status: active
maturity: seed
scope: project
project: agent-memory-beacon
title: 多路弱能力通过融合形成稳定系统
summary: 多个互补但能力有限的通道可通过排名融合提高稳定性和可解释性
novelty: 不依赖单一语义检索或单一评分器
transfer:
  - 记忆召回
  - Skill 路由
  - 审查证据聚合
boundary: 各通道高度相关或共享相同偏置时收益有限
origin: user
confidence: 0.72
source_refs:
  - session:<stable-session-id>
```

`maturity`、`novelty`、`transfer`、`boundary`、`origin` 和关系字段必须进入 revision 计算，避免内容变化但 revision 不变。

### 8.3 标识和去重

初始 ID 绑定 scope、project 和首个规范化原理。后续同义来源通过保守相似度合并到原 ID；语义存在实质差异时创建新 ID 并建立 `related_to`，不得激进去重。

## 9. 索引和图谱

`memory_schema.py` 增加 `insight` 运行时类型和正式路径允许规则。`knowledge_index.py` 解析正式 Insight，候选路径继续在收集、构建和运行时三层排除。

图谱增加以下关系：

- `derived_from`：Insight 来自哪个 session 或来源记录。
- `reinforced_by`：哪些独立证据强化了 Insight。
- `applies_to`：Insight 可迁移到哪些项目、主题或问题。
- `supports`：Insight 支持哪个 Decision。
- `operationalized_as`：Insight 被落实为哪个 Workflow。
- `related_to`：概念相关但无更强方向关系。

Obsidian 正式笔记使用 wikilink 保证可见图谱也能显示来源和关联；JSON 图谱保存稳定 ID 关系供运行时使用。

## 10. 受限召回

### 10.1 触发场景

Insight 只在以下意图中自动参与：

- 设计系统、功能或架构。
- 寻找新方案、替代方案或类比。
- 当前路径卡住，需要换思路。
- 比较项目并提炼可借鉴机制。
- 用户明确要求拓展思路、创新、启发或“有没有别的方法”。

普通执行、简单事实查询、明确单步操作和无探索需求的高确定性任务不自动注入 Insight。

### 10.2 检索和排序

`memory_recall.py` 增加 `insight` 类型意图，并复用 lexical、structured、type、temporal、graph 五路检索与 RRF 融合。自动召回还要求具体内容锚点，不能只凭“想法”“思路”等弱通用词召回整个 Insight 清单。

排序优先级依次考虑：

1. 当前问题的内容相关性。
2. transfer 场景匹配。
3. boundary 是否与当前约束兼容。
4. 来源和证据完整性。
5. maturity 的小幅权重。

`reinforced` 只能获得小幅加权，不能让重复次数压过更相关的一次性 `seed`。

### 10.3 预算和呈现

- 每次自动召回最多 2 条 Insight。
- Insight 合计预算默认不超过 400 tokens，并受总 memory runtime 预算约束。
- 同一 session 对同一 Insight 使用现有重复抑制。
- 至多一个低置信 `seed`；没有高相关结果时保持静默。

运行时格式：

```text
[INSIGHT]
idea: 多个互补的弱通道可以通过排名融合提高系统稳定性
maturity: seed
why_relevant: 当前任务也在组合多个不完全可靠的判断器
boundary: 多个通道共享同一偏置时收益有限
source: [[05-Agent-Memory/insights#insight-...]]
[/INSIGHT]
```

提示词必须明确：Insight 是启发，不是事实或指令；用户指令、正式 Workflow、Decision 和已验证 Error 的优先级更高。

## 11. 编译上下文

`compiler.py` 将 `[LEARN]` 采集协议、分类边界和 Insight 优先级规则安装进受管理 Agent context。为避免每个任务固定消耗 token，不把全部 Insight 正文静态编译进 `AGENTS.md`；Insight 内容由动态召回按需注入。

编译后的项目摘要可以显示正式 Insight 数量和最近强化日期，但不能用数量代替内容召回。

## 12. 可见反馈

用户可以从四个位置确认功能：

1. Codex 回复末尾的 `[LEARN:...]`。
2. 收割日志中的 `[insight-learner] SEED|CANDIDATE|REINFORCED`。
3. Obsidian 的 `insights.md` 或 `_insight-candidates/`。
4. 未来任务中的 `[INSIGHT]` 动态召回块。

收割输出必须显示标题、maturity、confidence、来源数和目标路径，不打印敏感 evidence 全文。

## 13. 配置

默认配置：

```yaml
insight_memory:
  enabled: true
  candidate_dir: "04-Feedback/_insight-candidates"
  formal_path: "05-Agent-Memory/insights.md"
  similarity_threshold: 0.58
  direct_seed_threshold: 0.72
  reinforce_source_count: 2
  max_auto_recall: 2
  recall_token_budget: 400
```

`enabled: false` 立即停止新 Insight 写入和自动召回，但不删除已有正式记录或证据。

## 14. 安全和隐私

- 复用现有脱敏、safe path、原子写入、锁和 revision 校验。
- evidence 长度受限，只保存用户明确表达思路所需的最短片段。
- 不保存 reasoning、tool raw output、凭据、token 或认证信息。
- assistant 不能仅靠声明 `source:user` 绕过来源验证。
- candidate 在路径、类型和状态三层继续 fail closed。
- 正式 Insight 的修改遵守现有不可物理删除和生命周期审计要求。

## 15. 模块改动

### 新模块

- `scripts/insight_memory.py`：解析 `[LEARN]`、来源验证、质量评分、候选、正式 seed、去重和强化。

### 扩展模块

- `scripts/memory_schema.py`：正式类型、字段、revision、路径允许规则和解析。
- `scripts/session_harvester.py`：提取、处理、可见日志和事务变更判断。
- `scripts/knowledge_index.py`：正式 Insight 解析、索引、图谱节点和关系。
- `scripts/memory_recall.py`：Insight 意图、结构化字段、成熟度加权和自动数量限制。
- `scripts/memory_runtime.py`：探索意图门控、Insight token 子预算和呈现。
- `scripts/compiler.py`：Agent context 中的 `[LEARN]` 协议和类型优先级。
- `scripts/config.py`、`scripts/config.example.yaml`：默认配置和校验。
- `scripts/doctor.py`、`scripts/install_runtime.py`：正式路径、schema 和稳定 runtime 验证。
- README、architecture、workflow、CHANGELOG 和 Vault 模板文档。

## 16. 测试策略

### 16.1 单元测试

- 完整 `[LEARN]` 解析和字段转义。
- 用户 evidence 真实存在时可成为首次正式 `seed`。
- 一次性高价值 Insight 不因来源数为 1 而进入候选。
- 缺 transfer、boundary、来源或证据时进入候选或拒绝。
- assistant 自行猜想不能伪造 `source:user`。
- Decision、Favor、Workflow 和普通任务进度不被误分类为 Insight。
- 同 session 重放幂等，不增加来源数。
- 不同 session 相似记录强化为 `reinforced`。
- 语义变化不自动覆盖旧 Insight，而是产生 proposal 或独立记录。

### 16.2 索引和召回测试

- 正式 seed 和 reinforced 均可进入索引。
- candidate、retracted、superseded 和 expired 永不进入运行时。
- “有哪些启发”可以执行显式清单查询。
- 具体探索问题召回不超过 2 条相关 Insight。
- “给我一个想法”这类无内容锚点请求不会展开整个库。
- reinforced 只小幅加权，相关 seed 可以排在不相关 reinforced 之前。
- Insight 不覆盖 Workflow、Decision 或 Error 的渲染优先级。
- 图谱关系可从正式记录稳定重建。

### 16.3 端到端测试

1. 构造 Codex transcript，用户只提出一次完整新思路。
2. assistant 输出来源可验证的 `[LEARN]`。
3. Stop/SessionStart 收割生成正式 `seed`。
4. 知识索引和 Obsidian 图谱包含 Insight 及来源。
5. 新 Codex 任务提出高度相关的探索问题。
6. UserPromptSubmit 注入一个 `[INSIGHT]`。
7. 普通执行问题不注入该 Insight。
8. 第二个独立 session 提供支持证据后，记录变为 `reinforced`。

### 16.4 验收门槛

- 一次性高质量 Insight 保存率：固定正例集 `100%`。
- candidate 运行时泄漏：`0`。
- assistant 伪造用户来源准入：`0`。
- 普通进度/偏好/流程误记为 Insight：固定负例集 `0`。
- 自动 Insight 每轮：`0-2` 条。
- Insight 子预算：不超过 `400 tokens`。
- 固定评测上的 recall p95：不高于 `25 ms`。
- 现有 Decision、Error、Favor、Skill、Workflow 测试无回归。
- `doctor.py --profile ci`、release verification 和 live doctor 全部通过。

## 17. 发布顺序

1. 先扩展 schema、解析器和质量门，使用 TDD 固定一次性 seed 准入。
2. 接入 harvester 和正式 Markdown 写入，验证候选绝对隔离。
3. 接入知识索引、图谱和显式查询。
4. 接入 Codex 动态受限召回和 token 子预算。
5. 更新 compiler 协议、文档、doctor 和安装器。
6. 运行聚焦测试、完整测试、固定评测和真实 Vault 只读检查。
7. 事务安装稳定 runtime，验证稳定路径而不是仓库路径。

不从旧 Decision、Error 或 session 自动生成正式 Insight。若以后需要旧内容提炼，必须先生成只读预览，并保留原来源和用户审批边界。

## 18. 回滚

- 配置 `insight_memory.enabled: false` 可立即停止新写入和自动召回。
- 运行时在 schema 不兼容、索引损坏或超时时静默忽略 Insight，不阻断 Codex。
- 已有正式 Insight 保留在 Obsidian，可审计但不被自动注入。
- candidate 目录可保留或归档，不影响正式记忆。
- 不物理删除正式 Insight；撤回和替代继续使用现有生命周期工具。

## 19. 决策摘要

- 新增独立 `insight` 类型，不塞入 Decision 或 Workflow。
- `[LEARN]` 用于采集，`[INSIGHT]` 用于未来召回。
- 一次性高价值启发立即成为正式 `seed`。
- 重复只强化可信度和成熟度，不决定是否值得记住。
- 自动正式准入只接受用户来源或可验证的共同推导。
- Insight 动态按需召回，不把全部正文常驻编译到 Agent context。
- 每轮最多 2 条、最多 400 tokens，并明确是启发而非事实。
- 候选继续三层隔离，绝不进入运行时。
