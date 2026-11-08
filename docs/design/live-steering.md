# Design Proposal: Provider-Level Live Steering

**Status:** proposed
**Related:** `docs/design/rubber-duck-peer-review.md` (auto-steer is the main consumer);
inspired by oh-my-pi's `packages/agent/src/live-steering.ts`.

---

## 1. Background

Today, a mid-turn steer (`turn/steer`, `UserMessageMode::Steer`) is queued in
`ActiveTurn::pending_steer` (`harness/core/src/core/state.rs:587`) and committed by
`commit_pending_model_input` (`harness/core/src/core/turn.rs:844`) right before the
*next* agent completion. The in-flight completion is abandoned and restarted with the
steer message appended — every steer mid-stream discards the tokens the provider
already generated and pays a full new prefill.

Verified behavior (backend-contract repro, this repo): a steer delivered while a
completion is gated open causes the gated response to be discarded and a fresh
completion to be issued with the steer text appended.

oh-my-pi solves the same problem with provider live steering: providers that support
it (their fork uses the OpenAI Responses `response.steer` input) accept queued user
input *into the response currently streaming*, so no tokens are discarded. The loop
records what the provider accepted so the transcript matches what the model saw, and
anything declined is injected at the next boundary as ordinary steering.

## 2. Goals

- When the active provider adapter advertises live steering, a mid-turn steer is
  forwarded into the in-flight completion instead of abandoning it.
- The transcript records exactly what the model saw: accepted steers appear in the
  conversation at the point the provider consumed them; declined steers fall back to
  the current deferred path.
- Providers without support keep today's behavior byte-for-byte.
- Zero change to the external wire contract for clients: `turn/steer` is unchanged;
  only the runtime's handling of it differs.

## 3. Design

### 3.1 Capability detection

`adapter_capabilities()` (Python side, `mistralai_vibe_local_harness/vibe/...`) gains
a `live_steer: bool` flag, surfaced through `RustHarnessCapabilitySet` /
`LocalRuntimeAdapterConfig` so Core knows per session which path to take. Default
false; only the provider adapters that actually implement the provider-side channel
set it true.

### 3.2 Runtime flow

- `handle_user_message(mode=Steer)` in `turn.rs` currently always calls
  `defer_steer`. New behavior: if the active turn has an in-flight agent completion
  AND the session's capabilities say `live_steer`, emit a new action
  `Action::LiveSteer { turn_id, content }` instead of queueing.
- Core forwards `LiveSteer` to the host runtime as a sub-command on the running
  completion (new wire method, e.g. `completion/steer`), alongside the existing
  `turn/steer` command. The host adapter keeps the HTTP stream open and sends the
  steer to the provider.
- The provider answers with accepted/declined (omp: the Responses steer ack). The
  host reports `Observation::LiveSteerAccepted { turn_id, content }` or
  `LiveSteerDeclined { turn_id, content }` back to Core.
  - Accepted: Core appends the user message to the context at the current position
    and the in-flight completion continues; the transcript shows the steer exactly
    where the provider consumed it.
  - Declined: Core falls back to `defer_steer` (current abandon+restart path).
- If no completion is in flight (turn between calls, tool running), keep today's
  `defer_steer` — there is nothing to steer into.

### 3.3 Transcript / checkpoint

`LiveSteerAccepted`/`Declined` are new `Observation` variants, recorded in the
checkpoint like `TurnSteered` today, so resume/rewind see the same conversation the
model saw. No format-version bump needed if observations are additive (verify
against `checkpoint/v1` schema tests); otherwise bump the minor.

### 3.4 Fallback and safety

- Provider errors mid-steer → treat as declined, fall back.
- A steer arriving while compaction is pending keeps the current rejection.
- The rubber-duck auto-steer and `/peer` steer use the same `turn/steer` entry point,
  so they benefit automatically with no app-server changes.

## 4. Alternatives considered

- **A: Always abandon+restart (status quo).** Simple, but pays full re-prefill per
  steer; the duck's auto-steer makes this expensive in practice.
- **B: Provider live steer only, no fallback (omp's shape).** Drops the safety net;
  we keep the deferred path for providers without support.
- **C: Pause gate + steer at boundary (omp's pause.ts).** Cheaper to build but still
  discards in-flight tokens; complements rather than replaces live steer.

## 5. Testing

- Core unit tests: steer with no in-flight completion → deferred (unchanged); steer
  with in-flight + capability on → `LiveSteer` action, no abandonment; declined →
  falls back to `pending_steer`; capability off → unchanged.
- Checkpoint schema tests for the new observations (additive, no version bump).
- Python host tests with a fake adapter: accepted and declined paths.
- Backend-contract e2e: gated streaming response + steer → assert the same HTTP
  request continues (no second request) when supported, and a second request when not.

## 6. Status after implementation (2026-11-04)

The full mechanism is in place: `live_steer` model fact, `Action::LiveSteer`,
`live_steer_accepted`/`live_steer_declined` commands and observations, checkpoint
of pending live steers, and the Runtime dispatch (`LiveStreamSteerer` registry in
`_completion.py`, `_local_actions.py` answering accepted/declined).

Finding: no adapter in this Runtime can currently steer an open stream. The
Responses `response.steer` channel that oh-my-pi uses exists only on the Codex
WebSocket transport, not on plain HTTP SSE, so `adapter_capabilities()` reports
`live_steer=False` for every wire and Core keeps the deferred path. The
`create_live_steerer` hook in `_completion.py` is the extension point: an adapter
with a real steer channel (e.g. a WebSocket transport) returns a steerer there
and sets the capability flag, and everything else engages automatically.

Open questions:
- Does the provider charge the steer as input tokens mid-stream? Record usage
  from the provider ack for telemetry once a real channel exists.
