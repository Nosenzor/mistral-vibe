from __future__ import annotations

from typing import Any, cast

from pydantic import ValidationError
import pytest

from tests.conftest import build_test_vibe_config
from vibe.app_server._dispatch import DispatchResult, RequestFailure
from vibe.app_server._model import validate_wire
from vibe.app_server._peer_review import (
    STEER_MESSAGE_PREFIX,
    PeerReviewController,
    StoredPeerReview,
)
from vibe.app_server._session_backend_port import (
    SessionBackendHost,
    SessionBackendResult,
)
from vibe.app_server.events import ClientProjection
from vibe.app_server.models import IdleSessionStatus, PublicSession, PublicSessionState
from vibe.app_server.protocol import (
    SERVER_METHODS,
    ConfigEffectiveReadResponse,
    Notification,
    PeerConfigureParams,
    PeerConfigView,
    PeerReadParams,
    PeerReviewParams,
    PeerReviewStatus,
    PeerSteerParams,
    PeerSteerResponse,
    ProtocolErrorCode,
    TurnSteerParams,
    TurnSteerResponse,
)
from vibe.app_server.server import AppServer
from vibe.app_server.transport import memory_transport_pair
from vibe.core.config import ModelConfig, PeerSeverity, VibeConfigSchema


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

    async def effective_config(self, session_id: str) -> VibeConfigSchema:
        return self._config

    async def steer(self, params: TurnSteerParams) -> TurnSteerResponse:
        if self.steer_error is not None:
            raise self.steer_error
        self.steered.append(params)
        return TurnSteerResponse()


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
    controller = PeerReviewController()

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
    controller = PeerReviewController()
    ports = _FakePorts(_config(utility_models={"peer": "beta"}))

    view = await controller.read(PeerReadParams(session_id="session-1"), ports)

    assert view.enabled is True
    assert view.model == "beta"


@pytest.mark.asyncio
async def test_configure_enables_with_a_known_alias() -> None:
    controller = PeerReviewController()
    ports = _FakePorts(_config())

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", model="beta"), ports
    )

    assert view.enabled is True
    assert view.model == "beta"


@pytest.mark.asyncio
async def test_configure_rejects_an_unknown_alias() -> None:
    controller = PeerReviewController()
    ports = _FakePorts(_config())

    with pytest.raises(RequestFailure) as exc_info:
        await controller.configure(
            PeerConfigureParams(session_id="session-1", model="does-not-exist"), ports
        )

    assert exc_info.value.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_configure_rejects_an_empty_alias() -> None:
    controller = PeerReviewController()
    ports = _FakePorts(_config())

    with pytest.raises(RequestFailure):
        await controller.configure(
            PeerConfigureParams(session_id="session-1", model="  "), ports
        )


@pytest.mark.asyncio
async def test_configure_off_overrides_the_config_default() -> None:
    controller = PeerReviewController()
    ports = _FakePorts(_config(utility_models={"peer": "beta"}))

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", model=None), ports
    )

    assert view.enabled is False
    assert view.model is None


@pytest.mark.asyncio
async def test_configure_auto_steer_override_wins_over_config() -> None:
    controller = PeerReviewController()
    ports = _FakePorts(_config(peer_review={"auto_steer": True}))

    view = await controller.read(PeerReadParams(session_id="session-1"), ports)
    assert view.auto_steer is True

    view = await controller.configure(
        PeerConfigureParams(session_id="session-1", auto_steer=False), ports
    )
    assert view.auto_steer is False


@pytest.mark.asyncio
async def test_steer_rejects_an_unknown_review() -> None:
    controller = PeerReviewController()
    ports = _FakePorts(_config())

    with pytest.raises(RequestFailure) as exc_info:
        await controller.steer(
            PeerSteerParams(session_id="session-1", review_id="nope"), ports
        )

    assert exc_info.value.code is ProtocolErrorCode.INVALID_PARAMS


@pytest.mark.asyncio
async def test_steer_injects_the_marked_critique_once() -> None:
    controller = PeerReviewController()
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
    assert steered.input[0].text == f"{STEER_MESSAGE_PREFIX}nit"


@pytest.mark.asyncio
async def test_steer_reports_not_delivered_when_the_turn_ended() -> None:
    controller = PeerReviewController()
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


class _FakeRootBackend:
    def __init__(self, config: VibeConfigSchema) -> None:
        self.session_id = "session-1"
        self._config = config

    async def dispatch_extension(
        self, method: str, raw_params: dict[str, Any]
    ) -> DispatchResult:
        assert method == "config/effective/read"
        return DispatchResult(
            ConfigEffectiveReadResponse(config=self._config.model_dump(mode="json"))
        )

    async def steer_turn(
        self, params: TurnSteerParams
    ) -> SessionBackendResult[TurnSteerResponse]:
        return SessionBackendResult(response=TurnSteerResponse())


def _make_server() -> AppServer:
    _client_transport, server_transport = memory_transport_pair()
    return AppServer(
        server_transport,
        session_backend_host_factory=lambda _: cast(SessionBackendHost, object()),
    )


@pytest.mark.asyncio
async def test_peer_methods_route_to_the_controller() -> None:
    server = _make_server()
    root = _FakeRootBackend(_config())

    result = await server._dispatch_backend_operation(
        root, "peer/read", {"sessionId": "session-1"}
    )
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is False

    result = await server._dispatch_backend_operation(
        root, "peer/configure", {"sessionId": "session-1", "model": "beta"}
    )
    assert isinstance(result.response, PeerConfigView)
    assert result.response.enabled is True

    result = await server._dispatch_backend_operation(
        root, "peer/read", {"sessionId": "session-1"}
    )
    assert isinstance(result.response, PeerConfigView)
    assert result.response.model == "beta"


@pytest.mark.asyncio
async def test_peer_methods_reject_malformed_payloads() -> None:
    server = _make_server()
    root = _FakeRootBackend(_config())

    with pytest.raises(ValidationError):
        await server._dispatch_backend_operation(
            root, "peer/configure", {"sessionId": "session-1", "model": 42}
        )
