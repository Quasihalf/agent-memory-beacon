# Learn / Insight Memory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a production-quality Codex `LEARN` pipeline that preserves a one-shot valuable idea as a formal `insight` seed, grounds it in user evidence, reinforces it without rewriting its meaning, links it in Obsidian, and recalls at most two relevant insights only in exploratory contexts.

**Architecture:** A focused `insight_memory.py` owns annotation parsing, evidence grounding, deterministic admission, candidate persistence, formal seed creation, deduplication, and reinforcement. Existing schema, harvester, knowledge-index, graph, hybrid recall, memory-runtime, compiler, installer, and doctor layers gain narrowly scoped `insight` support; candidate isolation remains enforced at collection, indexing, and runtime boundaries.

**Tech Stack:** Python 3 standard library, PyYAML already locked by the repository, Obsidian Markdown, JSON indexes, Codex JSONL hooks, `unittest`, existing Agent Memory Beacon safety and lifecycle helpers.

## Global Constraints

- Codex on macOS is the only dynamic Learn/Insight runtime in this phase.
- Obsidian Markdown remains the authoritative store.
- No external LLM call, database, vector service, Docker container, or daemon is added.
- A first high-value, source-grounded idea becomes `maturity: seed` immediately; repetition is never a prerequisite.
- Candidate paths, candidate types, and non-active statuses never enter runtime recall.
- Assistant-only speculation cannot become a formal seed.
- Reinforcement may append evidence and derive maturity only; it cannot rewrite insight, transfer, boundary, project, or scope.
- Automatic recall emits at most two insights and spends at most 400 estimated tokens on insight content.
- Machine labels and field names remain English; human-readable content may be Chinese.
- Existing dirty-worktree changes are preserved; commits stage only files named by the current task.

---

## File Structure

### New files

- `scripts/insight_memory.py`: Learn annotation parser, source grounding, admission, candidate/formal persistence, deduplication, and evidence-only reinforcement.
- `tests/test_insight_memory.py`: focused domain and persistence tests for the learner.

### Modified production files

- `scripts/transcript_utils.py`: expose a bounded pre-cursor `context_messages` window for evidence grounding without replaying old messages.
- `scripts/memory_schema.py`: add formal `insight` type, maturity/insight fields, revision coverage, parser contract, and source allowlist.
- `scripts/config.py` and `scripts/config.example.yaml`: validate and document `insight_memory` settings.
- `scripts/session_harvester.py`: invoke the learner once per non-subagent Codex delta, report visible outcomes, and include writes in transaction/index decisions.
- `scripts/knowledge_index.py`: discover `insights.md`, parse formal insight units, include structured fields in terms, and materialize graph relations.
- `scripts/memory_recall.py`: recognize explicit insight intent and rank structured insight fields through existing RRF channels.
- `scripts/memory_runtime.py`: gate automatic insight use to exploratory prompts, cap results at two, and render `[INSIGHT]` with maturity and boundary.
- `scripts/compiler.py`: install the `[LEARN]` sensory protocol without statically compiling all insight bodies.
- `scripts/doctor.py` and `scripts/install_runtime.py`: validate source/runtime parity and Insight candidate isolation.
- `README.md`, `references/architecture.md`, `references/workflow.md`, `CHANGELOG.md`: document user-visible behavior and rollback.

### Modified tests

- `tests/test_end_to_end_agents.py`: bounded Codex pre-cursor context-window behavior.
- `tests/test_memory_schema.py`: formal record validation, revision, and path checks.
- `tests/test_session_harvester.py`: incremental end-to-end seed creation and visible output.
- `tests/test_knowledge_index.py`: candidate exclusion, formal indexing, graph relations.
- `tests/test_memory_recall.py`: explicit inventory and content-anchored insight retrieval.
- `tests/test_memory_runtime.py`: exploration gate, two-item cap, token budget, precedence, and rendering.
- `tests/test_compiler.py`, `tests/test_doctor.py`, `tests/test_install_runtime.py`: managed protocol and release validation.

---

### Task 1: Insight Configuration And Formal Schema

