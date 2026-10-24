# Design Proposal: Rubber Duck Peer Review

**Tracking issue:** Not created

---

## 1. Background

The feature request: a "rubber duck" mode in which a secondary model reviews the
main model live while a turn runs, critiques its decisions peer-programmer
style, argues with it, and keeps it on track with the user's instructions and
plan. For example, Mistral Large as the main model with GLM 5.3 as the duck in
the background.

Accepted decisions from the design session (2026-10-24):

- **Delivery:** the duck's critique always streams to the UI; additionally,
  opt-in auto-steer injects strong critiques into the running turn so the main
  model hears them directly.
- **Cadence:** live, per step — the duck reviews as the turn progresses, with a
  configurable per-turn budget.
- **Control surface:** a `[utility_models] peer` config key sets the default
  duck model (empty = off); a `/peer` slash command toggles and re-models the
  duck per session.
- **Architecture:** an app-server feature module (Approach A below). No
  Runtime/Core (harness) changes.
- **UI:** a side panel (split view) in the Rust TUI, chosen from three layout
  options in a visual review.

## 2. Problem

A single model working a turn has no second opinion. It can drift off the
user's instructions, skip the plan, or take a risky action without anyone
challenging it until a human reads the transcript after the fact. Vibe has no
mechanism for a second model to observe a running session and advise on it.
Current adjacent behavior, source-confirmed:

- Secondary "utility" models exist but only for background one-shot completions
  (title generation, smart-approve classification, compaction, vision
  fallback). None observe the live turn.
- Post-hoc code review (`vibe/core/review/manager.py`) reviews file changes
  after they land; it does not review the model's reasoning or steer the turn.

## 3. Goals

- A configured secondary model observes every running turn live and produces a
  critique after each assistant/tool step, visible in the TUI while work is
  ongoing.
- With opt-in auto-steer, critiques at or above a severity threshold are
  injected into the running turn so the main model hears them.
- The duck can reference the user's original instructions and keep the main
  model on plan.
- The duck is advisory only: it gets no tools and cannot act on the workspace.
- Cost is bounded: a configurable per-turn budget caps peer completions.
- The main turn is never blocked or failed by duck errors.

## 4. Non-Goals

- No harness (Unified Harness Core/Runtime) changes. In-loop peering, streaming
  token visibility, and pre-execution tool gating are a possible future
  deepening, not this version.
- The duck cannot call tools, edit files, or spawn subagents.
- No ACP-specific rendering of critiques in v1; the `peer/review` notification
  is on the wire, but only the Rust TUI renders it.
- No cross-session duck memory; each session's duck starts fresh.
- No persistence of the per-session `/peer` override beyond the session.

## 5. Terminology and Constraints

- **Main model:** the session's active model (`active_model`).
- **Duck (peer model):** a secondary model configured via
  `[utility_models] peer`, used only for review completions.
- **Review:** one peer completion that critiques the transcript since the last
  review. A review has a structured verdict: `severity` (`info` | `warning` |
  `critical`), `steerable` (whether injecting it would help), and `text` (the
  critique).
- **Steer:** injecting a message into the running turn via the existing
  `turn/steer` RPC.
- **Budget:** max reviews per turn (`peer_review.max_reviews_per_turn`,
  default 10).
- Constraints: the harness is an external dependency (ADR 0014 version skew);
  new agent behavior belongs in an app-server feature module (ADR 0018);
  server-owned behavior ships behind app-server methods/notifications, with the
  TUI only rendering (ADR 0002, ADR 0009, ADR 0016); config changes follow
  layered configuration (ADR 0005); every feature ships telemetry (ADR 0008).

## 6. Current System and Evidence

Production path today: the Rust TUI speaks JSON-RPC to the app server
(`vibe/app_server/server.py`); the server drives one active session backend,
the `UnifiedHarnessBackendAdapter`
(`vibe/app_server/_unified_harness_backend_adapter.py`), which hosts the
external Unified Harness runtime that owns the agent loop. The server holds a
single event subscription per active backend and fans events out in
`_forward_backend_event` (`server.py:350`).

