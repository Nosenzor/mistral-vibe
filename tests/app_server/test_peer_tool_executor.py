from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest

import vibe.app_server._peer_tool_executor as executor_module
from vibe.app_server._peer_tool_executor import WorkspacePeerToolExecutor
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.read_file import MAX_BYTES


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("def main():\n    return 42\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("needle in a dep\n")
    return tmp_path


@pytest.mark.asyncio
async def test_read_file_reads_within_the_workspace(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute("read_file", {"file_path": "src/main.py"})
    )

    assert "return 42" in result["content"]
    assert result["startLine"] == 1
    assert result["numLines"] == 2
    assert result["wasTruncated"] is False


@pytest.mark.asyncio
async def test_read_file_applies_offset_and_limit(workspace: Path) -> None:
    (workspace / "big.txt").write_text(
        "\n".join(f"line {index}" for index in range(1, 11))
    )
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute(
            "read_file", {"file_path": "big.txt", "offset": 3, "limit": 2}
        )
    )

    assert result["content"] == "line 3\nline 4"
    assert result["startLine"] == 3
    assert result["numLines"] == 2


@pytest.mark.asyncio
async def test_read_file_caps_output_bytes(workspace: Path) -> None:
    (workspace / "huge.txt").write_text("x" * 200_000)
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("read_file", {"file_path": "huge.txt"}))

    assert result["wasTruncated"] is True
    assert len(result["content"].encode()) <= MAX_BYTES


@pytest.mark.asyncio
async def test_read_file_denies_paths_outside_the_workspace(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="outside the session workspace"):
        await executor.execute("read_file", {"file_path": "../outside.txt"})


@pytest.mark.asyncio
async def test_read_file_denies_sensitive_patterns(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="denied to peer review"):
        await executor.execute("read_file", {"file_path": ".env"})


@pytest.mark.asyncio
async def test_read_file_reports_missing_files(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="Cannot read"):
        await executor.execute("read_file", {"file_path": "missing.py"})


@pytest.mark.asyncio
async def test_grep_finds_matches_and_respects_excludes(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))
    assert result["matchCount"] == 0  # node_modules is excluded

    (workspace / "src" / "other.py").write_text("needle here\n")
    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))
    assert result["matchCount"] == 1
    assert "src/other.py:1: needle here" in result["matches"]


@pytest.mark.asyncio
async def test_grep_skips_sensitive_files(workspace: Path) -> None:
    (workspace / ".env").write_text("needle=1\n")
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))

    assert result["matchCount"] == 0


@pytest.mark.asyncio
async def test_grep_caps_matches_and_marks_truncation(workspace: Path) -> None:
    (workspace / "many.txt").write_text(
        "\n".join(f"needle {index}" for index in range(200))
    )
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))

    assert result["matchCount"] == 100
    assert result["wasTruncated"] is True


@pytest.mark.asyncio
async def test_grep_rejects_invalid_patterns(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="Invalid grep pattern"):
        await executor.execute("grep", {"pattern": "("})


@pytest.mark.asyncio
async def test_grep_denies_paths_outside_the_workspace(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="outside the session workspace"):
        await executor.execute("grep", {"pattern": "x", "path": ".."})


@pytest.mark.asyncio
async def test_non_allowlisted_tools_are_refused(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    for name in ("write_file", "edit", "bash", "task", "web_search"):
        with pytest.raises(ToolError, match="not in the peer review allowlist"):
            await executor.execute(name, {})


@pytest.mark.asyncio
async def test_web_fetch_follows_the_allow_web_tools_switch(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace, allow_web_tools=False)
    assert "web_fetch" not in executor.supported_tools
    with pytest.raises(ToolError, match="not in the peer review allowlist"):
        await executor.execute("web_fetch", {"url": "https://example.com"})

    executor.set_allow_web_tools(True)
    assert "web_fetch" in executor.supported_tools


@pytest.mark.asyncio
async def test_web_fetch_rejects_non_http_urls(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="http"):
        await executor.execute("web_fetch", {"url": "file:///etc/passwd"})


class _FakeResponse:
    def __init__(
        self,
        raw: bytes,
        *,
        status_code: int = 200,
        content_type: str = "text/plain",
        url: str = "https://example.com/page",
    ) -> None:
        self._raw = raw
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.url = url
        self.encoding = "utf-8"

    async def aread(self) -> bytes:
        return self._raw


def _fake_http_client(response: _FakeResponse) -> Any:
    class _Client:
        requests: ClassVar[list[str]] = []

        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            _Client.requests.append(url)
            return response

    return _Client


@pytest.mark.asyncio
async def test_web_fetch_returns_text_and_strips_html(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _fake_http_client(
        _FakeResponse(
            b"<html><body><p>Hello &amp; bye</p></body></html>",
            content_type="text/html; charset=utf-8",
        )
    )
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute("web_fetch", {"url": "https://example.com/page"})
    )

    assert result["statusCode"] == 200
    assert "Hello & bye" in result["content"]
    assert "<p>" not in result["content"]
    assert result["wasTruncated"] is False
    assert client.requests == ["https://example.com/page"]


@pytest.mark.asyncio
async def test_web_fetch_caps_response_bytes(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        executor_module,
        "VibeAsyncHTTPClient",
        _fake_http_client(_FakeResponse(b"y" * 200_000)),
    )
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute("web_fetch", {"url": "https://example.com"})
    )

    assert result["wasTruncated"] is True
    assert len(result["content"].encode()) <= executor_module.WEB_FETCH_MAX_BYTES


@pytest.mark.asyncio
async def test_web_fetch_wraps_network_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailingClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> _FailingClient:
            return self

        async def __aexit__(self, *args: Any) -> None:
            pass

        async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
            raise OSError("connection refused")

    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", _FailingClient)
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="web_fetch failed"):
        await executor.execute("web_fetch", {"url": "https://example.com"})
