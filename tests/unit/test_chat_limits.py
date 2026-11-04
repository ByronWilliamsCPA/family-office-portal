# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Per-user question limits, tested with an injected clock (no sleeping).

User keys here are made-up strings. The limiter never sees real identities
in tests, and none of its messages carry the key.
"""

from __future__ import annotations

import pytest

from app.chat.limits import (
    ABANDONED_AFTER_SECONDS,
    MSG_BUSY,
    MSG_TOO_FAST,
    ChatLimiter,
    Limits,
    Refusal,
    Ticket,
    refusal_message,
)
from tests.unit.chat_fakes import FakeClock

DEFAULTS = Limits(per_minute=6, per_hour=60)


def _ask(
    limiter: ChatLimiter, key: str = "user-a", limits: Limits = DEFAULTS
) -> Ticket:
    """Acquire and finish one question, returning the (released) ticket."""
    ticket = limiter.acquire(key, limits)
    assert isinstance(ticket, Ticket), ticket
    ticket.release()
    return ticket


def _refused(
    limiter: ChatLimiter, key: str = "user-a", limits: Limits = DEFAULTS
) -> Refusal:
    """Acquire and expect a refusal."""
    result = limiter.acquire(key, limits)
    assert isinstance(result, Refusal), result
    return result


def test_limits_must_be_positive_whole_numbers() -> None:
    """A zero or negative limit is a programming error, not a lockout."""
    for bad in (0, -1):
        with pytest.raises(ValueError, match="per_minute"):
            Limits(per_minute=bad, per_hour=60)
        with pytest.raises(ValueError, match="per_hour"):
            Limits(per_minute=6, per_hour=bad)


def test_the_seventh_question_in_a_minute_is_refused_and_clears_after_60s() -> None:
    """Six questions pass; the seventh trips the per-minute limit."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    for _ in range(6):
        _ask(limiter)
    refusal = _refused(limiter)
    assert refusal.reason == "per_minute"
    assert refusal.retry_after == 60
    clock.advance(59.0)
    assert _refused(limiter).retry_after == 1
    clock.advance(1.0)
    _ask(limiter)


def test_a_refused_request_does_not_extend_the_window() -> None:
    """Hammering while limited must not push the clearing time later."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    for _ in range(6):
        _ask(limiter)
    for _ in range(20):
        clock.advance(2.0)
        _refused(limiter)
    clock.advance(20.0)  # t = 60 since the first six
    _ask(limiter)


def test_questions_spread_over_the_minute_expire_one_at_a_time() -> None:
    """The retry hint points at the oldest question that blocks the user."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    for _ in range(6):
        _ask(limiter)
        clock.advance(10.0)
    # t = 60: the first question (t = 0) has just expired.
    _ask(limiter)
    refusal = _refused(limiter)
    assert refusal.reason == "per_minute"
    assert refusal.retry_after == 10


def test_the_hourly_limit_trips_after_sixty_questions() -> None:
    """Sixty questions in ten minutes pass; the next one waits for the hour."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    for minute in range(10):
        clock.now = minute * 60.0
        for _ in range(6):
            _ask(limiter)
    clock.now = 600.0
    refusal = _refused(limiter)
    assert refusal.reason == "per_hour"
    assert refusal.retry_after == 3000
    clock.now = 3600.0
    _ask(limiter)


def test_two_users_are_independent() -> None:
    """One user at the limit does not stop another."""
    limiter = ChatLimiter(clock=FakeClock())
    for _ in range(6):
        _ask(limiter, "user-a")
    _refused(limiter, "user-a")
    _ask(limiter, "user-b")


def test_a_second_question_in_flight_is_refused_until_the_first_finishes() -> None:
    """One question at a time per user; the refusal consumes nothing."""
    limiter = ChatLimiter(clock=FakeClock())
    first = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(first, Ticket)
    assert limiter.in_flight_count == 1
    for _ in range(10):
        assert _refused(limiter).reason == "in_flight"
    # Another user is not blocked by this user's question.
    _ask(limiter, "user-b")
    first.release()
    assert limiter.in_flight_count == 0
    # Ten refusals did not use up the quota: five more questions still pass.
    for _ in range(5):
        _ask(limiter)
    _refused(limiter)  # six in total now (the first plus five)


def test_in_flight_refusal_has_a_short_retry_hint() -> None:
    """A busy refusal tells the browser to try again in a few seconds."""
    limiter = ChatLimiter(clock=FakeClock())
    held = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(held, Ticket)
    refusal = _refused(limiter)
    assert refusal.reason == "in_flight"
    assert 1 <= refusal.retry_after <= 30


def test_release_is_idempotent() -> None:
    """Releasing twice never frees someone else's slot."""
    limiter = ChatLimiter(clock=FakeClock())
    first = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(first, Ticket)
    first.release()
    second = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(second, Ticket)
    first.release()
    assert limiter.in_flight_count == 1
    assert _refused(limiter).reason == "in_flight"


def test_the_ticket_context_manager_releases_on_error() -> None:
    """An exception inside the block still frees the slot."""
    limiter = ChatLimiter(clock=FakeClock())
    ticket = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(ticket, Ticket)
    with pytest.raises(RuntimeError), ticket:
        msg = "model exploded"
        raise RuntimeError(msg)
    assert limiter.in_flight_count == 0
    _ask(limiter)