**Files:**
- Modify: `scripts/config.py`
- Modify: `scripts/config.example.yaml`
- Modify: `scripts/memory_schema.py`
- Modify: `scripts/memory_lifecycle.py`
- Test: `tests/test_memory_schema.py`
- Test: `tests/test_memory_runtime.py`
- Test: `tests/test_memory_lifecycle.py`

**Interfaces:**
- Consumes: existing `normalize_formal_record`, `memory_revision`, `parse_active_formal_section`, `runtime_source_path`, and `safe_vault_path`.
- Produces: runtime memory type `insight`; parser kind `insight`; normalized fields `maturity`, `novelty`, `transfer`, `boundary`, `origin`, `supports`, `operationalized_as`, and `related_to`; validated `cfg['insight_memory']`; lifecycle discovery and transition support for `insights.md`.

- [ ] **Step 1: Write failing schema and config tests**

Add tests that construct a normalized active insight and require all behavior-affecting fields to change its revision:

```python
def test_active_insight_requires_formal_path_and_revision_covers_boundaries(self):
    record = normalize_formal_record(
        {
            "id": "insight-one-shot-fusion",
            "status": "active",
            "scope": "project",
            "project": "demo",
            "title": "互补弱通道可以形成稳定系统",
            "summary": "多个互补通道可通过排名融合提高稳定性",
            "maturity": "seed",
            "novelty": "不依赖单一路径",
            "transfer": ["记忆召回", "审查聚合"],
            "boundary": "通道共享相同偏置时不适用",
            "origin": "user",
            "source_refs": ["session:source-1"],
            "path": "05-Agent-Memory/insights",
            "source_note": "note:05-Agent-Memory/insights",
        },
        memory_type="insight",
    )
    self.assertTrue(is_valid_runtime_record(record))
    changed = dict(record, boundary="来源完全相关时不适用", revision="")
    changed["revision"] = memory_revision(changed)
    self.assertNotEqual(record["revision"], changed["revision"])
```

Add config tests that require the documented defaults and reject a non-mapping, absolute paths, thresholds outside `0..1`, non-positive limits, and `max_auto_recall > 2`.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_memory_schema \
  tests.test_memory_runtime.MemoryRuntimeConfigTests \
  tests.test_memory_lifecycle -v
```

Expected: failures because `insight` is not a runtime type and `insight_memory` is not configured.

- [ ] **Step 3: Implement schema fields and config validation**

Add exact constants and normalized collection handling:

```python
INSIGHT_MATURITIES = frozenset({"seed", "reinforced"})
INSIGHT_SCALAR_FIELDS = ("maturity", "novelty", "boundary", "origin")
INSIGHT_LIST_FIELDS = ("transfer", "supports", "operationalized_as", "related_to")
```

Extend revision construction with a stable `insight-v1` JSON payload when `type == 'insight'`. Extend formal parsing with `kind == 'insight'`; require `maturity`, `origin`, `Insight`, `Novelty`, `Transfer`, and `Boundary`, and accept only `seed|reinforced` plus `user|jointly_validated`. Extend `runtime_source_path` with:

```python
"05-Agent-Memory/insights": {"insight"}
```

Add `INSIGHT_MEMORY_DEFAULTS` and `_configure_insight_memory(cfg)` with defaults copied verbatim from the design, resolving both candidate and formal paths under the Vault.

Extend lifecycle formal-store discovery and adaptive kind detection with `insight_memory`, default path `05-Agent-Memory/insights.md`, and parser kind `insight`. Add a lifecycle test that creates an active seed, previews it by ID, applies a retract transition through the existing explicit transition API, and verifies the revised inactive section remains auditable and absent from runtime recall.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add scripts/config.py scripts/config.example.yaml scripts/memory_schema.py \
  scripts/memory_lifecycle.py tests/test_memory_schema.py \
  tests/test_memory_runtime.py tests/test_memory_lifecycle.py
git commit -m "feat: define formal insight memory schema"
```

---

### Task 2: Source-Grounded One-Shot Insight Learner

**Files:**
- Create: `scripts/insight_memory.py`
- Create: `tests/test_insight_memory.py`
- Modify: `scripts/transcript_utils.py`
- Test: existing Codex transcript parser tests under `tests/test_end_to_end_agents.py` and `tests/test_session_harvester.py`

