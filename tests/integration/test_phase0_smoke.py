"""Phase 0 integration smoke test.

Replaced in Phase 1 with full-page render and HTMX partial assertions.
"""

import app


def test_app_package_importable() -> None:
    """Verify the app package is importable end-to-end from the test runner."""
    assert app.__doc__ is not None