def test_a_refund_returns_the_question_to_the_quota() -> None:
    """A question refused before any work (too long, empty) is not counted."""
    limiter = ChatLimiter(clock=FakeClock())
    for _ in range(30):
        ticket = limiter.acquire("user-a", DEFAULTS)
        assert isinstance(ticket, Ticket)
        ticket.refund()
        ticket.refund()  # idempotent
        ticket.release()
    for _ in range(6):
        _ask(limiter)
    _refused(limiter)


def test_a_refund_does_not_free_the_in_flight_slot() -> None:
    """Refund and release are separate: the slot stays until release."""
    limiter = ChatLimiter(clock=FakeClock())
    ticket = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(ticket, Ticket)
    ticket.refund()
    assert _refused(limiter).reason == "in_flight"
    ticket.release()
    _ask(limiter)


def test_an_abandoned_in_flight_slot_expires() -> None:
    """A slot a request never gave back cannot lock a user out for good."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    stuck = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(stuck, Ticket)
    clock.advance(ABANDONED_AFTER_SECONDS - 1)
    assert _refused(limiter).reason == "in_flight"
    clock.advance(1.0)
    fresh = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(fresh, Ticket)
    stuck.release()  # the old request finally ends; the new slot stays held
    assert _refused(limiter).reason == "in_flight"
    fresh.release()
    assert limiter.in_flight_count == 0


def test_idle_users_are_dropped_so_memory_stays_bounded() -> None:
    """After an hour of quiet, a sweep forgets everyone who is not active."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    for number in range(500):
        _ask(limiter, f"user-{number}")
    assert limiter.tracked_users == 500
    clock.advance(3601.0)
    _ask(limiter, "latecomer")
    assert limiter.tracked_users == 1


def test_a_user_with_a_question_in_flight_is_not_swept() -> None:
    """The sweep never drops a user whose question is still running."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    held = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(held, Ticket)
    clock.advance(ABANDONED_AFTER_SECONDS - 10)
    _ask(limiter, "user-b")
    assert limiter.tracked_users == 2
    assert _refused(limiter).reason == "in_flight"


def test_reset_forgets_everything() -> None:
    """Reset is for tests and for clearing state."""
    limiter = ChatLimiter(clock=FakeClock())
    held = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(held, Ticket)
    limiter.reset()
    assert limiter.tracked_users == 0
    assert limiter.in_flight_count == 0
    _ask(limiter)


def test_lowered_limits_apply_to_existing_history() -> None:
    """Limits are read per call, so a lower value takes effect at once."""
    limiter = ChatLimiter(clock=FakeClock())
    for _ in range(4):
        _ask(limiter)
    assert _refused(limiter, limits=Limits(per_minute=3, per_hour=60)).reason == (
        "per_minute"
    )


def test_messages_are_plain_and_carry_no_identity() -> None:
    """Each refusal gives one plain sentence and never names the user."""
    busy = refusal_message(Refusal("in_flight", 5))
    fast = refusal_message(Refusal("per_minute", 30))
    hour = refusal_message(Refusal("per_hour", 3000))
    assert busy == MSG_BUSY
    assert fast == MSG_TOO_FAST
    assert "wait for it to finish" in busy
    assert "wait a minute" in fast
    assert "50 minutes" in hour
    assert "1 minute" in refusal_message(Refusal("per_hour", 30))
    assert "2 minutes" in refusal_message(Refusal("per_hour", 61))
    for text in (busy, fast, hour):
        assert "user-a" not in text
        assert "429" not in text
        assert "limit exceeded" not in text.lower()


def test_an_hour_old_question_is_pruned_between_sweeps() -> None:
    """The user's own call prunes old stamps even when no sweep is due."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    tight = Limits(per_minute=1, per_hour=1)
    _ask(limiter, "user-a", tight)
    clock.advance(3599.0)
    _ask(limiter, "user-b", tight)  # sweeps; the first stamp is still in range
    clock.advance(1.0)  # no sweep is due, but the first stamp is now an hour old
    _ask(limiter, "user-a", tight)


def test_another_users_call_sweeps_an_abandoned_slot() -> None:
    """A sweep frees a slot nobody released, even if its owner never returns."""
    clock = FakeClock()
    limiter = ChatLimiter(clock=clock)
    stuck = limiter.acquire("user-a", DEFAULTS)
    assert isinstance(stuck, Ticket)
    clock.advance(ABANDONED_AFTER_SECONDS)
    _ask(limiter, "user-b")
    assert limiter.in_flight_count == 0


def test_a_per_minute_limit_above_the_per_hour_limit_is_refused() -> None:
    """The minute cap could never be reached if it exceeded the hour cap."""
    with pytest.raises(ValueError, match=r"per_minute must not be above per_hour"):
        Limits(per_minute=61, per_hour=60)


def test_equal_minute_and_hour_limits_are_allowed() -> None:
    """Equal limits are consistent, so they pass."""
    assert Limits(per_minute=10, per_hour=10) == Limits(per_minute=10, per_hour=10)
