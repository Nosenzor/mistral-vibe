from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterable
import json
from pathlib import Path
import time
from typing import Any, Literal, cast

from pydantic import ValidationError
import pytest

from tests.conftest import build_test_vibe_config
from tests.mock.utils import mock_llm_chunk
from tests.stubs.fake_backend import FakeBackend
from vibe.app_server._config_paths import UTILITY_MODEL_PEER_PATH
from vibe.app_server._dispatch import DispatchResult, RequestFailure
from vibe.app_server._model import validate_wire
from vibe.app_server._patch import make_json_patch
import vibe.app_server._peer_review as peer_review_module
from vibe.app_server._peer_review import (
    PEER_LEDGER_MAX_CONCERNS,
    PEER_MIRROR_MAX_ENTRIES,
    STEER_MESSAGE_PREFIX,
    PeerReviewController,
    PeerReviewSessionPorts,
    StoredPeerReview,
)
from vibe.app_server._peer_tool_executor import (
    PeerReviewToolExecutor,
    WorkspacePeerToolExecutor,
)
from vibe.app_server._session_backend_port import (
    SessionBackend,
    SessionBackendEvent,
    SessionBackendHost,
    SessionBackendResult,
    SessionEventSubscription,
)
from vibe.app_server.events import (
    ClientProjection,
    HistoryEntryAdded,
    HistoryEntryUpdated,
    TurnCompleted,
    TurnStarted,
)
from vibe.app_server.models import (
    IdleSessionStatus,
    PublicEntryGenerationStatus,
    PublicMessageEntry,
    PublicSession,
    PublicSessionState,
    PublicTurn,
    PublicTurnStatus,
    TextContentBlock,
)
from vibe.app_server.protocol import (
    SERVER_METHODS,
    ConfigEffectiveReadResponse,
    ConfigWriteOpWire,
    ConfigWriteParams,
    ConfigWriteResponse,
    EmptyResponse,
    HistoryEntryAddedParams,
    HistoryEntryUpdatedParams,
    Notification,
    PeerConfigureParams,
    PeerConfigView,
    PeerReadParams,
    PeerReviewParams,
    PeerReviewStatus,
    PeerSteerParams,
    PeerSteerResponse,
    ProtocolErrorCode,
    SessionDeleteParams,
    SessionReadParams,
    SessionReadResponse,
    TurnCompletedParams,
    TurnStartedParams,
    TurnSteerParams,
    TurnSteerResponse,
)
from vibe.app_server.server import AppServer
from vibe.app_server.transport import memory_transport_pair
from vibe.core.config import ModelConfig, PeerSeverity, VibeConfigSchema
from vibe.core.config.layers.overrides import OverridesLayer
from vibe.core.tools.base import ToolError
from vibe.core.types import FunctionCall, LLMChunk, LLMMessage, Role, ToolCall


def _config(**overrides: Any) -> VibeConfigSchema:
    return build_test_vibe_config(
        models=[
            ModelConfig(name="model-a", provider="mistral", alias="alpha"),
            ModelConfig(name="model-b", provider="mistral", alias="beta"),
        ],
        **overrides,
    )


class _FakePorts:
    def __init__(self, config: VibeConfigSchema) -> None:
        self._config = config
        self.steered: list[TurnSteerParams] = []
        self.steer_error: RequestFailure | None = None
        self.peer_model_writes: list[str | None] = []
        self.events: list[tuple[str, dict[str, Any]]] = []

    async def effective_config(self, session_id: str) -> VibeConfigSchema:
        return self._config

    async def steer(self, params: TurnSteerParams) -> TurnSteerResponse:
        if self.steer_error is not None:
            raise self.steer_error
        self.steered.append(params)
        return TurnSteerResponse()

    async def set_peer_model(self, session_id: str, alias: str | None) -> None:
        # Simulate the session override layer: the write is visible to the
        # next effective-config read, and an empty value means off.
        self.peer_model_writes.append(alias)
        self._config.utility_models.peer = alias or ""

    async def record_event(self, name: str, properties: dict[str, Any]) -> None:
        self.events.append((name, properties))


class _FakeNotify:
    def __init__(self) -> None:
        self.notifications: list[tuple[str, Any]] = []

    async def __call__(self, method: str, params: Any) -> None:
        self.notifications.append((method, params))


def _steered_text(params: TurnSteerParams) -> str:
    block = params.input[0]
    assert isinstance(block, TextContentBlock)
    return block.text


def _message_text(message: LLMMessage) -> str:
    content = message.content
    assert content is not None
    return content


def _user_content(backend: FakeBackend, request_index: int = 0) -> str:
    return _message_text(backend.requests_messages[request_index][1])


def test_peer_methods_are_advertised() -> None:
    assert {"peer/configure", "peer/read", "peer/steer"} <= set(SERVER_METHODS)


def test_peer_review_params_use_camel_case_and_forbid_extras() -> None:
    params = PeerReviewParams(
        event_id=1,
        session_id="session-1",
        emitted_at=2,
        review_id="review-1",
        turn_id="turn-1",
        target_entry_id="entry-1",
        status=PeerReviewStatus.COMPLETE,
        severity=PeerSeverity.WARNING,
        steerable=True,
        text="nit",
    )

    dumped = params.model_dump(mode="json")

    assert dumped["reviewId"] == "review-1"
    assert dumped["targetEntryId"] == "entry-1"
    assert dumped["status"] == "complete"
    with pytest.raises(ValidationError):
        validate_wire(PeerReviewParams, {**dumped, "bogus": True})
    with pytest.raises(ValidationError):
        validate_wire(PeerConfigureParams, {"session_id": "session-1"})


def test_peer_configure_params_wire_shape() -> None:
    params = validate_wire(
        PeerConfigureParams,
        {
            "sessionId": "session-1",
            "model": "beta",
            "disable": False,
            "autoSteer": True,
        },
    )
    assert params.session_id == "session-1"
    assert params.model == "beta"
    assert params.disable is False
    assert params.auto_steer is True
    # Every field is optional apart from the session id.
    minimal = validate_wire(PeerConfigureParams, {"sessionId": "session-1"})
    assert minimal.model is None
    assert minimal.disable is False
    assert minimal.auto_steer is None
    with pytest.raises(ValidationError):
        validate_wire(PeerConfigureParams, {"sessionId": "session-1", "bogus": True})


