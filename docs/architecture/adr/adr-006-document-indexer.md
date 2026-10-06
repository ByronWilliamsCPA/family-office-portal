# ADR-006: Document Indexer as a Scheduled Command

> **Status**: Accepted
> **Date**: 2026-10-05

## TL;DR

The portal embeds the family's document chunks and stores them in a Qdrant
collection, `family-docs`. A separate command in the portal image does this
on a schedule; the web process never indexes. Settings for the embedding
service, Qdrant and the chunk directory are optional, and with them unset the
feature is off and pages say "not connected". Tax returns are indexed only
when consent is on file, and the rule fails closed.

## Context

The document pipeline ends at chunks. For each document it writes one
chunk-set file, `<document_id>.json`, into a shared directory, atomically
(temporary file, then rename). Embedding, vector storage and search belong to
the application that uses them, which for the family is this portal.

Three constraints shape the design:

- The embedding service runs on CPU and shares that CPU with the query
  embeddings that search needs. Bulk indexing must not run inside the web
  process, where it would compete with page requests, and it should run
  off-hours.
- Some documents are tax returns. They may only be indexed while signed
  consent from each taxpayer is on file, and consent can be withdrawn after a
  return was indexed.
- Search will filter on confidentiality and entity inside the vector query,
  so those payload fields must be present, indexed and fail closed.

## Decision

### A command, not a web job

`python -m app.retrieval.indexer` runs one pass and exits. The deployment
runs it on a schedule in the same image as the web process, one instance per
project, with the embedding key, Qdrant access and a read-only mount of the
chunk directory. Bulk runs are scheduled off-hours and must not overlap
another instance's bulk run.

Exit status: 0 when every file was handled, 1 when any file failed (its old
points are kept), 2 when the indexer is not configured, cannot find its
directory, or Qdrant stops answering.

### Modules

| Module | Role |
| --- | --- |
| `app/retrieval/settings.py` | All retrieval setting names; connection objects that cannot exist without a key |
| `app/retrieval/embeddings.py` | OpenAI-compatible `/v1/embeddings` client; query prefix; count, dimension and finiteness checks |
| `app/retrieval/qdrant_store.py` | Collection creation with `dense` and `sparse` vectors; the `family-docs` writer |
| `app/retrieval/chunk_sets.py` | Reads and checks one chunk-set file; consent and confidentiality rules |
| `app/retrieval/indexer.py` | The command: per-file rules, removed-file cleanup, exit status |

### Collection

`family-docs` is created with a named dense vector `dense` (1024 dimensions,
cosine) and a named sparse vector `sparse`. Only `dense` is filled. Qdrant
cannot add a sparse vector to an existing collection, so the slot exists from
the start for a later hybrid search. Payload indexes cover
`is_confidential` (bool), `entity_id` and `document_id` (keyword).

Each point keeps the pipeline's chunk-contract fields (`chunk_id`,
`document_id`, `trace_id`, `trust_score`, `ocr_engine_provenance`,
`page_range`, `section_hierarchy`, `hallucination_risk`, plus
`chunk_strategy`, `token_count`, `source_track`), the family document fields
(`entity_id`, `document_type`, `category`, `is_confidential`, `title`,
`document_date`, `sha256`, `consent_on_file`), and `text`, `chunk_index`,
`chunk_count`, `embedding_model` and `embedded_at`. Trust and risk values are
stored as given: a null means "not scored" and stays null, never zero or one.

Point IDs are a UUID derived from the document ID and chunk position. A
document is replaced by deleting all of its points, then writing the new
set, so re-indexing never leaves two sets.

### Rules for each file

1. **Consent.** A set is a tax return when any `category` is `Tax Returns`
   or any `document_type` is `tax_return` or `tax_election`, on the set or on
   any chunk. It is indexed only when every `consent_on_file` value present is
   exactly `true`; a missing value counts as false. Otherwise its existing
   points are deleted and the skip is logged by document ID only.
2. **Unchanged.** A file is skipped only when its `sha256`, its consent and
   the configured embedding model all match the stored points, and every
   point recorded in `chunk_count` is present. A consent change does not
   change the hash, so the hash alone never decides. A file without a hash is
   never skipped.
3. **Replace.** Otherwise the non-blank chunks are embedded (documents get no
   prefix) and the document's points are replaced. A set with no chunk text
   leaves the document with no points.
4. **Removed.** After all files, documents that have points but no file are
   deleted. If the directory holds no files at all, this step is skipped and
   a warning is logged, because an empty directory may be a mount that is not
   ready.

`is_confidential` is stored as given when it is a boolean; any other value,
including a missing one, is stored as `true`.

### Settings

All optional. With the embedding URL, Qdrant URL or chunk directory unset,
the command exits 2 and logs one line; templates get
`connected.document_search` false when either service is unset.

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMBED_BASE_URL` | unset | Embedding service base URL |
| `EMBED_API_KEY` | unset | Bearer key; required when `EMBED_BASE_URL` is set |
| `EMBEDDING_MODEL` | unset | Model name; required when `EMBED_BASE_URL` is set |
| `EMBED_TIMEOUT_SECONDS` | `60` | Timeout for one embedding request |
| `QDRANT_URL` | unset | Qdrant base URL |
| `QDRANT_API_KEY` | unset | Qdrant key; required when `QDRANT_URL` is set |
| `CHUNKS_DIR` | unset | Read-only chunk-set directory (indexer only) |

No host or port is hardcoded. Keys are read as secrets and never logged.

## Consequences

- Indexing load stays out of the web process; a failed run leaves search
  answering from the last good points.
- Withdrawn consent is honored on the next run even though the document hash
  does not change.
- Changing `EMBEDDING_MODEL` re-embeds every document on the next run.
- Fakes prove the rules above. They do not prove the real embedding
  dimension, Qdrant behavior at volume, or query latency while a batch runs.

Open assumptions: #ASSUME the embedding service returns 1024-dimension vectors for the
configured model. #VERIFY run the indexer once against the real service; a
dimension mismatch fails each document with a clear error and writes nothing.

Edge case: #EDGE consent is withdrawn after a tax return was indexed. #VERIFY withdraw
consent on a test tax-return record, let the pipeline rewrite its chunk set,
run the indexer, and confirm the document has no points in `family-docs`.

Edge case: #EDGE a query arrives while a bulk run is embedding. #VERIFY measure query
embedding latency during a scheduled batch before relying on the chat
latency target.
