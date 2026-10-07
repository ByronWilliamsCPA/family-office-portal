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
* At most two chat calls run at once in this process, because the service
  has two slots. The wait for a slot counts toward the model-call timeout;
  the search that runs before the call is not counted.
* A failed or slow call is reported once; there is no retry loop. The
  request never follows a redirect and ignores proxy environment variables,
  so the bearer key goes only to the configured URL.

#CRITICAL: security: the request body has no path for client fields.
#VERIFY: tests/unit/test_chat_client.py
::test_request_body_has_only_server_fields and
tests/integration/test_chat_route.py
::test_client_chat_template_kwargs_never_reach_the_model.
#ASSUME: concurrency: the portal is the only caller of the chat service, so a
semaphore of 2 here matches its 2 slots. #VERIFY with homelab-infra that no
other service calls chat before relying on 2.
#ASSUME: concurrency: the semaphore is per process and per event loop, so
the cap of 2 holds only while the portal runs one uvicorn worker (the
Dockerfile CMD uses ``--workers 1``). #VERIFY the Dockerfile CMD and the
replica count before raising either, and size the gate again if they change.
#ASSUME: the service's default temperature suits answers; none is sent
because the provider contract does not state one. #VERIFY against the
homelab-infra bench settings before adding a temperature.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, cast

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

ChatReason = Literal["timeout", "unavailable", "bad_response"]
TIMEOUT: Final = "timeout"
UNAVAILABLE: Final = "unavailable"
BAD_RESPONSE: Final = "bad_response"

# A JSON object read from the model service; its values are checked one by
# one before use.
_JsonObject = dict[str, object]


class ChatError(RuntimeError):
    """The model call failed. ``reason`` is a short category safe to log.

    ``status_code`` and ``error_type`` carry the only extra detail that is
    safe to log: the HTTP status the service returned, and the class name of
    the exception that stopped the call. Neither holds a response body, a
    URL or the key.

    Args:
        reason (ChatReason): ``timeout``, ``unavailable`` or ``bad_response``.
        status_code (int | None): HTTP status returned by the service.
        error_type (str | None): Class name of the underlying exception.
    """

    def __init__(
        self,
        reason: ChatReason,
        *,
        status_code: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason: ChatReason = reason
        self.status_code = status_code
        self.error_type = error_type


@dataclass(frozen=True)
class ModelAnswer:
    """The text of one model answer.

    Attributes:
        text (str): ``choices[0].message.content``.
        truncated (bool): True when the model stopped at its token limit, so
            the answer may end mid-thought.
    """

    text: str
    truncated: bool = False


class _ChatGate:
    """One semaphore of ``CHAT_SLOTS`` per running event loop.

    The cap is per process: a second uvicorn worker or replica gets its own
    gate and doubles the calls the 2-slot service can receive.
    """

    def __init__(self, slots: int) -> None:
        self._slots = slots
        self._loop: asyncio.AbstractEventLoop | None = None
        self._semaphore: asyncio.Semaphore | None = None

    def semaphore(self) -> asyncio.Semaphore:
        """Return this loop's semaphore, creating it on first use.

        Returns:
            asyncio.Semaphore: A semaphore of ``CHAT_SLOTS`` slots.
        """
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
) -> _JsonObject:
    """Build the request body for one question.

    Args:
        model (str): Model name, or empty to leave it out.
        system_prompt (str): Instructions, balance table and passages.
        question (str): The user's question.
        image (PreparedImage | None): One prepared image, if any.

    Returns:
        _JsonObject: The request body.
    """
    user_content: str | list[_JsonObject]
    if image is None:
        user_content = question
    else:
        user_content = [
            {"type": "text", "text": question},
            {"type": "image_url", "image_url": {"url": image.data_url()}},
        ]
    payload: _JsonObject = {
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


def parse_answer(response: httpx.Response) -> ModelAnswer:
    """Read ``choices[0].message.content`` from a response.

    Args:
        response (httpx.Response): The service's answer.

    Returns:
        ModelAnswer: The answer text, and whether the model hit its limit.

    Raises:
        ChatError: ``unavailable`` for a non-200 status, ``bad_response``
            when the body is not the expected shape or the answer is blank.
    """
    if response.status_code != _HTTP_OK:
        raise ChatError(UNAVAILABLE, status_code=response.status_code)
    try:
        body: object = response.json()
    except ValueError as exc:
        raise ChatError(BAD_RESPONSE, error_type=type(exc).__name__) from exc
    content: object = None
    finish: object = None
    if isinstance(body, dict):
        choices = cast("_JsonObject", body).get("choices")
        if isinstance(choices, list) and choices:
            first = cast("list[object]", choices)[0]
            if isinstance(first, dict):
                first_choice = cast("_JsonObject", first)
                finish = first_choice.get("finish_reason")
                message = first_choice.get("message")
                if isinstance(message, dict):
                    content = cast("_JsonObject", message).get("content")
    if not isinstance(content, str) or not content.strip():
        raise ChatError(BAD_RESPONSE)
    return ModelAnswer(text=content, truncated=finish == "length")


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
        """The most one model call may take, including the wait for a slot."""
        return self._connection.timeout_seconds

    async def ask(
        self,
        *,
        system_prompt: str,
        question: str,
        image: PreparedImage | None = None,
    ) -> ModelAnswer:
        """Ask the model one question and return its answer.

        Args:
            system_prompt (str): Instructions, balance table and passages.
            question (str): The user's question.
            image (PreparedImage | None): One prepared image, if any.

        Returns:
            ModelAnswer: The answer text and whether it was cut short.

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

    async def _post(self, payload: _JsonObject) -> ModelAnswer:
        async with CHAT_GATE.semaphore():
            try:
                # trust_env is off and redirects are not followed, so the
                # bearer key goes only to the configured URL.
                async with httpx.AsyncClient(
                    base_url=self._connection.base_url,
                    headers={
                        "Authorization": (
                            f"Bearer {self._connection.api_key.get_secret_value()}"
                        )
                    },
                    timeout=self._connection.timeout_seconds,
                    transport=self._transport,
                    trust_env=False,
                    follow_redirects=False,
                ) as client:
                    response = await client.post(CHAT_PATH, json=payload)
            except httpx.TimeoutException as exc:
                raise ChatError(TIMEOUT, error_type=type(exc).__name__) from exc
            except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
                raise ChatError(UNAVAILABLE, error_type=type(exc).__name__) from exc
            return parse_answer(response)