def _projection() -> ClientProjection:
    return ClientProjection(
        PublicSessionState(
            event_id=0,
            session=PublicSession(
                id="session-1", status=IdleSessionStatus(), created_at=1, updated_at=1
            ),
            history=[],
            turns=[],
            active_callbacks=[],
        )
    )


def test_peer_review_notification_projects_to_no_state_change() -> None:
    projection = _projection()
    notification = Notification(
        method="peer/review",
        params=PeerReviewParams(
            event_id=1,
            session_id="session-1",
            emitted_at=2,
            review_id="review-1",
            turn_id="turn-1",
            status=PeerReviewStatus.COMPLETE,
            severity=PeerSeverity.WARNING,
            steerable=True,
            text="nit",
        ).model_dump(mode="json"),
    )

    assert projection.consume(notification) is None


def test_peer_review_notification_rejects_malformed_payloads() -> None:
    projection = _projection()
    notification = Notification(method="peer/review", params={"bogus": True})

    with pytest.raises(ValidationError):
        projection.consume(notification)


@pytest.mark.asyncio
async def test_read_reports_disabled_without_a_peer_model() -> None:
    controller = PeerReviewController(notify=_FakeNotify())

    view = await controller.read(
        PeerReadParams(session_id="session-1"), _FakePorts(_config())
    )

    assert view.enabled is False
    assert view.model is None
    assert view.auto_steer is False
    assert view.max_reviews_per_turn == 10
    assert view.auto_steer_severity == PeerSeverity.CRITICAL


@pytest.mark.asyncio
async def test_read_reflects_the_configured_peer_model() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config(utility_models={"peer": "beta"}))

    view = await controller.read(PeerReadParams(session_id="session-1"), ports)

    assert view.enabled is True
    assert view.model == "beta"


@pytest.mark.asyncio
async def test_configure_enables_with_a_known_alias() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config())

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", model="beta"), ports
    )

    assert view.enabled is True
    assert view.model == "beta"
    # The pick lands in the session override layer, not controller memory.
    assert ports.peer_model_writes == ["beta"]
    effective = await ports.effective_config("session-1")
    assert effective.utility_models.peer == "beta"


@pytest.mark.asyncio
async def test_configure_rejects_an_unknown_alias() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config())

    with pytest.raises(RequestFailure) as exc_info:
        await controller.configure(
            PeerConfigureParams(session_id="session-1", model="does-not-exist"), ports
        )

    assert exc_info.value.code is ProtocolErrorCode.INVALID_PARAMS
    assert ports.peer_model_writes == []


@pytest.mark.asyncio
async def test_configure_rejects_an_empty_alias() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config())

    with pytest.raises(RequestFailure):
        await controller.configure(
            PeerConfigureParams(session_id="session-1", model="  "), ports
        )


@pytest.mark.asyncio
async def test_configure_off_overrides_the_config_default() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config(utility_models={"peer": "beta"}))

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", disable=True), ports
    )

    assert view.enabled is False
    assert view.model is None
    # Off is an explicit empty override, so it outranks the config default.
    assert ports.peer_model_writes == [None]
    effective = await ports.effective_config("session-1")
    assert effective.utility_models.peer == ""


@pytest.mark.asyncio
async def test_configure_auto_steer_override_wins_over_config() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(
        _config(utility_models={"peer": "beta"}, peer_review={"auto_steer": True})
    )

    view = await controller.read(PeerReadParams(session_id="session-1"), ports)
    assert view.auto_steer is True

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", auto_steer=False), ports
    )
    assert view.auto_steer is False


@pytest.mark.asyncio
async def test_configure_steer_only_leaves_the_model_untouched() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config(utility_models={"peer": "beta"}))

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", auto_steer=True), ports
    )

    # A steer-only configure must not disable (or re-pick) the duck.
    assert ports.peer_model_writes == []
    assert view.enabled is True
    assert view.model == "beta"
    assert view.auto_steer is True


@pytest.mark.asyncio
async def test_configure_model_and_steer_together() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config(peer_review={"auto_steer": False}))

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", model="beta", auto_steer=True),
        ports,
    )

    assert ports.peer_model_writes == ["beta"]
    assert view.enabled is True
    assert view.model == "beta"
    assert view.auto_steer is True


@pytest.mark.asyncio
async def test_configure_rejects_disable_combined_with_a_model() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config(utility_models={"peer": "beta"}))

    with pytest.raises(RequestFailure) as exc_info:
        await controller.configure(
            PeerConfigureParams(session_id="session-1", model="beta", disable=True),
            ports,
        )

    assert exc_info.value.code is ProtocolErrorCode.INVALID_PARAMS
    assert ports.peer_model_writes == []


@pytest.mark.asyncio
async def test_steer_rejects_an_unknown_review() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config())

    with pytest.raises(RequestFailure) as exc_info:
        await controller.steer(
            PeerSteerParams(session_id="session-1", review_id="nope"), ports
        )

    assert exc_info.value.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_steer_injects_the_marked_critique_once() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    controller.store_review(
        StoredPeerReview(review_id="review-1", turn_id="turn-1", text="nit"),
        session_id="session-1",
    )
    ports = _FakePorts(_config())
    params = PeerSteerParams(session_id="session-1", review_id="review-1")

    first = await controller.steer(params, ports)
    second = await controller.steer(params, ports)

    assert first == PeerSteerResponse(accepted=True, delivered=True)
    assert second == PeerSteerResponse(accepted=True, delivered=True)
    assert len(ports.steered) == 1
    steered = ports.steered[0]
    assert steered.expected_turn_id == "turn-1"
    assert _steered_text(steered) == f"{STEER_MESSAGE_PREFIX}nit"


@pytest.mark.asyncio
async def test_steer_reports_not_delivered_when_the_turn_ended() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    controller.store_review(
        StoredPeerReview(review_id="review-1", turn_id="turn-1", text="nit"),
        session_id="session-1",
    )
    ports = _FakePorts(_config())
    ports.steer_error = RequestFailure(
        ProtocolErrorCode.INVALID_PARAMS, "turn already completed"
    )

    response = await controller.steer(
        PeerSteerParams(session_id="session-1", review_id="review-1"), ports
    )

    assert response == PeerSteerResponse(accepted=False, delivered=False)


