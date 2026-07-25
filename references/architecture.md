# Agent Memory Beacon Architecture Decisions

> 本文记录 Agent Memory Beacon 在 macOS、Codex、Claude Code 和 ZCode 上的当前架构及其取舍。

---

## 1. Why Four-Layer Evolution? (v2.0 New)

### The Problem with v1.0's Single-Layer Design

v1.0 had one trigger: a weekly cron job. This meant:
- **Knowledge vacuum**: Close a session on Monday, the scanner runs Sunday — 6 days of stale knowledge
- **No real-time feedback**: The AI in session N+1 doesn't know what happened in session N
- **Single point of failure**: If the cron didn't fire (computer off), nothing was learned

### Current Solution: Four Independent Layers

| Layer | Trigger | Latency | Failure Mode |
|-------|---------|---------|-------------|
| L1: Instant Annotation | AI outputs DECISION/ERROR/FAVOR/SUMMARY | Instant | Managed AGENTS/CLAUDE context keeps labels visible |
| L2: Incremental Harvest | Stop + SessionStart + short launchd job | Seconds/minutes | Shared fingerprints, cursors, and lock prevent duplicate learning |
| L3: Deep Analysis | Weekly launchd job | Days | `runner.py` catches up after a missed week |
| L4: Manual Capture | Explicit memory skill or selected history import | On demand | User can force-capture important content without enabling raw storage |

**Key insight**: No single layer needs to be 100% reliable. A missed Stop hook is caught by SessionStart or launchd, while all paths share the same idempotent state.

---

## 2. Why SessionStart Hook? (v2.0 New)

### The Knowledge Vacuum Problem

User workflow: "Do work in Window A → handoff → close A → open Window B → continue."

Without SessionStart hook:
- Window A closes → Stop hook might not fire (Ctrl+C twice, kill -9, etc.)
- Window B opens → AI loads old Agent Memory → doesn't know what happened in Window A
- Wait until the weekly scan → multi-day vacuum

With SessionStart hook:
- Window B opens → SessionStart hook fires → scans 48h of unprocessed transcripts → harvests them → Agent Memory updated
- AI initializes with fresh knowledge from Window A
- Vacuum closed: seconds, not hours

### Design Choice: 48-Hour Window

Why 48 hours, not 24? Covers the "close at 11pm, open at 9am next day" pattern. Why not 7 days? Too wide — would repeatedly scan the same transcripts (harvested by Stop hook and launchd already).

### Design Choice: Install Baseline + Adaptive Cursor

首次后台安装只记录现存 JSONL 的字节位置、ZCode session 的消息数和 transcript 版本，不读取并学习旧正文。之后 `[DECISION]`、`[ERROR]`、`[SESSION_SUMMARY]`、Personal、Skill、Workflow 和错误证据都只接收高水位之后的新消息。JSONL 错误证据只回看游标前最多 4 MiB 的结构化工具调用上下文，不重新扫描整段长会话。这样续聊既不会重放旧记忆，也不会让后台收割耗时随会话长度持续增长。

所有由 harvester 管理的 repair 读取、遍历、创建、写入和删除都从 Vault 根描述符逐级使用 `openat`/`O_NOFOLLOW`。Vault 根或中间目录在调用前被替换为符号链接时，repair 会 fail closed，不能读取或修改 Vault 外文件。

平台注入的 AGENTS/CLAUDE 指令、插件清单和环境上下文会先剥离；Codex sub-agent 与 Claude sidechain 也不参与自适应学习。

---

## 3. Why Root-Cause Analysis Instead of Counting? (v2.0 New)

### What v1.0 Did

```
Scan sessions → count error types → "shell-cli_curl_ssl: 4 occurrences" → approval card "(TBD)"
```

### What v2.0 Does

```
Scan sessions → find 6 error types → cluster by ROOT CAUSE:
  - shell-cli_curl_ssl + api-network_gfw_rst → SAME root: GFW interference
  - path-filesystem_file_not_found → root: Windows path separator
  - R-package_package_not_found → root: non-standard R library path
→ For each root cause, extract ONE principle that prevents ALL symptoms
→ Check if existing rules already cover this → action=reinforce (don't duplicate)
→ Generate concrete rule text, not "TBD"
```

### Design Choice: Heuristic KB + LLM Tiered Approach

