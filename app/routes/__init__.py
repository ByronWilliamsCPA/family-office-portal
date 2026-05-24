# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Route handlers grouped by portal section.

Each module exposes a FastAPI ``APIRouter`` that ``app.main`` mounts. Page
routes return server-rendered HTML stubs at Phase 0; JSON-returning routes use
Pydantic response models so the OpenAPI schema is well-typed.
"""
