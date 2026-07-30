# Rolling Conversation Summary Design

## Status

Approved for implementation on 2026-07-31. The user requested silent,
periodically refreshed conversation summaries that replace the prior summary
for the same session and may be recalled in later conversations at lower
priority than formal memory.

## Goal

Preserve the current state and useful subject matter of long conversations,
including goals, progress, constraints, important references, and unfinished
work, without requiring every durable detail to fit Decision, Error,
Preference, Workflow, or Insight.

## Non-Goals

- Do not index raw transcripts, complete user prompts, tool output, or full
  assistant responses.
- Do not make a rolling summary a formal lifecycle memory or let it mutate a
  Decision, Error, Preference, Workflow, Skill, or Insight.
- Do not call a second model, require an API key, or start a resident service.
- Do not show the rolling summary in the rendered Codex reply.
- Do not retain every rolling revision as a recall candidate.
- Do not allow a summary-only content match to expand through formal-memory
  graph relationships or experience bundles.

## Chosen Approach

The active Codex response generates the summary when the existing
`UserPromptSubmit` hook marks a checkpoint as due. The hook adds a private
instruction asking the model to append one strict HTML comment after the normal
answer. Markdown rendering hides the comment, while the Codex transcript keeps
the source text for the existing Stop/SessionStart harvester.

This reuses the model that already holds the conversation context. It adds only
the bounded summary output tokens at checkpoint turns and avoids a separate
network request, account dependency, API charge, or recursive Codex process.

## Checkpoint Policy

`memory_runtime` owns the checkpoint schedule because it already maintains
per-session state for every substantive user prompt.

A checkpoint is due when all conditions hold:

- rolling summaries are enabled;
- the message is substantive under the existing short-confirmation filter;
- at least five substantive user messages have been observed;
- either ten substantive messages have elapsed since the last request or
  thirty minutes have elapsed since the last request;
- no checkpoint was requested on the immediately preceding substantive turn.

The time condition is evaluated on the next substantive user prompt. The
system does not wake an idle conversation or call a model in the background.
Short confirmations such as “可以”, “继续”, or “好的” neither advance the
message threshold nor trigger a checkpoint.

The checkpoint request is orthogonal to memory recall. A due checkpoint must
still be injected when normal recall is silent, and it may share one hook
response with a normal `[MEMORY_REFRESH]`.

## Hidden Transcript Contract

The model appends one bounded marker:

```text
<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1
project: <project-slug>
current_goal: <one concise sentence>
topics:
  - <specific subject>
progress:
  - <completed or verified state>
constraints:
  - <durable constraint relevant to the current work>
important_context:
  - <reference, mechanism, or fact needed to continue>
open_items:
  - <unfinished task or unresolved question>
summary: <compact coherent account of the conversation so far>
-->
```

Required fields are `current_goal`, `topics`, and `summary`. Other fields may be
empty lists. The complete decoded marker is capped at 4 KiB, each list is
capped, and each scalar is length-limited. Nested objects, unknown fields,
markup terminators, control characters, and invalid project values are
rejected.

Only markers in assistant-role messages are eligible. Code-fenced examples,
user-authored markers, managed instructions, subagent messages, and malformed
or oversized markers are ignored. Existing harvested-content redaction applies
before persistence.

The model instruction explicitly requires summarizing conversational meaning,
not copying prompts, credentials, command output, or private absolute paths.

## Persistence And Replacement

The canonical human-readable location remains:

`01-Projects/{project}/Memory/sessions/{session-note}.md`

The existing `## Session Summary` section becomes the latest effective summary.
For the same stable session ID:

- a valid rolling summary replaces the previous rolling summary;
- a later rolling summary replaces an earlier final-style summary only while
  the conversation continues and carries a newer transcript cursor;
- an explicit visible `[SESSION_SUMMARY]` remains the final summary authority
  and replaces the rolling summary from the same harvest;
- later Decision or Error writes preserve the latest summary when no new
  summary is present.

Frontmatter records summary provenance without storing old bodies:

