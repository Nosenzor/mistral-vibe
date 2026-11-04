from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum, auto
import json
import re
import time
from types import TracebackType
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, JsonValue

from mistralai_vibe_local_harness.protocol import (
    RustAssistantMessage,
    RustCompletionResultToolCallPart,
    RustInvalidJsonToolArguments,
    RustJsonToolArguments,
    RustModelToolCallPart,
    RustSystemMessage,
    RustTextContentBlock,
    RustToolDefinition,
    RustToolMessage,
    RustUserMessage,
)

if TYPE_CHECKING:
    from mistralai_vibe_local_harness.protocol import (
        RustAssistantPart,
        RustCompletionResult,
        RustMessage,
        RustToolArguments,
    )

from vibe.app_server._config_paths import UTILITY_MODEL_PEER_PATH
from vibe.app_server._dispatch import RequestFailure
from vibe.app_server._model import ProtocolModel, validate_wire
from vibe.app_server._patch import apply_json_patch
from vibe.app_server._peer_tool_executor import PeerReviewToolExecutor, PeerToolPolicy
from vibe.app_server._session_backend_port import (
    SessionBackend,
    SessionBackendError,
    SessionBackendEvent,
    SessionBackendExtension,
)
from vibe.app_server.models import (
    CompletedEffectState,
    EffectState,
    FailedEffectState,
    PendingEffectState,
    PublicEffectEntry,
    PublicEntryGenerationStatus,
    PublicHistoryEntry,
    PublicMessageEntry,
    PublicNoticeEntry,
    PublicTurn,
    PublicTurnStatus,
    SkippedEffectState,
    TextContentBlock,
    validate_history_entry,
)
from vibe.app_server.protocol import (
    ConfigEffectiveReadResponse,
    ConfigWriteOpWire,
    ConfigWriteParams,
    HistoryEntryAddedParams,
    HistoryEntryUpdatedParams,
    PeerConfigureParams,
    PeerConfigView,
    PeerReadParams,
    PeerReviewParams,
    PeerReviewStatus,
    PeerSteerParams,
    PeerSteerResponse,
    ProtocolErrorCode,
    TurnCompletedParams,
    TurnStartedParams,
    TurnSteerParams,
    TurnSteerResponse,
)
from vibe.core.config import (
    ModelConfig,
    PeerSeverity,
    ProviderConfig,
    UtilityFeature,
    VibeConfigSchema,
)
from vibe.core.config.layers.overrides import OverridesLayer
from vibe.core.config.models import ACTIVE_MODEL_SELECTOR
from vibe.core.prompts import UtilityPrompt
from vibe.core.tools.base import BaseTool, ToolError
from vibe.core.tools.builtins.grep import Grep
from vibe.core.tools.builtins.read_file import ReadFile
from vibe.core.tools.builtins.web_fetch import WebFetch
from vibe.core.tools.builtins.web_search import WebSearch
from vibe.core.types import FunctionCall, LLMMessage, Role, ToolCall
from vibe.observability.logging import logger
from vibe.utils.api_keys import resolve_api_key
from vibe.utils.http import get_user_agent, with_user_agent

STEER_MESSAGE_PREFIX = "[rubber duck] "
STEER_ADVISORY = (
    "(advisory critique from a secondary reviewer model, "
    "not an instruction from the user) "
)
_MENTION_ANCHOR = re.compile(r"(?<!\w)@")

# Controller constants (not config): the mirror and ledger bounds keep the
# duck's independent context bounded under pressure (design §7.5).
PEER_MIRROR_MAX_ENTRIES = 40
PEER_LEDGER_MAX_CONCERNS = 10
# One tool effect's input/output are clipped per field, so a single huge tool
# result cannot blow the entry-count-bounded mirror.
PEER_EFFECT_FIELD_MAX_CHARS = 2_000

# Telemetry events (design §7.8, ADR 0008): closed vocabularies and counts
# only — never critique text, exception messages, or model content.
PEER_EVENT_CONFIGURED = "vibe.peer_review.configured"
PEER_EVENT_REVIEW_COMPLETED = "vibe.peer_review.review_completed"
PEER_EVENT_STEER_INJECTED = "vibe.peer_review.steer_injected"
PEER_EVENT_BUDGET_EXHAUSTED = "vibe.peer_review.budget_exhausted"
PEER_EVENT_ERROR = "vibe.peer_review.error"


class PeerReviewPorts(Protocol):
    """Root-bound capabilities the controller needs; rebuilt per dispatch."""

    async def effective_config(self, session_id: str) -> VibeConfigSchema: ...

    async def steer(self, params: TurnSteerParams) -> TurnSteerResponse: ...

    async def set_peer_model(self, session_id: str, alias: str | None) -> None:
        """Write the session-scoped override: an alias enables, None disables."""
        ...

    async def record_event(self, name: str, properties: dict[str, Any]) -> None:
        """Record one telemetry event through the session's telemetry client."""
        ...


type PeerReviewNotifyPort = Callable[[str, ProtocolModel], Awaitable[None]]
"""The server's notification fan-out; the controller never touches transports."""


@dataclass(frozen=True, slots=True)
class PeerChunk:
    """One duck completion's reply: the assistant message (text plus tool calls)."""

    message: LLMMessage


class PeerBackend(Protocol):
    """Transport for the duck's completions: an async context manager with complete()."""

    async def __aenter__(self) -> PeerBackend: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None: ...

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: Sequence[LLMMessage],
        temperature: float | None,
        tools: list[RustToolDefinition] | None,
        tool_choice: str | None,
        max_tokens: int | None,
        extra_headers: dict[str, str] | None,
    ) -> PeerChunk: ...


