# Changelog

All notable changes to Agent Memory Beacon are documented here.

## [Unreleased]

### Fixed

- Allow a Codex task that originated as a sub-agent rollout and was later resumed directly by the user to persist rolling context only when its outer thread has a real `UserPromptSubmit` checkpoint; ordinary sub-agent memory remains isolated.
- Reserve final render budget for a matched `[CONTEXT]` after higher-authority formal memory so a crowded Decision/Error result set cannot silently discard the latest conversation summary.
- Create a fresh authority ledger on Python 3.11 without serializing an uninitialized in-memory SQLite database.
- Complete the Windows synchronization path with native handle-relative atomic rename, path-bound recursive cleanup identities, and lazy loading of macOS-only launchd dependencies.

## [0.7.0] - 2026-07-31

### Changed

- Expose validated stable memory IDs in every Codex runtime refresh so later annotations can safely reference an existing formal record.
- Extend explicit `supports`, `operationalized_as`, `related_to`, and `contradicts` relations across every formal memory type, with revision-bound persistence and graph provenance.
- Derive the latest valid session summary into an isolated low-priority recall channel instead of treating all session content as either formal memory or non-recallable evidence.

### Added

- Add exact-hash, read-only approval plans and transactional batch application for adding reviewed semantic relations to legacy formal memory.
- Add silent Codex rolling-summary checkpoints that reuse the active response, replace older summaries by transcript cursor, and recall at most one bounded `[CONTEXT]` without a second model request.
- Add verified Windows producer/Mac authority synchronization with immutable transcript events, strict global sequencing, sealed content-addressed generations, receipts, and a read-only Windows Vault replica.
- Add one cross-platform sync CLI, a current-user launchd/Task Scheduler installer, optional non-destructive Windows collector hooks, and read-only Doctor transport checks.
- Preserve transcript/gap event and ready schema v1 while adding attachment schema v2, producer state v3, and authority ledger v4 with resumable stepwise migration.
- Capture only transcript-referenced files under explicit attachment roots, store content-signature-addressed blobs plus auditable metadata notes, and require both files to belong to the sealed receipt generation.
- Add immutable manifest-verified Windows runtime releases under `releases/<release-id>` and bind Task Scheduler plus optional hooks to each release's private Python, script, and redacted config.

### Fixed

- Reject invalid memory IDs at the final runtime rendering boundary instead of allowing an untrusted identifier into injected context.
- Bind recall generation identity to indexable note titles as well as bodies and links, preventing a renamed note from sharing a stale index/graph generation.
- Restore formal sources, approval plans, recall indexes, memory graphs, graph quality reports, and generated indexes when a semantic-relation rebuild fails.
- Redact secrets before persistence, and reject user-authored, code-fenced, sub-agent, malformed, oversized, stale-cursor, or unsafe-path rolling summaries while preserving the last valid revision.
- Preserve explicit rolling-summary and graph-projection settings across stable-runtime upgrades while excluding their resolved local paths.
- Strip complete or truncated rolling-summary controls before formal annotation and adaptive-learning extraction, preventing summary text from being promoted as Decision, Error, Preference, Workflow, Skill, or Insight memory.
- Commit rolling-summary cadence before emitting a checkpoint, keep it available when the recall index is unavailable, and avoid suppressing a rendered recall after its final state commit.
- Persist consumed effectiveness feedback during index failures, prefer a larger same-session transcript cursor over a newer wall-clock timestamp, and render custom summary byte limits in checkpoint instructions.
- Keep remote devices collection-only for formal lifecycle authority, reject lifecycle event kinds, detect replica drift before overwrite, and roll back interrupted replica applies before advancing the active-generation marker.
- Reject relative, unresolved, overlapping, or symlink-aliased synchronization roots before any producer, authority, or replica state is activated.
- Recover sealed orphan generations after a publisher crash, use the configured object bound consistently, verify rollback backup hashes, and retain generation history until replica acknowledgement authority exists.
- Install authority sync together with the stable runtime transaction, restore all launchd state on failure, and durably fsync runtime and manifest directory renames.
- Bind Windows tasks and collector hooks to explicit ownership markers, reject foreign same-name tasks, and roll back task plus hook changes as one transaction.
- Make Doctor enforce the 24-hour delivery SLO independently of GC retention, fail on an unmaterialized or lagging replica, and validate Task Scheduler rather than launchd on Windows.
- Freeze pending receipts to the sealed generation before file publication, serialize reducer and publisher cycles, and report partial ledger bindings immediately.
- Prevent attachment receipts from authorizing GC until the sealed generation contains both the canonical blob and metadata with ledger-bound path, hash, and byte membership; rebuild missing unbound effects from retained inbox events.
- Derive canonical attachment extensions from payload signatures instead of remote MIME/name metadata, and include attachment roots in every storage-overlap check and attachment limit in both transport bounds.
- Make ledger migration commit `v1 -> v2 -> v3 -> v4` as independently restartable transactions so an interrupted upgrade resumes from its exact durable version.
- Require explicit replica bootstrap, validate receiver-side path/class invariants, bound recovery journals, and preserve unmanaged files during file/directory shape-transition rollback.
- Keep the Windows temporary file handle pinned through atomic rename and reject systems older than Windows 10 1809 / Windows Server 2019 build 17763.

