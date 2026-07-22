# Memory Beacon Effectiveness And Authority Design

## Status

Approved for implementation on 2026-07-22. The user requested all five
improvements: effectiveness measurement, authority ownership, promotion
proposals, experience bundles, and explainable recall.

## Goal

Move Agent Memory Beacon from a system that can capture, govern, and retrieve
memory to one that can also show whether memory helped, route mature knowledge
to its strongest owner, and recover a bounded chain of related experience.

## Non-Goals

- Do not add a vector database, graph database, resident service, or extra LLM
  call.
- Do not store raw prompts, assistant responses, tool output, credentials, or
  absolute private paths in effectiveness telemetry.
- Do not automatically modify formal memory, source repositories, tests,
  permissions, or runbooks.
- Do not compile all memory or all experience bundles into `AGENTS.md`.
- Do not add a new capture annotation type.

## Prior Art Receipt

Sources inspected on 2026-07-22:

- `lopopolo/harness-engineering`: keep each truth with its owner; promote
  stable decisions to an executable owner; measure situated effectiveness.
- `vectorize-io/hindsight`: structured search traces, audit events, bounded
  metric cardinality, and retrieval-channel explanations.
- `vectorize-io/agent-memory-benchmark`: bind correctness, retrieval latency,
  context tokens, exact inputs, and reproducible result records.
- `TencentCloud/TencentDB-Agent-Memory`: keep high-level structure linked to
  lower-level evidence and measure task success and token cost.

Borrow only the architecture patterns. No source code or wording is copied.
Beacon remains local, deterministic, Markdown-authoritative, and approval
bounded.

## Architecture

### 1. Effectiveness Events

`memory_effectiveness.py` owns a privacy-safe append-only event stream at:

`04-Feedback/_logs/memory-effectiveness.jsonl`

Each event contains only:

- schema version and stable event ID;
- timestamp and hashed session ID;
- event kind: `exposure`, `feedback`, or `manual`;
- memory ID, revision, and type;
- trigger, retrieval channels, duration, and estimated tokens where known;
- outcome signal, confidence, and signal source.

It must never contain prompt text, memory body text, source excerpts, commands,
tool output, or unredacted session identifiers.

Every successful runtime injection writes one `exposure` event. The next user
message may close that exposure with a conservative weak signal only when it is
an explicit short confirmation or correction. All other responses become
`unobserved`. Weak signals never mutate formal memory. A CLI can append explicit
manual outcomes after validating the current memory revision.

The report at `04-Feedback/memory-effectiveness.md` aggregates per-revision
exposures, accepted/corrected/helpful/misleading/unobserved counts, token cost,
and last use. It labels automatic feedback as weak evidence.

### 2. Authority Metadata

All formal runtime memory types may optionally carry:

- `authority_role`: `canonical`, `rationale`, `index`, or `operationalized`;
- `authority_owner`: a short owner or system name;
- `canonical_source`: one safe relative locator;
- `enforced_by`: a deterministic list of safe locators;
- `verification_refs`: a deterministic list of safe locators;
- `verified_at`: an ISO-8601 date;
- `freshness_policy`: `manual`, `source-change`, or `weekly`.

Allowed locator prefixes are `repo:`, `file:`, `test:`, `lint:`, `runbook:`,
`system:`, `url:`, `note:`, and `memory:`. Local absolute paths, traversal,
control characters, and secret-bearing URL components are rejected.

Authority metadata is optional. Existing records retain their current revision.
When any authority field is present, all authority fields participate in the
revision digest. Lifecycle commands remain the only way to change an existing
formal record.

Recall treats `canonical` and `operationalized` records as stronger than
`rationale` and `index` only after content relevance. It never lets authority
metadata create an unanchored match. The rendered context identifies the owner
or canonical source instead of presenting a rationale as live operational
truth.

### 3. Promotion Proposals

`memory_promotion.py` scans active formal Decision, Error, and Workflow records.
It proposes a stronger owner only when the record has repeated independent
source evidence or repeated effectiveness evidence and lacks an existing
execution surface.

Proposals are written under:

`04-Feedback/_promotion-proposals/`

A proposal binds the memory ID, expected revision, recommended surface,
evidence counts, reason, and a deterministic digest. It is excluded from
collection, indexing, compilation, and recall. Applying a proposal means doing
normal repository work later; the proposal command itself cannot edit code or
formal memory.

The scanner is capped per run and idempotent. Preferences, environment facts,
and Insights are not automatically promoted to executable controls.

### 4. Experience Bundles

`experience_memory.py` derives bundles from existing formal records that share
an independent `session:` source reference. A bundle requires at least two
active records and at least two memory types. It stores only IDs, revisions,
roles, project, date, and source session reference; it does not copy session
content.

Bundles are embedded in `recall-index.json` and represented in
`memory-graph.json` with `part_of_experience` edges. They are derived data, not
formal memory and not lifecycle targets.

The `experience` retrieval channel activates only for an explicit experience
request such as “以前怎么处理”, “类似经验”, or “完整过程”, plus a concrete content
anchor. It starts from a normal content match and may add at most two related
formal records from one bundle. Inventory, vague, and ordinary queries cannot
fan out through bundles.

### 5. Explainable Recall

Every recalled result exposes a compact `why_recalled` object containing:

- trigger-independent retrieval channels;
- graph or experience path when present;
- authority role and owner/source when present.

The Codex injection renders a short `why_recalled` value and an authority route
without including raw scores or verbose trace payloads. Diagnostic CLI JSON
retains the full structured evidence. Explanations participate in the existing
token budget and fail closed if unsafe.

## Data Flow

1. `UserPromptSubmit` decides whether retrieval is needed.
2. Normal RRF retrieval produces formal results and channel evidence.
3. Explicit experience intent may add bounded bundle companions.
4. Authority metadata adjusts tie-breaking and rendering, never admission.
5. Runtime state stores one bounded pending exposure with IDs and revisions.
6. The privacy logger appends exposure and next-message feedback events.
7. Harvest/weekly maintenance rebuilds the human-readable effectiveness report
   and bounded promotion proposals.
8. Lifecycle authority remains unchanged for every formal-memory mutation.

## Safety And Failure Behavior

- Runtime and telemetry are fail-open for the user message and fail-closed for
  memory admission.
- Telemetry write failure cannot block Codex or change recall state.
- Invalid authority metadata invalidates the record at the existing read
  boundary.
- A stale proposal is rejected by its bound expected revision.
- Candidate/proposal directories are excluded by doctor and knowledge-index
  checks.
- Automatic feedback is explicitly weak and cannot cause promote, retract,
  expire, supersede, or restore.

## Acceptance Criteria

- Exposure events contain no prompt or memory body and bind exact revisions.
- Explicit confirmation/correction closes only the immediately prior exposure;
  unrelated messages are `unobserved`.
- Existing records without authority fields keep their old revisions.
- Unsafe authority locators and stale proposal revisions are rejected.
- Promotion scans are deterministic, idempotent, capped, and recall-isolated.
- Experience expansion requires explicit intent and a concrete anchor, returns
  no more than two companions, and never leaks candidate/inactive records.
- Runtime injections show concise reasons and authority routes within the
  existing token budget.
- Fixed runtime evaluation preserves precision, candidate isolation, critical
  Error recall, and latency gates.
- Full tests, CI doctor, release verification, real Vault read-only smoke, and
  live doctor all pass before stable installation is claimed complete.