def _config_write_response() -> ConfigWriteResponse:
    # The peer controller reads only `applied`/`failures`; the runtime snapshot
    # payload is irrelevant to it, so the fake skips building one.
    return ConfigWriteResponse.model_construct()


class _FakeRootBackend:
    def __init__(self, config: VibeConfigSchema) -> None:
        self.session_id = "session-1"
        self._config = config
        self.written_ops: list[ConfigWriteOpWire] = []
        self.telemetry: list[tuple[str, dict[str, Any]]] = []

    async def dispatch_extension(
        self, method: str, raw_params: dict[str, Any]
    ) -> DispatchResult:
        if method == "telemetry/record":
            self.telemetry.append((raw_params["name"], raw_params["properties"]))
            return DispatchResult(EmptyResponse())
        assert method == "config/effective/read"
        return DispatchResult(
            ConfigEffectiveReadResponse(config=self._config.model_dump(mode="json"))
        )

    async def steer_turn(
        self, params: TurnSteerParams
    ) -> SessionBackendResult[TurnSteerResponse]:
        return SessionBackendResult(response=TurnSteerResponse())

    async def write_config(
        self, params: ConfigWriteParams
    ) -> SessionBackendResult[ConfigWriteResponse]:
        # Simulate the backend applying a config/write batch to the session
        # override layer.
        self.written_ops.extend(params.ops)
        for op in params.ops:
            assert op.path == UTILITY_MODEL_PEER_PATH
            assert op.target_layer == OverridesLayer.NAME
            self._config.utility_models.peer = str(op.value or "")
        return SessionBackendResult(response=_config_write_response())


class _FakeBackendHost:
    async def shutdown(self) -> None: ...


def _make_server() -> AppServer:
    _client_transport, server_transport = memory_transport_pair()
    return AppServer(
        server_transport,
        session_backend_host_factory=lambda _: cast(
            SessionBackendHost, _FakeBackendHost()
        ),
    )