class PeerReviewBackendFactory(Protocol):
    """Builds the LLM backend a duck completion runs on."""

    def backend_for(
        self, model: ModelConfig, config: VibeConfigSchema
    ) -> PeerBackend: ...


class ConfigPeerReviewBackendFactory:
    """Builds the duck's backend from the effective config (mirrors _narration)."""

    def backend_for(self, model: ModelConfig, config: VibeConfigSchema) -> PeerBackend:
        return HarnessPeerBackend(
            provider=config.get_provider_for_model(model), config=config
        )


class HarnessPeerBackend:
    """Runs duck completions on the Harness provider adapters (the v3 completion path).

    The adapters own one HTTP client per call, so the context manager is a no-op;
    it keeps the controller's ``async with backend:`` shape. ``tool_choice`` is
    accepted for the port's shape and dropped: offering tools implies "auto",
    and the adapters take no other choice.
    """

    def __init__(self, *, provider: ProviderConfig, config: VibeConfigSchema) -> None:
        self._provider = provider
        self._config = config

    async def __aenter__(self) -> HarnessPeerBackend:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        return None

    async def complete(
        self,
        *,
        model: ModelConfig,
        messages: Sequence[LLMMessage],
        temperature: float | None,
        tools: list[RustToolDefinition] | None,
        tool_choice: str | None,
        max_tokens: int | None,
        extra_headers: dict[str, str] | None,
    ) -> PeerChunk:
        # Imported here: the Harness pulls the provider SDKs, which stay off the
        # CLI startup path.
        from mistralai_vibe_local_harness.vibe import (
            LocalModelRoute,
            LocalRuntimeAdapterConfig,
            ProviderCredentialSnapshot,
        )
        from mistralai_vibe_local_harness.vibe.adapters.generic import (
            execute_generic_completion,
        )
        from mistralai_vibe_local_harness.vibe.adapters.mistral import (
            execute_mistral_completion,
        )

        provider = self._provider
        config = self._config
        token = (
            resolve_api_key(provider.api_key_env_var)
            if provider.api_key_env_var
            else None
        )
        headers = with_user_agent(
            {**provider.extra_headers, **(extra_headers or {})}, str(provider.backend)
        )
        adapter_config = LocalRuntimeAdapterConfig(
            provider=provider.name,
            backend=str(provider.backend),
            api_style=provider.api_style,
            base_url=provider.api_base,
            max_tokens=max_tokens,
            reasoning_field_name=provider.reasoning_field_name,
            emits_finish_reason=provider.emits_finish_reason,
            extra_headers=headers,
            project_id=provider.project_id,
            region=provider.region,
            timeout_s=config.api_timeout,
            stream_idle_timeout_s=config.api_stream_idle_timeout or None,
            retry_max_elapsed_time_s=config.api_retry_max_elapsed_time,
            enable_system_trust_store=config.enable_system_trust_store,
        )
        route = LocalModelRoute(
            model=model.name,
            # top_p belongs to the model's own temperature: Mistral rejects it
            # alongside greedy sampling.
            temperature=model.temperature if temperature is None else temperature,
            top_p=model.top_p if temperature is None else None,
            max_output_tokens=model.max_output_tokens,
        )
        credential = ProviderCredentialSnapshot(
            token=token, headers={}, revision=provider.name
        )
        wire_messages = _to_wire_messages(messages)
        wire_tools = list(tools or [])
        match str(provider.backend):
            case "mistral":
                result = await execute_mistral_completion(
                    wire_messages, wire_tools, adapter_config, credential, route
                )
            case "generic":
                result = await execute_generic_completion(
                    wire_messages, wire_tools, adapter_config, credential, route=route
                )
            case other:
                raise RuntimeError(
                    f"Unsupported completion backend for peer review: {other}"
                )
        return _to_peer_chunk(result)


@dataclass(frozen=True, slots=True)
class PeerReviewSessionPorts:
    """Session-scoped ports the review loop runs against (ADR 0018)."""

    backend: PeerReviewPorts
    tool_executor: PeerReviewToolExecutor


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
        try:
            return (await self._root.steer_turn(params)).response
        except SessionBackendError as exc:
            # The controller reports steer failures as accepted=false; the
            # backend speaks SessionBackendError (e.g. stale turn once the
            # turn completed), so translate at the port boundary.
            raise RequestFailure(exc.code, str(exc), exc.data) from exc

    async def record_event(self, name: str, properties: dict[str, Any]) -> None:
        # Telemetry rides the session's `telemetry/record` extension — the same
        # bridge the client's features use — so the event carries the session's
        # standardized metadata block. Best-effort: recording never fails the
        # review loop.
        if not isinstance(self._root, SessionBackendExtension):
            return
        try:
            await self._root.dispatch_extension(
                "telemetry/record",
                {
                    "sessionId": self._root.session_id,
                    "name": name,
                    "properties": properties,
                },
            )
        except Exception:
            logger.debug("Peer review telemetry event dropped", exc_info=True)

    async def set_peer_model(self, session_id: str, alias: str | None) -> None:
        # The override layer outranks every durable layer, so an empty value
        # is how a session says "off" even when a global config names a peer;
        # the layer itself dies with the session.
        result = await self._root.write_config(
            ConfigWriteParams(
                session_id=session_id,
                ops=[
                    ConfigWriteOpWire(
                        op="set",
                        path=UTILITY_MODEL_PEER_PATH,
                        value=alias or "",
                        target_layer=OverridesLayer.NAME,
                    )
                ],
                reason="peer review model override",
            )
        )
        if not result.response.applied:
            detail = "; ".join(result.response.failures) or "the write was rejected"
            raise RequestFailure(
                ProtocolErrorCode.INTERNAL_ERROR,
                f"Failed to set the peer model: {detail}",
            )


