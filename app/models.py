# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Pydantic models for the family office portal API surface.

These models describe the JSON-shaped responses and request bodies used by the
admin, health and balance intake endpoints. Page routes return
server-rendered HTML and do not have a response_model; see ADR-001 for the
rendering decision.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import TYPE_CHECKING, Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pydantic.config import JsonDict
    from pydantic_core import ErrorDetails


class HealthResponse(BaseModel):
    """Liveness probe payload returned by ``GET /health``."""

    status: str = Field(description="Service status indicator.", examples=["ok"])
    service: str = Field(
        description="Service name reported by the running process.",
        examples=["family-office-portal"],
    )


class RefreshLogEntry(BaseModel):
    """One row of the refresh history for a backend service."""

    service: str = Field(
        description="Backend service identifier.",
        examples=["llc-manager"],
    )
    last_success_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp of the most recent successful refresh.",
        examples=["2026-05-16T03:00:00Z"],
    )
    last_error_at: str | None = Field(
        default=None,
        description="ISO 8601 timestamp of the most recent failed refresh, if any.",
        examples=["2026-05-15T22:14:01Z"],
    )
    last_error_message: str | None = Field(
        default=None,
        description="Truncated error message from the most recent failure.",
        examples=["upstream returned HTTP 500"],
    )
    is_stale: bool = Field(
        description="True when the cached dataset has exceeded its staleness window.",
        examples=[False],
    )


class RefreshStatusResponse(BaseModel):
    """Response payload for ``GET /admin/refresh-status``."""

    entries: list[RefreshLogEntry] = Field(
        description="One entry per backend service tracked by the scheduler.",
    )


class RefreshTriggerRequest(BaseModel):
    """Request body for ``POST /admin/refresh/{service}``."""

    force: bool = Field(
        default=False,
        description=(
            "When true, bypass the staleness check and refresh immediately even "
            "if cached data is fresh."
        ),
        examples=[True],
    )


class RefreshTriggerResponse(BaseModel):
    """Response payload for ``POST /admin/refresh/{service}``."""

    service: str = Field(
        description="Backend service whose refresh job was scheduled.",
        examples=["llc-manager"],
    )
    scheduled: bool = Field(
        description="True when the refresh job was enqueued.",
        examples=[True],
    )
    forced: bool = Field(
        description="True when the staleness check was bypassed.",
        examples=[False],
    )


# --------------------------------------------------------------------------- #
# Balance intake (``POST /api/v1/balances``)
# --------------------------------------------------------------------------- #

BALANCE_CATEGORIES = Literal[
    "Investments", "Retirement", "Cash", "Digital currency", "Alternatives"
]
# Most rows one delivery may carry. The body byte cap is sized from this in
# ``app.routes.balances.MAX_BODY_BYTES``.
MAX_DELIVERY_ROWS = 2000
# Providers a delivery may name, as account id prefixes. This tuple is the one
# place the set is written down; the account id pattern is built from it.
BALANCE_PROVIDERS: tuple[str, ...] = ("pp", "xero", "crypto")
# Bank-style rows that may carry a reconciled-through date.
_RECONCILED_PROVIDER = "xero"
_ACCOUNT_ID_PATTERN = rf"^({'|'.join(BALANCE_PROVIDERS)}):[A-Za-z0-9._:-]{{1,100}}$"
_PROVIDER_LIST = ", ".join(f"{name}:" for name in BALANCE_PROVIDERS)
# ``re.ASCII`` keeps ``\d`` to the digits 0 to 9; without it, other Unicode
# decimal digits would pass the pattern.
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
# Unicode categories refused in an account name: control (C0 and C1),
# format (bidirectional overrides and isolates, zero-width marks), and the
# line and paragraph separators.
_REFUSED_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def provider_of(account_id: str) -> str:
    """Return the provider part of an account id, the text before the first colon.

    Args:
        account_id (str): Provider-prefixed account id such as ``pp:example``.

    Returns:
        str: The prefix, or an empty string when the id has no colon.
    """
    prefix, separator, _rest = account_id.partition(":")
    return prefix if separator else ""