- `summary_mode`: `rolling` or `final`;
- `summary_revision`: SHA-256 of the canonical summary payload;
- `summary_updated_at`: harvest timestamp;
- `summary_source_cursor`: the bound transcript cursor;
- `summary_checkpoint`: monotonically observed checkpoint sequence when
  available.

The latest body is authoritative for recall. Existing session notes without
these fields remain valid and are upgraded only when they receive a new
summary.

## Derived Recall Channel

Rolling and final summaries remain session evidence, not formal memory.
`knowledge_index.py` derives a separate top-level
`conversation_summaries` collection in `recall-index.json`. It does not place
them in formal `units`.

Each derived record contains only:

- deterministic ID derived from stable session ID;
- summary revision, project, date, title, and safe source note;
- bounded current goal, topics, progress, constraints, important context,
  open items, and compact summary;
- trusted search terms derived from those fields;
- `status: active` and `type: conversation_summary`.

There is at most one active record per session. Candidate paths, raw sessions,
malformed notes, missing stable session IDs, and summaries that fail redaction
or size limits are excluded.

The recall engine runs a dedicated lexical `conversation_summary` channel. It
requires a concrete content anchor, uses a lower score contribution than every
formal-memory channel, and cannot establish type-only, time-only, graph, or
experience expansion. At render time:

- at most one conversation summary is selected;
- its budget is capped at 400 estimated tokens;
- a stronger relevant formal memory wins under total token pressure;
- duplicate content already covered by selected formal memory is suppressed;
- the output label is `CONTEXT`, not `MEMORY`, `DECISION`, or `RULE`;
- the source session and summary revision remain visible to the agent for
  provenance.

## Data Flow

1. A substantive `UserPromptSubmit` increments bounded per-session checkpoint
   state.
2. When due, the hook appends a private rolling-summary instruction to any
   normal recall context.
3. Codex answers normally and adds the hidden marker.
4. Stop or SessionStart harvest reads only the new assistant transcript slice.
5. The parser validates and sanitizes the latest valid marker.
6. The session writer atomically replaces the effective summary and provenance
   fields.
7. The knowledge-index rebuild derives one conversation-summary record for the
   session.
8. A later related prompt may retrieve at most one low-priority `CONTEXT`
   result.

## Failure Behavior

- Hook, parsing, persistence, indexing, and recall remain fail-open for the
  user request.
- A missed or malformed checkpoint does not erase the previous summary.
- A checkpoint request may be retried only after the minimum retry interval;
  it cannot create a marker loop on every turn.
- Concurrent harvest keeps the existing lock, transcript cursor, atomic-write,
  and post-index cursor-commit contracts.
- Invalid summary data is excluded rather than downgraded into formal memory.
- Legacy recall indexes without `conversation_summaries` remain readable.
- Candidate isolation and formal lifecycle authority remain unchanged.

## Configuration

Default configuration:

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

Invalid values fail configuration validation. Disabling the feature stops new
checkpoint requests and summary recall but preserves existing session notes.

## Acceptance Criteria

- Checkpoints are silent in rendered Markdown and use no additional model
  invocation.
- Short confirmations do not advance or trigger the schedule.
- A due checkpoint is requested even when formal-memory recall has no match.
- Parser admission is assistant-only, bounded, strict, and secret-redacted.
- Repeated summaries for one session update one session note and one derived
  recall record instead of accumulating duplicates.
- An explicit final summary wins over a rolling marker from the same harvest.
- A missing or rejected new marker preserves the previous summary.
- Conversation summaries never enter formal units, lifecycle commands,
  adaptive promotion, graph expansion, or `AGENTS.md` compilation.
- Recall requires a concrete content match, returns at most one `CONTEXT`
  record, stays below formal memory, and respects the 400-token sub-budget.
- Existing indexes, session notes, Codex hooks, Claude/ZCode collection, and
  disabled-feature configurations remain backward compatible.
- Unit, integration, long-session, fixed runtime evaluation, source CI,
  staged release verification, stable installation, and live Doctor checks all
  pass before completion is reported.
