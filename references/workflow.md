# Agent Memory Beacon 详细使用流程 / Detailed Workflow

> 本文档详细描述 Agent Memory Beacon 的日常使用流程和故障排除。
> This document describes the detailed recurring workflow and troubleshooting for Agent Memory Beacon.

---

## 一、四层进化工作流 / 1. Four-Layer Evolution Workflow

Agent Memory Beacon 使用四层独立触发机制。每层有不同的可靠性保证，互相补偿。

```
L1: 即时标注 → 模型输出 DECISION / ERROR / FAVOR / LEARN / SESSION_SUMMARY
L2: 增量收割 → Stop + SessionStart hook；ZCode 由 launchd 兜底
L3: 深度分析 → macOS launchd 每周执行 runner.py；距上次扫描至少 8 个完整日时自动全量补扫
L4: 手动入口 → manual-memory-capture、单会话历史导入或 --mode index
```

关键改进: **SessionStart hook 消除了"知识真空期"**。首次安装会先建立高水位，不回放安装前历史；长线程继续时，自适应学习只处理高水位之后的新消息。

## 二、会话标注流程 / 2. Session Annotation Flow

### 2.1 v2.0 简化格式 / v2.0 Simplified Format

v2.0 使用**内联格式**（inline），不再使用块格式（block）。AI 更可能遵守，harvester 更容易解析。

#### [DECISION] — 耐久技术取舍 (单行内联)

```
[DECISION: <一句话总结> | context: <为什么>]
```

**何时**: 在多个方案中形成会影响未来工作的可复用取舍，例如选库、算法、架构、持久配置、命名约定或已验证 workaround。`context` 必须说明原因或代价。

**不要标注**: 完成汇报、临时步骤、纯审查结论、普通观察、问题句或不影响后续工作的选择。

#### [ERROR] — 已解决且可复用的错误 (单行内联)

```
[ERROR: type=<来自error-taxonomy> | resolution=<怎么修的>]
```

**何时**: 非预期错误已经明确根因、修复动作和验证结果。单独一次非零退出不足以成为正式错误。

**不要标注**: 预期 TDD RED、未解决 finding、正常重试后消失的暂时失败、警告、一次性界面误操作或尚未修复验证的审查问题。

#### 质量分流

标签是候选输入，不是正式记忆授权。harvester 会将它们分为：

- `formal`: 信息完整且可复用，写入项目正式记忆。
- `candidate`: 有潜在价值但不够确定，写入 `04-Feedback/_annotation-candidates/`。
- `rejected`: 明确属于状态噪声或非记忆，不进入正式存储。

每个独立耐久事实只写一个标签，不要用多条近义句重复记录。候选、rejected、历史 session 和非 active 正式记录都不会进入运行时召回。

#### [LEARN] — 一次性启发与可迁移思路

```text
[LEARN:<可复用原理>| novelty:<非显而易见之处>| transfer:<场景1,场景2>| boundary:<失效边界>| evidence:<用户原话>| source:user| project:<项目>| scope:<project|global>]
```

只有 Codex 主任务处理 LEARN。完整且来源可核验的首次启发可直接成为 `seed`；重复只把证据和 maturity 强化为 `reinforced`。不完整内容进入 `_insight-candidates/`，assistant-only 推测、敏感内容、普通事实和进度不会进入正式记忆。

验证位置：收割日志显示 `[insight-learner] SEED|CANDIDATE|REINFORCED`，`00-Inbox/Agent Memory Index.md` 显示正式/候选数量，正式正文在 `05-Agent-Memory/insights.md`。未来探索任务可能出现 `[INSIGHT]`；它是启发，不是命令。

#### [SESSION_SUMMARY] — 会话结束时 (块格式)

触发词: "好的/谢谢/完成/收尾/bye/整理"。格式见 CLAUDE.md patch。

### 2.2 质量积压与基线评测

历史正式记忆的审计默认只读：

```bash
.venv/bin/python memory_quality_audit.py --json
```

输出把低质量积压拆成 `evidence_insufficient_count`、`blocked_lifecycle_action_count` 和 `executable_recommendation_count`。第一类等待更多来源证据；第二类会列出 alias owner 等 blocker；只有第三类能通过 `--propose` 生成 pending 提案。任何一类都不会自动改变 formal lifecycle。

