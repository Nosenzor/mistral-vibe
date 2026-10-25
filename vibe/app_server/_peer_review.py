from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from vibe.app_server._dispatch import RequestFailure
from vibe.app_server._model import validate_wire
from vibe.app_server._session_backend_port import (
    SessionBackend,
    SessionBackendExtension,
)
from vibe.app_server.models import TextContentBlock
from vibe.app_server.protocol import (
    ConfigEffectiveReadResponse,
    PeerConfigureParams,
    PeerConfigView,
    PeerReadParams,
    PeerSteerParams,
    PeerSteerResponse,
    ProtocolErrorCode,
    TurnSteerParams,
    TurnSteerResponse,
)
from vibe.core.config import UtilityFeature, VibeConfigSchema
from vibe.core.config.models import ACTIVE_MODEL_SELECTOR

STEER_MESSAGE_PREFIX = "[rubber duck] "


class PeerReviewPorts(Protocol):
    """Root-bound capabilities the controller needs; rebuilt per dispatch."""

    async def effective_config(self, session_id: str) -> VibeConfigSchema: ...

    async def steer(self, params: TurnSteerParams) -> TurnSteerResponse: ...


class SessionBackendPeerReviewPorts:
    """Binds the attached session backend to the controller's ports."""

    def __init__(self, root: SessionBackend) -> None:
        self._root = root

    async def effective_config(self, session_id: str) -> VibeConfigSchema:
        if not isinstance(self._root, SessionBackendExtension):
            raise RequestFailure(
                ProtocolErrorCode.NOT_IMPLEMENTED,
                "The selected session backend cannot read its effective config",
            )
        result = await self._root.dispatch_extension(
            "config/effective/read", {"sessionId": session_id}
        )
        return VibeConfigSchema.model_validate(
            validate_wire(ConfigEffectiveReadResponse, result.response).config
        )

    async def steer(self, params: TurnSteerParams) -> TurnSteerResponse:
        return (await self._root.steer_turn(params)).response


@dataclass(frozen=True, slots=True)
class StoredPeerReview:
    review_id: str
    turn_id: str
    text: str
    delivered: bool = False


@dataclass(slots=True)
class _SessionPeerState:
    model: str | None = None
    explicitly_off: bool = False
    auto_steer: bool | None = None
    reviews: dict[str, StoredPeerReview] = field(default_factory=dict)


class PeerReviewController:
    """App-server module owning rubber-duck peer review (ADR 0018).

    Holds the per-session duck configuration and the delivered reviews the
    panel can steer from. The review loop that produces reviews lands with
    the event tap; until then reviews are registered through
    ``store_review``.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionPeerState] = {}

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def store_review(self, review: StoredPeerReview, *, session_id: str) -> None:
        self._sessions.setdefault(session_id, _SessionPeerState()).reviews[
            review.review_id
        ] = review

    async def configure(
        self, params: PeerConfigureParams, ports: PeerReviewPorts
    ) -> PeerConfigView:
        state = self._sessions.setdefault(params.session_id, _SessionPeerState())
        if params.model is None:
            state.model = None
            state.explicitly_off = True
        else:
            alias = params.model.strip()
            if not alias:
                raise RequestFailure(
                    ProtocolErrorCode.INVALID_PARAMS,
                    "Peer model alias must not be empty",
                )
            config = await ports.effective_config(params.session_id)
            if (
                alias != ACTIVE_MODEL_SELECTOR
                and alias not in config.available_models()
            ):
                raise RequestFailure(
                    ProtocolErrorCode.INVALID_PARAMS,
                    f"Unknown model alias for peer review: '{alias}'",
                )
            state.model = alias
            state.explicitly_off = False
        if params.auto_steer is not None:
            state.auto_steer = params.auto_steer
        return await self._view(params.session_id, state, ports)

    async def read(
        self, params: PeerReadParams, ports: PeerReviewPorts
    ) -> PeerConfigView:
        state = self._sessions.setdefault(params.session_id, _SessionPeerState())
        return await self._view(params.session_id, state, ports)

    async def steer(
        self, params: PeerSteerParams, ports: PeerReviewPorts
    ) -> PeerSteerResponse:
        state = self._sessions.get(params.session_id)
        review = state.reviews.get(params.review_id) if state is not None else None
        if state is None or review is None:
            raise RequestFailure(
                ProtocolErrorCode.INVALID_PARAMS,
                f"Unknown peer review: '{params.review_id}'",
            )
        if review.delivered:
            return PeerSteerResponse(accepted=True, delivered=True)
        steer_params = TurnSteerParams(
            session_id=params.session_id,
            expected_turn_id=review.turn_id,
            message=[TextContentBlock(text=f"{STEER_MESSAGE_PREFIX}{review.text}")],
        )
        try:
            await ports.steer(steer_params)
        except RequestFailure:
            return PeerSteerResponse(accepted=False, delivered=False)
        state.reviews[params.review_id] = StoredPeerReview(
            review_id=review.review_id,
            turn_id=review.turn_id,
            text=review.text,
            delivered=True,
        )
        return PeerSteerResponse(accepted=True, delivered=True)

    async def _view(
        self, session_id: str, state: _SessionPeerState, ports: PeerReviewPorts
    ) -> PeerConfigView:
        config = await ports.effective_config(session_id)
        config_alias = config.utility_models.alias_for(UtilityFeature.PEER)
        enabled = state.model is not None or (
            bool(config_alias) and not state.explicitly_off
        )
        model = state.model or (config_alias if enabled else None)
        auto_steer = (
            state.auto_steer
            if state.auto_steer is not None
            else config.peer_review.auto_steer
        )
        return PeerConfigView(
            enabled=enabled,
            model=model or None,
            auto_steer=auto_steer,
            max_reviews_per_turn=config.peer_review.max_reviews_per_turn,
            auto_steer_severity=config.peer_review.auto_steer_severity,
        )