@pytest.mark.asyncio
async def test_peer_methods_route_to_the_controller() -> None:
    server = _make_server()
    root = cast(SessionBackend, _FakeRootBackend(_config()))

    result = await server._dispatch_backend_operation(
        root, "peer/read", {"sessionId": "session-1"}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is False

    result = await server._dispatch_backend_operation(
        root, "peer/configure", {"sessionId": "session-1", "model": "beta"}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is True

    result = await server._dispatch_backend_operation(
        root, "peer/read", {"sessionId": "session-1"}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.model == "beta"


@pytest.mark.asyncio
async def test_peer_configure_writes_the_session_overrides_layer() -> None:
    server = _make_server()
    backend = _FakeRootBackend(_config())
    root = cast(SessionBackend, backend)

    result = await server._dispatch_backend_operation(
        root, "peer/configure", {"sessionId": "session-1", "model": "beta"}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is True

    # /peer lands in the session-scoped overrides layer, not a durable file.
    assert len(backend.written_ops) == 1
    op = backend.written_ops[0]
    assert op.op == "set"
    assert op.path == UTILITY_MODEL_PEER_PATH
    assert op.value == "beta"
    assert op.target_layer == OverridesLayer.NAME

    # /peer off writes an explicit empty override, outranking any default.
    result = await server._dispatch_backend_operation(
        root, "peer/configure", {"sessionId": "session-1", "disable": True}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is False
    assert backend.written_ops[-1].value == ""

    # /peer steer on flips auto-steer without touching the model override.
    result = await server._dispatch_backend_operation(
        root, "peer/configure", {"sessionId": "session-1", "autoSteer": True}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.auto_steer is True
    assert len(backend.written_ops) == 2

    # The override is session memory: a fresh backend has no peer model.
    fresh_root = cast(SessionBackend, _FakeRootBackend(_config()))
    result = await server._dispatch_backend_operation(
        fresh_root, "peer/read", {"sessionId": "session-1"}
    )
    assert result is not None
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is False


@pytest.mark.asyncio
async def test_peer_methods_reject_malformed_payloads() -> None:
    server = _make_server()
    root = cast(SessionBackend, _FakeRootBackend(_config()))

    with pytest.raises(ValidationError):
        await server._dispatch_backend_operation(
            root, "peer/configure", {"sessionId": "session-1", "model": 42}
        )


@pytest.mark.asyncio
async def test_peer_configure_records_telemetry_through_the_backend() -> None:
    server = _make_server()
    backend = _FakeRootBackend(_config())
    root = cast(SessionBackend, backend)

    await server._dispatch_backend_operation(
        root, "peer/configure", {"sessionId": "session-1", "model": "beta"}
    )

    assert backend.telemetry == [
        (
            "vibe.peer_review.configured",
            {
                "enabled": True,
                "auto_steer": False,
                "auto_steer_severity": "critical",
                "max_reviews_per_turn": 10,
            },
        )
    ]


@pytest.mark.asyncio
async def test_peer_steer_records_telemetry_through_the_backend() -> None:
    server = _make_server()
    backend = _FakeRootBackend(_config())
    root = cast(SessionBackend, backend)
    server._peer_review.store_review(
        StoredPeerReview(
            review_id="review-1",
            turn_id="turn-1",
            text="critique",
            severity=PeerSeverity.WARNING,
        ),
        session_id="session-1",
    )

    result = await server._dispatch_backend_operation(
        root, "peer/steer", {"sessionId": "session-1", "reviewId": "review-1"}
    )

    assert result is not None
    assert isinstance(result.response, PeerSteerResponse)
    assert result.response.delivered is True
    assert backend.telemetry == [
        (
            "vibe.peer_review.steer_injected",
            {"trigger": "manual", "severity": "warning", "delivered": True},
        )
    ]


# --- Slice 4: session plumbing (override writes, controller lifecycle) ---


class _FakeSessionBackend(_FakeRootBackend):
    """A session backend double with a controllable event stream."""

    def __init__(
        self,
        config: VibeConfigSchema,
        *,
        session_id: str = "session-1",
        cwd: str | None = "/workspace",
    ) -> None:
        super().__init__(config)
        self.session_id = session_id
        self._cwd = cwd
        self._events: asyncio.Queue[SessionBackendEvent | None] = asyncio.Queue()
        self.shutdown_called = False

    def _snapshot(self) -> SessionReadResponse:
        return SessionReadResponse(
            state=PublicSessionState(
                event_id=0,
                session=PublicSession(
                    id=self.session_id,
                    status=IdleSessionStatus(),
                    created_at=1,
                    updated_at=1,
                    cwd=self._cwd,
                ),
            ),
            last_event_id=0,
        )

    async def read(self, params: SessionReadParams) -> SessionReadResponse:
        return self._snapshot()

    async def subscribe(self, params: SessionReadParams) -> SessionEventSubscription:
        return SessionEventSubscription(
            snapshot=self._snapshot(), events=self._event_stream()
        )

    async def _event_stream(self) -> AsyncIterator[SessionBackendEvent]:
        while True:
            envelope = await self._events.get()
            if envelope is None:
                return
            yield envelope

    async def push(self, envelope: SessionBackendEvent) -> None:
        await self._events.put(envelope)

    async def shutdown(self) -> None:
        self.shutdown_called = True


async def _wait_for(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met within timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_peer_review_lifecycle_follows_backend_activation() -> None:
    server = _make_server()
    backend = _FakeSessionBackend(_config())

    await server._activate_backend(cast(SessionBackend, backend), 200)

    state = server._peer_review._sessions.get("session-1")
    assert state is not None
    assert state.ports is not None
    assert isinstance(state.ports.tool_executor, WorkspacePeerToolExecutor)

    # The server event fan-out taps the controller: turn/started arms the loop.
    await backend.push(_turn_event("turn/started", _turn()))
    await _wait_for(lambda: state.phase is peer_review_module._PeerPhase.ARMED)

    # A backend switch detaches and forgets the old session's duck state.
    replacement = _FakeSessionBackend(_config(), session_id="session-2")
    await server._activate_backend(cast(SessionBackend, replacement), 200)

    assert backend.shutdown_called is True
    assert "session-1" not in server._peer_review._sessions
    replacement_state = server._peer_review._sessions.get("session-2")
    assert replacement_state is not None
    assert replacement_state.ports is not None

    await server.close()


@pytest.mark.asyncio
async def test_peer_review_state_dies_with_server_close() -> None:
    server = _make_server()
    backend = cast(SessionBackend, _FakeSessionBackend(_config()))
    await server._activate_backend(backend, 200)
    assert "session-1" in server._peer_review._sessions

    await server.close()

    assert server._peer_review._sessions == {}


@pytest.mark.asyncio
async def test_peer_review_state_dies_with_session_delete() -> None:
    class _DeleteHost:
        async def delete(self, params: SessionDeleteParams) -> EmptyResponse:
            return EmptyResponse()

    _client_transport, server_transport = memory_transport_pair()
    server = AppServer(
        server_transport,
        session_backend_host_factory=lambda _: cast(SessionBackendHost, _DeleteHost()),
    )
    backend = cast(SessionBackend, _FakeSessionBackend(_config()))
    await server._activate_backend(backend, 200)
    assert "session-1" in server._peer_review._sessions

    result = await server._delete_session({"sessionId": "session-1"})

    assert result is not None
    assert server._peer_review._sessions == {}


# --- Slice 3: the review loop (event tap, mirror, coalesced peer call) ---


class _FakeBackendFactory:
    def __init__(self, backend: FakeBackend) -> None:
        self._backend = backend
        self.calls = 0

    def backend_for(self, model: ModelConfig, config: VibeConfigSchema) -> FakeBackend:
        self.calls += 1
        return self._backend


class _FakeToolExecutor:
    def __init__(
        self, *, supported: Iterable[str] = ("read_file", "grep", "web_fetch")
    ) -> None:
        self._supported = frozenset(supported)
        self.allow_web_tools = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.results: dict[str, str] = {}

    @property
    def supported_tools(self) -> frozenset[str]:
        tools = {"read_file", "grep"} & self._supported
        if self.allow_web_tools:
            tools |= {"web_fetch"} & self._supported
        return frozenset(tools)

    def set_allow_web_tools(self, allow: bool) -> None:
        self.allow_web_tools = allow

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append((name, arguments))
        if name not in self.supported_tools:
            raise ToolError(f"Tool '{name}' is not in the peer review allowlist")
        return self.results.get(name, f"result:{name}")


class _GatedBackend(FakeBackend):
    """Fake backend whose first completion waits on a gate."""

    def __init__(self, chunks: Any, gate: asyncio.Event) -> None:
        super().__init__(chunks)
        self._gate = gate

    async def complete(self, **kwargs: Any) -> LLMChunk:
        await self._gate.wait()
        return await super().complete(**kwargs)


def _make_loop(
    config: VibeConfigSchema,
    backend: FakeBackend,
    *,
    executor: PeerReviewToolExecutor | None = None,
    ports: _FakePorts | None = None,
) -> tuple[
    PeerReviewController,
    _FakeNotify,
    _FakePorts,
    PeerReviewToolExecutor,
    _FakeBackendFactory,
]:
    notify = _FakeNotify()
    factory = _FakeBackendFactory(backend)
    controller = PeerReviewController(notify=notify, backend_factory=factory)
    fake_ports = ports or _FakePorts(config)
    fake_executor = executor or _FakeToolExecutor()
    controller.attach(
        "session-1",
        PeerReviewSessionPorts(backend=fake_ports, tool_executor=fake_executor),
    )
    return controller, notify, fake_ports, fake_executor, factory


async def _drain(
    controller: PeerReviewController, session_id: str = "session-1"
) -> None:
    state = controller._sessions.get(session_id)
    if state is None:
        return
    while state.review_task is not None:
        await state.review_task


def _turn(
    turn_id: str = "turn-1", status: PublicTurnStatus = PublicTurnStatus.IN_PROGRESS
) -> PublicTurn:
    return PublicTurn(id=turn_id, session_id="session-1", status=status, started_at=1)


def _turn_event(method: str, turn: PublicTurn) -> SessionBackendEvent:
    if method == "turn/started":
        params: Any = TurnStartedParams(
            event_id=1, session_id=turn.session_id, emitted_at=1, turn=turn
        )
        event: Any = TurnStarted(turn)
    else:
        params = TurnCompletedParams(
            event_id=1, session_id=turn.session_id, emitted_at=1, turn=turn
        )
        event = TurnCompleted(turn)
    return SessionBackendEvent(
        event=event,
        method=method,
        params=params,
        session_id=turn.session_id,
        event_id=1,
    )


def _message_entry(
    entry_id: str,
    text: str,
    *,
    role: Literal["system", "user", "assistant"] = "assistant",
    turn_id: str = "turn-1",
) -> PublicMessageEntry:
    return PublicMessageEntry(
        id=entry_id,
        session_id="session-1",
        turn_id=turn_id,
        created_at=1,
        updated_at=1,
        generation_status=PublicEntryGenerationStatus.COMPLETED,
        role=role,
        content=[TextContentBlock(text=text)],
    )


def _entry_added(entry: PublicMessageEntry) -> SessionBackendEvent:
    params = HistoryEntryAddedParams(
        event_id=1,
        session_id=entry.session_id,
        emitted_at=1,
        turn_id=entry.turn_id,
        entry=entry,
    )
    return SessionBackendEvent(
        event=HistoryEntryAdded(entry),
        method="history/entryAdded",
        params=params,
        session_id=entry.session_id,
        event_id=1,
    )


def _entry_updated(
    previous: PublicMessageEntry, entry: PublicMessageEntry
) -> SessionBackendEvent:
    patch = make_json_patch(
        previous.model_dump(mode="json", by_alias=True),
        entry.model_dump(mode="json", by_alias=True),
    )
    params = HistoryEntryUpdatedParams(
        event_id=1,
        session_id=entry.session_id,
        emitted_at=1,
        turn_id=entry.turn_id,
        entry_id=entry.id,
        patch=patch,
    )
    return SessionBackendEvent(
        event=HistoryEntryUpdated(previous=previous, entry=entry, patch=patch),
        method="history/entryUpdated",
        params=params,
        session_id=entry.session_id,
        event_id=1,
    )


def _verdict_chunk(
    severity: str = "info", steerable: bool = False, text: str = "No concerns."
) -> LLMChunk:
    return mock_llm_chunk(
        content=json.dumps({"severity": severity, "steerable": steerable, "text": text})
    )


def _tool_call_chunk(
    name: str, arguments: dict[str, Any], call_id: str = "call-1"
) -> LLMChunk:
    return mock_llm_chunk(
        tool_calls=[
            ToolCall(
                id=call_id,
                index=0,
                function=FunctionCall(name=name, arguments=json.dumps(arguments)),
            )
        ]
    )


def _complete_notifications(notify: _FakeNotify) -> list[PeerReviewParams]:
    return [
        params
        for _, params in notify.notifications
        if params.status is PeerReviewStatus.COMPLETE
    ]


@pytest.mark.asyncio
async def test_burst_of_entries_yields_one_coalesced_review() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    for index in range(3):
        await controller.handle_backend_event(
            _entry_added(_message_entry(f"entry-{index}", f"step {index}"))
        )
    await _drain(controller)

    assert len(backend.requests_messages) == 1
    user_content = _user_content(backend)
    for index in range(3):
        assert f"assistant: step {index}" in user_content
    statuses = [params.status for _, params in notify.notifications]
    assert statuses == [PeerReviewStatus.RUNNING, PeerReviewStatus.COMPLETE]


@pytest.mark.asyncio
async def test_review_context_is_the_independent_duck_context() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-user", "do the thing", role="user"))
    )
    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "working on it"))
    )
    await _drain(controller)

    messages = backend.requests_messages[0]
    assert messages[0].role is Role.system
    assert "rubber duck" in _message_text(messages[0])
    user_content = _message_text(messages[1])
    assert "## User instructions for this turn" in user_content
    assert "do the thing" in user_content
    assert "## New transcript material since the last review" in user_content
    assert "assistant: working on it" in user_content
    assert "## Concerns ledger" in user_content


@pytest.mark.asyncio
async def test_user_instructions_survive_when_the_user_entry_precedes_turn_started() -> (
    None
):
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    # Snapshot order puts history entries before turns; the user's entry can
    # arrive while the duck is still idle.
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-user", "do the thing", role="user"))
    )
    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(_entry_added(_message_entry("entry-1", "ok")))
    await _drain(controller)

    assert "do the thing" in _user_content(backend)


@pytest.mark.asyncio
async def test_budget_exhaustion_skips_further_reviews() -> None:
    backend = FakeBackend(chunks=[[_verdict_chunk()]])
    controller, _, _, _, factory = _make_loop(
        _config(
            utility_models={"peer": "beta"}, peer_review={"max_reviews_per_turn": 1}
        ),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two"))
    )
    await _drain(controller)

    assert factory.calls == 1
    assert len(backend.requests_messages) == 1


@pytest.mark.asyncio
async def test_provider_error_fails_the_review_and_silences_the_duck() -> None:
    backend = FakeBackend(exception_to_raise=RuntimeError("provider boom"))
    controller, notify, _, _, factory = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)
    # The main turn is unaffected: the tap keeps consuming events.
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two"))
    )
    await controller.handle_backend_event(
        _turn_event("turn/completed", _turn(status=PublicTurnStatus.COMPLETED))
    )
    await _drain(controller)

    failed = [
        params
        for _, params in notify.notifications
        if params.status is PeerReviewStatus.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].error is not None
    assert "provider boom" in failed[0].error
    assert factory.calls == 1  # no automatic retry within the turn
    assert controller._sessions["session-1"].silenced is True