Codex Memory 对比先运行能力 probe：

```bash
.venv/bin/python evaluate_memory_comparison.py --probe-only
```

只有 feature 已启用、存储非空，并且 Beacon/Codex Memory 两个黑盒 arm 使用同一 Codex 版本和同一 fixture 哈希时，才运行完整比较。其他情况保持 `N/A`。Beacon 自身的 `evaluate_memory_runtime.py` 只负责回归门禁，不能替代这组 A/B。

---

## 三、Hook 配置 / 3. Hook Configuration

正式安装推荐使用稳定运行时事务。先预览 allowlist 和外部路径，再执行 staging、CI、切换及 live 验收：

```bash
.venv/bin/python install_runtime.py --dry-run
.venv/bin/python install_runtime.py --verify-release
.venv/bin/python install_runtime.py
```

`--verify-release` 会完整执行源码 CI、全新 staging venv、依赖安装和 quick Doctor，然后安全清理 staging，不切换任何 live binding。首次安装、升级和发布前都应先运行它。

成功结果会给出 `manifest_path`。需要回退时，从稳定运行时执行：

```bash
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/install_runtime.py \
  --rollback-manifest /absolute/path/from/manifest_path
```

组件级的 `install_codex.py`、`install_claude.py`、`install_zcode.py --context-only` 和 `install_launchd.py` 仍可用于开发排障。正式 Codex Hook 和 launchd 不应指向开发 checkout。命令路径变化后必须在 Codex `/hooks` 人工确认 trust。

| 触发器 | 触发时机 | 收割什么 |
|------|---------|---------|
| Stop | Codex/Claude 会话结束 | 当前平台的当前 session |
| SessionStart | Codex/Claude 新会话启动 | 48 小时内三端新增或变化的 session |
| harvest launchd | 默认每 300 秒 | 三端遗漏，重点覆盖 ZCode SQLite |
| weekly launchd | `config.yaml` 指定星期和时间 | 五步深度管道 |

**为什么需要多个触发器？** Stop 可能因强制退出而不触发，ZCode 也没有稳定原生 hook；SessionStart 和短周期 launchd 会补收割。所有入口共享 heartbeat 指纹、高水位和文件锁，不会因重复运行重复晋升记忆。

SessionStart 和 harvest launchd 使用有界批次：默认最多 32 条、180 秒软预算。批内先完成 transcript 写入，再统一重建一次索引，最后提交发生变更的高水位；索引失败时不提交这些游标，下一轮会幂等恢复。数量与软预算可通过 `harvest_start_max_transcripts` 和 `harvest_start_time_budget_seconds` 调整。日志除批次的 processing、index、total 外，还会把索引拆成 repair、collect、knowledge、render_write、dirty_clear 五段。

harvest job 使用 launchd `ProcessType=Standard`，保证短周期索引不会被 `Background` 的 CPU/I/O 限速拖到下一次触发；每周深度任务仍使用 `Background`。

每次成功提交并重建索引后，harvester 会刷新 `04-Feedback/memory-effectiveness.md` 和 `_promotion-proposals/`。两者失败只写 warning，不回滚已验证的正式记忆或 transcript cursor。每周 `maintainer.py` 会再次刷新；`--dry-run` 只预览建议，不写效果报告或候选文件。

效果事件只含记忆 ID/revision、哈希任务身份、通道、耗时、估算 token 和反馈分类。转化建议只有在 Decision/Error/Workflow 满足独立来源或正向效果门槛时生成，且绑定当前 revision 和提案 digest。它们不是 formal memory，不进入 recall，也不会自动改源码、测试、Agent context 或正式记录。

## 四、深度扫描管道 / 4. Deep Scanner Pipeline (v2.0)

### 4.1 管道概览 / Pipeline Overview

