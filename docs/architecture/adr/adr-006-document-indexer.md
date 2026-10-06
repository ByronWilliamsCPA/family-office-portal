# ADR-006: Document Indexer as a Scheduled Command

> **Status**: Accepted
> **Date**: 2026-10-05
> **Amends**: [ADR-003](./adr-003-backend-data-aggregation.md) (one carve-out,
> see "Relation to earlier decisions") and the out-of-scope list in the
> [project vision](../../planning/project-vision.md)

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

## Relation to earlier decisions

- **ADR-003** says page renders read SQLite and never call a remote service
  during a user request. That still holds for every page this ADR touches:
  the indexer is a separate command, and `connected.document_search` only
  reads settings. Document search, added later, will embed the user's query
  and query Qdrant during the request; that is the one carve-out from
  ADR-003, limited to search, and it must show "not connected" rather than an
  error when either service is down.
- **Project vision**: its out-of-scope list names Q&A, full-text document
  search, and LLM infrastructure. This ADR narrows that list: the portal now
  owns the document index and, later, the search that reads it. The vision
  carries a dated note pointing here. The other exclusions stand.

## Decision

### A command, not a web job

`python -m app.retrieval.indexer` runs one pass and exits. The deployment
runs it on a schedule in the same image as the web process, one instance per
project, with the embedding key, Qdrant access and a read-only mount of the
chunk directory. Bulk runs are scheduled off-hours and must not overlap
another instance's run.

Exit status:

| Code | Meaning |
| --- | --- |
| 0 | Every file was handled |
| 1 | At least one file failed; the others were handled |
| 2 | The run could not start or had to stop: settings unset or unparseable, the directory is missing, the collection has an unexpected vector schema, the embedding service refused the key (HTTP 401 or 403), or Qdrant stopped answering (a transport failure or HTTP 429) |

A Qdrant error response for one document (for example a 400 or 500 on its
write) fails that document only, with exit 1; the run goes on.

### Modules

| Module | Role |
| --- | --- |
| `app/retrieval/settings.py` | All retrieval setting names; connection objects that cannot exist without a key |
| `app/retrieval/embeddings.py` | OpenAI-compatible `/v1/embeddings` client; query prefix; bounded retries; count, dimension and finiteness checks |
| `app/retrieval/qdrant_store.py` | Collection creation and schema check with `dense` and `sparse` vectors; the `family-docs` writer |
| `app/retrieval/chunk_sets.py` | Reads and checks one chunk-set file; consent, tax-return and confidentiality rules |
| `app/retrieval/indexer.py` | The command: per-file rules, content digest, removed-file cleanup, exit status |
| `app/retrieval/tax_law.py` | A second command for the `tax-law` collection (see below) |

### Collection

`family-docs` is created with a named dense vector `dense` (1024 dimensions,
cosine) and a named sparse vector `sparse`. Only `dense` is filled. Qdrant
cannot add a sparse vector to an existing collection, so the slot exists from
the start for a later hybrid search. Payload indexes cover `is_confidential`
(bool), and `entity_id` and `document_id` (keyword).

On each run the indexer checks an existing collection: a missing `dense` or
`sparse` vector, or a `dense` vector of another size or distance, stops the
run with exit 2 rather than writing into it, and a missing payload index is
created. Two runs that race to
create the collection are safe; the loser treats HTTP 409 as "exists".

Each point keeps the pipeline's chunk-contract fields (`chunk_id`,
`document_id`, `trace_id`, `trust_score`, `ocr_engine_provenance`,
`page_range`, `section_hierarchy`, `hallucination_risk`, plus
`chunk_strategy`, `token_count`, `source_track`), the family document fields
(`entity_id`, `document_type`, `category`, `is_confidential`, `title`,
`document_date`, `sha256`, `consent_on_file`), the derived `is_tax_return`,
and `text`, `chunk_index`, `chunk_count`, `embedding_model`, `embedded_at`
and `index_digest`. Trust and risk values are stored as given: a null means
"not scored" and stays null, never zero or one.

Point IDs are a UUID derived from the document ID and chunk position. A
document is replaced by writing the new points first, in batches of 64, and
then deleting the document's points that are not in the new set. A write that
fails part-way leaves a mix of old and new points for that one document,
never none; the stored digests then disagree, so the next run rewrites it.
An empty set deletes the document's points.

