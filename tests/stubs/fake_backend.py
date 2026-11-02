from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from vibe.app_server._peer_review import PeerBackend, PeerChunk
from vibe.core.types import LLMMessage, Role


class FakeBackend(PeerBackend):
    """Async duck-backend stub to drive the peer review loop without network.

    Provide a finite sequence of PeerChunk objects to be returned by
    `complete`. When exhausted, returns an empty assistant message.
    """

    def __init__(
        self,
        chunks: PeerChunk
        | Iterable[PeerChunk]
        | Iterable[Iterable[PeerChunk]]
        | None = None,
        *,
        exception_to_raise: Exception | None = None,
    ) -> None:
        """Fake backend that will output the given chunks in the order they are given.

        chunks: A single chunk, a sequence of chunks, or a sequence of sequences
        of chunks. A single chunk is outputted as such on the first completion.
        A sequence of chunks is one stream: the first completion aggregates it.
        A sequence of sequences is one stream per completion; later completions
        output an empty assistant message once the streams run out.
        """
        self._requests_messages: list[list[LLMMessage]] = []
        self._requests_tools: list[Any] = []
        self._requests_tool_choices: list[Any] = []
        self._exception_to_raise = exception_to_raise

        self._streams: list[list[PeerChunk]]
        if chunks is None:
            self._streams = []
            return
        if isinstance(chunks, PeerChunk):
            self._streams = [[chunks]]
            return
        materialized = list(chunks)
        if all(isinstance(chunk, PeerChunk) for chunk in materialized):
            self._streams = [cast(list[PeerChunk], materialized)]
            return
        self._streams = [
            list(stream) for stream in cast(Iterable[Iterable[PeerChunk]], materialized)
        ]

    @property
    def requests_messages(self) -> list[list[LLMMessage]]:
        return self._requests_messages

    @property
    def requests_tools(self) -> list[Any]:
        return self._requests_tools

    @property
    def requests_tool_choices(self) -> list[Any]:
        return self._requests_tool_choices

    async def __aenter__(self) -> FakeBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def complete(self, **kwargs: Any) -> PeerChunk:
        self._requests_messages.append(list(kwargs.get("messages") or []))
        self._requests_tools.append(kwargs.get("tools"))
        self._requests_tool_choices.append(kwargs.get("tool_choice"))
        if self._exception_to_raise is not None:
            raise self._exception_to_raise
        if self._streams:
            stream = self._streams.pop(0)
            text = "".join(chunk.message.content or "" for chunk in stream)
            tool_calls = [
                tool_call
                for chunk in stream
                for tool_call in (chunk.message.tool_calls or [])
            ]
            return PeerChunk(
                message=LLMMessage(
                    role=Role.assistant, content=text, tool_calls=tool_calls or None
                )
            )
        return PeerChunk(message=LLMMessage(role=Role.assistant, content=""))