@pytest.mark.asyncio
async def test_auto_steer_fires_at_threshold_when_steerable() -> None:
    backend = FakeBackend(
        chunks=[_verdict_chunk(severity="critical", steerable=True, text="stop that")]
    )
    controller, notify, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"auto_steer": True}),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert len(ports.steered) == 1
    assert _steered_text(ports.steered[0]) == f"{STEER_MESSAGE_PREFIX}stop that"
    complete = _complete_notifications(notify)[0]
    assert complete.delivered is True
    assert complete.severity is PeerSeverity.CRITICAL
    state = controller._sessions["session-1"]
    assert state.ledger == ["DELIVERED [critical]: stop that"]


@pytest.mark.asyncio
async def test_auto_steer_skips_below_threshold_and_unsteerable_verdicts() -> None:
    backend = FakeBackend(
        chunks=[
            [_verdict_chunk(severity="warning", steerable=True, text="drift")],
            [_verdict_chunk(severity="critical", steerable=False, text="note")],
        ]
    )
    controller, notify, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"auto_steer": True}),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two"))
    )
    await _drain(controller)

    assert ports.steered == []
    completes = _complete_notifications(notify)
    assert [params.delivered for params in completes] == [False, False]


@pytest.mark.asyncio
async def test_auto_steer_threshold_is_configurable() -> None:
    backend = FakeBackend(
        chunks=[_verdict_chunk(severity="warning", steerable=True, text="drift")]
    )
    controller, _, ports, _, _ = _make_loop(
        _config(
            utility_models={"peer": "beta"},
            peer_review={"auto_steer": True, "auto_steer_severity": "warning"},
        ),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert len(ports.steered) == 1


@pytest.mark.asyncio
async def test_auto_steer_is_off_by_default() -> None:
    backend = FakeBackend(
        chunks=[_verdict_chunk(severity="critical", steerable=True, text="stop")]
    )
    controller, notify, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert ports.steered == []
    assert _complete_notifications(notify)[0].delivered is False


@pytest.mark.asyncio
async def test_review_finishing_after_turn_end_is_marked_not_delivered() -> None:
    gate = asyncio.Event()
    backend = _GatedBackend(
        [_verdict_chunk(severity="critical", steerable=True, text="too late")], gate
    )
    controller, notify, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"auto_steer": True}),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    # The turn completes normally while the peer call is in flight.
    await controller.handle_backend_event(
        _turn_event("turn/completed", _turn(status=PublicTurnStatus.COMPLETED))
    )
    gate.set()
    await _drain(controller)

    assert ports.steered == []
    complete = _complete_notifications(notify)[0]
    assert complete.delivered is False
    assert complete.text == "too late"