@dataclass(frozen=True, slots=True)
class StoredPeerReview:
    review_id: str
    turn_id: str
    text: str
    delivered: bool = False
    severity: PeerSeverity = PeerSeverity.INFO


@dataclass(frozen=True, slots=True)
class PeerVerdict:
    """The duck's structured critique, parsed from the review's final message."""

    severity: PeerSeverity
    steerable: bool
    text: str


class _PeerPhase(StrEnum):
    """Per-turn loop state: idle → armed (turn/started) → retired (turn/completed)."""

    IDLE = auto()
    ARMED = auto()
    RETIRED = auto()


@dataclass(slots=True)
class _SessionPeerState:
    # Delivered reviews and the session's auto-steer toggle (survive across
    # turns). The duck's model is not cached here: it lives in the session's
    # override layer and is re-resolved from the effective config per review,
    # so a config reload re-resolves it live (design §7.5).
    auto_steer: bool | None = None
    reviews: dict[str, StoredPeerReview] = field(default_factory=dict)
    ledger: list[str] = field(default_factory=list)
    # Per-turn loop state.
    ports: PeerReviewSessionPorts | None = None
    phase: _PeerPhase = _PeerPhase.IDLE
    turn_id: str | None = None
    budget_used: int = 0
    budget_noticed: bool = False
    mirror: list[PublicHistoryEntry] = field(default_factory=list)
    user_instructions: str = ""
    # The user's entry can arrive ahead of turn/started (snapshot order puts
    # entries before turns), so stash it per turn and pick it up on start.
    pending_instructions: dict[str, str] = field(default_factory=dict)
    mirror_dirty: bool = False
    # Entries still streaming, as raw wire dicts, are held out of the mirror
    # until they complete so a review never sees half a message.
    in_progress: dict[str, JsonValue] = field(default_factory=dict)
    # Per-turn budget cap, resolved when the turn arms so the idle gates never
    # touch the config.
    max_reviews: int = 0
    scheduling: bool = False
    # A turn that completed while a review was still in flight owes its final
    # material exactly one more review, which the in-flight one hands off to.
    final_review_pending: bool = False
    # A provider error silences the duck for the rest of the turn; a timed-out
    # review only cools it down until this monotonic deadline, so the duck
    # recovers within the turn once the deadline passes.
    silenced: bool = False
    cooldown_until: float | None = None
    review_counter: int = 0
    review_task: asyncio.Task[None] | None = None