```
Tier 1: Heuristic Knowledge Base (ROOT_CAUSE_KB)
  - Always runs, no API needed
  - Maps known symptoms → root causes → principles → rule IDs
  - Covers ~80% of real-world error patterns after initial setup

Tier 2: LLM Deep Analysis (optional, API required)
  - Runs when API key is available AND patterns >= 2
  - Discovers NOVEL root causes not in the KB
  - Provides richer rule text and merge suggestions
  - Failure is non-blocking: falls back to Tier 1

Tier 3: Review Flag
  - Errors not matched by KB or LLM → flagged "review"
  - Human reviews and (optionally) adds to KB
```

### Why Not LLM-Only?

- **Cost**: Every scan hitting the API adds up
- **Reliability**: API can be down, rate-limited, or blocked by GFW
- **Speed**: Heuristic analysis is instant; LLM adds 2-10 seconds
- **Transparency**: Heuristic matches are deterministic and debuggable

---

## 4. Why Vault + Three Managed Context Targets?

Obsidian Markdown 是唯一主存储；三个 Agent 的用户级上下文只是同一份已晋升记忆的编译输出:

```
compiler.py
    ├─→ ~/.codex/AGENTS.md
    ├─→ ~/.claude/CLAUDE.md
    ├─→ ~/.zcode/AGENTS.md
    └─→ 05-Agent-Memory indexes and recall context
```

**Why this split?**
- **Cross-tool**: Codex、Claude Code 和 ZCode 读取同一组规则，而不是各自形成孤岛。
- **Auditable**: 候选、证据、正式规则和图关系都能在 Obsidian 中检查。
- **Non-destructive**: compiler 只替换受管理标记块，保留用户文件其他内容。
- **Recoverable**: 机器索引可以从 Markdown 重建，不依赖数据库或远程服务。

### The Timing

```
SessionStart hook fires
    → harvests unprocessed transcripts → writes to vault
    → rebuilds visible and machine indexes
    → compiler refreshes three managed context targets
    → next agent turn can read the same promoted rules
```

This is why SessionStart hook is so powerful: the entire learning cycle completes BEFORE the AI reads its context.

---

## 5. Why Atomic Writes + Defensive Parsing Everywhere? (v2.0 Hardened)

### v1.0's Fragility

v1.0 assumed well-formed input. Real-world transcripts have:
- Messages that are dicts, lists, or strings (Anthropic API format variation)
- YAML frontmatter with `datetime.date` objects (Python YAML parsing quirk)
- Non-UTF-8 text returned by external tools
- Missing or empty fields in session summaries

### v2.0's Defense-in-Depth

Every function that reads external data validates it:
```python
# analyzer.py: defensive taxonomy loading
taxonomy = yaml.safe_load(parts[1])
if not isinstance(taxonomy, dict):
    return {"categories": []}  # graceful degradation

# backup.py: defensive message parsing
if isinstance(msg, dict):
    msg_text = str(msg.get('content', ''))[:200]
elif isinstance(msg, list):
    msg_text = str(msg)[:200]
elif isinstance(msg, str):
    msg_text = msg[:200]

# reporter.py: defensive date handling
if hasattr(d, 'isoformat'):
    ds = d.isoformat()[5:]  # datetime.date → string
elif isinstance(d, str) and len(d) >= 10:
    ds = d[5:]
```

### UTF-8 Enforcement At Process Boundaries

```python
# Every managed Markdown/JSON/YAML boundary declares UTF-8 explicitly.
with open(path, "r", encoding="utf-8") as handle:
    content = handle.read()
```

The production platform is macOS. Explicit encoding still matters because transcripts and subprocess output can originate from tools with different locale settings.

---

## 6. Why No Persistent Python Daemon?

launchd only schedules short-lived processes; Python does not stay resident:

| Trigger | Frequency | CPU | Memory |
|---------|-----------|-----|--------|
| Stop hook | Per session end | <2s spike | ~50MB then released |
| SessionStart hook | Per session start | Usually seconds; bounded batch | Released after each run |
| Harvest launchd | Default every 300s | One bounded index rebuild; Standard scheduling | Released after each run |
| Weekly launchd | Once weekly | Pipeline spike; Background scheduling | Released after each run |

No background process. No memory leak. No "is it still running?" anxiety.

