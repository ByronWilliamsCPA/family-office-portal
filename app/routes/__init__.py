# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Route handlers grouped by portal section.

Each module exposes a FastAPI ``APIRouter`` that ``app.main`` mounts. Page
routes return server-rendered Jinja2 pages and HTMX partials that read the
SQLite cache, and the chat route returns an HTML fragment. The JSON endpoints
are the health probe, the admin refresh endpoints and the balance intake; they
use Pydantic response models so the OpenAPI schema is well-typed. The document
preview and download routes stream the file bytes from llc-manager.
"""
