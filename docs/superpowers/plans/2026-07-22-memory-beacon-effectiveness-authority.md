# Memory Beacon Effectiveness And Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add privacy-safe memory effectiveness measurement, authority ownership, isolated promotion proposals, bounded experience-chain recall, and compact recall explanations.

**Architecture:** Extend the existing schema, hook event log, recall index, and graph instead of adding a storage service. New machine artifacts remain derived or proposal-only; Obsidian formal memory and lifecycle commands remain authoritative.

**Tech Stack:** Python standard library, PyYAML already in the locked runtime, unittest, Obsidian Markdown, JSON/JSONL, existing deterministic RRF.

## Global Constraints

- No vector database, graph database, resident service, or extra LLM call.
- No raw prompt, assistant response, tool output, credential, or absolute private path in effectiveness telemetry.
- Automatic feedback and promotion scans cannot mutate formal memory or source repositories.
- Existing formal records without authority metadata retain their current revision.
- Experience expansion requires explicit experience intent plus a concrete content anchor and adds at most two records.
- Use `/Users/a0000/venv/bin/python` for source tests because the repository-local venv interpreter is unavailable.
- Preserve unrelated changes in the dirty worktree.

---

### Task 1: Privacy-Safe Effectiveness Event Model

**Files:**
- Create: `scripts/memory_effectiveness.py`
- Create: `tests/test_memory_effectiveness.py`
- Modify: `scripts/config.py`
- Modify: `scripts/config.example.yaml`

**Interfaces:**
- Produces: `build_exposure_event(...) -> dict`, `classify_feedback(prompt) -> tuple[str, float]`, `aggregate_events(events) -> dict`, `write_effectiveness_report(vault, config) -> dict`.
- Consumes: `memory_runtime.PrivacyLogger` only through dependency injection to avoid an import cycle.

- [ ] **Step 1: Write failing event and privacy tests**

Test stable event IDs, exact revision binding, allowed fields, prompt/body exclusion,
explicit short confirmation/correction classification, unrelated `unobserved`,
deduplication, and deterministic aggregation.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `/Users/a0000/venv/bin/python -m unittest tests.test_memory_effectiveness`

Expected: import failure because `memory_effectiveness.py` does not exist.

- [ ] **Step 3: Implement the event model and report writer**

Use schema `1.0`, SHA-256 event IDs, JSON-safe scalar fields, bounded lists, and
an atomic Markdown report. The report must label automatic feedback as weak
evidence and group results by memory ID plus revision.

- [ ] **Step 4: Add configuration defaults and rerun focused tests**

Defaults:

```yaml
memory_effectiveness:
  enabled: true
  event_log_path: "04-Feedback/_logs/memory-effectiveness.jsonl"
  report_path: "04-Feedback/memory-effectiveness.md"
  feedback_window_minutes: 15
  max_report_items: 100
```

Expected: all effectiveness tests pass.

---

### Task 2: Runtime Exposure And Feedback Capture

**Files:**
- Modify: `scripts/memory_runtime.py`
- Modify: `tests/test_memory_runtime.py`
- Modify: `scripts/session_harvester.py`
- Modify: `tests/test_session_harvester.py`

**Interfaces:**
- Consumes: Task 1 event constructors and report writer.
- Produces: state field `pending_effectiveness` with one bounded prior exposure.

- [ ] **Step 1: Write failing runtime tests**

Cover successful exposure events, immediate positive/correction feedback,
unobserved closure, expiry, no event for no-match, no raw prompt in state/log,
and fail-open behavior when the effectiveness log cannot be written.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `/Users/a0000/venv/bin/python -m unittest tests.test_memory_runtime tests.test_session_harvester`

Expected: failures for missing pending exposure and report refresh behavior.

- [ ] **Step 3: Implement bounded state and event writes**

Close the previous exposure before making the current recall decision. Store
only IDs, revisions, types, retrieval channels, trigger, timestamp, and token
count. Append through the existing pinned `PrivacyLogger` contract.

- [ ] **Step 4: Refresh the visible report after a successful harvest**

Report failure must print a warning only and must not roll back transcript
cursor or formal-memory writes.

- [ ] **Step 5: Rerun focused tests**

Expected: runtime and harvester suites pass.

---

### Task 3: Generic Authority Contract