```
runner.py (macOS launchd，每周默认周日 15:00；平时增量，漏跑时自动全量)
  │
  ├── Step 1: backup.py
  │     └── 跟踪 transcript 指纹；默认只写脱敏元数据，raw 正文需显式开启
  │
  ├── Step 2: analyzer.py  ← v2.0 重写
  │     ├── 关键词筛选所有 session 摘要
  │     ├── (可选) LLM 根因分析: 找 WHY，不只是 HOW MANY
  │     ├── 启发式知识库 (ROOT_CAUSE_KB): 离线也能做根因分析
  │     └── 输出: learnings (根因 + 原则 + 影响 + 规则建议) + summary
  │
  ├── Step 3: maintainer.py  ← v2.0 重写
  │     ├── 从 learnings 生成审批卡 (带具体规则文本，不再 "(TBD)")
  │     ├── 合并检测: ≥2 规则重叠 → 建议合并
  │     ├── 规则 reinforce: 更新已有规则的 last_triggered
  │     ├── 规则生命周期: beta→active (30天) + 过期归档 (60天)
  │     ├── 正式记忆到期: 只处理已有且已经到期的 expires_at，不推断日期
  │     ├── 刷新脱敏效果账本（dry-run 不写）
  │     └── 预览或写入隔离的记忆转化建议，不自动执行建议
  │
  ├── Step 4: reporter.py  ← v2.0 重写
  │     ├── 生成周报: 第一行就是最重要的发现
  │     ├── growth-metrics 真实填充 (不再全是 0)
  │     ├── 更新 heartbeat.md
  │     └── 重建 03-Maps/ (topic-index, timeline)
  │
  └── Step 5: compiler.py  ← v2.0 新增 Agent Memory 路径
        ├── ~/.codex/AGENTS.md: 更新受管理编译块
        ├── ~/.claude/CLAUDE.md: 更新同一份编译块
        ├── ~/.zcode/AGENTS.md: 更新同一份编译块
        └── 05-Agent-Memory: 重建本地 Markdown 召回入口
```

### 2.2 各步骤详解 / Step Details

#### Step 1: backup.py
- 输入：Codex/Claude JSONL 和 ZCode SQLite session
- 默认处理：记录版本指纹，并在 `04-Feedback/_raw-sessions/` 写脱敏元数据 Markdown
- `privacy.store_raw_transcripts: false` 时不复制消息正文；开启后也会先脱敏
- `_raw-sessions`、`_logs`、`_rollback`、`_cleanup-backups` 和 `codex-profile` 不进入 Obsidian 图谱、召回索引或链接校验

#### Step 2: analyzer.py
- 扫描范围：`01-Projects/*/Memory/sessions/` 中尚未标记 `processed` 的 session
- 关键词筛选：遍历 error-taxonomy 的 `keywords`，做文本包含匹配
- LLM 聚类（可选）：将关键词筛选出的候选发给 LLM，要求按"错误现象 + 根因"聚类
- 输出：每个聚类的 pattern name、涉及 session 列表、置信度

#### Step 3: maintainer.py
- **审批卡生成条件**：同一 pattern 出现在 ≥ 3 个不同项目
- **规则晋升**：`beta` 状态 + `created` 距今 ≥ 30 天 → `active`
- **过期清理**：`_rejected/` 中的文件 `proposed` 距今 ≥ 30 天 → 删除
- **跳过逻辑**：`skip_count < skip_until` → 不生成审批卡

#### Step 4: reporter.py
- 周报模板：`04-Feedback/weekly-reports/_TEMPLATE.md`
- 指标更新：在 `growth-metrics.md` YAML 的 `weeks` 列表中追加本周数据
- 地图重建：
  - `topic-index.md`：按错误分类/主题索引所有 session
  - `timeline.md`：按时间线排列所有决策和错误
  - `project-graph.md`：项目间关联的可视化（Mermaid 图）
- 心跳更新：`heartbeat.md` 更新 `last_scan` 和 `scan_status`

#### Step 5: compiler.py
- 只替换 `<!-- COMPILED:RULES_START -->` ... `<!-- COMPILED:RULES_END -->` 之间的内容
- 只替换 `<!-- COMPILED:PROJECTS_START -->` ... `<!-- COMPILED:PROJECTS_END -->` 之间的内容
- 同时维护配置中的 Codex、Claude Code 和 ZCode context targets
- **不会修改**标记块以外的其他部分
- 如果没有找到标记块 → 报错提示用户先加 patch

---

## 三、规则审批流程 / 3. Rule Approval Flow

### 3.1 方法 A：聊天内审批 / In-Chat Approval

