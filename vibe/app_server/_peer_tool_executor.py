from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
import fnmatch
import html
from itertools import islice
import json
import os
from pathlib import Path
import re
from typing import Any, Protocol
from urllib.parse import SplitResult, urlsplit

import anyio

from vibe.core.tools.base import ToolError
from vibe.core.tools.builtins.grep import GrepToolConfig
from vibe.core.tools.builtins.read_file import DEFAULT_LINE_LIMIT, MAX_BYTES
from vibe.core.tools.utils import DEFAULT_SENSITIVE_PATTERNS, matches_sensitive_pattern
from vibe.utils.http import VibeAsyncHTTPClient, get_user_agent
from vibe.utils.io import decode_safe, split_lines_verbatim

READ_FILE_TOOL = "read_file"
GREP_TOOL = "grep"
WEB_FETCH_TOOL = "web_fetch"

# The duck's read-only allowlist. ``web_search`` is deliberately absent: the
# runtime executes it in Rust and there is no Python client to reuse, so the
# catalog never advertises it (docs/design/rubber-duck-peer-review.md §5).
_FILE_TOOLS = frozenset({READ_FILE_TOOL, GREP_TOOL})

GREP_MAX_OUTPUT_BYTES = 64_000
GREP_MAX_FILE_BYTES = 1_000_000
# read_file bounds the bytes pulled into memory the same way (the main tool's
# max_read_bytes); the MAX_BYTES output cap still applies on top.
READ_FILE_MAX_BYTES = 1_000_000
WEB_FETCH_MAX_BYTES = 120_000
WEB_FETCH_DEFAULT_TIMEOUT = 30.0
WEB_FETCH_MAX_TIMEOUT = 120.0

# Paths per offloaded walk batch: bounds memory while keeping the walk lazy.
_WALK_BATCH = 256

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


