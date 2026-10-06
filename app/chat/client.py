# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Client for the chat model (OpenAI-compatible ``POST /v1/chat/completions``).

Request rules:

* Every request body is built here, from fixed fields. Nothing the browser
  sends is copied into it, so a client-supplied ``chat_template_kwargs``
  (which can turn the model's thinking back on) never reaches the service.
* ``stream`` is false. ``max_tokens`` is 700, or 500 when the request carries
  an image. At most one image goes in a request.
* Only ``choices[0].message.content`` is read; ``reasoning_content`` and any
  other field are ignored.
* At most two chat calls run at once across the portal, because the service
  has two slots. The wait for a slot counts toward the answer timeout.
* A failed or slow call is reported once; there is no retry loop.

#CRITICAL: security: the request body has no path for client fields.
#VERIFY: tests/unit/test_chat_client.py
::test_request_body_has_only_server_fields and
tests/integration/test_chat_route.py
::test_client_chat_template_kwargs_never_reach_the_model.
#ASSUME: concurrency: the portal is the only caller of the chat service, so a
semaphore of 2 here matches its 2 slots. #VERIFY with homelab-infra that no
other service calls chat before relying on 2.
#ASSUME: the service's default temperature suits answers; none is sent
because the provider contract does not state one. #VERIFY against the
homelab-infra bench settings before adding a temperature.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import anyio
import httpx

if TYPE_CHECKING:
    from app.chat.images import PreparedImage
    from app.chat.settings import ChatConnection

CHAT_PATH = "/v1/chat/completions"
CHAT_SLOTS = 2
MAX_TOKENS_TEXT = 700
MAX_TOKENS_IMAGE = 500
_HTTP_OK = 200

TIMEOUT = "timeout"
UNAVAILABLE = "unavailable"
BAD_RESPONSE = "bad_response"


class ChatError(RuntimeError):
    """The model call failed. ``reason`` is a short category safe to log.

    Args:
        reason (str): ``timeout``, ``unavailable`` or ``bad_response``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _ChatGate:
    """One semaphore of ``CHAT_SLOTS`` per running event loop."""

    def __init__(self, slots: int) -> None:
        self._slots = slots
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None

    def semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self._slots)
            self._loop = loop
        return self._semaphore


CHAT_GATE = _ChatGate(CHAT_SLOTS)


def build_payload(
    *,
    model: str,
    system_prompt: str,
    question: str,
    image: PreparedImage | None = None,
) -> dict[str, object]:
    """Build the request body for one question.

    Args:
        model (str): Model name, or empty to leave it out.
        system_prompt (str): Instructions, balance table and passages.
        question (str): The user's question.
        image (PreparedImage | None): One prepared image, if any.

    Returns:
        dict[str, object]: The request body.
    """
    user_content: str | list[dict[str, object]]
    if image is None:
        user_content = question
    else:
        user_content = [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": image.data_url()}},
        ]
    payload: dict[str, object] = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "stream": False,
        "max_tokens": MAX_TOKENS_TEXT if image is None else MAX_TOKENS_IMAGE,
    }
    if model:
        payload["model"] = model
    return payload


def parse_answer(response: httpx.Response) -> str:
    """Read ``choices[0].message.content`` from a response.

    Args:
        response (httpx.Response): The service's answer.

    Returns:
        str: The answer text.

    Raises:
        ChatError: ``unavailable`` for a non-200 status, ``bad_response``
            when the body is not the expected shape or the answer is blank.
    """
    if response.status_code != _HTTP_OK:
        raise ChatError(UNAVAILABLE)
    try:
        body: object = response.json()
    except ValueError as exc:
        raise ChatError(BAD_RESPONSE) from exc
    content: object = None
    if isinstance(body, dict):
        choices = cast("dict[str, object]", body).get("choices")
        if isinstance(choices, list) and choices:
            first = cast("list[object]", choices)[0]
            if isinstance(first, dict):
                message = cast("dict[str, object]", first).get("message")
                if isinstance(message, dict):
                    content = cast("dict[str, object]", message).get("content")
    if not isinstance(content, str) or not content.strip():
        raise ChatError(BAD_RESPONSE)
    return content


class ChatClient:
    """Sends one question at a time to the chat model.

    Args:
        connection (ChatConnection): URL, key, model and timeout.
        transport (httpx.AsyncBaseTransport | None): Transport override for
            tests; None uses the network.
    """

    def __init__(
        self,
        connection: ChatConnection,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._connection = connection
        self._transport = transport

    @property
    def timeout_seconds(self) -> float:
        """The most one answer may take, including the wait for a slot."""
        return self._connection.timeout_seconds

    async def ask(
        self,
        *,
        system_prompt: str,
        question: str,
        image: PreparedImage | None = None,
    ) -> str:
        """Ask the model one question and return its answer text.

        Args:
            system_prompt (str): Instructions, balance table and passages.
            question (str): The user's question.
            image (PreparedImage | None): One prepared image, if any.

        Returns:
            str: The answer text.

        Raises:
            ChatError: ``timeout`` when no answer arrives within the timeout
                (slot wait included), ``unavailable`` when the service cannot
                be reached or refuses, ``bad_response`` for an unusable body.
        """
        payload = build_payload(
            model=self._connection.model,
            system_prompt=system_prompt,
            question=question,
            image=image,
        )
        try:
            with anyio.fail_after(self._connection.timeout_seconds):
                return await self._post(payload)
        except TimeoutError as exc:
            raise ChatError(TIMEOUT) from exc

    async def _post(self, payload: dict[str, object]) -> str:
        async with CHAT_GATE.semaphore():
            async with httpx.AsyncClient(
                base_url=self._connection.base_url,
                headers={"Authorization": f"Bearer {self._connection.api_key}"},
                timeout=self._connection.timeout_seconds,
                transport=self._transport,
            ) as client:
                try:
                    response = await client.post(CHAT_PATH, json=payload)
                except httpx.TimeoutException as exc:
                    raise ChatError(TIMEOUT) from exc
                except httpx.HTTPError as exc:
                    raise ChatError(UNAVAILABLE) from exc
            return parse_answer(response)
