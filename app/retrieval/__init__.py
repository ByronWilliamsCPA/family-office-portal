# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Document retrieval: embeddings, the Qdrant vector store, and the indexer.

The portal owns embedding, vector storage and search for the family's
documents (ADR-006). The pieces are kept separate so later features can reuse
them:

* ``settings``: the retrieval settings, all in one place, off when unset.
* ``embeddings``: a client for an OpenAI-compatible ``/v1/embeddings`` service.
* ``qdrant_store``: collection setup and the family-docs point writer.
* ``chunk_sets``: reads one chunk-set JSON file into typed values.
* ``indexer``: the scheduled command that keeps the collection in step with
  the chunk-set directory. It never runs inside the web process.
* ``tax_law``: the command that indexes the tax-law knowledge base into the
  ``tax-law`` collection.
"""