The incremental harvester defaults to 32 transcripts and a 180-second soft processing budget per batch. It rebuilds the Vault index once after all prepared writes and advances changed transcript cursors only after that rebuild succeeds, so interruption causes an idempotent retry instead of a partially visible commit. Index timing is logged separately for repair, collection, knowledge-index construction, render/write, and dirty-marker clearance.

---

## 7. Why Deterministic Hybrid Recall?

Hindsight demonstrates the value of retrieving through independent semantic, keyword, graph, and temporal paths before rank fusion. Beacon adopts the architecture shape without adopting its database or model stack:

| Beacon channel | Candidate rule | Purpose |
|---|---|---|
| Lexical | Weighted term or phrase overlap | Primary content evidence |
| Structured | Exact Skill, workflow, error category, or other stable name | Preserve machine-name precision |
| Type | Explicit Decision/Error/Preference/Skill/Workflow/Insight intent | Rerank anchored facts; independently list only on an explicit inventory query |
| Temporal | Explicit latest, earliest, or date intent plus a valid record date | Rerank anchored facts; pair with an explicit type for unanchored temporal lookup |
| Graph | One-hop `recorded_in` or `links_to` edge from a content anchor | Recover explicitly related formal facts |
| Experience | Explicit similar-experience/process intent plus a concrete content anchor | Add at most two exact-revision companions from one shared-session bundle |

Each channel produces its own deterministic ranking. `memory_recall.py` combines those rankings with weighted reciprocal rank fusion, then calibrates the fused score with the original content relevance so RRF cannot flatten a strong and shallow lexical match into near-equals. The runtime relative-score gate is applied after fusion. Every result carries `retrieval_channels`, `retrieval_evidence`, `fusion_score`, a compact `why_recalled`, and normalized authority routing. Codex injection keeps only the human-readable reason and authority route, not raw rank evidence.

Type and time are intentionally narrow admission paths. Query parsing separates weak intent words such as “错误”, “失败”, “修复”, “问题”, “最近”, and “一次” from concrete content before lexical or structured anchors are built. “最近一次错误” may retrieve the newest formal Error without shared keywords, and its singular intent returns one item selected from matching content when content is present. A generic repair request cannot pull every Error merely because it contains action or failure words. Candidate, inactive, forged, session, and unsafe-source records are rejected before any channel runs.

This keeps the recall path local, deterministic, rebuildable, and normally below the existing 500 ms acceptance budget. It does not claim vector-semantic equivalence with Hindsight. The inspected-source and borrowing boundary is recorded in `references/prior-art-hindsight.md`.

### Insight learning without repetition as an admission gate

`insight_memory.py` adds a source-grounded generative-memory type without adding a second storage stack. Codex emits `[LEARN]` only for a user-originated reusable principle with explicit novelty, transfer scenes, boundary, and an exact user evidence excerpt. The deterministic harvester validates the source and shape; it does not pretend to prove semantic novelty by itself.

A complete first sighting may enter `05-Agent-Memory/insights.md` as `maturity: seed`. A second independent session appends evidence and may derive `reinforced`, but cannot rewrite the Insight, transfer set, boundary, project, or scope. Core changes create an isolated candidate and lifecycle proposal. Candidate files remain outside collection, indexing, runtime validation, doctor acceptance, and recall.

Formal Insight reuses schema `2.0`, revision binding, source refs, lifecycle tombstones, RRF recall, duplicate suppression, quality audit, and the JSON memory graph. Graph relations include `derived_from`, `reinforced_by`, `applies_to`, `supports`, `operationalized_as`, and `related_to`. Transfer concepts use stable digests rather than raw text IDs.

Automatic use requires exploration intent plus concrete content. The runtime allows at most two Insight records, at most one low-confidence seed, and a 400-token sub-budget. It reserves a bounded exploration slot in crowded results but renders Workflow, Decision, and Error first. Insight bodies are never statically compiled into every Agent context; only the capture protocol and formal counts are compiled.

---

## 8. Design Anti-Patterns (What We Explicitly Avoid)

| Anti-Pattern | Why Not |
|---|---|
| Promote uncertain memory after one inferred mention | A single ambiguous sentence is not a durable preference |
| Single trigger (weekly job only) | Knowledge vacuum of up to 7 days |
| Counting errors instead of analyzing root causes | "6 patterns found" is useless without "WHY they happen and HOW to prevent ALL of them" |
| Approval cards with "(TBD)" rule text | Creates busywork — the human has to write the rule from scratch |
| Piling up rules without merging | Rule count grows indefinitely, quality degrades |
| LLM-only analysis | Single point of failure, cost, latency |
| Persistent Python daemon | Complexity and lifecycle risk without benefit over launchd |
| Real-time monitoring | Knowledge refinement doesn't need sub-second latency |

