# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Per-user limits on questions: a sliding-window quota and one at a time.

Two protections, both in memory and keyed by the signed-in user's identity
(the key is never logged, echoed or put in a message):

* a sliding-window quota: at most ``Limits.per_minute`` questions in any 60
  seconds and ``Limits.per_hour`` in any 3600 seconds;
* at most one question in flight per user, so one person cannot hold both
  model slots (``CHAT_SLOTS``) and starve the other.

What counts, decided on purpose:

* A request refused for being over a limit is not recorded, so hammering
  the button while limited never pushes the clearing time later.
* A question rejected before any work is done (blank, too long, a bad
  picture, chat not connected, a body over the size cap) is refunded with
  ``Ticket.refund`` and does not count. Everything that reaches the search
  or the model counts, including one that then fails, because that work was
  done.
* The in-flight slot is separate from the quota. ``Ticket.release`` frees
  it, and the caller must call it in a ``finally`` block so success,
  refusal, timeout, model error, client disconnect and task cancellation
  all give the slot back. A slot not released within
  ``ABANDONED_AFTER_SECONDS`` is treated as abandoned, so a request that hung
  (for example a stalled upload) cannot lock a person out for good.

A request that ends mid-way (the client disconnects or the task is cancelled
after the quota was taken) releases its slot but is not refunded: it may have
already done work, so it stays counted. Tests: ``test_a_client_disconnect_*``
and ``test_a_cancelled_request_*`` in ``tests/integration/test_chat_route.py``.

Memory stays bounded: each user keeps at most ``per_hour`` timestamps (a
refused request adds none), timestamps older than an hour are pruned on
every call for that user, and a sweep at most once a minute drops every user
with no recent timestamps and no question in flight.

The clock is injected (default ``time.monotonic``), so tests never sleep.
Every method is synchronous and has no ``await``, so on one event loop each
call is atomic.

#ASSUME: the portal runs exactly one uvicorn worker, because this state is
per process; with N workers a user could ask N times as many questions.
#VERIFY the deployment's container command keeps a single worker (also
required by the in-process scheduler and ``CHAT_GATE``).
#EDGE: the counters live in memory, so a restart resets every quota and
in-flight slot. #VERIFY that is acceptable: the model-service semaphore and
timeout still bound the load across a restart.
#EDGE: the key is the signed-in username, not an address, so a person on two
tablets shares one budget and two people never share one.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

MINUTE_SECONDS = 60.0
HOUR_SECONDS = 3600.0
# A question that has not finished after this long is treated as abandoned.
ABANDONED_AFTER_SECONDS = 300.0
# How long to tell a browser to wait when the earlier question is running.
IN_FLIGHT_RETRY_SECONDS = 5
_SWEEP_INTERVAL_SECONDS = 60.0

RefusalReason = Literal["in_flight", "per_minute", "per_hour"]

MSG_BUSY = "Your last question is still being answered. Please wait for it to finish."
MSG_TOO_FAST = (
    "You have asked several questions very quickly. Please wait a minute and try again."
)


@dataclass(frozen=True)
class Limits:
    """How many questions one person may ask.

    Attributes:
        per_minute (int): Most questions in any 60 seconds; above zero.
        per_hour (int): Most questions in any hour; above zero.
    """

    per_minute: int
    per_hour: int

    def __post_init__(self) -> None:
        """Reject a limit that is not a positive whole number.

        Raises:
            ValueError: If ``per_minute`` or ``per_hour`` is below one, or
                ``per_minute`` is above ``per_hour`` (the minute cap could
                never be reached).
        """
        if self.per_minute < 1:
            msg = "per_minute must be at least 1"
            raise ValueError(msg)
        if self.per_hour < 1:
            msg = "per_hour must be at least 1"
            raise ValueError(msg)
        if self.per_minute > self.per_hour:
            msg = "per_minute must not be above per_hour"
            raise ValueError(msg)


@dataclass(frozen=True)
class Refusal:
    """A question the limiter turned away.

    Attributes:
        reason (RefusalReason): Which protection was hit.
        retry_after (int): Whole seconds, at least 1, until it may work.
    """

    reason: RefusalReason
    retry_after: int


def refusal_message(refusal: Refusal) -> str:
    """Return the plain-English sentence for a refusal.

    Args:
        refusal (Refusal): What the limiter decided.

    Returns:
        str: One sentence for the person; it never names a limit number or
        a user.
    """
    if refusal.reason == "in_flight":
        return MSG_BUSY
    if refusal.reason == "per_minute":
        return MSG_TOO_FAST
    minutes = max(1, math.ceil(refusal.retry_after / MINUTE_SECONDS))
    unit = "minute" if minutes == 1 else "minutes"
    return (
        "You have reached the number of questions allowed in one hour. "
        f"Please try again in about {minutes} {unit}."
    )


