from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
import ipaddress
import json
import os
from pathlib import Path
import shutil
from typing import Any, ClassVar

import pytest

import vibe.app_server._peer_tool_executor as executor_module
from vibe.app_server._peer_tool_executor import WorkspacePeerToolExecutor
from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.grep import GrepToolConfig
from vibe.core.tools.builtins.read_file import MAX_BYTES

requires_ripgrep = pytest.mark.skipif(
    shutil.which("rg") is None, reason="the duck's grep runs ripgrep"
)


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
async def test_read_file_caps_the_read_and_marks_truncation(workspace: Path) -> None:
    (workspace / "deep.txt").write_text(
        "\n".join(f"line {index}" for index in range(200_000))
    )
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute(
            "read_file", {"file_path": "deep.txt", "offset": 150_000, "limit": 5}
        )
    )

    assert result["content"] == ""
    assert result["numLines"] == 0
    assert result["wasTruncated"] is True


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
async def test_read_file_denies_symlinks_that_leave_the_workspace(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("SECRET-TOKEN-12345\n")
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="outside the session workspace"):
        await executor.execute("read_file", {"file_path": "link.txt"})


@pytest.mark.asyncio
async def test_read_file_reports_missing_files(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="Cannot read"):
        await executor.execute("read_file", {"file_path": "missing.py"})