## [0.6.0] - 2026-07-26

### Added

- Add a schema-first Graph v3 with stable node types, relation domain/range contracts, per-edge confidence and revision-bound provenance.
- Add deterministic, bidirectional semantic path recall from direct content anchors, restricted to five explicit relation types and at most two hops.
- Generate `05-Agent-Memory/memory-graph-quality.md` and make Doctor reject illegal edges, malformed evidence, duplicate identities, stale revisions, and orphaned resolved-memory nodes.
- Add bounded preflight compatibility for Graph v2 and pre-generation Graph v3 so old Vaults can reach the rebuild step; live recall remains strict Graph v3.
- Bind each recall index and graph pair to the same deterministic `generation_id`, with scale and latency gates covering 1,600 nodes and 6,600 edges.

### Changed

- Keep formal Obsidian Markdown authoritative while treating the graph as a disposable, locally rebuilt index.
- Replace broad graph association with conservative content-anchored expansion that cannot diffuse through project membership, sessions, sources, or concepts.
- Place `memory-graph.json` beside the configured recall index instead of assuming the default `05-Agent-Memory` directory.

### Fixed

- Reject graph-only semantic assertions that are absent from the authoritative source unit, and keep Obsidian `links_to` edges out of runtime recall expansion.
- Require revision-bound evidence for every graph edge, including deterministic note and experience source digests.
- Make runtime graph loading fail closed on legacy or non-object graphs while keeping bounded legacy acceptance only in quick/CI upgrade preflight.
- Let quick/CI upgrade the immediately preceding Graph v3 only when its sole defect is the known missing note/experience source revision; live validation remains strict.
- Enforce project allowlists even when callers also pass an explicit project.
- Preserve custom `memory_runtime.index_path` and its sibling graph across frozen brand migration, rebuild, and rollback planning.
- Rebuild evaluator graph snapshots after unit additions, removals, and revision changes so CI exercises production generation and revision validation.
- Fold an explicitly truncated Decision summary into its same-title complete record in the derived runtime view when all meaningful short-summary terms are covered; distinct complete Decisions remain separate and formal Markdown is not mutated.
- Prevent unrelated memories from becoming graph neighbors merely because their formal records share one aggregate `decisions.md` or `pitfalls.md` note.
- Reject stale, forged, unbound, duplicate, low-confidence, or cross-project graph paths before they can affect recall.
- Make the cooperative harvester and visible Obsidian index honor a custom recall/graph directory consistently.
- Apply truncated-summary folding only to Decision records so Workflow, Preference, Skill, and Insight memories retain their independent lifecycle identities.
- Exclude every configured adaptive candidate directory and promotion-proposal directory from cooperative keyword and graph rebuilds, including custom locations under `05-Agent-Memory`.
- Preserve at most two source-grounded Insight results across the relative-score gate when the prompt has explicit exploration intent, while retaining authority memories first.

## [0.5.0] - 2026-07-22

### Fixed

