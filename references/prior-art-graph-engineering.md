# Prior Art: graph-engineering

Inspected on 2026-07-26:

- [codejunkie99/graph-engineering](https://github.com/codejunkie99/graph-engineering)
- `README.md`
- `graph-engineering/SKILL.md`
- `graph-engineering/references/fusion-and-llm.md`

## What It Is

`graph-engineering` is primarily a graph-design methodology and Claude Skill. It describes schema design, entity and relation extraction, provenance, graph fusion, and retrieval patterns. It is not a production graph runtime: the inspected repository does not provide a graph database integration, migration engine, executable evaluation suite, or tested memory-recall service.

## Borrowed

- **Schema first:** define a small stable node vocabulary before extracting relations.
- **Typed relations:** validate relation domain and range instead of accepting arbitrary triples.
- **Provenance on every edge:** preserve where a relation came from and which source version it describes.
- **Conservative fusion:** merge exact identities and duplicate evidence deterministically; do not silently collapse merely similar memories.
- **Path-aware retrieval:** start from a content match and use a short relationship path to recover adjacent knowledge.

## Adapted For Beacon

- Obsidian Markdown remains the canonical record. `memory-graph.json` is generated and may always be rebuilt.
- The broad graph-engineering entity taxonomy is reduced to six infrastructure node types. Decision, Error, Favor, Workflow, Skill, and Insight remain memory `kind` values so retrieval and lifecycle code do not acquire parallel type systems.
- Relation provenance includes `source_ref`, exact formal-memory `source_revision`, `observed_at`, and deterministic `derivation`.
- The graph and recall index share a deterministic generation identity; a stale or mismatched pair is rejected instead of partially merged.
- Semantic retrieval is limited to two hops and five high-signal relations after a direct content anchor. Project membership and shared-session edges cannot act as semantic expansion routes.
- Legacy Graph v2 and pre-generation Graph v3 are accepted only by bounded upgrade preflight; new indexes and live recall are always strict Graph v3.

## Deliberately Not Borrowed

- No Neo4j, Docker, vector database, embedding model, or LLM extraction dependency.
- No automatic fuzzy entity merge.
- No assistant-generated relation promoted without formal source evidence.
- No graph authority over formal lifecycle state.
- No unbounded traversal or project-wide neighborhood recall.

These boundaries preserve Beacon's local, deterministic, privacy-safe operation while adding the graph discipline that is useful for reliable Agent memory.