### Rules for each file

1. **Consent.** A set is a tax return when any `category` or
   `document_type`, on the set or on any chunk, names a tax return or tax
   election (`Tax Returns`, `tax_return`, `tax_election`, compared without
   case and with spaces and hyphens read as underscores). A value that is
   present but not a string, or a set that gives neither field anywhere, is
   treated as a tax return. A tax return is indexed only when every
   `consent_on_file` value is exactly `true`: every chunk counts and a chunk
   without the field counts as false; a set-level value counts when present.
   Otherwise its existing points are deleted and the skip is logged by
   document ID only.
2. **Unchanged.** A file is skipped only when it has a `sha256`, and its
   hash, its consent, the configured embedding model, and a digest of its
   chunk text and payload all match the stored points, and every point
   recorded in `chunk_count` is present with that digest. Consent,
   confidentiality, entity and chunking changes do not change the document
   hash, so the hash alone never decides. A file without a hash is never
   skipped, and each one is logged as a warning.
3. **Replace.** Otherwise the non-blank chunks are embedded (documents get no
   prefix) and the document's points are replaced. A set with no chunk text
   leaves the document with no points.
4. **Removed.** After all files, documents that have points but no file are
   deleted. The sweep is skipped, with a warning, when the directory holds no
   files at all, or when five or more documents and more than half of the
   indexed documents would be deleted at once: both look like a mount that is
   not ready. An operator who really removed most documents clears the
   collection by hand.

A file that cannot be read keeps its existing points and fails, unless those
points are a tax return. Those are deleted, because the file may be a consent
withdrawal that failed to parse. A file that cannot be embedded keeps its
points and fails.

`is_confidential` is resolved for the whole document and stored the same on
every point: `false` only when every value given, on the set and on every
chunk, is exactly `false` and each chunk has a value of its own or the set's.
Anything else, including a missing value, is stored as `true`.

### Embedding requests

Chunks are sent 32 to a request. A request that times out, loses its
connection, or gets HTTP 429, 502, 503 or 504 is retried up to three attempts
in all, waiting 1 s then 2 s, or the `Retry-After` seconds when given, capped
at 30 s. Other failures are not retried. A 401 or 403 stops the run at once,
since no later document can succeed with the same key.

### Settings

All optional. With the embedding URL, Qdrant URL or chunk directory unset,
the command exits 2 and logs one warning; templates get
`connected.document_search` false when either service is unset.

| Variable | Default | Purpose |
| --- | --- | --- |
| `EMBED_BASE_URL` | unset | Embedding service base URL, without `/v1` |
| `EMBED_API_KEY` | unset | Bearer key; required when `EMBED_BASE_URL` is set |
| `EMBEDDING_MODEL` | unset | Model name; required when `EMBED_BASE_URL` is set |
| `EMBED_TIMEOUT_SECONDS` | `60` | Timeout for one embedding request; a finite number above zero |
| `QDRANT_URL` | unset | Qdrant base URL |
| `QDRANT_API_KEY` | unset | Qdrant key; required when `QDRANT_URL` is set |
| `CHUNKS_DIR` | unset | Read-only chunk-set directory (indexer only) |
| `TAX_LAW_PATH` | unset | Tax-law knowledge-base JSON file (tax-law command only) |

No host or port is hardcoded. Keys are read as secrets, kept as `SecretStr`
on the connection objects, and never logged.

These settings are deliberately not checked at startup, unlike the backend
pairs in ADR-003's amendment (where a URL without its key exits 1). The web
process does not need them to serve any page, so a bad value (a URL without
its key, or a timeout that cannot be parsed) makes
`connected.document_search` false and logs one warning naming the variable,
never the value. The indexer command reports the same problem and exits 2.
Whether to fail the web process at startup instead is left for when search
lands and the setting becomes part of serving pages.

### Tax-law collection

`python -m app.retrieval.tax_law` indexes the tax-law knowledge base, one
JSON file at `TAX_LAW_PATH`, into its own collection, `tax-law`, created
through the same `ensure_collection` (so it also has the `sparse` slot). The
file's shape is a contract with the repository that maintains it:
`{"knowledgeBase": [{"id", "topic", "subtopics": [{"id", "title",
"content"}]}]}`.