1. Claude 会话开始时自动检查 `00-Rules/_inbox/` 是否有 `status: proposed` 的文件
2. 如果有，列出待审批列表
3. 你选择 Y/N/M/S
4. AI 更新审批卡的 frontmatter 并执行相应操作

**操作含义**:
- **[Y]**: `approved_by=用户名`, `approved_at=日期`, `target_rule_id` 指向新生成的规则文件 → 脚本在下一次扫描时生成规则文件
- **[N]**: 移动文件到 `_inbox/_rejected/`
- **[M]**: `status=modification_requested` → AI 按你的指示修改后重新提案
- **[S]**: `skip_count += 1`, `skip_until += N`（默认 N=3，即再跳过 3 次触发）→ 暂时不提醒

### 3.2 方法 B：Obsidian 直接审批 / Direct Obsidian Approval

1. 在 Obsidian 中打开 `00-Rules/_inbox/` 里的审批卡
2. 手动修改 YAML frontmatter：
   - 批准：添加 `approved_by: "{你的名字}"`, `approved_at: "{日期}"`
   - 拒绝：移动文件到 `_rejected/`
3. 下次扫描时，`maintainer.py` 会检测到变更并执行相应操作

### 3.3 规则生命周期 / Rule Lifecycle

```
proposed (审批卡) / proposed (approval card)
  │
  ├─ [Y] 批准 → beta (30天观察期 / 30-day observation)
  │     │
  │     ├─ 30天后 / After 30 days: auto → active
  │     │     │
  │     │     └─ 用户手动 / User manually: active → archived
  │     │
  │     └─ 用户在观察期内拒绝 / User rejects during beta: beta → archived
  │
  ├─ [N] 拒绝 → rejected (30天后自动清理 / auto-cleaned after 30 days)
  │
  ├─ [M] 修改 → modification_requested → (修改后) proposed / back to proposed
  │
  └─ [S] 跳过 → proposed (延迟提醒 / delayed reminder)
```

---

## 四、历史 Session 迁移指南 / 4. Historical Session Migration Guide

### 4.1 适用场景 / When to Migrate

你已经有几十甚至上百次 AI 对话记录，想把这些历史知识导入到新系统中。

### 4.2 迁移步骤 / Migration Steps

```bash
# 步骤1: 评分——选出最有价值的 Codex/Claude JSONL session
cd path/to/repo/scripts
.venv/bin/python score_sessions.py ~/.codex/sessions --top 20

# 步骤2: 审阅——确认要深度摘要的 Top N
# 脚本会输出一个列表，你手动选择要摘要的 session

# 步骤3: 显式导入每个选中的 session
CODEX_TRANSCRIPT_PATH="/absolute/path/to/selected.jsonl" \
  .venv/bin/python session_harvester.py --mode stop --agent codex

# 步骤4: 导入完成后运行一次深度管道
.venv/bin/python runner.py --full
```

### 4.3 评分维度 / Scoring Dimensions

| 维度 / Dimension | 权重 / Weight | 说明 / Description |
|---|---|---|
| **决策密度 / Decision density** | 40% | 决策数 / 助手消息数。值越高说明这个 session 产出了重要决策。 |
| **错误密度 / Error density** | 35% | 错误数 / 助手消息数。坑多的 session 值得记录。 |
| **项目覆盖 / Project coverage** | 25% | 会话涉及了还没被其他 session 代表的子项目 → 加分。避免全部高分 session 都来自同一个子领域。 |

### 4.4 注意事项 / Notes

- **不要全量迁移**——100 个 session 全做深度摘要会花几个小时，而且很多 session 没有多少价值。选 Top 15-20 个就够了。
- **评分是启发式的**——机器学习项目 vs 前端项目的决策密度天然不同。不要求绝对公平，只求选出最有代表性的。
- **后台首次安装不会自动迁移历史**。这是隐私和防污染边界，不是漏扫。
- 显式导入会保留所选 session 的结构化标注；已建立高水位的旧用户消息不会自动变成个人偏好。

### 4.5 旧记忆 Schema 2.0 升级 / Legacy Memory Upgrade

此迁移升级的是 Vault 中已经存在的记忆结构，不是导入 transcript。它保留 session 作为证据，把项目聚合记忆和已晋升的 Personal、Skill、Workflow 记忆转换为带 lifecycle 的正式记录，并隔离所有候选内容。

