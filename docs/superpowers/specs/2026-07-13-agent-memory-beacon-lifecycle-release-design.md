# Agent Memory Beacon Lifecycle And Stable Runtime Design

**Status:** approved by the user on 2026-07-13

## Goal

Give the user explicit control over formal-memory replacement, withdrawal,
expiry, and restoration, then remove the live Codex and launchd dependency on
the development checkout. Obsidian remains the source of truth and the control
surface; inactive history remains auditable but cannot be recalled.

## Scope

- Codex remains the only dynamic-recall target.
- Claude Code remains collection-only. ZCode receives no new behavior.
- No vector database, remote service, daemon, or new formal memory type is
  introduced.
- Candidate learning, formal memory, runtime recall, and lifecycle proposals
  remain separate trust domains.
- The existing dirty worktree is not reset, cleaned, committed, or pushed.

## Lifecycle Authority Contract

### User-authorized transitions

An unambiguous user instruction may authorize one named record transition:

- `active -> superseded` when a distinct active replacement of the same type,
  scope, and project is identified.
- `active -> retracted` when the user says the memory is wrong or should be
  forgotten.
- `active -> expired` when the user explicitly expires it.
- `retracted|expired -> active` when the user explicitly restores it.
- A superseded record can be restored only after its active successor is made
  inactive, preventing two contradictory current memories.

The command defaults to preview. A mutating call requires an explicit apply
flag, one exact memory ID, a non-empty reason, and a current-revision precondition.
Bulk mutation is not part of this phase; therefore no bulk change can bypass a
preview and confirmation gate.

### Program-authorized transitions

The program may automatically expire a record only when that record already
contains an explicit `expires_at` timestamp and the timestamp has passed. It may
never infer an expiry date.

The program may detect a likely conflict or obsolete memory, but that produces a
lifecycle proposal under `04-Feedback/_lifecycle-proposals/`. A proposal is not
a formal memory and cannot change recall eligibility.

### Cascade semantics

Formal records may optionally declare `requires: [<memory-id>, ...]`. A record
with a missing or inactive required memory is omitted from the runtime recall
index and compiled context without changing or deleting its stored record. This
derived suppression is recursive and reversible: restoring every required
memory makes the dependent record eligible again.

Ordinary `source_refs` remain provenance and do not create a hard dependency.
This preserves independently supported memories when one source is withdrawn.

### Audit and rollback

Every applied transition:

1. acquires the same Vault writer lock used by the harvester;
2. validates the source record, revision precondition, transition, and target;
3. writes a byte-exact rollback snapshot under
   `04-Feedback/_rollback/lifecycle/<operation-id>/`;
4. updates the source note atomically without following symlinks;
5. rebuilds the visible index, recall index, memory graph, and compiled context;
6. verifies that inactive or cascade-suppressed IDs are absent from runtime;
7. appends a user-readable event to
   `05-Agent-Memory/lifecycle-audit.md` only after successful validation.

Failure restores the source and derived outputs from the snapshot. No lifecycle
operation physically deletes the formal record or its evidence sessions.

## Stable Runtime Installation

The live runtime root is:

`~/.local/share/agent-memory-beacon/runtime`

It contains the Python scripts, managed patch, templates, configuration,
dedicated virtual environment, and a release manifest. The development checkout
remains the editable source but is no longer required by Codex hooks or launchd.

### Transaction

1. Refuse symlinked or non-owned install targets and inspect all live bindings.
2. Stage a complete runtime beside the target.
3. Copy only the declared runtime allowlist; do not copy `.git`, tests, planning
   files, caches, transcripts, credentials, or arbitrary repository files.
4. Create a dedicated virtual environment and install declared requirements.
5. Rewrite the staged configuration so `python_path` points to the stable
   interpreter; preserve Vault and agent paths from the approved live config.
6. Compile all scripts and run the offline health profile against staging.
7. Publish the runtime directory atomically while retaining the previous runtime.
8. Snapshot and switch Codex hooks, the managed global `AGENTS.md` block, and
   launchd jobs to the stable paths.
9. Run the live health profile, a prompt-hook probe, an index rebuild, and
   launchd service checks.
10. On any failure, restore external files and launchd services, then restore the
    previous runtime bytes.

The installer writes a rollback manifest outside the runtime directory. A
successful migration preserves the previous release until the live acceptance
checks have passed. Hook trust is never forged; if Codex treats the new absolute
command as untrusted, the installer reports the exact `/hooks` review required.

## Health Entry Point

`scripts/doctor.py` provides one reproducible interface:

- `--profile quick`: imports, configuration, schema, and script compilation.
- `--profile ci`: quick checks, complete unit tests, fixture runtime evaluation,
  and repository whitespace validation; no real Vault mutation.
- `--profile live`: quick checks plus real Vault frontmatter/wikilinks, recall
  candidate isolation, Hook path ownership, launchd path/service state, and a
  non-mutating prompt-hook probe.

Output is structured JSON when `--json` is used and concise Chinese otherwise.
Every check is read-only except an explicitly requested `--repair-index` action.

## Acceptance Criteria

- Unauthorized or inferred lifecycle changes cannot alter active formal memory.
- Retraction, supersession, expiry, restoration, and recursive dependency
  suppression each have failing-then-passing regression tests.
- Inactive and cascade-suppressed records are absent from recall, graph
  expansion results, and compiled context while audit history remains present.
- Failed lifecycle writes or rebuilds restore prior bytes.
- `doctor --profile ci` is one CI-ready command and passes.
- Live Codex SessionStart, UserPromptSubmit, Stop, and both launchd jobs point
  only into the stable runtime.
- Dynamic recall and harvesting pass real probes after migration.
- The development checkout can be moved or made temporarily unavailable without
  breaking the installed runtime.
