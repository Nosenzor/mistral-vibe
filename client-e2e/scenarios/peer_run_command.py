"""`/peer run` and `/peer agent` start duck tasks; the acks land in the chat."""

from __future__ import annotations

from e2e.app_server.scenario import Timeline

handshake = {
    "peer/read": {
        "enabled": True,
        "model": "glm-5.3",
        "autoSteer": False,
        "maxReviewsPerTurn": 10,
        "autoSteerSeverity": "critical",
        "runs": [],
    },
    "peer/configure": {
        "enabled": True,
        "model": "glm-5.3",
        "autoSteer": False,
        "maxReviewsPerTurn": 10,
        "autoSteerSeverity": "critical",
        "runs": [],
    },
    "peer/run": {"runId": "run-1", "status": "running"},
    "peer/agent": {"runId": "run-2", "status": "running"},
    "peer/stop": {"accepted": True},
}

request_methods = frozenset(
    {"peer/read", "peer/configure", "peer/run", "peer/agent", "peer/stop"}
)

screen_contains = ("Peer run started as run-1", "Peer agent started as run-2")

timeline: Timeline = [
    "/peer glm-5.3\r",
    "/peer run review the parser change\r",
    "/peer agent research the mesh sweep\r",
    "/peer stop run-1\r",
]