| Observation | Source | Verification status | Design consequence |
| :--- | :--- | :--- | :--- |
| Secondary model routing exists: `[utility_models] title / smart_approve` (`UtilityFeature` enum, `UtilityModelsConfig`) | `vibe/core/config/models.py:72-108`, `vibe/core/config/vibe_schema.py:528,1054` | Source-confirmed | Add `UtilityFeature.PEER` + `utility_models.peer`, reusing `get_utility_model` |
| Utility models are derived into runtime routes next to `active_model` | `vibe/app_server/_runtime.py:1357-1377` (`compaction_model`, `title_model`) | Source-confirmed | Peer model resolution follows the same path, but the peer call is made by the app server, not the runtime |
| The app server can call any configured model directly via `create_backend(provider=...)` | `vibe/core/llm/backend/factory.py` | Source-confirmed | The duck completion runs in app-server code with the peer's `ModelConfig` + provider |
| `turn/steer` injects a message into a running turn | `vibe/app_server/protocol.py:2402` (`TurnSteerParams`), `_unified_harness_backend_adapter.py:8125` (`steer_turn`) | Source-confirmed | Auto-steer and the manual "steer main" action reuse this RPC unchanged |
| Session events observable: `TurnStarted`, `TurnCompleted`, `HistoryEntryAdded`, `HistoryEntryUpdated` | `vibe/app_server/events.py:44-87`, fanned out in `server.py:344-380` | Source-confirmed | The controller taps the server's existing event fan-out; no second subscription |
| Per-session model override pattern: writes mirrored into the session `OverridesLayer` | `vibe/app_server/_session_model.py:24-95` | Source-confirmed | `/peer` persists its override the same way (`utility_models.peer` path) |
| Slash commands are declared in one registry | `vibe/cli-rust/src/commands/commands.rs:35` (`/model`) | Source-confirmed | `/peer` registers alongside `/model` |
| Feature prompts live as markdown in `vibe/core/prompts/` | `vibe/core/prompts/session_title.md`, `vision_describe.md` | Source-confirmed | The duck's critic prompt ships as `peer_review.md` |
| Post-hoc file review is a different feature (checkpoint-based change review) | `vibe/core/review/manager.py` | Source-confirmed | Out of scope; no reuse |

## 7. Proposed Design

### 7.1 Decision Summary and High-Level Overview

An app-server feature module, `PeerReviewController`, observes the active
session's event stream, mirrors new history entries into a bounded transcript,
and runs review completions against the configured duck model through Vibe's
own LLM backend factory. Each review is emitted as a `peer/review`
notification (started, then completed — the same added/updated pattern as
history entries) and rendered in a new TUI side panel. When auto-steer is
enabled, reviews at or above the configured severity are injected into the
running turn with `turn/steer`; the panel also offers a manual per-critique
"steer main" action.

Normal path: user runs `/peer glm-5.3` (or config sets `[utility_models] peer`)
→ session starts a turn → `turn/started` arms the duck and resets the budget
→ each `history/entry/added` is mirrored and coalesced → the duck model is
called with the critic prompt + the turn's user message + the bounded mirror →
`peer/review` (running) then `peer/review` (complete, verdict + text) → TUI
panel renders it → if auto-steer is on and severity crosses the threshold,
the text is steered into the turn → `turn/completed` retires the duck.

### 7.2 User and Product Behavior

- **Off by default.** No duck unless `[utility_models] peer` names a model or
  the user enables it.
- **`/peer`** with no argument shows current state; `/peer <model>` enables
  with that model for this session; `/peer off` disables. Mirrors `/model`.
- **Panel:** a toggleable right-hand split panel. Header shows the duck model
  and its provider, plus status ("reviewing step N", "budget reached",
  "peer error: …"). Each
  critique shows severity, target step, text, and actions: *steer main*
  (injects into the running turn; disabled once the turn ends or the review is
  delivered) and *dismiss*. When closed, the main chat reclaims the width.
- **Auto-steer** (`peer_review.auto_steer`, default `false`): when on,
  reviews with `severity >= peer_review.auto_steer_severity` (default
  `critical`) are injected automatically; the panel marks them "delivered".
- **Degraded states:** peer provider errors mark the review failed and silence
  the duck for the rest of the turn; the main turn is unaffected. Budget
  exhaustion silences reviews but keeps the panel. A steer that arrives after
  the turn ended leaves the critique displayed and marked "not delivered".
- **Cancellation:** interrupting the turn cancels the in-flight peer call.

### 7.3 Architecture and Ownership

```mermaid
flowchart LR
    Harness["Unified Harness (agent loop)"] -->|"turn/started, history/entry/added, turn/completed"| Tap
    Tap["Server event tap (server.py fan-out)"] --> Controller["PeerReviewController (vibe/app_server/_peer_review.py)"]
    Controller -->|"bounded mirror"| Mirror["Per-session transcript mirror"]
    Controller -->|"create_backend(provider)"| LLM["Duck model completion (vibe/core/llm)"]
    Controller -->|"notify peer/review"| Notify["Server notification port"]
    Controller -->|"turn/steer"| Steer["Server steer port -> root backend"]
    Notify --> TUI["Rust TUI side panel"]
    TUI -->|"peer/configure, peer/steer"| Server["App server RPCs"]
    Server --> Controller
```