先运行只读预览:

```bash
cd scripts
.venv/bin/python migrate_memory_v2.py --vault /path/to/your/vault
```

检查输出中的 `planned_writes`、`duplicates_merged`、`candidates_rejected` 和每个 `writes[].path`。确认后使用固定 migration ID 应用:

```bash
.venv/bin/python migrate_memory_v2.py \
  --vault /path/to/your/vault \
  --apply \
  --migration-id 20260712-memory-v2
```

备份 manifest 位于 `04-Feedback/_rollback/memory-v2/<migration-id>/manifest.json`。不要删除该目录；需要精确回退时运行:

```bash
.venv/bin/python migrate_memory_v2.py \
  --vault /path/to/your/vault \
  --rollback /path/to/your/vault/04-Feedback/_rollback/memory-v2/20260712-memory-v2/manifest.json
```

迁移后应满足: runtime candidate 为 0、session runtime unit 为 0、非 active unit 为 0、重复 identity 为 0。索引和 Agent context 会在 apply 或 rollback 后重建。

schema `2.0` 聚合记录是迁移后的权威来源，迁移器不会用 session 内容覆盖它，即使历史记录本身不够完整。记录级 `project` 决定聚合目标路径，归位时保持 ID、revision、status 和内容不变。成功 apply 后必须再运行一次预览；只有 `planned_writes: 0` 才表示迁移已经幂等收敛。

迁移完成后若要分批治理旧正式记忆，不要把过滤后的报告直接交给全局提案协调。使用排他日期生成子集安全计划：

```bash
.venv/bin/python memory_quality_audit.py \
  --old-before 2026-07-13 \
  --propose \
  --json
```

输出计划把建议动作绑定到 ID、Revision、稳定的 `path#key[id=...]` Source Locator、单记录 Source Digest、替代项、理由和 Canonical SHA256。同文件追加无关记录不会使计划失效。它只创建 pending 提案，不改变正式状态；未达到保守阈值的低质量记录继续等待来源证据，不能批量猜测撤回。

用户批准整份计划的 Canonical SHA256 后，先运行只读预览；确认输出仍是同一批动作后再加 `--apply`：

```bash
.venv/bin/python memory_lifecycle_batch.py \
  --plan /path/to/vault/04-Feedback/_lifecycle-proposals/old-memory-lifecycle-plan-before-2026-07-13.md \
  --expected-sha256 <approved-canonical-sha256>

.venv/bin/python memory_lifecycle_batch.py \
  --plan /path/to/vault/04-Feedback/_lifecycle-proposals/old-memory-lifecycle-plan-before-2026-07-13.md \
  --expected-sha256 <approved-canonical-sha256> \
  --apply
```

执行器会把同一聚合文件中的多项变更合并后发布，并对整批只运行一次派生重建。成功回执、审批计划、精确匹配提案和 lifecycle audit 都绑定批准 SHA；任一写入、重建或后置验证失败会恢复源、派生索引、Agent context、计划和提案。没有新的精确 SHA 批准时只能生成或预览下一份计划，不能执行其中动作。

---

## 五、常见问题排查 / 5. Troubleshooting

先运行统一只读检查，再根据失败项进入下面的专项排查:

```bash
.venv/bin/python doctor.py --profile quick
.venv/bin/python doctor.py --profile ci
```

上述命令在源码 checkout 的 `scripts/` 目录执行。`ci` 需要源码中的 tests、fixtures 和 Git 元数据；机器读取结果时增加 `--json`。doctor 默认不修复、不写 Vault，也不更改 Hook 或 launchd。

稳定迁移后运行：

```bash
RUNTIME="$HOME/.local/share/agent-memory-beacon/runtime"
"$RUNTIME/.venv/bin/python" "$RUNTIME/scripts/doctor.py" --profile live
```

stable runtime 只运行 `quick` 或 `live`，不运行依赖源码测试材料的 `ci`。

### Scanner 不跑了 / Scanner Not Running

1. 检查 `heartbeat.md` 的 `scan_status` — 如果是 `error`，查看 `errors` 列表
2. 检查 `04-Feedback/_logs/` 中的最新日志文件
3. 手动跑一次看报错：`python runner.py --dry-run`
4. 检查 `launchctl print gui/$(id -u)/io.agent-memory-beacon.harvest`