- One point per subtopic with non-blank content. The payload is the subtopic
  `id` and `title` (what chat cites), `topic`, `topic_id`, `text`,
  `embedding_model` and `embedded_at`. The embedded text is the title and
  content, with no query prefix. Point IDs derive from the subtopic `id`.
- The whole file is checked first. A subtopic `id` used twice anywhere
  rejects the file, and so does a file with nothing to index, so a bad or
  empty file never empties the collection.
- Every subtopic is embedded before any write. New points are upserted, then
  points whose subtopic left the file are deleted.
- It is a separate command, not part of the document indexer, because the
  file changes rarely and its settings differ: with `TAX_LAW_PATH`, the
  embedding URL or the Qdrant URL unset it exits 2 and logs one line. It
  exits 1 when the file cannot be used or embedding fails, leaving the
  collection unchanged.

The knowledge base is licensed material: it lives outside this repository
and tests use a small synthetic file.

## Options Considered

| Option | Why not |
| --- | --- |
| Index inside the web process (a scheduler job) | Bulk embedding would compete for CPU with page requests and query embeddings |
| Index in the document pipeline | The pipeline's job ends at chunks; embedding and storage belong to the application that searches, and the pipeline would need the family's Qdrant key |
| Skip on `sha256` alone | Consent withdrawal, confidentiality and re-chunking do not change the document hash, so stale or unconsented points would survive |
| Delete then write when replacing | A failed write would leave the document with no points until the next run |
| List indexed documents with a facet query | Facets return the top values up to a limit, so a large collection could hide orphans; a payload-only scroll is exhaustive and cheap at family scale |

## Security

- **Keys.** Both keys are secrets in the deployment environment, read as
  `SecretStr`, unwrapped only when the request header or Qdrant client is
  built, and never logged or shown in `repr`. A refused embedding key stops
  the run with the status code only.
- **Content.** Qdrant holds the full chunk text of every indexed document,
  with confidentiality, entity and tax-return flags on each point. Access to
  Qdrant is as sensitive as access to the documents: it must sit on a
  private network with its key required, and its storage is covered by the
  host's disk encryption and backups. See the tech spec, section 5.
- **Logs.** Logs carry document IDs, counts, status codes and exception type
  names only, never chunk text, embedding input, response bodies, setting
  values or tracebacks of request failures.
- **Fail closed.** Missing consent, classification or confidentiality values
  resolve to the stricter reading.
- **Dependencies.** `qdrant-client` (Apache-2.0) brings in `grpcio`, `numpy`,
  `protobuf`, `h2` and `portalocker`. They are covered by the lockfile and the
  pip-audit job like every other dependency.

## Consequences

- Indexing load stays out of the web process; a failed run leaves search
  answering from the last good points.
- Withdrawn consent is honored on the next run even though the document hash
  does not change, and a tax return whose file becomes unreadable loses its
  points.
- Changing `EMBEDDING_MODEL` re-embeds every document on the next run.
- Fakes prove the rules above. They do not prove the real embedding
  dimension, Qdrant behavior at volume, or query latency while a batch runs.

Open assumptions: #ASSUME the embedding service returns 1024-dimension vectors for the
configured model. #VERIFY run the indexer once against the real service; a
dimension mismatch fails each document with a clear error and writes nothing.

Concurrency: #ASSUME runs never overlap. #VERIFY the schedule allows one
concurrent run, and a full run finishes well inside its interval.

Edge case: #EDGE consent is withdrawn after a tax return was indexed. #VERIFY withdraw
consent on a test tax-return record, let the pipeline rewrite its chunk set,
run the indexer, and confirm the document has no points in `family-docs`.

Edge case: #EDGE a query arrives while a bulk run is embedding. #VERIFY measure query
embedding latency during a scheduled batch before relying on the chat
latency target.

## Related

- [ADR-001: Frontend Rendering Architecture](./adr-001-frontend-rendering-architecture.md)
- [ADR-003: Backend Data Aggregation](./adr-003-backend-data-aggregation.md)
- [ADR-005: Authentik Forward Auth](./adr-005-authentication-authentik-forward-auth.md)
- [Project Vision](../../planning/project-vision.md)
- [Technical Implementation Spec](../../planning/tech-spec.md)
