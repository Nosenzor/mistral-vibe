"""The Unified session store must load documents written by newer builds.

``_StoredModel`` documents the rule: unknown fields are kept, not rejected,
because one store is read and rewritten by whichever build a machine runs.
Nested shapes with strict configs (e.g. the subagent records) must not
break that on load.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from mistralai_vibe_local_harness.vibe._storage import (
    PluginLockV1,
    ProjectionStateV1,
    _load_runtime_state,
    empty_runtime_state,
)
from mistralai_vibe_local_harness.vibe._subagents._models import (
    ChildSessionRecord,
    ReservedChild,
    ResolvedSubagentPolicyCeiling,
    SubagentRuntimeState,
)


def _runtime_state_value_with_unknown_child_field() -> dict[str, Any]:
    subagents = SubagentRuntimeState(
        policy_ceiling=ResolvedSubagentPolicyCeiling(
            workdir="/tmp",
            read_roots=[],
            write_roots=[],
            completion_grants={},
            tool_grants={},
            skill_grants={},
            mcp_grants={},
            allowed_connector_ids=[],
            connector_policy_digest=None,
            allowed_connector_authentication=[],
            allowed_connector_execution_identities=[],
            hook_grants={},
            sandbox_binding_id="sandbox",
            sandbox_authority_digest="digest",
            network_access=False,
            allowed_environment_names=[],
            approval_bypass=False,
            max_turn_iterations=None,
            max_output_tokens=None,
        ),
        children={
            "critical-review-mars-p1": ChildSessionRecord(
                admission_sequence=0,
                agent_name="critical-review-mars-p1",
                agent_type=None,
                child_session_id="child-a446892353850e6505222a7d",
                spawn_action_id="action-1",
                spawn_request_digest="digest",
                template_digest="digest",
                policy_ceiling_digest="digest",
                depth=1,
                state=ReservedChild(),
                last_notified_generation=0,
            )
        },
    )
    state = empty_runtime_state(
        session_id="session-1",
        snapshot_sequence=0,
        plugin_lock=PluginLockV1(plugins=[]),
        subagents=subagents,
    )
    value = cast(dict[str, Any], state.model_dump(mode="json"))
    # A newer build recorded fields this build does not name (in 3.1.0
    # ``instructions`` became a declared field; a future build may add more).
    child = value["subagents"]["children"]["critical-review-mars-p1"]
    child["instructions"] = None
    child["some_future_field"] = "x"
    return value


def test_load_runtime_state_tolerates_fields_from_newer_builds() -> None:
    value = _runtime_state_value_with_unknown_child_field()
    loaded = _load_runtime_state(value, cast(ProjectionStateV1, None))
    assert loaded.subagents is not None
    assert "critical-review-mars-p1" in loaded.subagents.children


def test_load_runtime_state_keeps_unknown_fields_on_the_loaded_instance() -> None:
    value = _runtime_state_value_with_unknown_child_field()
    loaded = _load_runtime_state(value, cast(ProjectionStateV1, None))
    assert loaded.subagents is not None
    child = loaded.subagents.children["critical-review-mars-p1"]
    # Declared fields load normally; unknown ones are kept on the instance
    # (a model_copy advance carries them), per the _StoredModel rule.
    assert child.instructions is None
    assert child.__pydantic_extra__ == {"some_future_field": "x"}
    # Document-level unknown fields round-trip through model_dump.
    value["unknown_top_level_field"] = "kept"
    loaded = _load_runtime_state(value, cast(ProjectionStateV1, None))
    dumped = cast(dict[str, Any], loaded.model_dump(mode="json"))
    assert dumped["unknown_top_level_field"] == "kept"


def test_load_runtime_state_still_rejects_other_state_versions() -> None:
    value = _runtime_state_value_with_unknown_child_field()
    value["runtime_state_version"] = 99
    with pytest.raises(ValueError, match="unsupported Runtime state version"):
        _load_runtime_state(value, cast(ProjectionStateV1, None))
