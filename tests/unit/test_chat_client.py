# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the chat model client (app.chat.client)."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.chat.client import (
    CHAT_GATE,
    CHAT_PATH,
    CHAT_SLOTS,
    MAX_TOKENS_IMAGE,
    MAX_TOKENS_TEXT,
    ChatClient,
    ChatError,
    build_payload,
    parse_answer,
)
from app.chat.images import PreparedImage
from app.chat.settings import ChatConnection
from tests.unit.chat_fakes import FakeModel, chat_answer, random_key

IMAGE = PreparedImage(data=b"\xff\xd8jpeg", width=10, height=10)


def _connection(timeout: float = 5.0, model: str = "") -> ChatConnection:
    return ChatConnection(
        base_url="http://chat.test",
        api_key=random_key(),
        model=model,
        timeout_seconds=timeout,
    )


def test_chat_slots_equal_two() -> None:
    """The provider has two slots; the portal semaphore must match."""
    assert CHAT_SLOTS == 2


def test_request_body_has_only_server_fields() -> None:
    """The body has exactly messages, stream, max_tokens (and model if set)."""
    body = build_payload(model="", system_prompt="sys", question="q")
    assert set(body) == {"messages", "stream", "max_tokens"}
    assert body["stream"] is False
    assert body["max_tokens"] == MAX_TOKENS_TEXT
    assert "chat_template_kwargs" not in body
    with_model = build_payload(model="qwen", system_prompt="sys", question="q")
    assert with_model["model"] == "qwen"


def test_image_request_uses_lower_cap_and_one_image() -> None:
    """An image request carries one image part and max_tokens 500."""
    body = build_payload(model="", system_prompt="sys", question="q", image=IMAGE)
    assert body["max_tokens"] == MAX_TOKENS_IMAGE == 500
    messages = body["messages"]
    assert isinstance(messages, list)
    user = messages[1]
    assert user["role"] == "user"
    parts = user["content"]
    assert [p["type"] for p in parts] == ["text", "image_url"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def _response(
    status: int = 200, body: object = None, text: str | None = None
) -> httpx.Response:
    if text is not None:
        return httpx.Response(status, text=text)
    return httpx.Response(status, json=body)


def test_parse_answer_reads_only_message_content() -> None:
    """reasoning_content is ignored; content is returned."""
    body = chat_answer("Visible answer.", reasoning_content="hidden thoughts")
    assert parse_answer(_response(body=body)) == "Visible answer."


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"choices": []},
        {"choices": ["x"]},
        {"choices": [{"message": "x"}]},
        {"choices": [{"message": {"content": None}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {"reasoning_content": "only thoughts"}}]},
        ["not", "a", "dict"],
    ],
)
def test_parse_answer_rejects_unusable_bodies(body: object) -> None:
    """Anything but a non-blank string at choices[0].message.content fails."""
    with pytest.raises(ChatError) as info:
        parse_answer(_response(body=body))
    assert info.value.reason == "bad_response"


def test_parse_answer_rejects_non_json() -> None:
    """A body that is not JSON is a bad response."""
    with pytest.raises(ChatError) as info:
        parse_answer(_response(text="<html>"))
    assert info.value.reason == "bad_response"


@pytest.mark.parametrize("status", [401, 500, 503])
def test_parse_answer_treats_errors_as_unavailable(status: int) -> None:
    """A non-200 status means the service is unavailable."""
    with pytest.raises(ChatError) as info:
        parse_answer(_response(status=status, body={}))
    assert info.value.reason == "unavailable"


async def test_ask_posts_bearer_key_to_chat_path() -> None:
    """The request goes to /v1/chat/completions with the bearer key."""
    model = FakeModel(answer="Hello.")
    connection = _connection()
    client = ChatClient(connection, transport=model.transport())
    answer = await client.ask(system_prompt="sys", question="q")
    assert answer == "Hello."
    request = model.requests[0]
    assert request.url.path == CHAT_PATH
    assert request.headers["authorization"] == f"Bearer {connection.api_key}"
    assert json.loads(request.content)["stream"] is False
    assert client.timeout_seconds == 5.0


async def test_ask_maps_connect_error_to_unavailable() -> None:
    """A connection failure is reported as unavailable, once."""
    model = FakeModel(raise_error=httpx.ConnectError("refused"))
    client = ChatClient(_connection(), transport=model.transport())
    with pytest.raises(ChatError) as info:
        await client.ask(system_prompt="sys", question="q")
    assert info.value.reason == "unavailable"
    assert len(model.requests) == 1


async def test_ask_maps_httpx_timeout_to_timeout() -> None:
    """An httpx read timeout is reported as a timeout."""
    model = FakeModel(raise_error=httpx.ReadTimeout("slow"))
    client = ChatClient(_connection(), transport=model.transport())
    with pytest.raises(ChatError) as info:
        await client.ask(system_prompt="sys", question="q")
    assert info.value.reason == "timeout"


async def test_ask_times_out_when_no_slot_frees_up() -> None:
    """Waiting for a slot counts toward the timeout."""
    semaphore = CHAT_GATE.semaphore()
    for _ in range(CHAT_SLOTS):
        await semaphore.acquire()
    try:
        model = FakeModel()
        client = ChatClient(_connection(timeout=0.05), transport=model.transport())
        with pytest.raises(ChatError) as info:
            await client.ask(system_prompt="sys", question="q")
        assert info.value.reason == "timeout"
        assert model.requests == []
    finally:
        for _ in range(CHAT_SLOTS):
            semaphore.release()


async def test_no_more_than_two_calls_run_at_once() -> None:
    """Five questions at once never put more than two calls in flight."""
    in_flight = 0
    peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        del request
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json=chat_answer("ok"))

    client = ChatClient(_connection(), transport=httpx.MockTransport(handler))
    answers = await asyncio.gather(
        *(client.ask(system_prompt="s", question="q") for _ in range(5))
    )
    assert answers == ["ok"] * 5
    assert peak == CHAT_SLOTS


def test_gate_makes_a_new_semaphore_per_event_loop() -> None:
    """Each event loop gets its own semaphore (tests use many loops)."""

    async def grab() -> asyncio.Semaphore:
        return CHAT_GATE.semaphore()

    first = asyncio.run(grab())
    second = asyncio.run(grab())
    assert first is not second
