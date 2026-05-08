"""Phase 0 unit smoke test.

Confirms the test runner can import the placeholder package. Phase 1 replaces
this with real unit tests for cache readers, JWT middleware, and template builders.
"""

import app


def test_app_package_importable() -> None:
    """Verify the app package is importable and carries its module docstring."""
    assert app.__doc__ is not None