Ownership:

- `PeerReviewController` owns: enabled state, budget, mirror, coalescing,
  review scheduling, verdict parsing, steer decisions. Lives in
  `vibe/app_server/_peer_review.py` (ADR 0018 module owner).
- The server owns: the single backend subscription, the event tap seam, RPC
  dispatch, notification fan-out, controller lifecycle (create on backend
  activation, cancel on switch/shutdown).
- `vibe/core/config` owns: `utility_models.peer`, the `peer_review` settings
  group, and validation (unknown alias warnings, same-model-as-main warning).
- `vibe/core/llm` owns: the backend used for duck completions (unchanged).
- The TUI owns: panel presentation, `/peer`, key toggle, local panel state.
- Injected ports (ADR 0018 — narrow, no whole-server access): a config getter, a
  backend-event tap registration, a notify port, a steer port, and a backend
  factory. The controller never reaches into another feature's state.

### 7.4 Contracts and Data

New wire shapes (all camelCase `ProtocolModel`s, `extra="forbid"`):

- Notification **`peer/review`** — `session_id`, `review_id`, `turn_id`,
  `target_entry_id` (history entry the review refers to, nullable),
  `status` (`running` | `complete` | `failed`), `severity`
  (`info` | `warning` | `critical`, nullable while running), `steerable`
  (bool), `delivered` (bool), `text` (string), `error` (string, nullable).
  Emitted twice per review (running → complete), mirroring
  `history/entry/added` + `history/entry/updated`.
- **`peer/configure`** — params `session_id`, `model` (string, nullable),
  `auto_steer` (bool, nullable); response `PeerConfigView` (`enabled`,
  `model`, `auto_steer`, `max_reviews_per_turn`, `auto_steer_severity`).
- **`peer/read`** — params `session_id`; response `PeerConfigView`.
- **`peer/steer`** — params `session_id`, `review_id`; injects that review's
  text into the running turn. Response `PeerSteerResponse` (`accepted`,
  `delivered`).

Config (layered, ADR 0005):

- `[utility_models] peer = "glm-5.3"` — duck model alias; empty = off.
  `UtilityFeature.PEER` added; `get_utility_model(PEER)` resolves it.
- `[peer_review]` group: `max_reviews_per_turn` (int, default 10, ge 1),
  `auto_steer` (bool, default false), `auto_steer_severity`
  (`info` | `warning` | `critical`, default `critical`).

Duck completion contract: no tools, `tool_choice="none"`, temperature from the
peer `ModelConfig`, output parsed as a strict JSON verdict
(`{"severity": ..., "steerable": ..., "text": ...}`) with a tolerant fallback
to plain text at `severity=info`.

Steer payload: a user-role message, text prefixed with a fixed marker
(`[rubber duck] `) so the main model can attribute the source.

### 7.5 Lifecycle, State, and Concurrency

- One controller per active root session, created on backend activation and
  cancelled on backend switch/shutdown (`cancel_tasks`, `_execution.py`).
- Per-turn state machine: `idle → armed (turn/started) → reviewing ⇄ coalescing
  → retired (turn/completed | turn/interrupted)`.
- Coalescing: entries arriving while a review is in flight batch into the next
  review (trailing edge); at most one peer call in flight per session.
- Budget decrements per completed review attempt (success or failure), per
  turn.
- Mirror bounds: the last N entries (default 40) plus the turn's original user
  message, token-capped; images stripped from the mirror (text-only review).
- Enabling mid-session starts the mirror fresh; the duck reviews from that
  point. Config reload re-resolves the peer model live; the enabled flag and
  budget survive.
- Idempotency: `peer/configure` and `peer/steer` are safe to repeat;
  `peer/steer` on an already-delivered review is a no-op returning
  `delivered=true`.

### 7.6 Failure Semantics