def _parse_date(value: str) -> str:
    """Check that text is a real ``YYYY-MM-DD`` date and return it unchanged.

    Args:
        value (str): Candidate date text.

    Returns:
        str: The same text.

    Raises:
        ValueError: If the text is not a real calendar date in that form.
    """
    if not _DATE_PATTERN.fullmatch(value):
        msg = "not a YYYY-MM-DD date"
        raise ValueError(msg)
    date.fromisoformat(value)
    return value


class BalanceRow(BaseModel):
    """One account balance in a delivery.

    Validation is strict: ``value`` must be a JSON string (a number is
    rejected), and no field is coerced from another type. Error messages never
    contain the submitted value.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    account_id: Annotated[
        str,
        StringConstraints(pattern=_ACCOUNT_ID_PATTERN),
    ] = Field(
        description=(
            f"Stable account id prefixed with its provider ({_PROVIDER_LIST}). "
            "Never a full account number."
        ),
        examples=["pp:example-brokerage"],
    )
    account_name: Annotated[str, StringConstraints(min_length=1, max_length=200)] = (
        Field(
            description="Plain-English account name shown to people.",
            examples=["Example Brokerage"],
        )
    )
    entity_id: str = Field(
        description="Entity UUID that owns the account. Never null.",
        json_schema_extra={"format": "uuid"},
        examples=["11111111-2222-4333-8444-555555555555"],
    )
    category: BALANCE_CATEGORIES = Field(description="Account category.")
    source: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,39}$")] = (
        Field(
            description="Feed name, for example broker_report or manual_mark.",
            examples=["broker_report"],
        )
    )
    value: Annotated[
        str, StringConstraints(pattern=r"^-?[0-9]{1,12}(\.[0-9]{1,4})?$")
    ] = Field(
        description=(
            "Decimal amount as a JSON string, never a number: an optional "
            "minus sign, at most 12 digits before the point and at most 4 "
            "after it (rounded half to even to whole cents when stored)."
        ),
        examples=["1234.56"],
    )
    currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")] = Field(
        description="Three-letter currency code.", examples=["USD"]
    )
    as_of: str = Field(
        description="Date the value is true for, YYYY-MM-DD.",
        json_schema_extra={"format": "date"},
        examples=["2026-09-26"],
    )
    reconciled_through: str | None = Field(
        default=None,
        description=(
            "Bank rows only: latest date with no unreconciled statement lines."
        ),
        json_schema_extra={"format": "date"},
    )

    @field_validator("account_name")
    @classmethod
    def _printable_name(cls, value: str) -> str:
        """Reject blank names and names with control or format characters.

        Format characters include the bidirectional overrides that can make a
        name display differently from how it is stored.

        Args:
            value (str): Account name.

        Returns:
            str: The same name.

        Raises:
            ValueError: If the name is only whitespace or has a control,
                format, or line or paragraph separator character.
        """
        if not value.strip():
            msg = "is blank"
            raise ValueError(msg)
        if any(unicodedata.category(ch) in _REFUSED_NAME_CATEGORIES for ch in value):
            msg = "contains a control or format character"
            raise ValueError(msg)
        return value

    @field_validator("entity_id")
    @classmethod
    def _canonical_uuid(cls, value: str) -> str:
        """Check the entity id is a UUID and return it in lower-case form.

        Args:
            value (str): Entity id text.

        Returns:
            str: Canonical lower-case hyphenated UUID.

        Raises:
            ValueError: If the text is not a UUID.
        """
        if not _UUID_PATTERN.fullmatch(value):
            msg = "not a UUID"
            raise ValueError(msg)
        return str(UUID(value))

    @field_validator("as_of", "reconciled_through")
    @classmethod
    def _real_date(cls, value: str | None) -> str | None:
        """Check a date field is a real ``YYYY-MM-DD`` date.

        Args:
            value (str | None): Date text, or None where the field is optional.

        Returns:
            str | None: The same value.
        """
        return None if value is None else _parse_date(value)

    @model_validator(mode="after")
    def _check_reconciled_through(self) -> BalanceRow:
        """Allow ``reconciled_through`` only on bank rows, and not after ``as_of``.

        Returns:
            BalanceRow: This row.

        Raises:
            ValueError: If the field is set on a non-bank row or is later
                than ``as_of``.
        """
        if self.reconciled_through is None:
            return self
        if self.provider != _RECONCILED_PROVIDER:
            msg = "reconciled_through is only allowed on bank rows"
            raise ValueError(msg)
        if self.reconciled_through > self.as_of:
            msg = "reconciled_through is later than as_of"
            raise ValueError(msg)
        return self

    @property
    def provider(self) -> str:
        """Return the provider prefix of the account id."""
        return provider_of(self.account_id)


# A delivery that passes validation. It is the request example in the
# OpenAPI document, so the generated contract test sends a body the route
# accepts. ``tests/unit/test_balance_intake.py`` checks that it validates.
_EXAMPLE_DELIVERY: JsonDict = {
    "items": [
        {
            "account_id": "pp:example-brokerage",
            "account_name": "Example Brokerage",
            "entity_id": "11111111-2222-4333-8444-555555555555",
            "category": "Cash",
            "source": "broker_report",
            "value": "1234.56",
            "currency": "USD",
            "as_of": "2026-09-26",
        }
    ],
    "total": 1,
}


class BalanceDelivery(BaseModel):
    """A full delivery of balance rows: ``{"items": [...], "total": n}``."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        json_schema_extra={"examples": [_EXAMPLE_DELIVERY]},
    )

    items: list[BalanceRow] = Field(
        max_length=MAX_DELIVERY_ROWS,
        description="Every balance row in this delivery.",
    )
    total: Annotated[int, Field(strict=True, ge=0)] = Field(
        description="Number of rows; must equal the length of items.",
    )

    @model_validator(mode="after")
    def _check_total_and_duplicates(self) -> BalanceDelivery:
        """Require ``total`` to match the rows and every account id to be unique.

        Returns:
            BalanceDelivery: This delivery.

        Raises:
            ValueError: If ``total`` differs from the row count or an account
                id appears twice. The message never includes an id.
        """
        if self.total != len(self.items):
            msg = "total does not match the number of items"
            raise ValueError(msg)
        ids = [row.account_id for row in self.items]
        if len(set(ids)) != len(ids):
            msg = "an account appears more than once"
            raise ValueError(msg)
        return self

    def providers(self) -> list[str]:
        """Return the sorted providers that have at least one row.

        Returns:
            list[str]: Provider prefixes present in the delivery.
        """
        return sorted({row.provider for row in self.items})


class BalanceReceipt(BaseModel):
    """Response for an accepted delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: int = Field(description="Number of rows stored.", examples=[3])
    providers: list[str] = Field(
        description="Providers whose rows were replaced.", examples=[["pp", "xero"]]
    )


def validation_problems(errors: Sequence[ErrorDetails]) -> list[dict[str, str]]:
    """Reduce pydantic errors to field paths and error codes only.

    Pydantic's own messages and ``input`` fields can contain the submitted
    value, so neither is copied. Unknown field names from the sender are shown
    as ``?``, so text from the request is never echoed.

    Args:
        errors (Sequence[ErrorDetails]): Result of ``ValidationError.errors()``.

    Returns:
        list[dict[str, str]]: At most 20 ``{"field", "problem"}`` entries.
    """
    known = {"items", "total", *BalanceRow.model_fields}
    problems: list[dict[str, str]] = []
    for error in errors[:20]:
        parts = [
            str(part) if isinstance(part, int) or part in known else "?"
            for part in error["loc"]
        ]
        problems.append({"field": ".".join(parts) or "body", "problem": error["type"]})
    return problems