### 审批卡不生成 / Approval Cards Not Generating

1. 确认 `maintainer.py` 的最小阈值：同一 pattern 必须出现在 ≥ 3 个项目中
2. 检查 `_inbox/` 里是否已经有类似提案（去重逻辑）
3. 检查 `skip_until` 是否还没到
4. 手动跑 `.venv/bin/python runner.py --step analyze --dry-run` 看中间输出

### CLAUDE.md 没更新 / CLAUDE.md Not Updating

1. 确认 CLAUDE.md 里有 `<!-- COMPILED:RULES_START -->` 和 `<!-- COMPILED:RULES_END -->` 标记
2. 确认两个标记是成对的——缺一个 compiler 会报错
3. 手动跑 `.venv/bin/python runner.py --step compile --dry-run` 看具体错误
4. 确认 `00-Rules/` 里有 `status: active` 或 `status: beta` 的规则文件

### 中文乱码 / Chinese Garbled

1. 所有脚本强制使用 `encoding="utf-8"` 读写文件
2. 确认当前终端和外部工具使用 UTF-8
3. 如果 Obsidian 里中文显示异常，检查 Obsidian 设置 → 文件与链接 → 默认编码是否为 UTF-8

### Wiki-link 断了 / Broken Wiki-links

```bash
.venv/bin/python link_validator.py /absolute/path/to/vault
```
会扫描所有 `[[...]]` 链接，报告死链。常见原因：文件被移动、文件名拼写错误。

---

## 六、如何自定义 / 6. How to Customize

### 6.1 添加新的错误分类 / Adding Error Categories

编辑 `04-Feedback/error-taxonomy.md`，在 YAML frontmatter 的 `categories` 列表中添加新条目：

```yaml
- name: "docker-deploy"
  description: "Docker 部署相关错误 / Docker deployment errors"
  keywords:
    - "docker"
    - "container"
    - "image"
    - "compose"
    - "registry"
    - "volume"
    - "network"
    - "port mapping"
    - "env file"
  subcategories:
    - name: "build-failure"
      description: "镜像构建失败 / Image build failure"
      examples:
        - "Docker build context too large"
        - "Multi-stage build cache miss"
    - name: "orchestration"
      description: "编排问题：compose、swarm、k8s"
      examples:
        - "Docker compose service dependency order wrong"
```

修改后，下次运行 `analyzer.py` 会自动使用新的 keywords。

### 6.2 自定义主题检测 / Custom Topic Detection

编辑 `config.yaml`：

```yaml
topic_map:
  "api-network": ["api", "http", "network", "request"],
  "data-processing": ["pandas", "spark", "etl", "pipeline"],
  "ml-training": ["model", "training", "gpu", "cuda", "overfit"],
  "frontend-ui": ["react", "css", "component", "layout"],
  "devops": ["ci", "cd", "deploy", "monitor"],
  # ...
```

每个分类的 value 是关键词列表。`reporter.py` 在重建 `topic-index.md` 时用这些词做自动归类和标签。

### 6.3 添加新项目 / Adding a New Project

```bash
RUNTIME="$HOME/.local/share/agent-memory-beacon/runtime"
"$RUNTIME/.venv/bin/python" "$RUNTIME/scripts/setup.py" \
  --add-project {project-name}
```

这会：
1. 在 `01-Projects/` 下创建新项目目录
2. 复制所有 Memory 和 Feedback 模板
3. 更新 `config.yaml` 的项目列表
4. 更新 CLAUDE.md 的 `COMPILED:PROJECTS` 块（下次扫描时生效）

---

## 七、安全与隐私 / 7. Security & Privacy

- **Vault 是你的本地文件**——不经过任何服务器
- **LLM 聚类是可选的**——不配 API key 就不会有任何数据离开你的电脑
- **如果配了 LLM API key**：只有关键词筛选出的少量 session 片段会被发送。不发送完整对话记录。
- **不要把 vault 直接放在云同步盘里**跑扫描——并发写入可能导致冲突。要么 vault 在本地 + 定期备份到云端，要么用 Obsidian Sync / Git。