class PeerReviewController:
    """App-server module owning rubber-duck peer review (ADR 0018).

    Holds the per-session duck configuration and the delivered reviews the
    panel can steer from, and runs the review loop: the server's backend event
    fan-out is tapped through ``handle_backend_event``, new history entries are
    mirrored into a bounded transcript, and coalesced review completions run
    against the configured duck model with a read-only tool loop.
    """

    def __init__(
        self,
        notify: PeerReviewNotifyPort,
        backend_factory: PeerReviewBackendFactory | None = None,
    ) -> None:
        self._notify = notify
        self._backend_factory = backend_factory or ConfigPeerReviewBackendFactory()
        self._sessions: dict[str, _SessionPeerState] = {}

    def attach(self, session_id: str, ports: PeerReviewSessionPorts) -> None:
        """Bind the review loop to a session's backend and workspace."""
        state = self._sessions.setdefault(session_id, _SessionPeerState())
        state.ports = ports

    async def detach(self, session_id: str) -> None:
        """Release the session's loop ports and cancel any in-flight review."""
        state = self._sessions.get(session_id)
        if state is None:
            return
        state.ports = None
        state.phase = _PeerPhase.IDLE
        task = state.review_task
        state.review_task = None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def forget(self, session_id: str) -> None:
        """Drop all per-session duck state, cancelling any in-flight review."""
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        state.ports = None
        state.phase = _PeerPhase.IDLE
        if state.review_task is not None:
            state.review_task.cancel()

    def store_review(self, review: StoredPeerReview, *, session_id: str) -> None:
        self._sessions.setdefault(session_id, _SessionPeerState()).reviews[
            review.review_id
        ] = review

    async def configure(
        self, params: PeerConfigureParams, ports: PeerReviewPorts
    ) -> PeerConfigView:
        state = self._sessions.setdefault(params.session_id, _SessionPeerState())
        if params.disable:
            if params.model is not None:
                raise RequestFailure(
                    ProtocolErrorCode.INVALID_PARAMS,
                    "Cannot set a peer model and disable peer review at once",
                )
            # Off is an explicit empty override: the override layer outranks
            # any lower layer that names a peer model.
            await ports.set_peer_model(params.session_id, None)
        elif params.model is not None:
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
            await ports.set_peer_model(params.session_id, alias)
        if params.auto_steer is not None:
            state.auto_steer = params.auto_steer
        view = await self._view(params.session_id, state, ports)
        await self._record(
            ports,
            PEER_EVENT_CONFIGURED,
            {
                "enabled": view.enabled,
                "auto_steer": view.auto_steer,
                "auto_steer_severity": view.auto_steer_severity.value,
                "max_reviews_per_turn": view.max_reviews_per_turn,
            },
        )
        return view

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
        steer_params = _steer_params(params.session_id, review.turn_id, review.text)
        try:
            await ports.steer(steer_params)
        except RequestFailure:
            await self._record(
                ports,
                PEER_EVENT_STEER_INJECTED,
                {
                    "trigger": "manual",
                    "severity": review.severity.value,
                    "delivered": False,
                },
            )
            return PeerSteerResponse(accepted=False, delivered=False)
        await self._record(
            ports,
            PEER_EVENT_STEER_INJECTED,
            {"trigger": "manual", "severity": review.severity.value, "delivered": True},
        )
        state.reviews[params.review_id] = StoredPeerReview(
            review_id=review.review_id,
            turn_id=review.turn_id,
            text=review.text,
            delivered=True,
            severity=review.severity,
        )
        if review.severity.rank >= PeerSeverity.WARNING.rank:
            # Record the delivered steer so the duck's next review knows what
            # landed (design §7.4: delivered steers live in the ledger).
            open_line = f"OPEN [{review.severity}]: {review.text}"
            delivered_line = f"DELIVERED [{review.severity}]: {review.text}"
            state.ledger = [
                delivered_line if item == open_line else item for item in state.ledger
            ]
            if delivered_line not in state.ledger:
                _append_ledger_line(state.ledger, delivered_line)
        return PeerSteerResponse(accepted=True, delivered=True)

    async def handle_backend_event(self, envelope: SessionBackendEvent) -> None:
        """Tap entry point, awaited inline by the server's event fan-out.

        Never raises: a duck failure must not break the session's event stream.
        """
        try:
            session_id = envelope.session_id
            if session_id is None:
                return
            state = self._sessions.get(session_id)
            if state is None or state.ports is None:
                return
            match envelope.method:
                case "turn/started":
                    if isinstance(envelope.params, TurnStartedParams):
                        await self._on_turn_started(
                            state, session_id, envelope.params.turn
                        )
                case "turn/completed":
                    if isinstance(envelope.params, TurnCompletedParams):
                        await self._on_turn_completed(
                            state, session_id, envelope.params.turn
                        )
                case "history/entryAdded":
                    if isinstance(envelope.params, HistoryEntryAddedParams):
                        self._on_entry_added(state, envelope.params.entry)
                case "history/entryUpdated":
                    if isinstance(envelope.params, HistoryEntryUpdatedParams):
                        self._on_entry_updated(state, envelope.params)
            await self._maybe_schedule_review(state, session_id)
        except Exception:
            logger.warning("Peer review event tap failed", exc_info=True)

    async def _on_turn_started(
        self, state: _SessionPeerState, session_id: str, turn: PublicTurn
    ) -> None:
        state.turn_id = turn.id
        state.phase = _PeerPhase.IDLE
        state.budget_used = 0
        state.budget_noticed = False
        state.mirror = []
        state.in_progress = {}
        state.final_review_pending = False
        state.mirror_dirty = False
        state.silenced = False
        state.cooldown_until = None
        # The ledger is session-scoped: open concerns survive across turns.
        state.user_instructions = state.pending_instructions.pop(turn.id, "")
        state.pending_instructions.clear()
        ports = state.ports
        if ports is None:
            return
        try:
            config = await ports.backend.effective_config(session_id)
        except Exception:
            logger.debug("Peer review could not read the config", exc_info=True)
            return
        if self._resolve_model(config) is None:
            return
        state.max_reviews = config.peer_review.max_reviews_per_turn
        state.phase = _PeerPhase.ARMED

    async def _on_turn_completed(
        self, state: _SessionPeerState, session_id: str, turn: PublicTurn
    ) -> None:
        if state.turn_id is not None and turn.id != state.turn_id:
            return
        interrupted = turn.status is PublicTurnStatus.INTERRUPTED
        try:
            if not interrupted and state.phase is _PeerPhase.ARMED:
                self._admit_in_progress(state)
                await self._maybe_schedule_review(state, session_id)
                state.final_review_pending = state.mirror_dirty and (
                    state.review_task is not None or state.scheduling
                )
        finally:
            state.phase = _PeerPhase.RETIRED
        if interrupted:
            # Interruption cancels the in-flight peer call; a normal completion
            # lets it finish (bounded by the review timeout) so the final
            # critique still displays with steer actions disabled.
            task = state.review_task
            state.review_task = None
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    def _on_entry_added(
        self, state: _SessionPeerState, entry: PublicHistoryEntry
    ) -> None:
        if (
            isinstance(entry, PublicMessageEntry)
            and entry.role == "user"
            and entry.text
        ):
            if entry.turn_id is not None:
                state.pending_instructions[entry.turn_id] = entry.text
            if (
                state.phase is _PeerPhase.ARMED
                and state.turn_id is not None
                and entry.turn_id == state.turn_id
            ):
                state.user_instructions = entry.text
        if state.phase is not _PeerPhase.ARMED:
            return
        if state.turn_id is not None and entry.turn_id != state.turn_id:
            return
        if _mirror_text(entry) is None:
            return
        if entry.generation_status is PublicEntryGenerationStatus.COMPLETED:
            self._admit(state, entry)
            return
        state.in_progress[entry.id] = entry.model_dump(mode="json", by_alias=True)
        while len(state.in_progress) > PEER_MIRROR_MAX_ENTRIES:
            del state.in_progress[next(iter(state.in_progress))]

    def _admit(self, state: _SessionPeerState, entry: PublicHistoryEntry) -> None:
        state.mirror.append(entry)
        if len(state.mirror) > PEER_MIRROR_MAX_ENTRIES:
            del state.mirror[: len(state.mirror) - PEER_MIRROR_MAX_ENTRIES]
        state.mirror_dirty = True

    def _admit_in_progress(self, state: _SessionPeerState) -> None:
        pending, state.in_progress = state.in_progress, {}
        for raw in pending.values():
            self._admit(state, validate_history_entry(raw))

    def _on_entry_updated(
        self, state: _SessionPeerState, params: HistoryEntryUpdatedParams
    ) -> None:
        if state.phase is not _PeerPhase.ARMED:
            return
        pending = state.in_progress.get(params.entry_id)
        if pending is not None:
            updated_raw = apply_json_patch(pending, params.patch)
            if (
                not isinstance(updated_raw, dict)
                or updated_raw.get("generationStatus") != "completed"
            ):
                state.in_progress[params.entry_id] = updated_raw
                return
            del state.in_progress[params.entry_id]
            completed = validate_history_entry(updated_raw)
            if state.turn_id is None or completed.turn_id == state.turn_id:
                self._admit(state, completed)
            return
        for index, entry in enumerate(state.mirror):
            if entry.id != params.entry_id:
                continue
            raw = entry.model_dump(mode="json", by_alias=True)
            updated = validate_history_entry(apply_json_patch(raw, params.patch))
            if state.turn_id is not None and updated.turn_id != state.turn_id:
                state.mirror.pop(index)
                return
            state.mirror[index] = updated
            state.mirror_dirty = True
            return

    async def _maybe_schedule_review(
        self, state: _SessionPeerState, session_id: str
    ) -> None:
        if not self._can_start_review(state):
            return
        ports = state.ports
        if ports is None or await self._budget_exhausted(state, ports.backend):
            return
        state.scheduling = True
        try:
            config = await ports.backend.effective_config(session_id)
            if not self._may_review(state):
                return
            model = self._resolve_model(config)
            if model is None:
                if state.phase is _PeerPhase.ARMED:
                    state.phase = _PeerPhase.IDLE
                return
            state.max_reviews = config.peer_review.max_reviews_per_turn
            if await self._budget_exhausted(state, ports.backend):
                return
            state.review_counter += 1
            review_id = f"{state.turn_id}-{state.review_counter}"
            state.review_task = asyncio.create_task(
                self._run_review(
                    session_id,
                    state,
                    config,
                    model,
                    review_id,
                    turn_id=state.turn_id or "",
                ),
                name=f"vibe-peer-review-{session_id}",
            )
            state.final_review_pending = False
        finally:
            state.scheduling = False

    def _can_start_review(self, state: _SessionPeerState) -> bool:
        """Local-state gates only: this runs after every backend event, so it
        must stay free of I/O until a review can actually start.

        A timed-out review cools the duck down for one timeout period; the
        accumulated mirror is reviewed once the deadline passes.
        """
        cooling_down = (
            state.cooldown_until is not None and time.monotonic() < state.cooldown_until
        )
        return (
            not state.silenced
            and self._may_review(state)
            and state.review_task is None
            and not state.scheduling
            and state.mirror_dirty
            and not cooling_down
        )

    @staticmethod
    def _may_review(state: _SessionPeerState) -> bool:
        return state.phase is _PeerPhase.ARMED or (
            state.phase is _PeerPhase.RETIRED and state.final_review_pending
        )

    async def _budget_exhausted(
        self, state: _SessionPeerState, ports: PeerReviewPorts
    ) -> bool:
        if state.budget_used < state.max_reviews:
            return False
        if not state.budget_noticed:
            # Once per turn: the budget blocks every further review.
            state.budget_noticed = True
            await self._record(
                ports,
                PEER_EVENT_BUDGET_EXHAUSTED,
                {
                    "max_reviews_per_turn": state.max_reviews,
                    "reviews_run": state.budget_used,
                },
            )
        return True

    async def _run_review(
        self,
        session_id: str,
        state: _SessionPeerState,
        config: VibeConfigSchema,
        model: ModelConfig,
        review_id: str,
        turn_id: str,
    ) -> None:
        # Snapshot the mirror at run time: entries that arrived after
        # scheduling but before the task started batch into this review, and
        # entries arriving while the backend call is in flight schedule the
        # trailing review (coalescing, design §7.5).
        started = time.monotonic()
        mirror = state.mirror
        state.mirror = []
        state.mirror_dirty = False
        target_entry_id = mirror[-1].id if mirror else None
        await self._emit(
            PeerReviewParams(
                event_id=0,
                session_id=session_id,
                emitted_at=int(time.time()),
                review_id=review_id,
                turn_id=turn_id,
                target_entry_id=target_entry_id,
                status=PeerReviewStatus.RUNNING,
            )
        )
        try:
            verdict = await self._complete_review(state, config, model, mirror)
        except asyncio.CancelledError:
            # Interrupt or backend switch: the panel must not show a review
            # stuck at running. The notification must not mask the
            # cancellation.
            with suppress(Exception):
                await self._emit_failed(
                    session_id, review_id, turn_id, target_entry_id, "cancelled"
                )
            await self._record_error(state, "cancelled", "CancelledError")
            raise
        except TimeoutError:
            # One completion hung past the per-call timeout. Unlike a provider
            # error this can be transient (cold model, slow first call), so
            # the duck cools down for one timeout period and may retry within
            # the turn instead of staying silent until the next one.
            state.budget_used += 1
            state.cooldown_until = time.monotonic() + config.peer_review.timeout_seconds
            await self._emit_failed(
                session_id, review_id, turn_id, target_entry_id, "review timed out"
            )
            await self._record_error(state, "timeout", "TimeoutError")
        except Exception as exc:
            logger.warning("Peer review failed", exc_info=exc)
            state.budget_used += 1
            state.silenced = True
            await self._emit_failed(
                session_id,
                review_id,
                turn_id,
                target_entry_id,
                str(exc) or type(exc).__name__,
            )
            await self._record_error(state, "run", type(exc).__name__)
        else:
            state.budget_used += 1
            delivered = await self._maybe_auto_steer(
                session_id, state, config, verdict, turn_id
            )
            if state.ports is not None:
                await self._record(
                    state.ports.backend,
                    PEER_EVENT_REVIEW_COMPLETED,
                    {
                        "severity": verdict.severity.value,
                        "steerable": verdict.steerable,
                        "delivered": delivered,
                        "budget_remaining": max(
                            config.peer_review.max_reviews_per_turn - state.budget_used,
                            0,
                        ),
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    },
                )
            if verdict.severity.rank >= PeerSeverity.WARNING.rank:
                prefix = "DELIVERED" if delivered else "OPEN"
                _append_ledger_line(
                    state.ledger, f"{prefix} [{verdict.severity}]: {verdict.text}"
                )
            state.reviews[review_id] = StoredPeerReview(
                review_id=review_id,
                turn_id=turn_id,
                text=verdict.text,
                delivered=delivered,
                severity=verdict.severity,
            )
            await self._emit(
                PeerReviewParams(
                    event_id=0,
                    session_id=session_id,
                    emitted_at=int(time.time()),
                    review_id=review_id,
                    turn_id=turn_id,
                    target_entry_id=target_entry_id,
                    status=PeerReviewStatus.COMPLETE,
                    severity=verdict.severity,
                    steerable=verdict.steerable,
                    delivered=delivered,
                    text=verdict.text,
                )
            )
        finally:
            state.review_task = None
            # Trailing edge: material that arrived while the review was in
            # flight schedules the next one.
            with suppress(Exception):
                if state.mirror_dirty:
                    await self._maybe_schedule_review(state, session_id)

    async def _complete_review(
        self,
        state: _SessionPeerState,
        config: VibeConfigSchema,
        model: ModelConfig,
        mirror: list[PublicHistoryEntry],
    ) -> PeerVerdict:
        """One duck completion with a capped, serialized read-only tool loop."""
        ports = state.ports
        if ports is None:
            raise RuntimeError("Peer review loop is not attached to a session")
        executor = ports.tool_executor
        executor.set_policy(PeerToolPolicy.from_config(config))
        catalog = _tool_catalog(executor.supported_tools)
        messages: list[LLMMessage] = [
            LLMMessage(role=Role.system, content=UtilityPrompt.PEER_REVIEW.read()),
            LLMMessage(role=Role.user, content=_build_context(state, mirror)),
        ]
        backend = self._backend_factory.backend_for(model, config)
        rounds_left = config.peer_review.max_tool_rounds
        async with backend:
            while True:
                offering_tools = rounds_left > 0 and bool(catalog)
                # The timeout bounds one completion, not the whole review: a
                # hung call fails after `timeout_seconds`, while a healthy
                # multi-round review may span several timeout periods.
                async with asyncio.timeout(config.peer_review.timeout_seconds):
                    chunk = await backend.complete(
                        model=model,
                        messages=messages,
                        temperature=model.temperature,
                        tools=catalog if offering_tools else None,
                        tool_choice="auto" if offering_tools else None,
                        max_tokens=None,
                        extra_headers={"user-agent": get_user_agent(model.provider)},
                    )
                message = chunk.message
                tool_calls = message.tool_calls or []
                if not tool_calls or not offering_tools:
                    # The final message carries the verdict; when the round cap
                    # is exhausted the last completion runs without tools so the
                    # duck still returns a verdict from gathered evidence.
                    return _parse_verdict(message.content or "")
                rounds_left -= 1
                messages.append(
                    LLMMessage(
                        role=Role.assistant,
                        content=message.content,
                        tool_calls=tool_calls,
                    )
                )
                for call in tool_calls:
                    result = await self._execute_tool_call(
                        executor, call, config.peer_review.timeout_seconds
                    )
                    messages.append(
                        LLMMessage(
                            role=Role.tool,
                            name=call.function.name or "",
                            tool_call_id=call.id or "",
                            content=result,
                        )
                    )

    async def _execute_tool_call(
        self, executor: PeerReviewToolExecutor, call: ToolCall, timeout_seconds: float
    ) -> str:
        """Run one duck tool call; errors are fed back so the review continues."""
        name = call.function.name or ""
        raw_arguments = call.function.arguments or "{}"
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return f"Error: invalid arguments for '{name}': {exc}"
        if not isinstance(arguments, dict):
            return f"Error: invalid arguments for '{name}': expected a JSON object"
        try:
            async with asyncio.timeout(timeout_seconds):
                return await executor.execute(name, arguments)
        except TimeoutError:
            return f"Error: '{name}' timed out after {timeout_seconds:g}s"
        except ToolError as exc:
            return f"Error: {exc}"
        except Exception as exc:
            logger.warning("Peer review tool call failed", exc_info=exc)
            return f"Error: {exc}"

    async def _maybe_auto_steer(
        self,
        session_id: str,
        state: _SessionPeerState,
        config: VibeConfigSchema,
        verdict: PeerVerdict,
        turn_id: str,
    ) -> bool:
        """Auto-steer fires at/above the severity threshold, when steerable."""
        armed = state.phase is _PeerPhase.ARMED and state.turn_id == turn_id
        ports = state.ports
        if (
            not _effective_auto_steer(state, config)
            or verdict.severity.rank < config.peer_review.auto_steer_severity.rank
            or not verdict.steerable
            or not armed
            or ports is None
        ):
            return False
        steer_params = _steer_params(session_id, turn_id, verdict.text)
        try:
            await ports.backend.steer(steer_params)
        except Exception:
            logger.warning("Peer review auto-steer failed", exc_info=True)
            await self._record(
                ports.backend,
                PEER_EVENT_STEER_INJECTED,
                {
                    "trigger": "auto",
                    "severity": verdict.severity.value,
                    "delivered": False,
                },
            )
            return False
        await self._record(
            ports.backend,
            PEER_EVENT_STEER_INJECTED,
            {"trigger": "auto", "severity": verdict.severity.value, "delivered": True},
        )
        return True

    @staticmethod
    def _resolve_model(config: VibeConfigSchema) -> ModelConfig | None:
        """The duck's model, resolved from the session's effective config.

        ``[utility_models] peer`` carries the session override when the user
        ran ``/peer``; an empty or unknown alias means no duck (design §7.2:
        off by default).
        """
        return config.get_utility_model(UtilityFeature.PEER)

    async def _record(
        self, ports: PeerReviewPorts, name: str, properties: dict[str, Any]
    ) -> None:
        """Fire-and-forget telemetry; recording never breaks the review loop."""
        try:
            await ports.record_event(name, properties)
        except Exception:
            logger.debug("Peer review telemetry event failed", exc_info=True)

    async def _record_error(
        self, state: _SessionPeerState, stage: str, error_class: str
    ) -> None:
        """One `vibe.peer_review.error`: the stage and the exception class only."""
        ports = state.ports
        if ports is None:
            return
        await self._record(
            ports.backend,
            PEER_EVENT_ERROR,
            {"stage": stage, "error_class": error_class},
        )

    async def _emit(self, params: PeerReviewParams) -> None:
        try:
            await self._notify("peer/review", params)
        except Exception:
            logger.warning("Failed to emit peer review notification", exc_info=True)

    async def _emit_failed(
        self,
        session_id: str,
        review_id: str,
        turn_id: str,
        target_entry_id: str | None,
        error: str,
    ) -> None:
        await self._emit(
            PeerReviewParams(
                event_id=0,
                session_id=session_id,
                emitted_at=int(time.time()),
                review_id=review_id,
                turn_id=turn_id,
                target_entry_id=target_entry_id,
                status=PeerReviewStatus.FAILED,
                error=error,
            )
        )

    async def _view(
        self, session_id: str, state: _SessionPeerState, ports: PeerReviewPorts
    ) -> PeerConfigView:
        config = await ports.effective_config(session_id)
        alias = config.utility_models.alias_for(UtilityFeature.PEER)
        return PeerConfigView(
            enabled=bool(alias),
            model=alias or None,
            auto_steer=_effective_auto_steer(state, config),
            max_reviews_per_turn=config.peer_review.max_reviews_per_turn,
            auto_steer_severity=config.peer_review.auto_steer_severity,
        )


