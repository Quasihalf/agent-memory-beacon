# Hindsight Prior-Art Source Note

Inspected on 2026-07-19. The inspected Python packages report version `0.8.4`.

## Sources

- [Repository README](https://github.com/vectorize-io/hindsight/blob/main/README.md)
- [Core package manifest](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/pyproject.toml)
- [Memory engine](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/engine/memory_engine.py)
- [Parallel retrieval](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/engine/search/retrieval.py)
- [Persistence models](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/models.py)

The README describes Retain, Recall, and Reflect. The source confirms PostgreSQL/pgvector persistence, world/experience/observation facts, entity and temporal links, parallel semantic/BM25/graph/temporal retrieval, rank fusion, Cross-Encoder reranking, LLM extraction, and generated mental models.

## Patterns Borrowed

- Keep retrieval strategies independent until ranking, rather than blending all signals into one opaque score.
- Use reciprocal rank fusion so one retrieval strategy cannot dominate only because its raw score uses a larger numeric scale.
- Treat time and explicit relationships as first-class retrieval evidence.
- Return enough trace data to explain which strategy found a memory and at what rank.
- Preserve token limits after ranking instead of expanding the prompt with every possible memory.

## Boundaries Kept

- Obsidian Markdown remains Beacon's only authoritative formal-memory source.
- Evidence, candidate, and formal-memory lifecycle rules remain unchanged.
- Beacon does not add PostgreSQL, pgvector, Docker, a resident daemon, an embedding model, a Cross-Encoder, or an extra LLM call on the runtime recall path.
- Type or temporal intent cannot broadly admit unrelated memory. Unanchored admission requires an explicit inventory query or a precise type-plus-time query.
- Beacon does not implement Hindsight Reflect or autonomous mental-model promotion. New inferred knowledge must continue through Beacon's candidate and approval rules.

## Reuse Statement

No Hindsight source code or wording was copied. Agent Memory Beacon implements the general multi-retriever and rank-fusion pattern independently with Python standard-library code. Hindsight's repository root identifies the project as MIT licensed; this work uses it as architectural prior art, not as a code dependency.
