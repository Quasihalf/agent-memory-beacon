# Changelog

All notable changes to Agent Memory Beacon are documented here.

## [Unreleased]

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
