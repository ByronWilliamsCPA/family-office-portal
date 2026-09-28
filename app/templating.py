# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Jinja2 environment, filters, and the shared page-render helper."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates

from app.config import load_settings

if TYPE_CHECKING:
    from starlette.requests import Request
    from starlette.responses import Response

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

NAV_ITEMS: tuple[tuple[str, str, str], ...] = (
    ("home", "Home", "/"),
    ("documents", "Documents", "/documents"),
    ("finances", "Finances", "/finances"),
    ("portfolio", "Portfolio", "/portfolio"),
    ("entities", "Entities", "/entities"),
)


def _display_zone() -> ZoneInfo | timezone:
    try:
        return ZoneInfo(load_settings().display_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.utc


def friendly_time(value: datetime | str | None) -> str:
    """Format a timestamp for primary users, for example "Sep 28, 2026, 3:05 PM".

    Args:
        value (datetime | str | None): Timestamp or ISO 8601 text.

    Returns:
        str: Plain-English timestamp, or "not yet" when missing.
    """
    if value is None or value == "":
        return "not yet"
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone(_display_zone())
    hour = local.strftime("%I").lstrip("0") or "12"
    clock = f"{hour}:{local.strftime('%M %p')}"
    return f"{local.strftime('%b')} {local.day}, {local.year}, {clock}"


def friendly_date(value: str | None) -> str:
    """Format an ISO date as "December 1, 2026".

    Args:
        value (str | None): ISO date text.

    Returns:
        str: Plain-English date, or an empty string when missing.
    """
    if not value:
        return ""
    parsed = datetime.fromisoformat(value)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def money(value: float | None) -> str:
    """Format a dollar amount with no cents, for example "$1,250,000".

    Args:
        value (float | None): Amount in dollars.

    Returns:
        str: Formatted amount, or "Not available" when missing.
    """
    if value is None:
        return "Not available"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.0f}"


templates.env.filters["friendly_time"] = friendly_time
templates.env.filters["friendly_date"] = friendly_date
templates.env.filters["money"] = money


def render(
    request: Request,
    name: str,
    *,
    section: str,
    status_code: int = 200,
    **context: Any,  # noqa: ANN401  # template context values are heterogeneous
) -> Response:
    """Render a full page with the shared navigation context.

    Args:
        request (Request): Current request (carries ``state.principal``).
        name (str): Template path under ``templates/``.
        section (str): Active navigation key.
        status_code (int): HTTP status code.
        **context (Any): Extra template variables.

    Returns:
        Response: Rendered HTML response.
    """
    principal = getattr(request.state, "principal", None)
    return templates.TemplateResponse(
        request,
        name,
        {
            "principal": principal,
            "nav_items": NAV_ITEMS,
            "section": section,
            **context,
        },
        status_code=status_code,
    )
