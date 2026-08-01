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
| L1: Instant Annotation | AI outputs DECISION/ERROR/FAVOR/SUMMARY plus hidden rolling summary checkpoints | Instant | Managed context keeps labels visible; strict parser rejects invalid hidden markers |
| L2: Incremental Harvest | Stop + SessionStart + short launchd job | Seconds/minutes | Shared fingerprints, cursors, and lock prevent duplicate learning |
| L3: Deep Analysis | Weekly launchd job | Days | `runner.py` catches up after a missed week |
| L4: Manual Capture | Explicit memory skill or selected history import | On demand | User can force-capture important content without enabling raw storage |

**Key insight**: No single layer needs to be 100% reliable. A missed Stop hook is caught by SessionStart or launchd, while all paths share the same idempotent state.

### Rolling conversation state without a second model

Long-session state does not fit cleanly into Decision, Error, Preference,
Workflow, or Insight. Codex therefore uses the existing `UserPromptSubmit`
state machine to request a bounded hidden summary on checkpoint turns. The
currently active Codex response writes the marker, so the system adds bounded
output tokens but no second inference request, API key, resident process, or
idle wake-up.

The harvester accepts only a strict assistant-authored HTML marker outside code
fences. It validates YAML shape and size, strips secrets, excludes sub-agents,
and atomically replaces the effective summary only when the transcript cursor
advances. The persisted session note remains evidence. `knowledge_index.py`
derives at most one `conversation_summary` record per stable session into a
separate top-level collection; it never promotes that record into formal
`units`.

Recall treats this collection as a one-result lexical side channel. It requires
a concrete project-local content anchor, has a 400-token sub-budget, loses to
stronger formal memory under pressure, and renders as `[CONTEXT]`. It cannot
open graph or experience expansion, enter lifecycle commands, compile into
Agent context, or affect formal-memory effectiveness metrics. This preserves
continuity without turning a model-written conversation synopsis into durable
authority.

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
    └─→ 05-Agent-Memory formal indexes plus isolated conversation summaries
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
    → replaces the latest session summary and rebuilds visible/machine indexes
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
| Graph | Direct note links plus at most two `supports` / `depends_on` / `operationalized_as` / `superseded_by` / `contradicts` hops from a content anchor | Recover explicitly related formal facts without project-wide diffusion |
| Experience | Explicit similar-experience/process intent plus a concrete content anchor | Add at most two exact-revision companions from one shared-session bundle |

Each channel produces its own deterministic ranking. `memory_recall.py` combines those rankings with weighted reciprocal rank fusion, then calibrates the fused score with the original content relevance so RRF cannot flatten a strong and shallow lexical match into near-equals. The runtime relative-score gate is applied after fusion. Every result carries `retrieval_channels`, `retrieval_evidence`, `fusion_score`, a compact `why_recalled`, and normalized authority routing. Codex injection keeps only the human-readable reason and authority route, not raw rank evidence.

Type and time are intentionally narrow admission paths. Query parsing separates weak intent words such as “错误”, “失败”, “修复”, “问题”, “最近”, and “一次” from concrete content before lexical or structured anchors are built. “最近一次错误” may retrieve the newest formal Error without shared keywords, and its singular intent returns one item selected from matching content when content is present. A generic repair request cannot pull every Error merely because it contains action or failure words. Candidate, inactive, forged, session, and unsafe-source records are rejected before any channel runs.

This keeps the recall path local, deterministic, rebuildable, and normally below the existing 500 ms acceptance budget. It does not claim vector-semantic equivalence with Hindsight. The inspected-source and borrowing boundary is recorded in `references/prior-art-hindsight.md`.

### Derived Graph v3

`memory-graph.json` is a disposable index over formal Markdown, never a second source of truth. Graph v3 uses six stable node types (`memory`, `note`, `project`, `experience`, `concept`, `source`); the concrete record category remains in `kind`. Every relation has an explicit domain/range contract, and every edge carries confidence plus provenance (`source_ref`, `source_revision`, `observed_at`, `derivation`). Memory-to-memory semantic edges must also be derivable from the authoritative source unit field (`requires`, `supports`, `operationalized_as`, `related_to`, `superseded_by`, or `contradicts`). Validation rejects a graph-only assertion even when its endpoints and evidence shape are otherwise valid.

The four editable semantic fields have distinct contracts. `supports` records directional evidential or principled support. `operationalized_as` links an abstract memory to the concrete Skill, Workflow, or mechanism that implements it. `contradicts` preserves an explicit incompatibility without silently deciding which memory wins. `related_to` is the weak review-only relation and is excluded from semantic recall traversal. `requires` remains a hard runtime dependency and cannot be repurposed as a visual association. Every relation mutation changes the source revision; targets retain their own revision unless their source fields also change.