**Interfaces:**
- Consumes: parsed transcript dictionaries with `messages` and optional `context_messages`; existing safety helpers `redact_sensitive`, `safe_vault_path`, `durable_atomic_write`, `split_frontmatter_text`, and stable source helpers.
- Produces: `process_insight_memory(cfg, parsed, project, session_id, date_str) -> dict`; `extract_learn_annotations(messages, context_messages, default_project) -> list[dict]`; `write_insight_conflict_proposal(cfg, existing, proposed, evidence) -> str`; result counts `candidates`, `seeds`, `reinforced`, `formal`, `updated`, `proposals`, and `items`.

- [ ] **Step 1: Write failing parser, grounding, and lifecycle tests**

Cover these independent behaviors in `tests/test_insight_memory.py`:

```python
def test_one_source_complete_user_insight_becomes_formal_seed(self):
    parsed = {
        "messages": [
            {"role": "user", "text": "好的启发可能只是一瞬间，不一定会重复"},
            {"role": "assistant", "text": (
                "[LEARN:启发价值与重复次数应分开判断| "
                "novelty:一次性灵感也可能有长期价值| "
                "transfer:适用于创意、研究假设和架构备选方案| "
                "boundary:普通进度和随口猜想不适用| "
                "evidence:好的启发可能只是一瞬间| source:user| "
                "project:demo| scope:project]"
            )},
        ]
    }
    result = process_insight_memory(self.cfg, parsed, "demo", "session-1", "2026-07-22")
    self.assertEqual(result["seeds"], 1)
    self.assertEqual(result["candidates"], 0)
```

Also prove: user-authored fake tags are ignored; evidence absent from all user/context messages cannot become formal; incomplete but plausible annotations become candidate; noise is rejected; same session is idempotent; a second session produces `reinforced`; reinforcement leaves principle, transfer, boundary, project, and scope byte-equivalent; a changed boundary writes an isolated lifecycle proposal rather than overwriting the formal seed.

- [ ] **Step 2: Write the bounded pre-cursor context test**

Create a JSONL fixture where the user evidence ends before the saved byte cursor and `[LEARN]` appears after it. Require `parse_transcript_since` to return the annotation in `messages`, the prior user text only in `context_messages`, and no old text in `text`.

- [ ] **Step 3: Run the tests and verify RED**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_insight_memory \
  tests.test_end_to_end_agents \
  tests.test_session_harvester -v
```

Expected: import failure for `insight_memory` and missing `context_messages` assertions.

- [ ] **Step 4: Implement bounded transcript context**

In `_parse_jsonl_byte_range`, retain at most four normalized user messages observed in the existing bounded lookback before `range_start`, expose them as `context_messages`, and never add them to `text`, normal `messages`, or adaptive observations. Return an empty list for other parsers that have no pre-cursor context.

- [ ] **Step 5: Implement deterministic Learn parsing and admission**

Use an anchored assistant-only regular expression and pipe-field parser. Ground `evidence` against normalized user text from `context_messages + messages[:assistant_index]`. Score complete records with fixed dimensions:

```python
score = 0.14  # nonempty atomic principle
score += 0.28 if evidence_verified else 0
score += 0.18 if novelty else 0
score += 0.22 if transfer else 0
score += 0.18 if boundary else 0
```

Require verified evidence, transfer, boundary, valid source, and score at least `direct_seed_threshold` for a direct seed. Use SHA-256-derived IDs, conservative normalized-term similarity, safe atomic Markdown writes, bounded source refs, and active-formal update guards. A new independent source may only append evidence and derive `maturity: reinforced`. When a similar proposal changes a core field, call the existing lifecycle proposal writer with the exact active ID/revision and new evidence; do not edit the formal section.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the Step 3 command. Expected: all selected tests pass with no warning output.

- [ ] **Step 7: Commit the task**

```bash
git add scripts/insight_memory.py scripts/transcript_utils.py \
  tests/test_insight_memory.py tests/test_end_to_end_agents.py \
  tests/test_session_harvester.py