class PeerReviewToolExecutor(Protocol):
    """The duck's read-only tool surface against the session workspace.

    The executor is the security boundary of peer review: it only ever runs
    the fixed read-only allowlist, confined to the session workspace, and
    refuses anything else. The controller advertises at most
    ``supported_tools`` to the duck model.
    """

    @property
    def supported_tools(self) -> frozenset[str]:
        """Tool names this executor will run; the catalog never exceeds it."""
        ...

    def set_allow_web_tools(self, allow: bool) -> None:
        """Re-apply the ``peer_review.allow_web_tools`` policy switch."""
        ...

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one read-only tool call and return its JSON result text.

        Raises ``ToolError`` when the call is refused; the controller feeds the
        error text back to the duck so the review continues.
        """
        ...


class WorkspacePeerToolExecutor:
    """Runs the duck's allowlist against the session workspace.

    The duck is non-interactive, so there is no approval path: anything the
    main model's tools would ask for is denied outright — reads that would
    trip ``read_file``'s ``sensitive_patterns`` gate included (the flagged
    ADR 0004 deviation, docs/design/rubber-duck-peer-review.md §7.8).
    """

    def __init__(self, cwd: Path, *, allow_web_tools: bool = True) -> None:
        self._cwd = cwd.expanduser().resolve()
        self._allow_web_tools = allow_web_tools
        self._sensitive_patterns = list(DEFAULT_SENSITIVE_PATTERNS)
        self._grep_config = GrepToolConfig()

    @property
    def supported_tools(self) -> frozenset[str]:
        tools = set(_FILE_TOOLS)
        if self._allow_web_tools:
            tools.add(WEB_FETCH_TOOL)
        return frozenset(tools)

    def set_allow_web_tools(self, allow: bool) -> None:
        self._allow_web_tools = allow

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self.supported_tools:
            raise ToolError(f"Tool '{name}' is not in the peer review allowlist")
        match name:
            case "read_file":
                return await self._read_file(arguments)
            case "grep":
                return await self._grep(arguments)
            case "web_fetch":
                return await self._web_fetch(arguments)
        raise ToolError(f"Tool '{name}' is not in the peer review allowlist")

    async def _read_file(self, arguments: dict[str, Any]) -> str:
        raw_path = arguments.get("file_path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ToolError("read_file requires a file_path")
        resolved = self._resolve_workspace_path(raw_path)
        self._deny_sensitive(resolved)
        offset = arguments.get("offset")
        if offset is not None and (not isinstance(offset, int) or offset < 1):
            raise ToolError("read_file offset must be a positive integer")
        limit = arguments.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ToolError("read_file limit must be a positive integer")
        try:
            raw, read_truncated = await _read_bytes_capped(
                resolved, READ_FILE_MAX_BYTES
            )
        except OSError as exc:
            raise ToolError(f"Cannot read '{raw_path}': {exc}") from exc
        lines = split_lines_verbatim(decode_safe(raw).text)
        start_line = offset or 1
        selected = lines[
            start_line - 1 : start_line - 1 + (limit or DEFAULT_LINE_LIMIT)
        ]
        content = "\n".join(selected)
        past_window = len(lines) > start_line - 1 + len(selected)
        was_truncated = read_truncated or past_window
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > MAX_BYTES:
            content = encoded[:MAX_BYTES].decode("utf-8", errors="ignore")
            was_truncated = True
        return json.dumps({
            "filePath": str(resolved),
            "content": content,
            "numLines": len(selected),
            "startLine": start_line,
            "wasTruncated": was_truncated,
        })

    async def _grep(self, arguments: dict[str, Any]) -> str:
        pattern = arguments.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("grep requires a pattern")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ToolError(f"Invalid grep pattern: {exc}") from exc
        raw_path = arguments.get("path")
        search_root = (
            self._resolve_workspace_path(raw_path)
            if isinstance(raw_path, str) and raw_path
            else self._cwd
        )
        max_matches = arguments.get("max_matches")
        if max_matches is not None and (
            not isinstance(max_matches, int) or max_matches < 1
        ):
            raise ToolError("grep max_matches must be a positive integer")
        match_cap = max_matches or self._grep_config.default_max_matches

        matches: list[str] = []
        output_bytes = 0
        was_truncated = False
        async for path in self._walk_async(search_root):
            if matches_sensitive_pattern(str(path), self._sensitive_patterns):
                continue
            try:
                raw, _ = await _read_bytes_capped(path, GREP_MAX_FILE_BYTES)
            except OSError:
                continue
            text = decode_safe(raw).text
            if "\x00" in text[:4096]:
                continue
            for line_number, line in enumerate(split_lines_verbatim(text), start=1):
                if len(matches) >= match_cap or output_bytes >= GREP_MAX_OUTPUT_BYTES:
                    was_truncated = True
                    break
                if regex.search(line) is None:
                    continue
                relative = path.relative_to(search_root).as_posix()
                entry = f"{relative}:{line_number}: {line}"
                matches.append(entry)
                output_bytes += len(entry.encode("utf-8", errors="replace")) + 1
            if was_truncated:
                break
        return json.dumps({
            "matches": "\n".join(matches),
            "matchCount": len(matches),
            "wasTruncated": was_truncated,
        })

    async def _web_fetch(self, arguments: dict[str, Any]) -> str:
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ToolError("web_fetch requires a url")
        _parse_http_url(url)
        timeout = arguments.get("timeout")
        if timeout is not None and not isinstance(timeout, int | float):
            raise ToolError("web_fetch timeout must be a number of seconds")
        timeout_seconds = min(
            float(timeout) if timeout is not None else WEB_FETCH_DEFAULT_TIMEOUT,
            WEB_FETCH_MAX_TIMEOUT,
        )
        try:
            async with VibeAsyncHTTPClient(
                timeout=timeout_seconds, follow_redirects=True
            ) as client:
                response = await client.get(
                    url, headers={"user-agent": get_user_agent(None)}
                )
                raw = await response.aread()
        except Exception as exc:
            # httpx errors are not OSError subclasses; wrap anything the fetch
            # raises so the controller can feed it back to the duck.
            raise ToolError(f"web_fetch failed for '{url}': {exc}") from exc
        was_truncated = len(raw) > WEB_FETCH_MAX_BYTES
        if was_truncated:
            raw = raw[:WEB_FETCH_MAX_BYTES]
        text = raw.decode(response.encoding or "utf-8", errors="replace")
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            text = _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", text)).strip()
            text = html.unescape(text)
        return json.dumps({
            "url": str(response.url),
            "statusCode": response.status_code,
            "content": text,
            "wasTruncated": was_truncated,
        })

    def _resolve_workspace_path(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self._cwd / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self._cwd):
            raise ToolError(f"Path '{raw}' is outside the session workspace")
        return resolved

    def _deny_sensitive(self, resolved: Path) -> None:
        if matches_sensitive_pattern(str(resolved), self._sensitive_patterns):
            raise ToolError(f"Path '{resolved}' is denied to peer review")

    def _walk(self, root: Path) -> Iterator[Path]:
        """Yield files under ``root``, pruning excluded directories."""
        for current, dirnames, filenames in os.walk(root):
            dirnames.sort()
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if not _matches_exclude(
                    Path(current) / dirname,
                    root,
                    self._grep_config.exclude_patterns,
                    is_dir=True,
                )
            ]
            for filename in sorted(filenames):
                path = Path(current) / filename
                if _matches_exclude(
                    path, root, self._grep_config.exclude_patterns, is_dir=False
                ):
                    continue
                yield path

    async def _walk_async(self, root: Path) -> AsyncIterator[Path]:
        """``_walk`` off the event loop: each batch is collected in a worker
        thread (the vibe-async rule), and the walk stays lazy so the output
        caps still stop the search early.
        """
        walk = self._walk(root)
        while True:
            batch = await asyncio.to_thread(lambda: list(islice(walk, _WALK_BATCH)))
            if not batch:
                return
            for path in batch:
                yield path


def _parse_http_url(url: str) -> SplitResult:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError(f"web_fetch only supports http(s) URLs: '{url}'")
    return parsed


async def _read_bytes_capped(path: Path, max_bytes: int) -> tuple[bytes, bool]:
    """Stream at most ``max_bytes`` from ``path``; the flag reports more bytes.

    Chunked reads keep a huge file from spiking memory on the event loop.
    """
    raw = bytearray()
    async with await anyio.Path(path).open("rb") as handle:
        while len(raw) <= max_bytes:
            chunk = await handle.read(65536)
            if not chunk:
                break
            raw.extend(chunk)
    return bytes(raw[:max_bytes]), len(raw) > max_bytes


def _matches_exclude(
    path: Path, root: Path, patterns: list[str], *, is_dir: bool
) -> bool:
    """Match a path against grep's exclude globs, component-wise.

    Patterns ending in ``/`` are directory-only; the rest match any path
    component or the whole relative path, mirroring ``rg --glob`` closely
    enough for a bounded background search.
    """
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    parts = relative.parts
    for pattern in patterns:
        if pattern.endswith("/"):
            if not is_dir:
                continue
            candidate = pattern.rstrip("/")
        else:
            candidate = pattern
        if fnmatch.fnmatch(relative.as_posix(), pattern):
            return True
        if any(fnmatch.fnmatch(part, candidate) for part in parts):
            return True
    return False