@pytest.mark.asyncio
async def test_interrupt_cancels_the_in_flight_review_and_rearms_next_turn() -> None:
    gate = asyncio.Event()
    backend = _GatedBackend([[_verdict_chunk()], [_verdict_chunk()]], gate)
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn("turn-1")))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one", turn_id="turn-1"))
    )
    await asyncio.sleep(0)  # let the review task start and park on the gate
    await controller.handle_backend_event(
        _turn_event(
            "turn/completed", _turn("turn-1", status=PublicTurnStatus.INTERRUPTED)
        )
    )

    failed = [
        params
        for _, params in notify.notifications
        if params.status is PeerReviewStatus.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].error == "cancelled"

    gate.set()
    await controller.handle_backend_event(_turn_event("turn/started", _turn("turn-2")))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two", turn_id="turn-2"))
    )
    await _drain(controller)

    assert len(backend.requests_messages) == 1
    assert _complete_notifications(notify)[-1].turn_id == "turn-2"


@pytest.mark.asyncio
async def test_tool_calls_are_executed_and_results_fed_back() -> None:
    backend = FakeBackend(
        chunks=[
            [_tool_call_chunk("read_file", {"file_path": "a.py"})],
            [_verdict_chunk(severity="warning", text="found it")],
        ]
    )
    executor = _FakeToolExecutor()
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend, executor=executor
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert executor.calls == [("read_file", {"file_path": "a.py"})]
    assert len(backend.requests_messages) == 2
    tool_messages = [
        message for message in backend.requests_messages[1] if message.role is Role.tool
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].name == "read_file"
    assert tool_messages[0].tool_call_id == "call-1"
    assert "result:read_file" in _message_text(tool_messages[0])
    catalog_names = [tool.function.name for tool in backend.requests_tools[0]]
    assert catalog_names == ["read_file", "grep", "web_fetch"]


@pytest.mark.asyncio
async def test_tool_round_cap_stops_rounds_and_still_returns_a_verdict() -> None:
    backend = FakeBackend(
        chunks=[
            [_tool_call_chunk("read_file", {"file_path": "a.py"}, call_id="call-1")],
            [_verdict_chunk(text="verdict from partial evidence")],
        ]
    )
    executor = _FakeToolExecutor()
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"max_tool_rounds": 1}),
        backend,
        executor=executor,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert executor.calls == [("read_file", {"file_path": "a.py"})]
    assert len(backend.requests_messages) == 2
    # The cap is exhausted: the final completion runs without tools.
    assert backend.requests_tools[1] is None
    assert backend.requests_tool_choices[1] is None
    assert _complete_notifications(notify)[0].text == "verdict from partial evidence"


@pytest.mark.asyncio
async def test_forged_non_allowlisted_tool_call_is_refused(tmp_path: Path) -> None:
    backend = FakeBackend(
        chunks=[
            [_tool_call_chunk("write_file", {"file_path": "a.py", "content": "x"})],
            [_verdict_chunk(text="refused")],
        ]
    )
    executor = WorkspacePeerToolExecutor(tmp_path)
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend, executor=executor
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    tool_messages = [
        message for message in backend.requests_messages[1] if message.role is Role.tool
    ]
    assert len(tool_messages) == 1
    assert "not in the peer review allowlist" in _message_text(tool_messages[0])
    # The review continues after the refusal.
    assert _complete_notifications(notify)[0].text == "refused"


@pytest.mark.asyncio
async def test_catalog_omits_web_tools_when_disallowed() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, _, _, _ = _make_loop(
        _config(
            utility_models={"peer": "beta"}, peer_review={"allow_web_tools": False}
        ),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    catalog_names = [tool.function.name for tool in backend.requests_tools[0]]
    assert catalog_names == ["read_file", "grep"]


@pytest.mark.asyncio
async def test_zero_tool_rounds_disables_the_catalog() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"max_tool_rounds": 0}),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert backend.requests_tools[0] is None
    assert backend.requests_tool_choices[0] is None


@pytest.mark.asyncio
async def test_malformed_verdict_falls_back_to_plain_text_at_info() -> None:
    backend = FakeBackend(chunks=[mock_llm_chunk(content="this is not json")])
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    complete = _complete_notifications(notify)[0]
    assert complete.severity is PeerSeverity.INFO
    assert complete.steerable is False
    assert complete.text == "this is not json"


