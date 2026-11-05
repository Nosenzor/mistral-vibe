"""Contract: the rubber duck sees tool data in its mirror and its steer lands.

The duck's review call is a second chat completion on the peer model, so the
mocked Mistral route separates duck requests from main-model requests by
model name. The main model's second call is gated so the turn stays running
while the duck reviews and steers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import respx
import tomli_w

from tests.app_server.backend_contract.conftest import (
    _GatedSSEStream,
    _mistral_sse_payload,
)
from tests.conftest import get_base_config
from vibe.app_server._process_services import ProcessServices
from vibe.app_server._runtime import HarnessProcess, create_harness_server
from vibe.app_server.client import AppServerClient
from vibe.app_server.host import AppServerHost
from vibe.app_server.protocol import ClientCapabilities, ClientInfo, SessionOptions
from vibe.app_server.transport import memory_transport_pair

# Drives start_turn, steer_turn and write_config, whose runtime path depends on
# the Session API switch.

_DUCK_MODEL = "duck-model"
_UTILITY_MODELS = ("mistral-vibe-cli-fast", "mistral-small-latest")
_STEER_TEXT = "DUCK SAYS STOP 678"


@pytest_asyncio.fixture
async def peer_harness(
    config_dir: Path, process_services: ProcessServices, monkeypatch: pytest.MonkeyPatch
) -> Any:
    monkeypatch.setenv("VIBE_TEST_DISABLE_MODEL_PROBE", "1")
    config = get_base_config()
    config["models"].append({
        "name": _DUCK_MODEL,
        "provider": "mistral",
        "alias": "duck",
    })
    (config_dir / "config.toml").write_text(tomli_w.dumps(config), encoding="utf-8")
    client_transport, server_transport = memory_transport_pair()
    harness = await create_harness_server(
        server_transport,
        transport_kind="in_process",
        process=HarnessProcess(services=process_services),
    )
    client = AppServerClient(client_transport, run_peer=harness.serve)
    host = await AppServerHost.connect(
        client,
        client_info=ClientInfo(name="peer-contract-test", version="0"),
        capabilities=ClientCapabilities(),
        session_options=SessionOptions(),
        client_factory=harness.connect_client,
    )
    try:
        yield harness, client, host
    finally:
        await host.close()


def _sse(
    content: str, tool_calls: list[dict[str, Any]] | None = None
) -> httpx.Response:
    return httpx.Response(
        200,
        stream=httpx.ByteStream(stream=_mistral_sse_payload(content, tool_calls)),
        headers={"Content-Type": "text/event-stream"},
    )


@pytest.mark.one_path
@pytest.mark.asyncio
async def test_duck_mirror_sees_tool_data(
    peer_harness: Any,
    backend_contract_mistral_api: respx.Route,
    tmp_working_directory: Path,
) -> None:
    """The duck's mirror carries a tool call's input and its result body.

    One path only: on the Session API path the effect settles at turn end, so
    the mirror content timing is path-dependent; the payload assertions here
    observe the provider request, not the switch.
    """
    harness, client, host = peer_harness
    (tmp_working_directory / "a.py").write_text(
        "FILE BODY MARKER 12345\n", encoding="utf-8"
    )
    session = await host.open_session()
    await client.request(
        "peer/configure", {"sessionId": session.session_id, "model": "duck"}
    )

    main_release = asyncio.Event()
    state = {"main_calls": 0}

    def side_effect(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        model = payload.get("model")
        if model == _DUCK_MODEL:
            verdict = json.dumps({
                "severity": "info",
                "steerable": False,
                "text": "No concerns.",
            })
            return _sse(verdict)
        if model in _UTILITY_MODELS:
            return _sse("a title")
        state["main_calls"] += 1
        if state["main_calls"] == 1:
            tool_call = {
                "id": "call-1",
                "index": 0,
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "a.py"}),
                },
            }
            return _sse("reading the file", [tool_call])
        return httpx.Response(
            200,
            stream=_GatedSSEStream(
                _mistral_sse_payload("final answer", None),
                asyncio.Event(),
                main_release,
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    backend_contract_mistral_api.mock(side_effect=side_effect)

    async def drive() -> None:
        async for _event in session.act("fix the parser"):
            pass

    task = asyncio.create_task(drive())
    loop = asyncio.get_running_loop()

    def duck_user_text() -> str:
        texts: list[str] = []
        for call in backend_contract_mistral_api.calls:
            payload = json.loads(call.request.content)
            if payload.get("model") != _DUCK_MODEL:
                continue
            for msg in payload.get("messages", []):
                if msg.get("role") == "user" and isinstance(msg.get("content"), str):
                    texts.append(msg["content"])
        return "\n".join(texts)

    main_release.set()
    await asyncio.wait_for(task, timeout=15)
    deadline = loop.time() + 5
    while loop.time() < deadline and "FILE BODY MARKER 12345" not in duck_user_text():
        await asyncio.sleep(0.05)
    duck_user = duck_user_text()
    assert '"filePath": "a.py"' in duck_user
    assert "FILE BODY MARKER 12345" in duck_user
    await session.close()


@pytest.mark.both_paths
@pytest.mark.asyncio
async def test_duck_steer_reaches_the_main_model(
    peer_harness: Any,
    backend_contract_mistral_api: respx.Route,
    tmp_working_directory: Path,
) -> None:
    """A mid-turn steer is delivered into the running turn; a late steer is a
    clean accepted=false, not an RPC error.
    """
    harness, client, host = peer_harness
    session = await host.open_session()
    await client.request(
        "peer/configure", {"sessionId": session.session_id, "model": "duck"}
    )

    main_started = asyncio.Event()
    main_release = asyncio.Event()
    state = {"main_calls": 0}

    def side_effect(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        model = payload.get("model")
        if model == _DUCK_MODEL:
            verdict = json.dumps({
                "severity": "critical",
                "steerable": True,
                "text": _STEER_TEXT,
            })
            return _sse(verdict)
        if model in _UTILITY_MODELS:
            return _sse("a title")
        state["main_calls"] += 1
        if state["main_calls"] == 1:
            tool_call = {
                "id": "call-1",
                "index": 0,
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "a.py"}),
                },
            }
            return _sse("reading the file", [tool_call])
        main_started.set()
        return httpx.Response(
            200,
            stream=_GatedSSEStream(
                _mistral_sse_payload("final answer", None), main_started, main_release
            ),
            headers={"Content-Type": "text/event-stream"},
        )

    backend_contract_mistral_api.mock(side_effect=side_effect)

    async def drive() -> None:
        async for _event in session.act("fix the parser"):
            pass

    task = asyncio.create_task(drive())
    controller = harness._server._peer_review
    loop = asyncio.get_running_loop()

    def duck_reviews() -> dict[str, Any]:
        state_ = controller._sessions.get(session.session_id)
        return dict(state_.reviews) if state_ is not None else {}

    deadline = loop.time() + 15
    while not duck_reviews() and loop.time() < deadline:
        await asyncio.sleep(0.05)
    reviews = duck_reviews()
    assert reviews, "no peer review completed"

    # Steer while the turn is still running (the main model's second call is
    # gated open): the critique must be accepted and reach the main model.
    review_id = next(iter(reviews))
    steer_resp = await client.request(
        "peer/steer", {"sessionId": session.session_id, "reviewId": review_id}
    )
    assert steer_resp == {"accepted": True, "delivered": True}

    main_release.set()
    await asyncio.wait_for(task, timeout=15)

    main_text = "\n".join(
        json.dumps(json.loads(call.request.content).get("messages"))
        for call in backend_contract_mistral_api.calls
        if json.loads(call.request.content).get("model")
        not in (_DUCK_MODEL, *_UTILITY_MODELS)
    )
    assert _STEER_TEXT in main_text, (
        "the steered critique never reached the main model's requests"
    )

    # Steering after the turn completed is a clean refusal, not an RPC error.
    undelivered = [rid for rid, r in duck_reviews().items() if not r.delivered]
    late_rid = undelivered[0] if undelivered else review_id
    late_resp = await client.request(
        "peer/steer", {"sessionId": session.session_id, "reviewId": late_rid}
    )
    if late_rid in undelivered:
        assert late_resp == {"accepted": False, "delivered": False}

    await session.close()