The recall index and graph share a deterministic `generation_id` derived from revision-bound runtime units and indexable note links. Loading fails when the two files come from different generations. A custom recall index stores its graph as a sibling file so one configured runtime cannot accidentally load the default graph from another index.

Index generation fails on illegal endpoints, missing or unbound provenance, undeclared semantic assertions, duplicate identities, absent runtime memory nodes, or a source revision that no longer matches its authoritative input. Memory nodes use the formal unit revision; note and experience edge sources use deterministic source digests, so every edge is revision-bound. `memory-graph-quality.md` exposes the same checks for human inspection. Quick and CI preflight accept only Graph v2 with known relations and existing endpoints, pre-generation Graph v3 that passes the complete current node, edge, evidence, confidence, and revision contract, or the immediately preceding Graph v3 shape whose sole contract gap is missing non-memory source revisions. The latter is normalized in memory only and must then pass the current validator before preflight allows a rebuild. Runtime loading, semantic expansion, and live Doctor remain strict Graph v3.

Graph retrieval is deliberately conservative. A lexical or structured content match must establish the first anchor. From there, only five semantic relation types may expand, traversal may follow a relation in either direction, and the shortest deterministic path is limited to two hops. `links_to` remains a visualization relation and never expands formal memory. `belongs_to`, aggregate notes, note/source/concept nodes, and shared sessions cannot diffuse a hit across a whole project or note. The graph-engineering borrowing boundary is recorded in `references/prior-art-graph-engineering.md`.

### Insight learning without repetition as an admission gate

`insight_memory.py` adds a source-grounded generative-memory type without adding a second storage stack. Codex emits `[LEARN]` only for a user-originated reusable principle with explicit novelty, transfer scenes, boundary, and an exact user evidence excerpt. The deterministic harvester validates the source and shape; it does not pretend to prove semantic novelty by itself.

A complete first sighting may enter `05-Agent-Memory/insights.md` as `maturity: seed`. A second independent session appends evidence and may derive `reinforced`, but cannot rewrite the Insight, transfer set, boundary, project, or scope. Core changes create an isolated candidate and lifecycle proposal. Candidate files remain outside collection, indexing, runtime validation, doctor acceptance, and recall.

Formal Insight reuses schema `2.0`, revision binding, source refs, lifecycle tombstones, RRF recall, duplicate suppression, quality audit, and the JSON memory graph. Graph relations include `derived_from`, `reinforced_by`, `applies_to`, `supports`, `operationalized_as`, and `related_to`. Transfer concepts use stable digests rather than raw text IDs. Runtime refreshes expose validated stable memory IDs; Codex may add the three optional semantic fields only when it can copy an exact recalled ID and the relationship is explicit. Unknown or uncertain relationships remain absent rather than being inferred from titles.

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
| Evidence | `01-Projects/*/Memory/sessions/` | 保留历史与来源，不生成正式 session unit；只从最新有界摘要派生独立 `CONTEXT` |
| Formal memory | project aggregates and `05-Agent-Memory/` | 只有带稳定 ID、revision、source refs 且 `status: active` 的记录可召回 |
| Candidates | `04-Feedback/_*-candidates/` and `_skill-preferences/` | 可审阅、可晋升，但永不直接注入 Agent |

正式记录的 ID 表示同一个长期事实，revision 表示该事实当前可见状态。内容、scope、project 或 lifecycle 状态变化时 revision 会变化，但稳定 ID 不变。对追加式 transcript，harvester 使用“会话内增量起始 cursor + 批次内来源序号”建立来源键；同批重试复用该键，后续批次获得新键，避免长会话追加内容覆盖或复用前一批 ID。`superseded`、`retracted`、`expired` 和 `rejected` 仍保留用于审计，同时由索引和召回两层独立拒绝。

显式 `[DECISION]` / `[ERROR]` 先经过确定性语义质量门，再进入上述三层。耐久且完整的标签进入 formal；理由、验证或耐久性不足的标签写入 `_annotation-candidates`；预期 RED、未解决错误、完成汇报和一次性操作直接 rejected。历史 active 记录不因新规则被静默改写：索引构建只在派生召回视图中抑制高置信噪声、保守折叠同项目同根因近重复，并公开 `suppressed_quality` 与 `duplicate_groups` 审计字段。

