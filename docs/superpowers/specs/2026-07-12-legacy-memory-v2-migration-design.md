# Legacy Memory V2 Migration Design

> Date: 2026-07-12
> Status: approved for implementation by the user
> Parent specification: `2026-07-12-agent-memory-beacon-foundation-design.md`

## Goal

Upgrade the existing Obsidian memory content to the Agent Memory Beacon v2
runtime contract without deleting historical session evidence. The migration
must remove duplicate runtime facts, isolate candidates, make lifecycle state
explicit, repair safe project aliases, and remain fully reversible.

## Non-Goals

- Do not rewrite or summarize raw transcripts.
- Do not delete historical session notes.
- Do not infer a new project when no reliable explicit project evidence exists.
- Do not implement the Codex `UserPromptSubmit` runtime in this migration.
- Do not use an external LLM or send Vault content over the network.

## Three-Layer Model

1. **Evidence**: session notes remain immutable historical evidence. They may
   contain intermediate or superseded conclusions and are not runtime units.
2. **Formal memory**: project `decisions.md` and `pitfalls.md`, personal memory,
   skill routing rules, and workflow rules contain canonical records with stable
   IDs and lifecycle metadata.
3. **Candidates**: candidate notes remain review material. They are never
   emitted into the runtime recall index, keyword runtime map, compiled context,
   or memory graph runtime units.

## Formal Record Contract

Every runtime unit uses schema `2.0` and contains:

```yaml
id: decision-<stable-hash>
revision: <sha256-of-visible-state>
type: decision
status: active
project: agent-memory-beacon
scope: project
title: one-line fact
summary: supporting context
date: 2026-07-12
source_refs:
  - session:<session-id>
aliases: []
```

Runtime types are limited to `decision`, `error`, `preference`,
`project_rule`, `environment`, `skill`, and `workflow`. Runtime status is
limited to `active`; `superseded`, `retracted`, `expired`, `candidate`,
`promoted`, and `rejected` remain auditable but are not injectable.

## Canonical Identity And Deduplication

- Existing explicit record IDs win.
- Legacy records without IDs receive a deterministic ID from canonical project,
  type, normalized title, and normalized supporting content.
- Exact facts from sessions and aggregate notes share one canonical ID.
- The canonical unit retains all source references and prior generated IDs as
  aliases.
- Source preference for display is formal aggregate note, then formal adaptive
  memory, then session evidence.
- A content revision changes when title, summary, status, scope, or project
  changes; it does not change the stable ID already persisted in Markdown.

## Project Routing

- `github-obsidian-knowledge-brain` and `obsidian-knowledge-brain` canonicalize
  to `agent-memory-beacon`.
- Record-level explicit project metadata overrides the containing session.
- If a legacy session has exactly one explicit non-container project, untagged
  records in that session inherit that project.
- Placeholder project `slug` is quarantined. Exact copies already present in a
  real project are omitted; unique placeholder records remain in the backup and
  are written as `retracted` with reason `legacy_placeholder_route`.
- Ambiguous records keep their existing project. The migration must not guess.

## Lifecycle Rules

- Valid durable records default to `active`.
- Empty values, examples, parser fragments, and oversized transcript leakage are
  `retracted` with a machine-readable reason.
- Obvious one-run review verdicts and reviewer/controller coordination records
  are `expired`; they remain in evidence sessions but not runtime memory.
- Within one session, a final `Ready` conclusion supersedes earlier `Not Ready`
  conclusions for the same subject.
- Candidate notes matching a formal promoted memory keep status `promoted` and
  are excluded from runtime.
- Question-only, one-off task requests, platform-injected subagent output, and
  non-Skill identifiers are marked `rejected` during legacy candidate cleanup.

## Generated Formal Notes

Project aggregate notes are rewritten deterministically from canonical records.
Their frontmatter is authoritative for runtime indexing; their body is a human
view containing status, context or resolution, and source links. Personal,
Skill, and Workflow formal notes are regenerated from valid promoted candidate
records after evidence cleaning.

Session files remain in place. Their frontmatter project aliases are normalized
only when safe, but their historical decisions and intermediate outcomes are not
deleted.

## Runtime Index And Compiler

- `recall-index.json` becomes schema `2.0`.
- Sessions, aggregate wrapper notes, candidates, rejected records, and inactive
  records are excluded.
- The runtime index performs path, type, and status checks independently of the
  migration.
- The compiled `AGENTS.md` recent memory section reads active canonical project
  records rather than raw session frontmatter.
- Project counts report active formal memory, not duplicate historical rows.

## Migration Safety

The CLI has preview, apply, and rollback modes. Preview performs no writes.
Apply acquires the existing harvester and scanner writer guard, hashes all input
files, writes a private backup and manifest under
`04-Feedback/_rollback/memory-v2/<migration-id>`, verifies the backup, and then
uses atomic file replacement. Any input drift aborts before mutation.

Rollback restores exact backed-up bytes and removes files that were absent before
the migration. Generated indexes and compiled context are rebuilt after either
apply or rollback.

## Post-Schema Authority And Idempotence

- A structurally valid schema `2.0` aggregate record is authoritative, including
  incomplete historical content. The migrator must not reconstruct or replace
  that identity from session evidence.
- Existing schema `2.0` candidates and Personal, Skill, and Workflow stores are
  not legacy inputs and must not be reclassified by repeated migration runs.
- Only genuine legacy records participate in legacy classification and
  deduplication.
- A record-level canonical project controls its aggregate destination. Safe
  relocation preserves ID, revision, lifecycle status, content, dependencies,
  expiry, aliases, and source references.
- Repeating preview after a successful apply must produce zero writes. Any
  non-zero repeat plan is a migration regression or concurrent-input drift and
  must be investigated before another apply.

## Acceptance Criteria

- Candidate runtime units: `0`.
- Session and aggregate-wrapper runtime units: `0`.
- Runtime unit duplicate identities: `0`.
- Every runtime unit has ID, revision, active status, scope, project, and source
  references.
- `slug` contributes no active runtime units.
- The Phase A query returns the final active state without stale `Not Ready`
  results.
- The GitHub username and AppStorage false candidates do not appear in recall.
- Frontmatter validation and Wiki-link validation pass.
- Existing harvesting, Claude, ZCode, profile sync, brand migration, and launchd
  tests remain green.
