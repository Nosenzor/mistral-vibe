"""The peer panel docks on Ctrl+P, streams critiques during a turn, steers one, and /peer reads and configures."""

from __future__ import annotations

from e2e.app_server.events import peer_review, turn_started, user_msg
from e2e.app_server.scenario import Timeline

# The turn stays running, so settles must accept busy frames.
env = {"VIBE_REPLAY_SETTLE_BUSY": "1"}

# The replay server answers the peer RPCs from this handshake.
handshake = {
    "peer/read": {
        "enabled": True,
        "model": "glm-5.3",
        "autoSteer": False,
        "maxReviewsPerTurn": 10,
        "autoSteerSeverity": "critical",
    },
    "peer/configure": {
        "enabled": True,
        "model": "glm-5.3",
        "autoSteer": False,
        "maxReviewsPerTurn": 10,
        "autoSteerSeverity": "critical",
    },
    "peer/steer": {"accepted": True, "delivered": True},
}

# The goldens pin the peer round-trips alongside the compared request set.
request_methods = frozenset({"peer/read", "peer/configure", "peer/steer"})

screen_contains = ("Peer · glm-5.3", "warning · delivered", "Peer review is on")

timeline: Timeline = [
    # Ctrl+P docks the panel focused and seeds peer/read for the header.
    "\x10",
    # Esc unfocuses so the composer can take the turn.
    "\x1b",
    # The prompt enqueues the turn; the released batch carries the duck's
    # critiques. The harness drains the queue right after turn/started, so the
    # first four events land before the frame below.
    "fix the parser\r",
    turn_started(),
    user_msg("fix the parser"),
    peer_review("r1", status="running"),
    peer_review(
        "r1",
        status="complete",
        severity="critical",
        steerable=True,
        text="The plan says add a test first; this change skips it.",
    ),
    peer_review(
        "r2",
        status="complete",
        severity="warning",
        delivered=True,
        text="Consider naming the helper parse_args.",
    ),
    {"release_after": 4},
    # The two verdicts land: the critical is open, the warning already delivered.
    {"release": 2},
    # Ctrl+P focuses the docked panel; Up selects the open critical; Enter
    # steers it into the running turn.
    "\x10",
    "\x1b[A",
    "\r",
    # d dismisses the steered critique; Ctrl+P undocks the panel.
    "d",
    "\x10",
    # /peer <model> configures the duck: the echo, the result line, the panel.
    "/peer glm-5.3",
    "\r",
    # /peer reads the state back: the echo and the result line.
    "/peer",
    "\r",
]