@pytest.mark.asyncio
async def test_hung_peer_call_fails_at_the_review_timeout() -> None:
    gate = asyncio.Event()  # never set: the peer call hangs
    backend = _GatedBackend([_verdict_chunk()], gate)
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"timeout_seconds": 0.05}),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    failed = [
        params
        for _, params in notify.notifications
        if params.status is PeerReviewStatus.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].error == "review timed out"
    state = controller._sessions["session-1"]
    assert state.silenced is True
    assert state.budget_used == 1  # the attempt counts against the budget


@pytest.mark.asyncio
async def test_mirror_is_bounded_to_the_latest_entries() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    for index in range(PEER_MIRROR_MAX_ENTRIES + 5):
        await controller.handle_backend_event(
            _entry_added(_message_entry(f"entry-{index}", f"marker-{index}"))
        )
    await _drain(controller)

    user_content = _user_content(backend)
    assert "marker-0" not in user_content
    assert f"marker-{PEER_MIRROR_MAX_ENTRIES + 4}" in user_content


@pytest.mark.asyncio
async def test_entry_updates_refresh_the_mirror() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    partial = _message_entry("entry-1", "partial")
    await controller.handle_backend_event(_entry_added(partial))
    await controller.handle_backend_event(
        _entry_updated(partial, _message_entry("entry-1", "partial full text"))
    )
    await _drain(controller)

    user_content = _user_content(backend)
    assert "assistant: partial full text" in user_content
    assert user_content.count("assistant:") == 1


@pytest.mark.asyncio
async def test_ledger_records_only_warning_and_above() -> None:
    backend = FakeBackend(
        chunks=[
            [_verdict_chunk(severity="info", text="fine")],
            [_verdict_chunk(severity="warning", text="drift")],
            [_verdict_chunk()],
        ]
    )
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )
    state = controller._sessions["session-1"]

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)
    assert state.ledger == []

    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two"))
    )
    await _drain(controller)
    assert state.ledger == ["OPEN [warning]: drift"]

    # The next review's context carries the ledger.
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-3", "three"))
    )
    await _drain(controller)
    assert "OPEN [warning]: drift" in _user_content(backend, 2)


@pytest.mark.asyncio
async def test_ledger_cap_drops_oldest_delivered_first() -> None:
    backend = FakeBackend(
        chunks=[_verdict_chunk(severity="warning", text="new concern")]
    )
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )
    state = controller._sessions["session-1"]
    state.ledger.extend(
        [f"DELIVERED [warning]: old {index}" for index in range(5)]
        + [f"OPEN [critical]: open {index}" for index in range(5)]
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert len(state.ledger) == PEER_LEDGER_MAX_CONCERNS
    assert "DELIVERED [warning]: old 0" not in state.ledger
    assert "DELIVERED [warning]: old 1" in state.ledger
    assert state.ledger[-1] == "OPEN [warning]: new concern"


@pytest.mark.asyncio
async def test_ledger_cap_drops_oldest_open_when_none_are_delivered() -> None:
    backend = FakeBackend(
        chunks=[_verdict_chunk(severity="warning", text="new concern")]
    )
    controller, _, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )
    state = controller._sessions["session-1"]
    state.ledger.extend(f"OPEN [critical]: open {index}" for index in range(10))

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert len(state.ledger) == PEER_LEDGER_MAX_CONCERNS
    assert "OPEN [critical]: open 0" not in state.ledger
    assert state.ledger[-1] == "OPEN [warning]: new concern"


@pytest.mark.asyncio
async def test_no_review_until_a_peer_model_is_configured() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, _, ports, _, factory = _make_loop(_config(), backend)

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)
    assert factory.calls == 0

    await controller.configure(
        PeerConfigureParams(session_id="session-1", model="beta"), ports
    )
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two"))
    )
    await _drain(controller)

    assert factory.calls == 1
    # The review sees everything mirrored since the turn started.
    user_content = _user_content(backend)
    assert "assistant: one" in user_content
    assert "assistant: two" in user_content


@pytest.mark.asyncio
async def test_completed_review_is_stored_for_the_panel_steer_action() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk(severity="warning", text="nit")])
    controller, notify, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    review_id = notify.notifications[0][1].review_id
    response = await controller.steer(
        PeerSteerParams(session_id="session-1", review_id=review_id), ports
    )
    assert response == PeerSteerResponse(accepted=True, delivered=True)
    assert _steered_text(ports.steered[0]) == f"{STEER_MESSAGE_PREFIX}nit"
    # The delivered steer flips the ledger line.
    assert controller._sessions["session-1"].ledger == ["DELIVERED [warning]: nit"]


@pytest.mark.asyncio
async def test_review_notifications_are_running_then_complete_with_matching_ids() -> (
    None
):
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert [method for method, _ in notify.notifications] == [
        "peer/review",
        "peer/review",
    ]
    running, complete = (params for _, params in notify.notifications)
    assert running.status is PeerReviewStatus.RUNNING
    assert running.severity is None
    assert running.review_id == complete.review_id
    assert running.turn_id == "turn-1"
    assert running.target_entry_id == "entry-1"
    # The server sequences event IDs; the emitter only stamps the time.
    assert running.event_id == 0
    assert running.emitted_at > 0
    assert complete.status is PeerReviewStatus.COMPLETE
    assert complete.severity is PeerSeverity.INFO
    assert complete.text == "No concerns."


@pytest.mark.asyncio
async def test_detach_cancels_the_in_flight_review() -> None:
    gate = asyncio.Event()
    backend = _GatedBackend([[_verdict_chunk()]], gate)
    controller, notify, _, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await asyncio.sleep(0)  # let the review task start and park on the gate
    await controller.detach("session-1")

    failed = [
        params
        for _, params in notify.notifications
        if params.status is PeerReviewStatus.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].error == "cancelled"
    assert controller._sessions["session-1"].ports is None