git commit -m "feat: learn source-grounded insight seeds"
```

---

### Task 3: Transactional Harvester Integration And Visible Feedback

**Files:**
- Modify: `scripts/session_harvester.py`
- Test: `tests/test_session_harvester.py`
- Test: `tests/test_end_to_end_agents.py`

**Interfaces:**
- Consumes: `process_insight_memory` and its stable result dictionary.
- Produces: exactly-once non-subagent Codex processing and `[insight-learner] SEED|CANDIDATE|REINFORCED` output.

- [ ] **Step 1: Write failing harvester integration tests**

Build a Codex transcript with one user idea and one assistant `[LEARN]`, call `process_transcript`, and assert:

```python
self.assertTrue((vault / "05-Agent-Memory" / "insights.md").exists())
self.assertIn("maturity: `seed`", (vault / "05-Agent-Memory" / "insights.md").read_text())
self.assertNotIn("insight-candidate", recall_index_text)
self.assertIn("[insight-learner] SEED", stdout.getvalue())
```

Add tests for subagent suppression, non-Codex collection suppression in phase one, incremental context grounding, cursor retry idempotence, index rebuild when only an Insight changes, and the visible Agent Memory Index showing formal/candidate Insight counts and links.

- [ ] **Step 2: Run the integration tests and verify RED**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_session_harvester \
  tests.test_end_to_end_agents -v
```

Expected: no insights file and no learner output.

- [ ] **Step 3: Integrate the learner**

Import `process_insight_memory`, call it only when `not meta['is_subagent'] and meta['agent'] == 'codex'`, include its counters in `total_found` and `changed`, and add `print_insight_memory_items`. The printer exposes title, action, maturity, confidence, source count, and relative path, never the evidence excerpt. Extend the existing Vault status collector and Agent Memory Index renderer with formal/candidate Insight counts and Obsidian links.

- [ ] **Step 4: Run the integration tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add scripts/session_harvester.py tests/test_session_harvester.py \
  tests/test_end_to_end_agents.py
git commit -m "feat: harvest learn annotations into insights"
```

---

### Task 4: Formal Indexing And Insight Graph Relations

**Files:**
- Modify: `scripts/knowledge_index.py`
- Test: `tests/test_knowledge_index.py`
- Modify: `scripts/link_validator.py`
- Test: `tests/test_link_validator.py`

**Interfaces:**
- Consumes: formal `insights.md` sections parsed by `parse_active_formal_section(..., 'insight')`.
- Produces: enriched runtime units with structured Insight fields and graph edges `derived_from`, `reinforced_by`, `applies_to`, `supports`, `operationalized_as`, and `related_to`.

- [ ] **Step 1: Write failing index and graph tests**

Create a formal insight fixture plus an `_insight-candidates` file. Require one runtime unit for the formal record, zero candidate units, terms from novelty/transfer/boundary, and relation edges with stable source/target IDs. Require inactive Insight revisions to veto duplicate active copies using existing tombstone logic.

- [ ] **Step 2: Run tests and verify RED**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_knowledge_index tests.test_link_validator -v
```

Expected: `insights.md` is not discovered or parsed.

- [ ] **Step 3: Implement discovery, enrichment, and relations**

Add the configured formal path as note type `insights`, parse it through kind `insight`, include structured text in `recall_summary` and `terms`, and map source refs/relations into deterministic graph edges. Create concept nodes for transfer values using a stable SHA-256 digest; do not use raw transfer text as a graph ID.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add scripts/knowledge_index.py scripts/link_validator.py \
  tests/test_knowledge_index.py tests/test_link_validator.py