| Failure point | Caller result | Durable state | Recovery or retry |
| :--- | :--- | :--- | :--- |
| Peer provider error mid-review | `peer/review` status `failed` | Review counted against budget; duck silenced until next turn | Next turn re-arms; no automatic retry within the turn |
| Peer model alias unknown | `peer/configure` fails with a typed validation error; config warns at load | Override not applied | User picks a valid alias |
| Budget exhausted | Panel shows "budget reached" | Counter persists for the turn | Resets on next `turn/started` |
| Steer rejected (turn ended) | Review stays displayed, `delivered=false` | Review record kept in panel | Manual re-steer disabled; user can paste the text |
| Turn interrupted | In-flight peer call cancelled | Mirror kept; duck re-arms on next turn | `turn/started` of the next turn |
| Backend switch/shutdown | Controller cancelled | Nothing persisted | New session creates a fresh controller |
| Malformed duck output | Fallback parse: plain text, `severity=info`, `steerable=false` | Review still delivered | None needed |

### 7.7 Compatibility and Migration

- Additive only: new config keys (empty defaults), new RPC family, new
  notification. No existing wire shape changes; clients that do not know
  `peer/review` ignore it per the existing notification policy.
- No persisted state to migrate. Sessions resumed mid-feature start with the
  duck disabled until configured.
- Settings Manager (Vibe Code managed config) and Le Chat Web backend
  allowlists must be checked for the new config fields before release; if the
  allowlist is closed, the fields stay CLI-local until updated.

### 7.8 Observability, Privacy, and Security

- **Telemetry (ADR 0008):** `vibe.peer_review.configured`,
  `vibe.peer_review.review_completed` (properties: severity, steerable,
  delivered, budget_remaining, duration — **never** critique text),
  `vibe.peer_review.steer_injected`, `vibe.peer_review.budget_exhausted`,
  `vibe.peer_review.error` (error class only). Verified locally with
  `DEBUG_LEVEL=1 uv run vibe`.
- **Privacy:** the transcript mirror is sent to the duck model's provider. This
  is the same trust boundary as the main model only when both share a
  provider; crossing providers sends session content to a second provider —
  the config warning for `peer` naming a different provider than the main
  model covers this, and the panel header always shows the duck's provider.
- **Security:** the duck receives no tools and no credentials beyond its
  provider's; steer injection is a user-role message, which the main model
  treats as untrusted input like any user message. The duck cannot escalate
  permissions.

## 8. Implementation Plan

| Slice | Behavior proved | Owner and likely files | Exit criteria |
| :--- | :--- | :--- | :--- |
| 1. Config | `[utility_models] peer` + `[peer_review]` group load, merge, validate, and resolve via `get_utility_model(PEER)` | `vibe/core/config/models.py`, `vibe_schema.py`, `_config_introspect.py`; tests under `tests/core/config/` | New keys round-trip through layers; unknown alias warns; pyright + unit tests pass |
| 2. Protocol | `peer/review` notification + `peer/configure` / `peer/read` / `peer/steer` RPCs validate on the wire and dispatch | `vibe/app_server/protocol.py`, `events.py`, `server.py` dispatch; contract tests | Malformed payloads rejected with typed errors; methods route to the module |
| 3. Controller | Event tap → mirror → coalesced peer call → notification; budget; steer on threshold | `vibe/app_server/_peer_review.py`, `vibe/core/prompts/peer_review.md`; unit tests with `FakeBackend` | Fake-driven tests cover coalescing, budget, failure silence, steer trigger, interrupt cancel |
| 4. Session plumbing | `/peer` override lands in the session `OverridesLayer`; runtime derivation resolves the peer model; controller lifecycle follows backend activation | `vibe/app_server/_session_model.py` pattern, `_runtime.py`, `server.py` wiring | Session override survives reload, dies with the session; derivation test |
| 5. TUI | Side panel renders `peer/review`, `/peer` command, key toggle, steer/dismiss actions | `vibe/cli-rust/src/commands/`, `vibe/cli-rust/src/ui/`; `client-e2e` scenario + golden | Golden shows the panel; `/peer` round-trips; `make test_rust` passes |
| 6. Telemetry + docs | Events fire with approved properties; built-in `vibe` skill documents `/peer` and the config keys | `vibe/core/telemetry/`, `vibe/plugins/builtins/vibe/skills/vibe/SKILL.md` | `DEBUG_LEVEL=1` run shows events; skill updated |

No ADR changes required: the design stays inside ADR 0018's app-server module
rule and ADR 0002/0009's server-owned behavior boundary. No harness sibling
repository changes.

## 9. Validation Plan

- Every implementation slice meets its exit criteria (section 8).
- `uv run ruff check --fix .` and `uv run ruff format .` clean; report modified files.
- `uv run pyright` strict passes.
- Focused pytest slices per module, then `uv run pytest` full suite.
- Rust gates: `make test_rust QUIET=1`, then the new `client-e2e` golden.
- Manual: `DEBUG_LEVEL=1 uv run vibe` with a configured peer model — panel shows
  live critiques; auto-steer delivers a marked critique; telemetry lines appear.
