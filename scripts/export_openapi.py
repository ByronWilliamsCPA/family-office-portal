# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Export the FastAPI OpenAPI schema to ``docs/api/openapi.json``.

Invoked manually after a route change and (later) from a CI workflow so the
committed schema never drifts from the running application. Phase A only
emits the ``/health`` operation; the schema grows as Phase 1 routes land.

Usage:
    uv run python scripts/export_openapi.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import app  # noqa: E402  (sys.path bootstrap must run before import)

OUTPUT_PATH = PROJECT_ROOT / "docs" / "api" / "openapi.json"


def main() -> None:
    """Write the current FastAPI OpenAPI schema to ``docs/api/openapi.json``."""
    schema = app.openapi()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(schema, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote OpenAPI schema to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