- Prevent unverified repeat annotations from reinforcing formal Insight records, and mark completed Insight candidates as promoted without deleting their audit evidence.
- Separate weak query-intent words from content anchors, keep type-only inventory results pure, and enforce one-result semantics for singular latest/earliest recall.
- Keep date-filtered quality-proposal batches isolated from unrelated pending proposals while staling older proposals for the selected memory IDs.
- Treat existing schema 2.0 aggregate and adaptive records as authoritative during legacy migration, preserve lifecycle metadata during relocation, and make repeated migration previews converge to zero writes.
- Ignore placeholder wiki-links from reusable Vault templates while keeping those templates available as link targets and explicit validation inputs.
- Serialize heartbeat state updates across same-process threads as well as local processes, preventing reporter writes from overwriting newly harvested adaptive cursors.
- Report duplicate formal-memory IDs as identity conflicts, exclude them from lifecycle recommendations, and preflight complete quality-proposal batches before writing.
- Validate quality-proposal lifecycle semantics from one Vault snapshot, avoiding a full formal-memory rescan for every recommendation.
- Generate a read-only `04-Feedback/memory-quality-conflicts.md` review plan with exact source paths and revisions for every formal-ID collision.
- Reuse the locked quality-audit snapshot while writing proposals, removing the remaining per-action Vault rescans.
- Give incremental transcript annotations a batch cursor plus source-order key, preventing later messages in a long session from reusing earlier formal IDs.

### Added

- Add privacy-safe memory effectiveness events and a visible Obsidian ledger without storing prompts, memory bodies, or real session IDs.
- Add formal authority ownership metadata with canonical/execution routes, verification references, freshness policy, and relevance-first tie-breaking.
- Add isolated, revision-bound promotion proposals for repeated or positively observed Decision, Error, and Workflow memory; proposals never mutate formal memory or source code.
- Add bounded task-experience bundles and `part_of_experience` graph edges, with explicit-intent expansion limited to two content-bridged companion memories from one bundle.
- Add compact `why_recalled` and `authority` explanations to CLI and Codex runtime output.

- Add source-grounded `[LEARN]` capture and formal `insight` memory with one-shot `seed`, evidence-only reinforcement, isolated candidates, lifecycle support, graph relations, and visible Obsidian counts.
- Add exploration-gated `[INSIGHT]` recall with concrete content anchors, authority-first ordering, a two-result limit, one low-confidence seed limit, and a 400-token sub-budget.
- Extend versioned runtime evaluation with one-shot Insight, ordinary/ambiguous suppression, candidate leakage, and forged assistant-source acceptance gates.
- Add Hindsight-inspired deterministic hybrid recall across lexical, structured-name, type, temporal, and explicit graph channels, fused with weighted RRF and per-result ranking evidence.
- Support narrow inventory and temporal questions such as “我有哪些个人偏好” and “最近一次错误”, while requiring content anchors for ordinary prompts.
- Generate reproducible old-memory lifecycle approval plans with an exclusive date cutoff, exact source and replacement bindings, evidence references, and a canonical SHA256 without mutating formal memory.
- Apply an explicitly approved old-memory plan through one exact-hash batch transaction with shared-source merging, one derived rebuild, proposal convergence, a batch audit receipt, and verified whole-operation rollback.
- Probe Codex Memory capability read-only and enforce a same-version, same-fixture black-box comparison contract with honest `N/A`, fixed 85/+15 claim gates, and non-scored structural capabilities.
- Stratify formal-memory quality backlog into evidence-insufficient records, lifecycle-blocked actions with alias-owner reasons, and executable recommendations.

### Changed

- Show retrieval channels in the human-readable `memory_recall.py` output while keeping compact Codex injections unchanged.

## [0.4.0] - 2026-07-18

### Added

- Codex Stop, SessionStart, and UserPromptSubmit integration with dynamic multi-memory recall.
- Incremental Codex and Claude Code transcript harvesting, plus maintained ZCode collection compatibility.
- Formal Decision, Error, Favor, Skill, Workflow, and personal-memory records with source provenance.
- Candidate isolation, annotation quality routing, error-evidence reconciliation, and privacy filters.
- Formal-memory lifecycle operations with revision checks, audit history, proposals, and rollback.
- Obsidian recall, keyword, graph, timeline, project, and cross-project indexes.
- Transactional macOS stable-runtime installation, launchd jobs, Doctor profiles, and manual rollback.
- Repeatable `install_runtime.py --verify-release` staging acceptance.
- Fresh Vault initialization of the valid empty recall index required by the stable installer.
- Audited exact-version dependency locking for the stable runtime.

### Changed

- Renamed the personalized product from Obsidian Knowledge Brain / Agent Memory Vault to Agent Memory Beacon.
- Made Codex the primary recall target; Claude Code remains collection-only and ZCode is compatibility-only.
- Moved live bindings away from the development checkout into `~/.local/share/agent-memory-beacon/runtime`.
- Updated the bundled Skill contract and release documentation.

### Attribution

- Derived from [Tubo2333/obsidian-knowledge-brain](https://github.com/Tubo2333/obsidian-knowledge-brain) under the MIT License.