def _steer_params(session_id: str, turn_id: str, text: str) -> TurnSteerParams:
    """The steer lands as a user message, but it is the duck's text, not the user's.

    Skill mentions stay unexpanded and every ``@`` anchor is broken, so a
    critique can never inline workspace files into the main turn.
    """
    safe_text = _MENTION_ANCHOR.sub("@\u200b", text)
    return TurnSteerParams(
        session_id=session_id,
        expected_turn_id=turn_id,
        message=[
            TextContentBlock(text=f"{STEER_MESSAGE_PREFIX}{STEER_ADVISORY}{safe_text}")
        ],
        inject_invoked_skill=False,
    )


def _effective_auto_steer(state: _SessionPeerState, config: VibeConfigSchema) -> bool:
    if state.auto_steer is not None:
        return state.auto_steer
    return config.peer_review.auto_steer


def _mirror_text(entry: PublicHistoryEntry) -> str | None:
    """The duck's text-only view of one history entry.

    Reasoning, callbacks and checkpoints are harness plumbing the duck never
    sees (design §7.4: the mirror is text-only). Tool calls and results are
    effects, so they carry their input and output — without them the duck
    would only ever see plain messages.
    """
    match entry:
        case PublicMessageEntry():
            return f"{entry.role}: {entry.text}"
        case PublicEffectEntry():
            return _effect_text(entry)
        case PublicNoticeEntry():
            return f"notice[{entry.level}]: {entry.message}"
        case _:
            return None


