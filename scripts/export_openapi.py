# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Generate ``docs/api/openapi.json`` from the FastAPI app.

Run from the repo root:

    uv run python -m scripts.export_openapi

The script imports ``app.main:app``, calls ``app.openapi()``, and writes the
result to ``docs/api/openapi.json``. It does not start a live HTTP server.
Invoked manually after a route change and from the OpenAPI export CI step.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT: Path = Path(__file__).resolve().parent.parent
OUTPUT_PATH: Path = REPO_ROOT / "docs" / "api" / "openapi.json"


def export() -> Path:
    """Render the OpenAPI document and write it to ``OUTPUT_PATH``.

    Returns:
        Path: Absolute path of the written JSON file.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from app.main import app

    schema: dict[str, Any] = app.openapi()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return OUTPUT_PATH


if __name__ == "__main__":
    written: Path = export()
    print(f"wrote {written.relative_to(REPO_ROOT)}")