`memory_quality_audit.py` 对历史正式记录产生只读报告和带精确 ID/revision 的 lifecycle 提案。质量积压分成证据不足、受 lifecycle 约束阻断和可执行建议；alias owner 会作为具体 blocker 保留，而不是让建议静默消失。重复正式 ID 会进入身份冲突章节和 `04-Feedback/memory-quality-conflicts.md` 逐来源复核计划，但不会生成普通 lifecycle action；批量提案会在写入前完成全量身份与 revision 预检，并复用锁内快照，避免按动作重复解析整个 Vault。按日期治理旧记录时，子集计划使用 `path#key[id=...]` 稳定定位器，冻结单记录规范摘要、替代项和理由，并对规范化动作载荷计算 Canonical SHA256；同聚合文件的无关追加不会使计划失效，目标记录变化仍会触发哈希漂移。子集协调只淘汰所选 ID 的旧 pending 版本，不影响其他提案。只有用户明确批准具体记录后，`memory_lifecycle.py` 才能改变唯一 ID 记录的状态；若批准对象是整份旧记忆计划，则 `memory_lifecycle_batch.py` 还会复核 Canonical SHA256、整批依赖资格及目标/替代项不重叠，在一个 writer lock 和 rollback snapshot 中合并同源写入、只重建一次派生状态，并把计划与对应提案收敛到 applied/stale。冲突 ID 必须改由 `memory_identity_repair.py` 执行：它把批准计划 SHA256 与每组 Owner、Revision、Source Digest、Source Locator 绑定，在共享 writer lock 内再次核验后，以单批 rollback snapshot 完成 rekey、relocate、supersede/retract、证据合并、一次索引重建和审计写入。任一快照漂移或后置条件失败都会停止或恢复全部文件；该边界防止启发式分类器把有效历史直接删掉。

`memory_relation_batch.py` 对历史正式记录采用同样的授权边界，但只修改显式语义关系。候选 JSON 不是授权；生成器会冻结 Source/Target 的 ID、revision、稳定 locator、`canonical-record-v1` digest、摘录、关系、理由和证据引用，并对完整 actions 计算 Canonical SHA256。只有用户批准该精确 SHA 后才可 apply。执行器在共享 writer lock 内再次验证计划字节和两端记录，合并同源更新并重建一次派生索引；任何写入、重建或后置条件失败都会恢复正式源、审批计划、recall index、memory graph、图质量报告和其他生成索引。相似度或标题匹配只能帮助发现候选，不能成为正式关系证据。

旧记忆升级采用只读 plan、输入哈希复核、writer guard、完整备份和原子替换。session 原文不删除；`github-obsidian-knowledge-brain` 安全归一到 `agent-memory-beacon`，`slug` 占位路由中的独有内容标为 `retracted`，不允许成为 active 记忆。迁移后的 schema `2.0` 聚合记录优先于 session 派生事实，重复运行只处理真正 legacy 输入；记录级项目归位保持正式身份和 lifecycle 语义，成功 apply 后的再次 preview 必须为零写入。

---

## 10. Why A Stable Allowlisted Runtime?

开发 checkout 是可编辑、可移动且可能长期 dirty 的源码目录，不适合作为 macOS Hook 和 launchd 的长期执行位置。正式运行时固定在：

```text
~/.local/share/agent-memory-beacon/runtime
```

`install_runtime.py` 只发布静态脚本 allowlist、MIT 许可证、受管 patch、Vault 模板、脱敏配置、经过审计的精确依赖 lock 和独立 venv。tests、`.git`、planning、日志、数据库、缓存、凭据及任意仓库文件不进入运行时。源码树先通过 `doctor ci`，staging 再通过 `doctor quick`；发布后必须通过 `doctor live`。`--verify-release` 可重复执行前两层真实验收并清理 staging，不修改 Hook、AGENTS、launchd 或当前稳定运行时。

切换事务在运行时目录之外保存 manifest，并根据当前配置动态枚举、逐字节快照所有受管 Hook、Agent context、当前/legacy LaunchAgent 和可选 sync LaunchAgent。它不依赖固定“六个文件”或“两个 job”的列表；新增 context target 或启用 authority sync 会自动进入同一事务。旧运行时保留到 rollback 目录对应的 previous path。任一 Hook、context、launchd 或 live 检查失败都会卸载新 job、恢复原文件、恢复旧运行时，并按安装前服务状态重新加载。

Windows producer-replica 使用独立的版本化 stable runtime：

```text
%LOCALAPPDATA%\AgentMemoryBeacon\runtime\releases\<release-id>
```

release manifest 绑定精确 allowlist 文件、脱敏配置、release ID 和 release 目录；
`.venv\Scripts\python.exe`、`scripts\beacon_sync.py` 和 `scripts\config.yaml` 都在
该 release 内。staging 必须通过 Windows 同步测试矩阵、依赖安装、`pip check`、
producer init、quick Doctor 和 manifest 复核，才允许把当前用户 Task/hook 事务
切换到该 release。旧 release 首版保留，不由后台任务自动删除。

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

---

## 13. Why Active-Active Conversation Production Still Uses One Writer