class Ticket:
    """Permission to run one question; release it when the question ends.

    Use as a context manager or call ``release`` in a ``finally`` block.
    """

    def __init__(
        self, limiter: ChatLimiter, key: str, stamp: float, started: float
    ) -> None:
        self.limiter = limiter
        self.key = key
        self.stamp = stamp
        self.started = started
        self.released = False
        self.refunded = False

    def refund(self) -> None:
        """Take this question back out of the quota (no work was done)."""
        self.limiter.refund(self)

    def release(self) -> None:
        """Give back the in-flight slot. Safe to call more than once."""
        self.limiter.release(self)

    def __enter__(self) -> Ticket:
        """Return the ticket.

        Returns:
            Ticket: This ticket.
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the slot whatever happened inside the block."""
        self.release()


class ChatLimiter:
    """In-memory per-user limiter; see the module docstring for the rules.

    Args:
        clock (Callable[[], float]): Returns seconds; must never go backwards.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._asked: dict[str, deque[float]] = {}
        self._active: dict[str, Ticket] = {}
        self._last_sweep = clock()

    @property
    def tracked_users(self) -> int:
        """How many users have a quota record or a question in flight."""
        return len(self._asked.keys() | self._active.keys())

    @property
    def in_flight_count(self) -> int:
        """How many questions hold an in-flight slot."""
        return len(self._active)

    def reset(self) -> None:
        """Forget every user and every in-flight slot."""
        self._asked.clear()
        self._active.clear()
        self._last_sweep = self._clock()

    def acquire(self, key: str, limits: Limits) -> Ticket | Refusal:
        """Start one question for a user, or refuse it.

        A refusal records nothing.

        Args:
            key (str): The signed-in user's identity; never logged.
            limits (Limits): The quotas to apply to this call.

        Returns:
            Ticket | Refusal: A ticket to release when the question ends, or
            the reason it was refused and when to retry.
        """
        now = self._clock()
        self._sweep(now)
        active = self._active.get(key)
        if active is not None and now - active.started < ABANDONED_AFTER_SECONDS:
            return Refusal("in_flight", IN_FLIGHT_RETRY_SECONDS)
        stamps = self._asked.setdefault(key, deque())
        while stamps and stamps[0] <= now - HOUR_SECONDS:
            stamps.popleft()
        refusal = self._over_quota(stamps, now, limits)
        if refusal is not None:
            return refusal
        stamps.append(now)
        ticket = Ticket(self, key, now, now)
        self._active[key] = ticket
        return ticket

    def release(self, ticket: Ticket) -> None:
        """Free a ticket's in-flight slot, once.

        Args:
            ticket (Ticket): The ticket to release.
        """
        if ticket.released:
            return
        ticket.released = True
        if self._active.get(ticket.key) is ticket:
            del self._active[ticket.key]

    def refund(self, ticket: Ticket) -> None:
        """Remove a ticket's question from the quota, once.

        Args:
            ticket (Ticket): The ticket to refund.
        """
        if ticket.refunded:
            return
        ticket.refunded = True
        stamps = self._asked.get(ticket.key)
        if stamps is not None and ticket.stamp in stamps:
            stamps.remove(ticket.stamp)

    @staticmethod
    def _over_quota(stamps: deque[float], now: float, limits: Limits) -> Refusal | None:
        recent = [s for s in stamps if s > now - MINUTE_SECONDS]
        minute_wait = 0.0
        if len(recent) >= limits.per_minute:
            blocker = recent[len(recent) - limits.per_minute]
            minute_wait = blocker + MINUTE_SECONDS - now
        hour_wait = 0.0
        if len(stamps) >= limits.per_hour:
            blocker = stamps[len(stamps) - limits.per_hour]
            hour_wait = blocker + HOUR_SECONDS - now
        if minute_wait <= 0 and hour_wait <= 0:
            return None
        reason: RefusalReason = "per_hour" if hour_wait >= minute_wait else "per_minute"
        return Refusal(reason, max(1, math.ceil(max(minute_wait, hour_wait))))

    def _sweep(self, now: float) -> None:
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        for key, ticket in list(self._active.items()):
            if now - ticket.started >= ABANDONED_AFTER_SECONDS:
                del self._active[key]
        for key, stamps in list(self._asked.items()):
            while stamps and stamps[0] <= now - HOUR_SECONDS:
                stamps.popleft()
            if not stamps and key not in self._active:
                del self._asked[key]


LIMITER = ChatLimiter()