- Settings Manager / Le Chat allowlist check completed or explicitly deferred.

<details>
<summary>Detailed validation checks</summary>

| Requirement or risk | Test level | Path or command | Expected result |
| :--- | :--- | :--- | :--- |
| Config keys merge across layers; unknown peer alias warns | Unit | `tests/core/config/` | Warnings emitted; resolution returns None |
| Protocol models reject unknown fields / bad enums | Contract | `tests/app_server/` protocol tests | `extra="forbid"` violations raise typed errors |
| Mirror + coalescing: burst of entries yields one review | Unit | `tests/app_server/test_peer_review.py` with `FakeBackend` | One peer call per burst; mirror bounded |
| Budget: N+1-th review is skipped | Unit | same | `budget_exhausted` telemetry; panel note |
| Peer error silences duck for the turn, main turn unaffected | Unit / Integration | same + server dispatch test | `peer/review` failed; turn completes normally |
| Auto-steer injects only at/above threshold | Unit | same | Steer port called with marked message |
| Steer after turn end marked not delivered | Unit | same | `delivered=false`; no exception |
| Interrupt cancels in-flight peer call | Unit | same | Task cancelled; duck re-arms next turn |
| `/peer` override session-scoped | Integration | `tests/app_server/` | Override gone on new session |
| TUI panel + `/peer` end-to-end | End-to-end | `client-e2e` scenario + golden | Golden matches; commands round-trip |
| Telemetry events fire with approved properties, no critique text | Manual | `DEBUG_LEVEL=1 uv run vibe` | Log lines show events; PII scan of properties |

</details>

## 10. Alternatives

| Option | Advantages | Costs and risks | Decision crux |
| :--- | :--- | :--- | :--- |
| **A. App-server peer module (chosen)** | No harness changes; reuses config, backend factory, event stream, `turn/steer`; ships in this repo; testable with fakes | Duck sees completed entries, not streaming tokens; steering lands between model steps; event-stream latency | ADR 0018 prefers app-server code when existing Runtime capabilities express the feature — they do |
| B. Harness/Runtime-level peer | True in-loop peering; streaming visibility; could gate tool calls pre-execution | External dependency, version skew (ADR 0014), much larger blast radius; violates ADR 0018's default | Deferred; revisit only if pre-execution tool gating becomes a requirement |
| C. Subagent-based duck | Reuses subagent infra and per-agent models | Subagents are one-shot task runners, not continuous peers; lifecycle, cost accounting, and live cadence all fight the model | Wrong tool for a continuous observe-and-advise loop |

## 11. Rollout and Document Lifecycle

- **Release plan:** config + protocol + controller first (slices 1–4), TUI in
  the same release so the feature is usable; the feature is inert until a peer
  model is configured, so rollout is unconditional with no gating flag.
- **Compatibility removal:** none — additive only.
- **Monitoring plan:** telemetry events in section 7.8; watch
  `vibe.peer_review.error` rate and `review_completed` latency as the
  feature-specific signals.
- **Document lifecycle:** retain as the design record; if the peer loop later
  moves into the harness (Alternative B), this document is superseded by a new
  proposal and an ADR update.

---

## Appendix

### Source Map

- `vibe/core/config/models.py` — `UtilityFeature`, `UtilityModelsConfig` (extend with `PEER` / `peer`)
- `vibe/core/config/vibe_schema.py` — `utility_models` field, `get_utility_model` (extend)
- `vibe/core/llm/backend/factory.py` — `create_backend` (duck completions)
- `vibe/core/prompts/` — prompt markdown home for `peer_review.md`
- `vibe/core/telemetry/` — event emission per ADR 0008
- `vibe/app_server/_peer_review.py` — new controller module
- `vibe/app_server/protocol.py` — new RPCs + notification models
- `vibe/app_server/events.py` — event shapes the tap consumes
- `vibe/app_server/server.py` — event fan-out, dispatch, controller lifecycle
- `vibe/app_server/_session_model.py` — session override pattern to mirror
- `vibe/app_server/_runtime.py` — utility model derivation pattern
- `vibe/cli-rust/src/commands/commands.rs` — slash command registry (`/peer`)
- `vibe/cli-rust/src/ui/` — side panel widget
- `client-e2e/` — golden scenario
- `docs/adr/0002`, `0005`, `0008`, `0009`, `0014`, `0016`, `0018` — governing decisions