**Files:**
- Create: `scripts/memory_authority.py`
- Create: `tests/test_memory_authority.py`
- Modify: `scripts/memory_schema.py`
- Modify: `tests/test_memory_schema.py`
- Modify: `scripts/knowledge_index.py`
- Modify: `tests/test_knowledge_index.py`

**Interfaces:**
- Produces: `normalize_authority_metadata(record) -> dict`, `authority_revision_payload(record) -> dict`, `authority_rank(record) -> int`, `authority_route(record) -> str`.
- Consumes: optional authority fields from every formal memory type.

- [ ] **Step 1: Write failing locator and revision tests**

Test allowed prefixes, traversal/control/credential rejection, deterministic
list ordering, ISO date validation, valid role/policy values, legacy revision
compatibility, and authority-aware revision changes.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `/Users/a0000/venv/bin/python -m unittest tests.test_memory_authority tests.test_memory_schema`

Expected: failures for missing authority normalization and revision payload.

- [ ] **Step 3: Implement optional authority normalization**

Only append the `authority-v1` revision payload when at least one authority
field exists. Reject malformed partial metadata at all runtime read boundaries.

- [ ] **Step 4: Preserve authority fields through parsers and indexing**

Project frontmatter records and adaptive Markdown sections must round-trip the
same normalized authority metadata. Existing records must continue validating
without migration.

- [ ] **Step 5: Rerun focused tests**

Expected: authority, schema, and index tests pass.

---

### Task 4: Isolated Promotion Proposals

**Files:**
- Create: `scripts/memory_promotion.py`
- Create: `tests/test_memory_promotion.py`
- Modify: `scripts/config.py`
- Modify: `scripts/config.example.yaml`
- Modify: `scripts/knowledge_index.py`
- Modify: `scripts/doctor.py`
- Modify: `tests/test_doctor.py`

**Interfaces:**
- Produces: `scan_promotion_opportunities(index, effectiveness, config) -> list[dict]`, `write_proposals(vault, proposals, apply=False) -> dict`.
- Consumes: active recall units, effectiveness aggregate, exact memory revisions.

- [ ] **Step 1: Write failing eligibility and isolation tests**

Test repeated evidence threshold, exclusion of Preference/Environment/Insight,
existing `enforced_by` suppression, deterministic recommended surfaces,
stable digest, stale revision rejection, cap, idempotence, and candidate-path
exclusion.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `/Users/a0000/venv/bin/python -m unittest tests.test_memory_promotion tests.test_doctor`

Expected: missing module and missing candidate root failures.

- [ ] **Step 3: Implement preview-first scanner and proposal writer**

Proposal frontmatter binds `memory_id`, `expected_revision`, evidence counts,
recommended surface, reason, status `candidate`, and SHA-256 digest. No command
in this module may edit code or formal memory.

- [ ] **Step 4: Add config and isolation checks**

Defaults:

```yaml
memory_promotion:
  enabled: true
  proposal_dir: "04-Feedback/_promotion-proposals"
  min_source_count: 3
  min_exposure_count: 2
  max_proposals_per_run: 10
```

- [ ] **Step 5: Rerun focused tests**

Expected: promotion and doctor tests pass.

---

### Task 5: Derived Experience Bundles

**Files:**
- Create: `scripts/experience_memory.py`
- Create: `tests/test_experience_memory.py`
- Modify: `scripts/knowledge_index.py`
- Modify: `scripts/memory_recall.py`
- Modify: `tests/test_memory_recall.py`
- Modify: `tests/test_knowledge_index.py`

**Interfaces:**
- Produces: `derive_experience_bundles(units) -> list[dict]`, `infer_experience_intent(query) -> bool`, `expand_experience_results(...) -> tuple[list, dict]`.
- Consumes: active formal units and `session:` source refs.

- [ ] **Step 1: Write failing bundle derivation tests**

Test two-type minimum, exact revision membership, project isolation, stable IDs,
no copied body text, inactive/candidate absence, and `part_of_experience` edges.

- [ ] **Step 2: Write failing retrieval admission tests**

Test explicit experience intent plus anchor, vague suppression, inventory
suppression, maximum two companions, one-bundle limit, and deterministic
`experience` retrieval evidence.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `/Users/a0000/venv/bin/python -m unittest tests.test_experience_memory tests.test_knowledge_index tests.test_memory_recall`

Expected: missing module and missing experience channel failures.

