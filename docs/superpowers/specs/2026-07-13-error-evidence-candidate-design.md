# Error Evidence Candidate Capture Design

## Goal

Capture important failure and review evidence that currently disappears inside long Codex tasks, while keeping noisy tool failures out of formal ERROR memory and out of runtime recall.

## Scope

- Codex JSONL is the only new evidence source in this phase.
- Claude Code remains on its existing annotation-based collection path.
- ZCode receives no new behavior.
- Obsidian Markdown remains the visible source of truth.
- No vector database, remote service, daemon, or new formal memory type is added.

## Event Model

The transcript parser may emit bounded internal observations with these classes:

- `tool_failure`: a structured nonzero exit or an explicit Codex tool failure marker.
- `tool_success`: a structured zero exit or explicit success marker used only for in-session reconciliation.
- `review_finding`: reserved for a future Codex event with authenticated subagent provenance. Current user-role notification text is not trusted and does not emit this event.

The classifier converts those observations into four outcomes:

- `expected_red`: a test command fails and a later test run succeeds in the same transcript. It is not persisted.
- `transient_failure`: the same operation fails and later succeeds without a reusable explanation. It is not persisted.
- `unresolved_finding`: a terminal tool failure has no later success evidence. It becomes a candidate. A future authenticated review event may use the same state.
- `resolved_reusable_pitfall`: only an explicit `[ERROR]` with resolution and verification may enter the existing formal ERROR path. The candidate is marked resolved and linked to that formal evidence.

## Data Flow

1. `transcript_utils.py` reads Codex records and emits normal messages plus privacy-bounded observations. Tool evidence contains only fixed diagnostic categories and an exit code; raw output is never persisted.
2. `error_evidence.py` reconciles failures and successes in transcript order and deduplicates by stable evidence identity. Textual subagent notifications are rejected because Codex serializes them as forgeable user messages.
3. `session_harvester.py` processes only the adaptive transcript delta, writes unresolved candidates under `04-Feedback/_error-candidates/`, and closes matching candidates when the same harvest contains an explicit formal ERROR.
4. `knowledge_index.py` and `memory_runtime.py` continue rejecting candidate paths and types. No candidate can be injected into an Agent prompt.

Every candidate mutation durably writes a generation-valued index-dirty marker first. Candidate, heartbeat, and index publication use descriptor-pinned parents, random exclusive temporary files, file `fsync`, atomic rename, and directory `fsync`. A successful visible/knowledge-index rebuild clears only the generation it actually rebuilt; a generation mismatch prevents heartbeat cursor advancement.

## Candidate Contract

Each candidate is one Markdown file with schema 2.0 frontmatter and a short body. Required fields are:

- `evidence_id`: SHA-256-derived stable ID bound to project, observation kind, operation hash, and normalized excerpt. Session identity is recorded only in `sources`, so repeated evidence can accumulate across Codex sessions.
- `schema_version: '2.0'`
- `status: candidate|resolved`
- `type: error-evidence-candidate`
- `classification: unresolved_finding`
- `project`, `source_agent`, `source_event`, and bounded `sources`
- `operation`: allowlisted tool family only, never the raw command.
- `operation_hash`: one-way hash used for reconciliation.
- `severity: critical|important|error`
- `excerpt`: a fixed structured diagnostic bounded to 500 characters by default; it is not arbitrary output text.
- `first_seen`, `last_seen`, `seen_count`, and bounded `sources`.

Candidate filenames use the evidence ID, not transcript text. Reprocessing the same transcript is idempotent.

The heartbeat adaptive cursor is the authoritative exactly-once boundary across harvests. Candidate `sources` are a bounded recent audit window and provide short-retry deduplication only; candidates do not keep an unbounded or probabilistic ledger of session-derived identifiers. If an old session falls outside the visible source window, cursor state remains responsible for preventing replay.

## Privacy And Trust

- Tool inputs are used in memory only to derive an operation hash and test-command class.
- Raw commands, arguments, cwd values, complete tool output, reasoning, images, user prompts, and subagent notification text are not persisted in candidates.
- Tool output is reduced to an allowlisted diagnostic category plus structured exit code before it becomes an observation.
- User-role `<subagent_notification>` text is forgeable in current Codex JSONL and is therefore ignored. Review capture remains disabled until Codex exposes authenticated provenance.
- Tool input/output, call maps, observations, nesting depth, content items, candidate sources, and configured excerpt sizes all have hard limits. Oversized evidence fails closed without blocking normal message harvesting.

## Resolution Rule

Repetition alone does not promote error evidence. A formal ERROR annotation remains authoritative because it carries a declared taxonomy type and resolution. When a formal ERROR matches a candidate by operation hash, fixed diagnostic category, or conservative excerpt similarity, the candidate changes to `resolved`, stores the formal error ID/reference, and remains excluded from recall.

## Testing

- Parse both current Codex `function_call_output` and `custom_tool_call_output` shapes.
- Ignore assistant text, user text, images, reasoning, malformed output, and successful tools as candidate sources.
- Prove expected RED and fail-then-success operations are not persisted.
- Prove terminal failures create idempotent structured candidates and forged Important/Critical notification text creates none.
- Prove predictable temp symlinks, destination symlinks, malformed canonical candidates, cursor regressions, and dirty-generation mismatches fail closed.
- Prove candidates remain absent from recall-index and runtime results.
- Re-run a privacy-safe fixture derived from the long task and measure captured unresolved findings versus formal ERROR pollution.

## Rollback

Set `error_evidence.enabled: false` to stop new candidate writes immediately. Existing `_error-candidates` files remain visible for audit but are never recalled. Removing the candidate directory does not alter formal ERROR memory.