@pytest.mark.asyncio
async def test_read_file_refuses_a_directory(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="not a regular file"):
        await executor.execute("read_file", {"file_path": "src"})


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
@pytest.mark.asyncio
async def test_read_file_refuses_a_fifo_instead_of_blocking_on_it(
    workspace: Path,
) -> None:
    os.mkfifo(workspace / "pipe")
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="not a regular file"):
        await asyncio.wait_for(
            executor.execute("read_file", {"file_path": "pipe"}), timeout=5
        )


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_finds_matches_and_respects_excludes(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))
    assert result["matchCount"] == 0  # node_modules is excluded

    (workspace / "src" / "other.py").write_text("needle here\n")
    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))
    assert result["matchCount"] == 1
    assert "src/other.py:1: needle here" in result["matches"]


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_skips_sensitive_files(workspace: Path) -> None:
    (workspace / ".env").write_text("needle=1\n")
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))

    assert result["matchCount"] == 0


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_refuses_a_sensitive_file_named_as_the_path(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="denied to peer review"):
        await executor.execute("grep", {"pattern": "SECRET", "path": ".env"})


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_caps_matches_and_marks_truncation(workspace: Path) -> None:
    (workspace / "many.txt").write_text(
        "\n".join(f"needle {index}" for index in range(200))
    )
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "needle"}))

    assert result["matchCount"] == 100
    assert result["wasTruncated"] is True


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_rejects_invalid_patterns(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="Invalid grep pattern"):
        await executor.execute("grep", {"pattern": "("})


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_denies_paths_outside_the_workspace(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(ToolError, match="outside the session workspace"):
        await executor.execute("grep", {"pattern": "x", "path": ".."})


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_does_not_follow_symlinks_out_of_the_workspace(
    workspace: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.txt").write_text("SECRET-TOKEN-12345\n")
    (outside / ".env").write_text("API_KEY=SECRET-ENV-67890\n")
    (workspace / "link.txt").symlink_to(outside / "secret.txt")
    (workspace / "envlink.txt").symlink_to(outside / ".env")
    (workspace / "dirlink").symlink_to(outside, target_is_directory=True)
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(await executor.execute("grep", {"pattern": "SECRET"}))

    assert result["matchCount"] == 0
    assert "SECRET-" not in result["matches"]


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_returns_promptly_on_a_catastrophic_pattern(workspace: Path) -> None:
    (workspace / "evil.txt").write_text("a" * 40 + "!\n")
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await asyncio.wait_for(
            executor.execute("grep", {"pattern": "(a+)+$", "path": "evil.txt"}),
            timeout=5,
        )
    )

    assert result["matchCount"] == 0


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_searches_a_single_file_path(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute("grep", {"pattern": "return", "path": "src/main.py"})
    )

    assert result["matchCount"] == 1
    assert "src/main.py:2:     return 42" in result["matches"]


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_reports_workspace_relative_paths_for_a_subdirectory(
    workspace: Path,
) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    result = json.loads(
        await executor.execute("grep", {"pattern": "return", "path": "src"})
    )

    assert result["matches"] == "src/main.py:2:     return 42"


@requires_ripgrep
@pytest.mark.asyncio
async def test_grep_honors_ignore_files_unless_told_otherwise(workspace: Path) -> None:
    (workspace / ".gitignore").write_text("ignored/\n")
    (workspace / ".vibeignore").write_text("vibe_ignored/\n")
    for name in ("ignored", "vibe_ignored", "kept"):
        (workspace / name).mkdir()
        (workspace / name / "x.txt").write_text("needle\n")
    executor = WorkspacePeerToolExecutor(workspace)

    default = json.loads(await executor.execute("grep", {"pattern": "needle"}))
    everything = json.loads(
        await executor.execute(
            "grep", {"pattern": "needle", "use_default_ignore": False}
        )
    )

    assert default["matches"].splitlines() == ["kept/x.txt:1: needle"]
    assert "ignored/x.txt:1: needle" in everything["matches"].splitlines()


class _FakeProcess:
    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()

    async def wait(self) -> int | None:
        return self.returncode


def _use_fake_ripgrep(monkeypatch: pytest.MonkeyPatch, process: _FakeProcess) -> None:
    async def fake_exec(*args: Any, **kwargs: Any) -> _FakeProcess:
        return process

    monkeypatch.setattr(executor_module, "_find_ripgrep", lambda: "rg")
    monkeypatch.setattr(executor_module.asyncio, "create_subprocess_exec", fake_exec)


@pytest.mark.asyncio
async def test_a_cancelled_grep_kills_the_ripgrep_process(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    _use_fake_ripgrep(monkeypatch, process)
    executor = WorkspacePeerToolExecutor(workspace)

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.2):
            await executor.execute("grep", {"pattern": "x"})

    assert process.killed is True


@pytest.mark.asyncio
async def test_a_grep_past_its_deadline_returns_what_it_found_and_kills_ripgrep(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    process = _FakeProcess()
    process.stdout.feed_data(b"src/main.py\x002:    return 42\n")
    _use_fake_ripgrep(monkeypatch, process)
    policy = executor_module.PeerToolPolicy(grep=GrepToolConfig(default_timeout=1))
    executor = WorkspacePeerToolExecutor(workspace, policy=policy)

    result = json.loads(await executor.execute("grep", {"pattern": "return"}))

    assert result["matchCount"] == 1
    assert result["wasTruncated"] is True
    assert process.killed is True


@pytest.mark.asyncio
async def test_grep_is_not_offered_without_ripgrep(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(executor_module, "_find_ripgrep", lambda: None)
    executor = WorkspacePeerToolExecutor(workspace)

    assert "grep" not in executor.supported_tools
    with pytest.raises(ToolError, match="not in the peer review allowlist"):
        await executor.execute("grep", {"pattern": "x"})


@pytest.mark.asyncio
async def test_non_allowlisted_tools_are_refused(workspace: Path) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    for name in ("write_file", "edit", "bash", "task", "web_search"):
        with pytest.raises(ToolError, match="not in the peer review allowlist"):
            await executor.execute(name, {})


@pytest.mark.asyncio
async def test_web_fetch_is_refused_unless_the_policy_allows_it(
    workspace: Path,
) -> None:
    executor = WorkspacePeerToolExecutor(workspace)

    assert "web_fetch" not in executor.supported_tools
    with pytest.raises(ToolError, match="not in the peer review allowlist"):
        await executor.execute("web_fetch", {"url": "https://example.com"})


def _web_executor(workspace: Path) -> WorkspacePeerToolExecutor:
    policy = executor_module.PeerToolPolicy(tools=frozenset({"web_fetch"}))
    return WorkspacePeerToolExecutor(workspace, policy=policy)


_HOSTS = {
    "example.com": "93.184.216.34",
    "docs.example.com": "93.184.216.35",
    "internal.example.com": "10.0.0.7",
}


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve(
        host: str, port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        return [ipaddress.ip_address(_HOSTS.get(host, host))]

    monkeypatch.setattr(executor_module, "_resolve_addresses", resolve)


class _FakeResponse:
    def __init__(
        self,
        chunks: AsyncIterator[bytes] | list[bytes],
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._chunks = chunks
        self.status_code = status_code
        self.headers = {"content-type": "text/plain", **(headers or {})}
        self.encoding = "utf-8"

    async def aiter_bytes(self) -> AsyncIterator[bytes]:
        if isinstance(self._chunks, list):
            for chunk in self._chunks:
                yield chunk
            return
        async for chunk in self._chunks:
            yield chunk


def _redirect(location: str, status_code: int = 302) -> _FakeResponse:
    return _FakeResponse([b""], status_code=status_code, headers={"location": location})


class _FakeStream:
    def __init__(self, response: _FakeResponse | Exception) -> None:
        self._response = response

    async def __aenter__(self) -> _FakeResponse:
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def __aexit__(self, *args: Any) -> None:
        return None


def _fake_http_client(*responses: _FakeResponse | Exception) -> Any:
    queue = list(responses)

    class _Client:
        requests: ClassVar[list[str]] = []
        kwargs: ClassVar[dict[str, Any]] = {}

        def __init__(self, **kwargs: Any) -> None:
            _Client.kwargs = kwargs

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *args: Any) -> None:
            return None

        def stream(self, method: str, url: str, **kwargs: Any) -> _FakeStream:
            _Client.requests.append(url)
            return _FakeStream(queue.pop(0))

    return _Client


@pytest.mark.asyncio
async def test_web_fetch_rejects_non_http_urls(workspace: Path) -> None:
    with pytest.raises(ToolError, match="http"):
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "file:///etc/passwd"}
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://[::ffff:7f00:1]/",
        "http://10.1.2.3/x",
        "http://172.16.0.9/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
    ],
)
@pytest.mark.asyncio
async def test_web_fetch_refuses_non_public_addresses(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    client = _fake_http_client()
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)

    with pytest.raises(ToolError):
        await _web_executor(workspace).execute("web_fetch", {"url": url})

    assert client.requests == []


@pytest.mark.asyncio
async def test_web_fetch_refuses_a_hostname_that_resolves_to_a_private_address(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    client = _fake_http_client()
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)

    with pytest.raises(ToolError, match="non-public"):
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://internal.example.com/admin"}
        )

    assert client.requests == []