def _effect_text(entry: PublicEffectEntry) -> str:
    """One tool effect as the duck sees it: title, status, input, output."""
    lines = [f"effect: {entry.title} ({entry.state.status})"]
    if entry.detail.input is not None:
        lines.append(f"input: {_clip(_json_text(entry.detail.input))}")
    output_text = _effect_output_text(entry.state)
    if output_text:
        lines.append(f"output: {_clip(output_text)}")
    return "\n".join(lines)


def _effect_output_text(state: EffectState) -> str:
    """The effect's result text: the transcript when there is one, else the
    structured output (a read_file's body lives in ``output.content``, not in
    ``output_text``).
    """
    match state:
        case PendingEffectState() | SkippedEffectState():
            return ""
        case CompletedEffectState() | FailedEffectState():
            if state.output_text:
                return state.output_text
            if state.output is not None:
                return _json_text(state.output)
            return ""
        case _:
            return state.output_text


def _json_text(value: Any) -> str:
    # Effect inputs are pydantic models; dump with field names (not wire
    # aliases) so they match the tool schemas the duck's catalog advertises.
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, default=str)


def _clip(text: str, limit: int = PEER_EFFECT_FIELD_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}… [+{len(text) - limit} chars]"


def _build_context(state: _SessionPeerState, mirror: list[PublicHistoryEntry]) -> str:
    """The duck's independent context for one review (design §7.4).

    The critic prompt goes in the system message; this is the user message:
    the turn's original instructions, the new transcript material, and the
    concerns ledger. The main model's system prompt and context window are
    never included.
    """
    mirror_text = "\n".join(
        text for entry in mirror if (text := _mirror_text(entry)) is not None
    )
    return "\n\n".join([
        "## User instructions for this turn",
        state.user_instructions or "(none recorded)",
        "## New transcript material since the last review",
        mirror_text or "(none)",
        "## Concerns ledger",
        "\n".join(state.ledger) or "(none)",
    ])