---

## 9. Why Evidence, Formal Memory, And Candidates Are Separate

把 session、正式事实和待确认内容混在同一个召回层，会让过期结论、重复事实和一次性问题一起进入上下文。schema `2.0` 因此把记忆分成三个职责明确的层:

| Layer | Source | Runtime behavior |
|---|---|---|
| Evidence | `01-Projects/*/Memory/sessions/` | 保留历史与来源，不生成 session runtime unit |
| Formal memory | project aggregates and `05-Agent-Memory/` | 只有带稳定 ID、revision、source refs 且 `status: active` 的记录可召回 |
| Candidates | `04-Feedback/_*-candidates/` and `_skill-preferences/` | 可审阅、可晋升，但永不直接注入 Agent |

正式记录的 ID 表示同一个长期事实，revision 表示该事实当前可见状态。内容、scope、project 或 lifecycle 状态变化时 revision 会变化，但稳定 ID 不变。对追加式 transcript，harvester 使用“会话内增量起始 cursor + 批次内来源序号”建立来源键；同批重试复用该键，后续批次获得新键，避免长会话追加内容覆盖或复用前一批 ID。`superseded`、`retracted`、`expired` 和 `rejected` 仍保留用于审计，同时由索引和召回两层独立拒绝。

显式 `[DECISION]` / `[ERROR]` 先经过确定性语义质量门，再进入上述三层。耐久且完整的标签进入 formal；理由、验证或耐久性不足的标签写入 `_annotation-candidates`；预期 RED、未解决错误、完成汇报和一次性操作直接 rejected。历史 active 记录不因新规则被静默改写：索引构建只在派生召回视图中抑制高置信噪声、保守折叠同项目同根因近重复，并公开 `suppressed_quality` 与 `duplicate_groups` 审计字段。

`memory_quality_audit.py` 对历史正式记录产生只读报告和带精确 ID/revision 的 lifecycle 提案。质量积压分成证据不足、受 lifecycle 约束阻断和可执行建议；alias owner 会作为具体 blocker 保留，而不是让建议静默消失。重复正式 ID 会进入身份冲突章节和 `04-Feedback/memory-quality-conflicts.md` 逐来源复核计划，但不会生成普通 lifecycle action；批量提案会在写入前完成全量身份与 revision 预检，并复用锁内快照，避免按动作重复解析整个 Vault。按日期治理旧记录时，子集计划使用 `path#key[id=...]` 稳定定位器，冻结单记录规范摘要、替代项和理由，并对规范化动作载荷计算 Canonical SHA256；同聚合文件的无关追加不会使计划失效，目标记录变化仍会触发哈希漂移。子集协调只淘汰所选 ID 的旧 pending 版本，不影响其他提案。只有用户明确批准具体记录后，`memory_lifecycle.py` 才能改变唯一 ID 记录的状态；若批准对象是整份旧记忆计划，则 `memory_lifecycle_batch.py` 还会复核 Canonical SHA256、整批依赖资格及目标/替代项不重叠，在一个 writer lock 和 rollback snapshot 中合并同源写入、只重建一次派生状态，并把计划与对应提案收敛到 applied/stale。冲突 ID 必须改由 `memory_identity_repair.py` 执行：它把批准计划 SHA256 与每组 Owner、Revision、Source Digest、Source Locator 绑定，在共享 writer lock 内再次核验后，以单批 rollback snapshot 完成 rekey、relocate、supersede/retract、证据合并、一次索引重建和审计写入。任一快照漂移或后置条件失败都会停止或恢复全部文件；该边界防止启发式分类器把有效历史直接删掉。

旧记忆升级采用只读 plan、输入哈希复核、writer guard、完整备份和原子替换。session 原文不删除；`github-obsidian-knowledge-brain` 安全归一到 `agent-memory-beacon`，`slug` 占位路由中的独有内容标为 `retracted`，不允许成为 active 记忆。迁移后的 schema `2.0` 聚合记录优先于 session 派生事实，重复运行只处理真正 legacy 输入；记录级项目归位保持正式身份和 lifecycle 语义，成功 apply 后的再次 preview 必须为零写入。

