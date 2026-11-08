"""Live-steer dispatch in the local action executor.

Core emits a ``live_steer`` action only when the adapter's model facts say it
can steer an open stream. The executor forwards it to the steerer registered
for that completion, or answers ``live_steer_declined`` so Core falls back to
the deferred steer.
"""

from __future__ import annotations

import pytest

from mistralai_vibe_local_harness.protocol import (
    RustLiveSteerAcceptedEvent,
    RustLiveSteerAction,
    RustLiveSteerDeclinedEvent,
    RustLLMCallAction,
    RustModelInputUpdate,
    RustModelMessageReplace,
    RustModelToolCatalogKeep,
    RustTextContentBlock,
)
from mistralai_vibe_local_harness.vibe._completion import create_live_steerer
from mistralai_vibe_local_harness.vibe._local_actions import (
    HookHandlers,
    _LocalActionState,
)
from mistralai_vibe_local_harness.vibe._runtime_config import LocalRuntimeAdapterConfig


def _action(completion_action_id: str = "completion-1") -> RustLiveSteerAction:
    return RustLiveSteerAction(
        action_id="steer-1",
        turn_id="turn-1",
        completion_action_id=completion_action_id,
        content=[RustTextContentBlock(text="change direction")],
    )


def _state() -> _LocalActionState:
    return _LocalActionState(
        LocalRuntimeAdapterConfig(), None, None, HookHandlers(), "session-1"
    )


class _FakeSteerer:
    def __init__(self, accepted: bool = True, raises: bool = False) -> None:
        self.accepted = accepted
        self.raises = raises
        self.seen: list[str] = []

    async def steer(self, text: str) -> bool:
        self.seen.append(text)
        if self.raises:
            raise RuntimeError("provider gone")
        return self.accepted


@pytest.mark.asyncio
async def test_live_steer_without_a_registered_stream_is_declined() -> None:
    state = _state()
    event = await state.execute(_action())
    assert isinstance(event, RustLiveSteerDeclinedEvent)
    assert event.action_id == "steer-1"
    assert event.turn_id == "turn-1"
    assert event.reason is not None


@pytest.mark.asyncio
async def test_live_steer_reaches_the_registered_stream_and_accepts() -> None:
    state = _state()
    steerer = _FakeSteerer(accepted=True)
    state._live_steerers["completion-1"] = steerer
    event = await state.execute(_action())
    assert isinstance(event, RustLiveSteerAcceptedEvent)
    assert event.action_id == "steer-1"
    assert steerer.seen == ["change direction"]


@pytest.mark.asyncio
async def test_live_steer_declined_when_the_provider_refuses() -> None:
    state = _state()
    state._live_steerers["completion-1"] = _FakeSteerer(accepted=False)
    event = await state.execute(_action())
    assert isinstance(event, RustLiveSteerDeclinedEvent)
    assert event.reason == "provider declined the steer"


@pytest.mark.asyncio
async def test_live_steer_declined_when_the_steerer_raises() -> None:
    state = _state()
    state._live_steerers["completion-1"] = _FakeSteerer(raises=True)
    event = await state.execute(_action())
    assert isinstance(event, RustLiveSteerDeclinedEvent)
    assert event.reason == "provider gone"


def test_no_adapter_provides_a_steerer_yet() -> None:
    # The Responses steer channel exists only on a WebSocket transport; until an
    # adapter implements one, Core's live_steer capability stays false and no
    # steerer is ever registered.
    config = LocalRuntimeAdapterConfig()
    llm_call = RustLLMCallAction(
        action_id="completion-1",
        turn_id="turn-1",
        iteration=0,
        max_iterations=None,
        model_input=RustModelInputUpdate(
            messages=RustModelMessageReplace(revision=0, messages=[]),
            tool_catalog=RustModelToolCatalogKeep(revision=0),
        ),
    )
    assert create_live_steerer(config, llm_call) is None