def _append_ledger_line(ledger: list[str], line: str) -> None:
    """Append a concern, dropping the oldest delivered, then the oldest open."""
    ledger.append(line)
    while len(ledger) > PEER_LEDGER_MAX_CONCERNS:
        delivered_index = next(
            (
                index
                for index, item in enumerate(ledger)
                if item.startswith("DELIVERED ")
            ),
            None,
        )
        if delivered_index is not None:
            ledger.pop(delivered_index)
        else:
            # The oldest open concern is superseded by the bounded window.
            ledger.pop(0)


_CATALOG_TOOL_CLASSES: tuple[type[BaseTool[Any, Any, Any, Any]], ...] = (
    ReadFile,
    Grep,
    WebSearch,
    WebFetch,
)


def _tool_catalog(supported: frozenset[str]) -> list[RustToolDefinition]:
    """The duck's advertised tools: the read-only classes the executor runs.

    ``web_search`` only appears when the executor supports it — the workspace
    executor does not in v1 (no Python search client exists; the runtime runs
    it in Rust), so the catalog honestly omits it.
    """
    catalog: list[RustToolDefinition] = []
    for tool_class in _CATALOG_TOOL_CLASSES:
        name = tool_class.get_name()
        if name not in supported:
            continue
        catalog.append(
            RustToolDefinition(
                name=name,
                description=tool_class.get_full_description(),
                parameters=tool_class.get_parameters(),
            )
        )
    return catalog


