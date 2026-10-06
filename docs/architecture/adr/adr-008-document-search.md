# ADR-008: Internal Document Search with an In-Query Confidentiality Filter

> **Status**: Accepted
> **Date**: 2026-10-05

## TL;DR

Search is a Python function inside the portal, `SearchService.search`, used
by the chat code. It embeds the query, runs one dense query per collection
(`family-docs` and `tax-law`) at `k=8` with no reranker, and returns plain
data with citations. For viewers, `is_confidential = false` is a Qdrant
payload filter inside the query, so a point that is confidential, or has no
flag at all, is never returned. There is no HTTP route.

## Context

The indexers (ADR-006) fill two collections. `family-docs` holds the
family's document chunks, some confidential and some tied to an entity.
`tax-law` holds public summaries of tax law, one point per subtopic. Chat
needs the most relevant passages from both, quickly, with enough citation
data to point the user at the source.

Constraints:

- Viewers must never see confidential text, including in the passages sent
  to the model. Filtering after Qdrant returns its top results would also
  starve viewers of results when confidential points score higher.
- Chunks written before the flag existed, or by a faulty writer, may lack
  `is_confidential`. They must be treated as confidential.
- The embedding service is CPU-only and shared with indexing; a hung
  dependency must not hold a chat request for the indexer's 60-second
  timeout.
- Chat is written against a fixed request and response shape so search can
  move behind an HTTP API later without changing the caller.

## Decision

### Shape

`SearchRequest(query, include_confidential=False, entity_ids=None,
collections=("family-docs", "tax-law"), top_k=8)` in, `SearchResponse(results,
embedding_model)` out. Each `SearchResult` has `text`, `score`, `collection`
and a citation: `DocumentCitation` (document, chunk, title, entity, date,
page start and end, section) or `TaxLawCitation` (subtopic `id`, `title`,
`topic`). `as_dict()` gives the same as plain JSON-ready data, with the
tax-law citation keyed `id`. Requests outside the shape (blank query, more
than 4000 characters, `top_k` outside 1 to 8, an empty `entity_ids`, an
unknown collection) raise `ValueError`.

`include_confidential` comes from the signed-in user's role
(`app.routes._context.include_confidential`), never from user input.

### Filters

- `family-docs`, viewer: `must: is_confidential == false`. A missing field,
  null, string or number does not match.
- `family-docs`, `entity_ids` given: `must: entity_id in entity_ids`.
- `tax-law`: no filter. It has no entities and nothing confidential.

As a second line, every `family-docs` point returned to a viewer is checked
again; one that is not exactly `false` is dropped and an error is logged.
That check never replaces the filter: tests prove a viewer still gets `k`
results when more than `k` confidential points score higher.

### Ranking, timing and failures

Each collection returns up to `top_k`; results are merged by cosine score and
cut to `top_k`. A collection that does not exist yet is skipped with a
warning. The query embedding and Qdrant calls use a 10-second timeout. The
latency budget is 2 seconds: a slower search still returns and logs
`search_over_budget`. Embedding or Qdrant failures raise `SearchError`, whose
message names the failure type only. Logs carry counts, collections, the
admin flag and elapsed time, never the query or result text.

`search` is blocking; `search_async` runs it in a worker thread for async
route code. `build_search_service()` returns None when the embedding service
or Qdrant is unset or misconfigured, and the caller shows "not connected".

## Consequences

- A viewer's results can never contain confidential text unless both the
  Qdrant filter and the second check fail.
- No reranker: ranking quality depends on the embedding model alone. Hybrid
  dense and sparse search can be added later because every collection already
  has a `sparse` slot.
- Fakes prove the rules. They do not prove latency against the real
  embedding service and Qdrant.

Open assumption: #ASSUME a warm query embedding plus two Qdrant queries
finish well under 2 seconds on the deployed services. #VERIFY run a search
from the portal container against the live services, including while a bulk
indexing run is embedding, and read `elapsed_ms` from the `search_finished`
log.