Windows 和 Mac 都可以产生对话，但只有 Mac 可以写 canonical Vault。Windows
producer 将 Codex/Claude JSONL 按完整记录边界切成不可变事件；事件身份绑定
`device_id`、`producer_instance_id`、全局 `seq`、stream epoch、cursor 和
payload SHA-256。对象、`event.json`、`ready.json` 依次提交，authority 只处理
连续序列。

协议版本按记录类型区分，而不是用一个版本覆盖所有文档：

- `transcript.chunk` / `transcript.gap` event 与 ready 保持 schema v1；golden bytes
  和既有 event ID 不变。
- `attachment.blob` event 与 ready 使用 schema v2；`reference_id` 绑定 producer、
  stream、epoch、record cursor、来源路径摘要、原名和 payload SHA-256。
- Producer state 为 v3，可迁移 v1/v2 queue 与 pending state；已经 durable 的旧
  v1 attachment bundle 仍可兼容消费，但 producer 不再生成新的 v1 attachment。
- Authority SQLite ledger 为 v4，并用独立事务执行 `1 -> 2 -> 3 -> 4` 迁移；每步
  提交版本后才进入下一步，因此崩溃可从准确的中间版本继续。

Mac reducer 在 SQLite ledger 中记录来源状态，把远端事件重建为持久 transcript
mirror，并在现有 `harvester.lock` 下调用同一个 `session_harvester`。因此本地和
远端对话共享 Decision/Error/Favor/Workflow/Insight/summary 的质量模型，不存在
第二个“远端记忆分类器”。

远端附件只有在 transcript 明确引用且源文件位于配置的 `attachment_roots` 内时
才采集。canonical blob 由内容签名决定扩展名并写到
`Attachments/Agent-Memory-Beacon/remote/objects/<sha[:2]>/<sha>.<ext>`；审计
metadata 写到
`04-Feedback/remote-attachments/<device>/<producer>/<seq20>-<event-id>.md`。
ledger v4 保存两者的 path/hash/bytes。generation binding 会把 sealed manifest
转成路径映射并逐项验证：blob 必须匹配 payload hash/bytes，metadata 必须匹配
独立 metadata hash/bytes；任一成员缺失时 receipt 保持 unbound。只要原 inbox
event 仍在，后续 reducer 可幂等重建缺失 effect，而不会倒退 sequence 或重复
harvest。

Canonical 输出按 generation 发布：先写 content-addressed objects，再写完整
snapshot 和 `complete.json`，最后替换 `current.json`。reducer 与 publisher
共享 `authority-cycle.lock`；generation 封存后、锁释放前，publisher 在 ledger
事务中把当时所有未绑定 pending 事件固定到该 generation。receipt 文件随后可
幂等重试，封存后才 reduce 的事件不会误绑旧代。Windows 必须完整验证 current、
snapshot、complete、对象大小和哈希、父 generation、接收端路径白名单、
`content_class`、大小写/Unicode 规范化和文件/目录前缀，才能在 staging 后应用；
`active-generation.json` 是最终 commit point，失败时按不超过 4 MiB 的 journal
恢复旧副本。目录与文件可跨代互换，但未托管本地内容只会阻止更新，不会被删除。

Windows 写入和删除固定最终父目录与文件句柄，临时文件从写入、`fsync`、属性
设置到 rename 始终使用同一句柄；最低支持 Windows 10 1809 / Windows Server
2019（build 17763）。首次接入只能显式运行 `materialize --bootstrap`，后台
`run` 不拥有隐式接管空副本的权限。

同步配置先建立独立存储域：state、outbox、published、received-published、
replica、inbox、attachment roots 和 canonical Vault 必须是互不包含的绝对路径，
已有符号链接按 realpath 再检查一次，Windows 还按大小写不敏感身份比较。Mac
stable runtime 在同一安装事务中动态纳入可选 sync LaunchAgent；Windows task 和
可选 hooks 使用 ownership 标记与整组回滚，不能接管同名第三方任务。

24 小时在线交付 SLO 与 outbox GC retention 是两套状态。Doctor 会把 received
head 未物化或 active generation 落后判为故障，而不会因为 retention 是 7 天就
把告警推迟到 14 天；partial receipt generation binding 会立即判为 ledger
损坏。v1 尚无 replica-to-authority generation acknowledgement，
所以 authority 保留 sealed generation 链，只删除所有保留 manifest 都未引用的
可验证孤立对象；这保证离线 Windows 可以按父代顺序恢复。

这个边界明确排除：

- canonical Vault 双向文件同步；
- Windows 直接发送 Decision、Error 或 lifecycle mutation；
- 缺 sequence 时跳过历史；
- 为了继续运行而接受未知 schema 或哈希冲突；
- 从 `skill-source` 副本自动安装或执行 Skill；
- 覆盖被用户本地修改过的 managed replica 文件。