@pytest.mark.asyncio
async def test_tap_ignores_events_for_unattached_sessions() -> None:
    backend = FakeBackend(chunks=[_verdict_chunk()])
    controller = PeerReviewController(
        notify=_FakeNotify(), backend_factory=_FakeBackendFactory(backend)
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert backend.requests_messages == []


# --- Slice 6: telemetry (design §7.8, ADR 0008) ---


def _events_named(ports: _FakePorts, name: str) -> list[dict[str, Any]]:
    return [properties for event, properties in ports.events if event == name]


@pytest.mark.asyncio
async def test_configure_records_the_configured_event() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    ports = _FakePorts(_config(peer_review={"auto_steer_severity": "warning"}))

    await controller.configure(
        PeerConfigureParams(session_id="session-1", model="beta"), ports
    )
    await controller.configure(
        PeerConfigureParams(session_id="session-1", disable=True), ports
    )

    assert _events_named(ports, "vibe.peer_review.configured") == [
        {
            "enabled": True,
            "auto_steer": False,
            "auto_steer_severity": "warning",
            "max_reviews_per_turn": 10,
        },
        {
            "enabled": False,
            "auto_steer": False,
            "auto_steer_severity": "warning",
            "max_reviews_per_turn": 10,
        },
    ]


@pytest.mark.asyncio
async def test_steer_records_steer_injected_manual() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    controller.store_review(
        StoredPeerReview(
            review_id="review-1",
            turn_id="turn-1",
            text="critique",
            severity=PeerSeverity.WARNING,
        ),
        session_id="session-1",
    )
    ports = _FakePorts(_config())
    response = await controller.steer(
        PeerSteerParams(session_id="session-1", review_id="review-1"), ports
    )

    assert response.delivered is True
    assert _events_named(ports, "vibe.peer_review.steer_injected") == [
        {"trigger": "manual", "severity": "warning", "delivered": True}
    ]


@pytest.mark.asyncio
async def test_steer_records_a_failed_injection_as_not_delivered() -> None:
    controller = PeerReviewController(notify=_FakeNotify())
    controller.store_review(
        StoredPeerReview(review_id="review-1", turn_id="turn-1", text="nit"),
        session_id="session-1",
    )
    ports = _FakePorts(_config())
    ports.steer_error = RequestFailure(
        ProtocolErrorCode.INVALID_PARAMS, "turn already completed"
    )
    response = await controller.steer(
        PeerSteerParams(session_id="session-1", review_id="review-1"), ports
    )

    assert response.delivered is False
    assert _events_named(ports, "vibe.peer_review.steer_injected") == [
        {"trigger": "manual", "severity": "info", "delivered": False}
    ]


@pytest.mark.asyncio
async def test_review_completed_records_the_verdict_event() -> None:
    backend = FakeBackend(
        chunks=[
            _verdict_chunk(severity="warning", steerable=True, text="secret critique")
        ]
    )
    controller, _, ports, _, _ = _make_loop(
        _config(
            utility_models={"peer": "beta"}, peer_review={"max_reviews_per_turn": 3}
        ),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    events = _events_named(ports, "vibe.peer_review.review_completed")
    assert len(events) == 1
    event = events[0]
    assert event["severity"] == "warning"
    assert event["steerable"] is True
    assert event["delivered"] is False
    assert event["budget_remaining"] == 2
    assert isinstance(event["duration_ms"], int)
    assert "secret critique" not in json.dumps(event)


@pytest.mark.asyncio
async def test_budget_exhaustion_records_once_per_turn() -> None:
    backend = FakeBackend(chunks=[[_verdict_chunk()], [_verdict_chunk()]])
    controller, _, ports, _, _ = _make_loop(
        _config(
            utility_models={"peer": "beta"}, peer_review={"max_reviews_per_turn": 1}
        ),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn("turn-1")))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one", turn_id="turn-1"))
    )
    await _drain(controller)
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-2", "two", turn_id="turn-1"))
    )
    await _drain(controller)
    # A new turn resets the budget and the once-per-turn notice.
    await controller.handle_backend_event(
        _turn_event(
            "turn/completed", _turn("turn-1", status=PublicTurnStatus.COMPLETED)
        )
    )
    await controller.handle_backend_event(_turn_event("turn/started", _turn("turn-2")))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-3", "three", turn_id="turn-2"))
    )
    await _drain(controller)
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-4", "four", turn_id="turn-2"))
    )
    await _drain(controller)

    assert _events_named(ports, "vibe.peer_review.budget_exhausted") == [
        {"max_reviews_per_turn": 1, "reviews_run": 1},
        {"max_reviews_per_turn": 1, "reviews_run": 1},
    ]


@pytest.mark.asyncio
async def test_review_error_records_the_error_class_only() -> None:
    backend = FakeBackend(exception_to_raise=RuntimeError("provider boom"))
    controller, _, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    events = _events_named(ports, "vibe.peer_review.error")
    assert events == [{"stage": "run", "error_class": "RuntimeError"}]
    assert "provider boom" not in json.dumps(events)


@pytest.mark.asyncio
async def test_interrupt_records_a_cancelled_error_event() -> None:
    gate = asyncio.Event()
    backend = _GatedBackend([[_verdict_chunk()]], gate)
    controller, _, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}), backend
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn("turn-1")))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one", turn_id="turn-1"))
    )
    await asyncio.sleep(0)  # let the review task start and park on the gate
    await controller.handle_backend_event(
        _turn_event(
            "turn/completed", _turn("turn-1", status=PublicTurnStatus.INTERRUPTED)
        )
    )

    assert _events_named(ports, "vibe.peer_review.error") == [
        {"stage": "cancelled", "error_class": "CancelledError"}
    ]


@pytest.mark.asyncio
async def test_auto_steer_records_steer_injected_auto() -> None:
    backend = FakeBackend(
        chunks=[_verdict_chunk(severity="critical", steerable=True, text="stop that")]
    )
    controller, _, ports, _, _ = _make_loop(
        _config(utility_models={"peer": "beta"}, peer_review={"auto_steer": True}),
        backend,
    )

    await controller.handle_backend_event(_turn_event("turn/started", _turn()))
    await controller.handle_backend_event(
        _entry_added(_message_entry("entry-1", "one"))
    )
    await _drain(controller)

    assert _events_named(ports, "vibe.peer_review.steer_injected") == [
        {"trigger": "auto", "severity": "critical", "delivered": True}
    ]