git commit -m "feat: index and link formal insights"
```

---

### Task 5: Hybrid Insight Retrieval And Runtime Guardrails

**Files:**
- Modify: `scripts/memory_recall.py`
- Modify: `scripts/memory_runtime.py`
- Test: `tests/test_memory_recall.py`
- Test: `tests/test_memory_runtime.py`
- Modify: `scripts/evaluate_memory_runtime.py`
- Test: `tests/test_memory_runtime_eval.py`

**Interfaces:**
- Consumes: runtime `insight` units containing maturity, novelty, transfer, and boundary.
- Produces: `infer_inspiration_intent(query) -> bool`; content-anchored explicit Insight retrieval; automatic runtime cap of two; safe `[INSIGHT]` rendering.

- [ ] **Step 1: Write failing recall tests**

Require `我有哪些启发` to list formal insights, a concrete query such as `设计多个不可靠审查器时怎样组合证据` to retrieve the matching fusion Insight, and the vague query `给我一个思路` to return no unanchored inventory. Require a more relevant `seed` to outrank an unrelated `reinforced` record.

- [ ] **Step 2: Write failing runtime tests**

Cover automatic exploration, ordinary execution suppression, at-most-two results, at-most-one low-confidence seed, Decision/Workflow precedence, 400-token Insight sub-budget, duplicate suppression, and rendering:

```python
self.assertRegex(rendered, r"(?m)^\[INSIGHT\] .+maturity: seed.+boundary: .+source: \[\[05-Agent-Memory/insights\]\]$")
```

- [ ] **Step 3: Run recall/runtime tests and verify RED**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_memory_recall \
  tests.test_memory_runtime.MemoryRuntimeRetrievalTests \
  tests.test_memory_runtime.MemoryRuntimeOrchestrationTests \
  tests.test_memory_runtime_eval -v
```

Expected: `insight` lacks type intent, runtime label, exploration gate, and sub-budget behavior.

- [ ] **Step 4: Implement recall intent and structured scoring**

Add explicit type terms `启发|洞见|灵感|insight`, plus a separate exploration-intent detector requiring both an exploration action and concrete non-filler content. Reuse existing five-channel RRF. Add only a small maturity tie-break so content relevance remains dominant.

- [ ] **Step 5: Implement runtime selection and rendering**

When exploration intent is absent, remove automatic insight units unless the prompt explicitly requests Insight inventory. When present, select at most two after normal ranking, allow at most one low-confidence seed, and reserve no more than `min(400, configured insight budget)` estimated tokens. Add `RUNTIME_LABELS['insight'] = 'INSIGHT'` and a type-specific renderer that includes maturity, boundary, and a source link while stating in the enclosing refresh instructions that Insight is inspiration rather than authority.

- [ ] **Step 6: Extend the deterministic evaluation fixture**

Add positive, negative, candidate-leak, one-shot-seed, unrelated-reinforced, and ambiguous-prompt cases. Preserve the existing precision, critical error recall, irrelevant trigger, and p95 metrics.

- [ ] **Step 7: Run tests and fixed evaluation, verify GREEN**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_memory_recall tests.test_memory_runtime \
  tests.test_memory_runtime_eval -v
scripts/.venv/bin/python scripts/evaluate_memory_runtime.py --json
```

Expected: all tests pass; candidate leak and assistant-source acceptance are zero; p95 remains below 25 ms.

- [ ] **Step 8: Commit the task**

```bash
git add scripts/memory_recall.py scripts/memory_runtime.py \
  scripts/evaluate_memory_runtime.py tests/test_memory_recall.py \
  tests/test_memory_runtime.py tests/test_memory_runtime_eval.py
git commit -m "feat: recall insights with exploration guardrails"
```

---

### Task 6: Agent Protocol, Context Compilation, And Operations

**Files:**
- Modify: `scripts/compiler.py`
- Modify: `AGENTS.md`
- Modify: `patches/AGENT_MEMORY_BEACON.md.patch`
- Modify: `scripts/doctor.py`
- Modify: `scripts/install_runtime.py`
- Modify: `scripts/memory_quality_audit.py`
- Test: `tests/test_compiler.py`
- Test: `tests/test_doctor.py`
- Test: `tests/test_install_runtime.py`
- Test: `tests/test_installers.py`
- Test: `tests/test_memory_quality_audit.py`

**Interfaces:**
- Consumes: the formal annotation contract and configured Insight paths.
- Produces: managed `[LEARN]` instructions in every supported Codex context target; release verification that includes `insight_memory.py` and rejects candidate leakage.

- [ ] **Step 1: Write failing compiler and release tests**

Require managed context to contain a concise `[LEARN]` section with the complete fields, one-shot seed rule, assistant-speculation prohibition, and no compiled Insight bodies. Require the release manifest/runtime copy to contain `insight_memory.py`. Require doctor and quality-audit candidate isolation to include `_insight-candidates`, and require formal Insight records to participate in the same revision/source audit as other runtime memories.

- [ ] **Step 2: Run tests and verify RED**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_compiler tests.test_doctor \
  tests.test_install_runtime tests.test_installers \
  tests.test_memory_quality_audit -v
```

