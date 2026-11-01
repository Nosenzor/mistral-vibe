from __future__ import annotations

import json
from pathlib import Path
import shutil
from typing import Any

import pytest

from tests.conftest import build_test_vibe_config
from vibe.app_server._peer_tool_executor import (
    PeerToolPolicy,
    WorkspacePeerToolExecutor,
)
from vibe.core.tools.base import ToolError

requires_ripgrep = pytest.mark.skipif(
    shutil.which("rg") is None, reason="the duck's grep runs ripgrep"
)


def _policy(**overrides: Any) -> PeerToolPolicy:
    return PeerToolPolicy.from_config(build_test_vibe_config(**overrides))


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, {"read_file", "grep"}),
        (
            {"peer_review": {"allow_web_tools": True}},
            {"read_file", "grep", "web_fetch"},
        ),
        (
            {"peer_review": {"allow_web_tools": True}, "disabled_tools": ["web_fetch"]},
            {"read_file", "grep"},
        ),
        (
            {
                "peer_review": {"allow_web_tools": True},
                "tools": {"web_fetch": {"permission": "never"}},
            },
            {"read_file", "grep"},
        ),
        (
            {"peer_review": {"allow_web_tools": True}, "enabled_tools": ["read_file"]},
            {"read_file"},
        ),
        ({"disabled_tools": ["gre*"]}, {"read_file"}),
        ({"tools": {"read_file": {"permission": "ask"}}}, {"grep"}),
        ({"tools": {"grep": {"permission": "never"}}}, {"read_file"}),
    ],
)
def test_the_duck_only_gets_tools_the_effective_config_allows(
    overrides: dict[str, Any], expected: set[str]
) -> None:
    assert _policy(**overrides).tools == frozenset(expected)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "secrets").mkdir()
    (tmp_path / "secrets" / "key.txt").write_text("needle in a secret\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "plan.txt").write_text("needle in a private file\n")
    (tmp_path / "generated").mkdir()
    (tmp_path / "generated" / "out.txt").write_text("needle in generated output\n")
    (tmp_path / "ok.txt").write_text("needle in an ordinary file\n")
    return tmp_path


def _executor(workspace: Path, **overrides: Any) -> WorkspacePeerToolExecutor:
    executor = WorkspacePeerToolExecutor(workspace)
    executor.set_policy(_policy(**overrides))
    return executor


@pytest.mark.asyncio
async def test_read_file_honors_configured_sensitive_patterns(workspace: Path) -> None:
    executor = _executor(
        workspace, tools={"read_file": {"sensitive_patterns": ["**/secrets/*"]}}
    )

    with pytest.raises(ToolError, match="denied to peer review"):
        await executor.execute("read_file", {"file_path": "secrets/key.txt"})
    result = json.loads(await executor.execute("read_file", {"file_path": "ok.txt"}))
    assert "ordinary" in result["content"]


@pytest.mark.asyncio
async def test_read_file_honors_a_configured_denylist(workspace: Path) -> None:
    executor = _executor(
        workspace, tools={"read_file": {"denylist": ["*/private/plan.txt"]}}
    )

    with pytest.raises(ToolError, match="denied to peer review"):
        await executor.execute("read_file", {"file_path": "private/plan.txt"})


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_honors_configured_sensitive_patterns_denylist_and_excludes(
    workspace: Path,
) -> None:
    executor = _executor(
        workspace,
        tools={
            "grep": {
                "sensitive_patterns": ["**/secrets/*"],
                "denylist": ["*/private/plan.txt"],
                "exclude_patterns": ["generated/"],
            }
        },
    )

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))

    assert result["matches"].splitlines() == ["ok.txt:1: needle in an ordinary file"]


def test_the_executor_offers_only_the_tools_its_policy_allows(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)
    assert "web_fetch" not in executor.supported_tools

    executor.set_policy(_policy(peer_review={"allow_web_tools": True}))
    assert "web_fetch" in executor.supported_tools

    executor.set_policy(_policy(disabled_tools=["read_file"]))
    assert "read_file" not in executor.supported_tools


@pytest.mark.asyncio
async def test_read_file_honors_a_denylisted_directory(workspace: Path) -> None:
    executor = _executor(workspace, tools={"read_file": {"denylist": ["*/private"]}})

    with pytest.raises(ToolError, match="denied to peer review"):
        await executor.execute("read_file", {"file_path": "private/plan.txt"})


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_honors_a_denylisted_directory(workspace: Path) -> None:
    executor = _executor(workspace, tools={"grep": {"denylist": ["*/private"]}})

    with pytest.raises(ToolError, match="denied to peer review"):
        await executor.execute("grep", {"pattern": "needle", "path": "private"})
    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))
    assert "private/" not in result["matches"]
    assert "ok.txt:1: needle in an ordinary file" in result["matches"]