- [ ] **Step 4: Implement derived bundles and bounded expansion**

Embed bundles under `experience_bundles` in the existing schema `2.0` index.
Use existing formal records as returned results; bundles never become runtime
memory units or lifecycle targets.

- [ ] **Step 5: Rerun focused tests**

Expected: experience, index, and recall tests pass.

---

### Task 6: Compact Recall Explanations And Authority Ordering

**Files:**
- Modify: `scripts/memory_recall.py`
- Modify: `scripts/memory_runtime.py`
- Modify: `tests/test_memory_recall.py`
- Modify: `tests/test_memory_runtime.py`
- Modify: `tests/fixtures/memory_runtime/index.json`
- Modify: `tests/fixtures/memory_runtime/graph.json`
- Modify: `tests/fixtures/memory_runtime/cases.json`

**Interfaces:**
- Produces: per-result `why_recalled` object and compact rendered
  `why_recalled:` / `authority:` fields.
- Consumes: retrieval evidence, experience evidence, and normalized authority metadata.

- [ ] **Step 1: Write failing explanation and ordering tests**

Test stable channel names, experience path, relevance-before-authority ordering,
rationale warnings, canonical/operationalized routing, sanitization, and token
budget truncation.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `/Users/a0000/venv/bin/python -m unittest tests.test_memory_recall tests.test_memory_runtime`

Expected: missing explanation and authority rendering failures.

- [ ] **Step 3: Implement compact structured explanations**

Authority rank may break ties only after fused content score. Runtime text must
omit raw scores and render safe locators only.

- [ ] **Step 4: Extend deterministic fixtures**

Add authority, experience, vague-suppression, candidate-lure, and privacy cases.
Keep all existing hard gates.

- [ ] **Step 5: Run the fixed evaluation**

Run: `/Users/a0000/venv/bin/python scripts/evaluate_memory_runtime.py --fixtures tests/fixtures/memory_runtime`

Expected: precision and critical Error recall remain `1.0`; irrelevant trigger,
candidate leak, assistant-source acceptance, duplicate memory, and deleted
residual counts remain `0`; p95 remains below the existing gate.

---

### Task 7: Product Integration, Documentation, And Release

**Files:**
- Modify: `scripts/reporter.py`
- Modify: `scripts/session_harvester.py`
- Modify: `scripts/install_runtime.py`
- Modify: `scripts/doctor.py`
- Modify: `README.md`
- Modify: `references/architecture.md`
- Modify: `references/workflow.md`
- Modify: `templates/vault/README.md`
- Modify: `templates/vault/用户手册.md`
- Modify: `CHANGELOG.md`
- Add tests beside every modified integration.

**Interfaces:**
- Consumes: Tasks 1-6.
- Produces: stable runtime package, live report/proposal paths, documentation,
  and rollback manifest.

- [ ] **Step 1: Add release allowlist, import, config-sanitizer, and doctor tests**

Run the focused installer and doctor suites and verify they fail before adding
new modules and config sections to the release contract.

- [ ] **Step 2: Integrate weekly/harvest reports and bounded proposal scans**

Dry-run must never write. Normal weekly execution may write only isolated
proposal files and the effectiveness report.

- [ ] **Step 3: Update documentation and prior-art boundary**

Document event privacy, weak-signal limits, authority semantics, proposal
approval boundaries, experience-intent admission, diagnostics, rollback, and
how to verify the feature in Obsidian.

- [ ] **Step 4: Run focused and full verification**

Run:

```bash
/Users/a0000/venv/bin/python -m unittest discover -s tests -q
/Users/a0000/venv/bin/python scripts/doctor.py --profile ci --json
/Users/a0000/venv/bin/python scripts/install_runtime.py --verify-release
git diff --check
```

Expected: zero failures, CI status `pass`, release verification succeeds, and
no whitespace errors.

- [ ] **Step 5: Verify the real Vault read-only**

Check latest Error count `1`, ambiguous repair count `0`, candidate/proposal
leaks `0`, legacy records still validate, experience expansion is bounded, and
the effectiveness report contains no prompt/body fields.

- [ ] **Step 6: Install and verify stable runtime**

Run the transactional installer, record release ID and rollback manifest, then
run stable `doctor.py --profile live --json` and stable smoke queries.

Expected: all required live checks pass and installed modules match the final
source release.