@pytest.mark.asyncio
async def test_web_fetch_returns_text_and_strips_html(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    client = _fake_http_client(
        _FakeResponse(
            [b"<html><body><p>Hello &amp; bye</p></body></html>"],
            headers={"content-type": "text/html; charset=utf-8"},
        )
    )
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)

    result = json.loads(
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://example.com/page"}
        )
    )

    assert result["statusCode"] == 200
    assert "Hello & bye" in result["content"]
    assert "<p>" not in result["content"]
    assert result["wasTruncated"] is False
    assert result["url"] == "https://example.com/page"
    assert client.requests == ["https://example.com/page"]


@pytest.mark.asyncio
async def test_web_fetch_stops_reading_at_the_byte_cap(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    consumed = 0

    async def endless() -> AsyncIterator[bytes]:
        nonlocal consumed
        while True:
            consumed += 1
            yield b"y" * 65536

    monkeypatch.setattr(
        executor_module,
        "VibeAsyncHTTPClient",
        _fake_http_client(_FakeResponse(endless())),
    )

    result = json.loads(
        await asyncio.wait_for(
            _web_executor(workspace).execute(
                "web_fetch", {"url": "https://example.com"}
            ),
            timeout=5,
        )
    )

    assert result["wasTruncated"] is True
    assert len(result["content"].encode()) <= executor_module.WEB_FETCH_MAX_BYTES
    assert consumed * 65536 <= executor_module.WEB_FETCH_MAX_BYTES + 2 * 65536


@pytest.mark.asyncio
async def test_web_fetch_follows_public_redirects(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    client = _fake_http_client(
        _redirect("https://docs.example.com/page"), _FakeResponse([b"final"])
    )
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)

    result = json.loads(
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://example.com/start"}
        )
    )

    assert result["content"] == "final"
    assert result["url"] == "https://docs.example.com/page"
    assert client.requests == [
        "https://example.com/start",
        "https://docs.example.com/page",
    ]