---

## 10. Why A Stable Allowlisted Runtime?

开发 checkout 是可编辑、可移动且可能长期 dirty 的源码目录，不适合作为 macOS Hook 和 launchd 的长期执行位置。正式运行时固定在：

```text
~/.local/share/agent-memory-beacon/runtime
```

`install_runtime.py` 只发布静态脚本 allowlist、MIT 许可证、受管 patch、Vault 模板、脱敏配置、经过审计的精确依赖 lock 和独立 venv。tests、`.git`、planning、日志、数据库、缓存、凭据及任意仓库文件不进入运行时。源码树先通过 `doctor ci`，staging 再通过 `doctor quick`；发布后必须通过 `doctor live`。`--verify-release` 可重复执行前两层真实验收并清理 staging，不修改 Hook、AGENTS、launchd 或当前稳定运行时。

切换事务在运行时目录之外保存 manifest 和六个外部文件的逐字节快照：`hooks.json`、全局 `AGENTS.md`、两个当前 plist 和两个 legacy plist。旧运行时保留到 rollback 目录对应的 previous path。任一 Hook、context、launchd 或 live 检查失败都会卸载新 job、恢复原文件、恢复旧运行时，并按安装前服务状态重新加载。

Codex command trust 不在 `hooks.json` 内，不能安全迁移或伪造。因此命令绝对路径变化后，安装器只返回 `trust_review_required`，用户仍需在 `/hooks` 审核三个 Agent Memory Beacon 命令。

---

## 11. Why Codex Memory Comparison Has A Capability Gate

`evaluate_memory_comparison.py` 把“本机是否存在 SQLite 文件”“Memory feature 是否启用”“是否已经产生 stage-1 学习结果”视为三个不同事实。probe 通过无 shell 的固定 argv 调用读取 Codex 版本与 feature 表，再用 SQLite read-only URI 计数；它不把一个空文件误判为可用基线，也不会自行开启 experimental feature。

公平比较要求两个 arm 绑定同一个 Codex 版本、fixture ID 和 SHA-256，并带有效证据引用。评分合同只接受五个已批准行为维度；未知指标和 NaN/Infinity 会被拒绝。版本不一致、fixture 漂移、证据无效、Codex Memory 关闭或为空时，结果必须是 `N/A`，不能把“不可比较”写成 Beacon 获胜。

Beacon 的用户自有 Obsidian 源、candidate 隔离、来源审计、正式撤回/替代和跨 Agent 可移植性作为结构能力单列，不进入行为分数。这个边界同时避免低估 Codex Memory 的隐藏检索能力，也避免用 Beacon 的可见性优势替代实际召回准确率。

---

## 12. Why Effectiveness, Authority, Promotion, And Experience Stay Separate?

四者解决不同问题，不能合并成一个自动自我修改循环：

| Layer | Purpose | Mutation authority |
|---|---|---|
| Effectiveness ledger | 观察精确 revision 被召回后是否出现弱接受、纠正或显式人工反馈 | 只能写脱敏事件和派生报告 |
| Authority contract | 声明事实的所有者、规范来源、执行面、验证引用和新鲜度 | 随正式记忆 revision 变化；无权自动改 owner |
| Promotion proposal | 建议把重复且有效的记忆转成测试、runbook 或受管规则 | 只能写 `_promotion-proposals` 候选 |
| Experience bundle | 把同一任务中不同类型的正式记忆连接成可复用过程 | 纯派生索引；不是 formal memory 或 lifecycle target |

效果事件不含 prompt、记忆正文或真实 session ID。自动反馈只能生成建议，不能修改正式记忆。经验包由共享 `session:` 来源派生，成员绑定当前精确 revision；索引读取会拒绝过期成员、重复身份或任何正文字段。经验展开要求明确意图、内容锚点，以及 companion 与前五个强锚点之间的具体词汇桥接，并限制为一个 bundle、两个 companion，避免普通查询或多任务长 session 沿会话关系放大。

权威性不是相关性的替代品。词项和结构化内容先决定原始分数，authority rank 只在同分时打破平局。运行时展示 `why_recalled` 与 `authority`，让 Agent 和用户都能区分“为什么找到它”和“它是否拥有执行权”。