Expected: managed context and release checks do not know Insight.

- [ ] **Step 3: Implement managed protocol and operational checks**

Add the approved `[LEARN]` protocol to the static managed block and compiler. Compile only protocol and counts, not all Insight content. Add new source/test files to release verification, candidate roots, quality audit, script compilation, and parity manifests using existing deterministic sorting and hashing.

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command. Expected: all selected tests pass.

- [ ] **Step 5: Commit the task**

```bash
git add scripts/compiler.py AGENTS.md patches/AGENT_MEMORY_BEACON.md.patch \
  scripts/doctor.py scripts/install_runtime.py scripts/memory_quality_audit.py \
  tests/test_compiler.py tests/test_doctor.py tests/test_install_runtime.py \
  tests/test_installers.py tests/test_memory_quality_audit.py
git commit -m "feat: install learn insight protocol"
```

---

### Task 7: Documentation, Full Regression, And Stable Runtime Publication

**Files:**
- Modify: `README.md`
- Modify: `references/architecture.md`
- Modify: `references/workflow.md`
- Modify: `CHANGELOG.md`
- Modify: `templates/vault/README.md`
- Verify: all files changed by Tasks 1-6

**Interfaces:**
- Consumes: completed source implementation and tests.
- Produces: user-facing documentation, verified source checkout, and a transactionally installed stable runtime.

- [ ] **Step 1: Update documentation**

Document `[LEARN]` capture, `candidate|seed|reinforced`, explicit evidence grounding, Obsidian paths, `[INSIGHT]` recall, relationship to Workflow, token/latency limits, rollback, and a concrete verification walkthrough. State that first sighting can be formal and that repetition only reinforces.

- [ ] **Step 2: Run formatting and compilation checks**

```bash
git diff --check
scripts/.venv/bin/python -m compileall -q scripts tests
```

Expected: both commands exit 0 with no output.

- [ ] **Step 3: Run focused Learn/Insight tests**

```bash
scripts/.venv/bin/python -m unittest \
  tests.test_insight_memory tests.test_memory_schema \
  tests.test_session_harvester tests.test_knowledge_index \
  tests.test_memory_recall tests.test_memory_runtime \
  tests.test_compiler tests.test_doctor tests.test_install_runtime -v
```

Expected: zero failures and zero errors.

- [ ] **Step 4: Run the complete repository suite**

```bash
scripts/.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: zero failures and zero errors.

- [ ] **Step 5: Run fixed evaluations and CI doctor**

```bash
scripts/.venv/bin/python scripts/evaluate_memory_runtime.py --json
scripts/.venv/bin/python scripts/doctor.py --profile ci
scripts/.venv/bin/python scripts/install_runtime.py --verify-release
```

Expected: fixed Insight gates pass, recall p95 is below 25 ms, CI doctor is PASS, and release verification is PASS.

- [ ] **Step 6: Perform a read-only real-Vault preview**

Query `/Users/a0000/ObsidianBrain` for an explicit Insight inventory, a concrete exploratory prompt, and a vague prompt. Confirm candidate count is zero in results, concrete results are at most two, and vague results are zero. Do not create a formal Insight from old content during this preview.

- [ ] **Step 7: Transactionally install and verify stable runtime**

Run the repository's transactional installer, then verify the installed path:

```bash
scripts/.venv/bin/python scripts/install_runtime.py
~/.local/share/agent-memory-beacon/runtime/.venv/bin/python \
  ~/.local/share/agent-memory-beacon/runtime/scripts/doctor.py --profile live
```

Expected: stable runtime reports PASS and its release ID matches the installed source release.

- [ ] **Step 8: Commit documentation and final integration files**

```bash
git add README.md references/architecture.md references/workflow.md CHANGELOG.md \
  templates/vault/README.md
git commit -m "docs: explain learn insight memory"
```

- [ ] **Step 9: Review final scope**

Run `git status --short`, `git diff --stat HEAD~7..HEAD`, and inspect every feature commit. Confirm no pre-existing unrelated dirty file was staged or reverted, no secrets appear, and the design acceptance checklist has a corresponding passing test or explicit operational check.
