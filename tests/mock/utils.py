from __future__ import annotations

from collections.abc import AsyncGenerator

from mistralai_vibe_local_harness.protocol import RustTokenUsage
from mistralai_vibe_local_harness.vibe import UtilityReply
from vibe.app_server._peer_review import PeerChunk
from vibe.core.types import LLMMessage, Role, ToolCall, ToolStreamEvent


def utility_reply(text: str = "Hello!") -> UtilityReply:
    return UtilityReply(
        text=text,
        usage=RustTokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        finish_reason="stop",
    )


def mock_llm_chunk(
    content: str = "Hello!",
    role: Role = Role.assistant,
    tool_calls: list[ToolCall] | None = None,
) -> PeerChunk:
    """One scripted peer-review completion chunk."""
    return PeerChunk(
        message=LLMMessage(role=role, content=content, tool_calls=tool_calls)
    )


async def collect_result[T](gen: AsyncGenerator[ToolStreamEvent | T]) -> T:
    result = None
    async for item in gen:
        if not isinstance(item, ToolStreamEvent):
            result = item
    if result is None:
        raise RuntimeError("Generator did not yield a result")
    return result