def _to_wire_messages(messages: Sequence[LLMMessage]) -> list[RustMessage]:
    """The duck's LLMMessage conversation in the Harness wire format.

    Unlike the utility-completion converter, this keeps the tool loop: assistant
    tool calls become ``RustModelToolCallPart`` and tool results become
    ``RustToolMessage``.
    """
    converted: list[RustMessage] = []
    for message in messages:
        text = message.content or ""
        match message.role:
            case Role.system:
                if text:
                    converted.append(
                        RustSystemMessage(content=[RustTextContentBlock(text=text)])
                    )
            case Role.user:
                if text:
                    converted.append(
                        RustUserMessage(content=[RustTextContentBlock(text=text)])
                    )
            case Role.assistant:
                parts: list[RustAssistantPart] = []
                if text:
                    parts.append(RustTextContentBlock(text=text))
                for call in message.tool_calls or []:
                    parts.append(
                        RustModelToolCallPart(
                            id=call.id or "",
                            name=call.function.name or "",
                            arguments=_wire_tool_arguments(call.function.arguments),
                        )
                    )
                if parts:
                    converted.append(RustAssistantMessage(content=parts))
            case Role.tool:
                converted.append(
                    RustToolMessage(
                        tool_call_id=message.tool_call_id or "",
                        name=message.name or "",
                        outcome="success",
                        content=[RustTextContentBlock(text=text)] if text else [],
                    )
                )
    return converted


def _wire_tool_arguments(raw: str | None) -> RustToolArguments:
    """The replayed tool call's arguments in the Harness wire format."""
    text = raw or "{}"
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return RustInvalidJsonToolArguments(raw=text, error="invalid JSON arguments")
    return RustJsonToolArguments(raw=text, value=value)


def _to_peer_chunk(result: RustCompletionResult) -> PeerChunk:
    """The adapter's completion result back into a duck reply message."""
    text = "".join(
        part.text for part in result.parts if isinstance(part, RustTextContentBlock)
    )
    tool_calls = [
        ToolCall(
            id=part.id,
            function=FunctionCall(name=part.name, arguments=part.arguments_json),
        )
        for part in result.parts
        if isinstance(part, RustCompletionResultToolCallPart)
    ]
    return PeerChunk(
        message=LLMMessage(
            role=Role.assistant, content=text, tool_calls=tool_calls or None
        )
    )


_FENCED_JSON = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _parse_verdict(content: str) -> PeerVerdict:
    """Parse the duck's strict JSON verdict; fall back to plain text at info."""
    text = content.strip()
    fenced = _FENCED_JSON.match(text)
    if fenced is not None:
        text = fenced.group(1).strip()
    fallback = PeerVerdict(
        severity=PeerSeverity.INFO, steerable=False, text=text or "No concerns."
    )
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    try:
        severity = PeerSeverity(payload.get("severity"))
    except (TypeError, ValueError):
        return fallback
    verdict_text = payload.get("text")
    if not isinstance(verdict_text, str) or not verdict_text.strip():
        return fallback
    return PeerVerdict(
        severity=severity,
        steerable=payload.get("steerable") is True,
        text=verdict_text.strip(),
    )