@pytest.mark.parametrize(
    "location",
    ["http://169.254.169.254/latest/meta-data/", "https://internal.example.com/"],
)
@pytest.mark.asyncio
async def test_web_fetch_revalidates_every_redirect_hop(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None, location: str
) -> None:
    client = _fake_http_client(_redirect(location), _FakeResponse([b"secret"]))
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)

    with pytest.raises(ToolError, match="non-public"):
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://example.com/start"}
        )

    assert client.requests == ["https://example.com/start"]


@pytest.mark.asyncio
async def test_web_fetch_gives_up_after_too_many_redirects(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    hops = [_redirect("https://example.com/next") for _ in range(20)]
    monkeypatch.setattr(
        executor_module, "VibeAsyncHTTPClient", _fake_http_client(*hops)
    )

    with pytest.raises(ToolError, match="redirects"):
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://example.com/start"}
        )


@pytest.mark.asyncio
async def test_web_fetch_enforces_a_total_deadline(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    async def trickle() -> AsyncIterator[bytes]:
        while True:
            yield b"."
            await asyncio.sleep(0.05)

    monkeypatch.setattr(
        executor_module,
        "VibeAsyncHTTPClient",
        _fake_http_client(_FakeResponse(trickle())),
    )

    with pytest.raises(ToolError, match="timed out"):
        await asyncio.wait_for(
            _web_executor(workspace).execute(
                "web_fetch", {"url": "https://example.com", "timeout": 0.3}
            ),
            timeout=5,
        )


@pytest.mark.asyncio
async def test_web_fetch_uses_the_shared_ssl_context_and_follows_redirects_itself(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    trust = object()
    client = _fake_http_client(_FakeResponse([b"ok"]))
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)
    monkeypatch.setattr(executor_module, "build_ssl_context", lambda: trust)

    await _web_executor(workspace).execute("web_fetch", {"url": "https://example.com"})

    assert client.kwargs["verify"] is trust
    assert client.kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_web_fetch_wraps_network_errors(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None
) -> None:
    monkeypatch.setattr(
        executor_module,
        "VibeAsyncHTTPClient",
        _fake_http_client(OSError("connection refused")),
    )

    with pytest.raises(ToolError, match="web_fetch failed"):
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://example.com"}
        )


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), 0, -5])
@pytest.mark.asyncio
async def test_web_fetch_rejects_a_non_finite_or_non_positive_timeout(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, dns: None, timeout: float
) -> None:
    client = _fake_http_client(_FakeResponse([b"ok"]))
    monkeypatch.setattr(executor_module, "VibeAsyncHTTPClient", client)

    with pytest.raises(ToolError, match="timeout"):
        await _web_executor(workspace).execute(
            "web_fetch", {"url": "https://example.com", "timeout": timeout}
        )

    assert client.requests == []


@pytest.mark.asyncio
async def test_web_fetch_checks_the_ascii_host_the_client_will_connect_to(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolved: list[str] = []

    async def resolve(
        host: str, port: int
    ) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
        resolved.append(host)
        return [ipaddress.ip_address("93.184.216.34")]

    monkeypatch.setattr(executor_module, "_resolve_addresses", resolve)
    monkeypatch.setattr(
        executor_module,
        "VibeAsyncHTTPClient",
        _fake_http_client(_FakeResponse([b"ok"])),
    )

    await _web_executor(workspace).execute(
        "web_fetch", {"url": "https://fa\u00df.example/"}
    )

    assert resolved == ["xn--fa-hia.example"]
