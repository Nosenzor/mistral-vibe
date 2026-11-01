from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
import html
import ipaddress
import json
import math
from pathlib import Path
import re
import shutil
import socket
import stat
from typing import Any, Protocol
from urllib.parse import SplitResult, urljoin, urlsplit

import anyio
import httpx

from vibe.core.config import VibeConfigSchema
from vibe.core.tools.base import BaseToolConfig, ToolError, ToolPermission
from vibe.core.tools.builtins.grep import GrepToolConfig
from vibe.core.tools.builtins.read_file import (
    DEFAULT_LINE_LIMIT,
    MAX_BYTES,
    ReadFileConfig,
)
from vibe.core.tools.builtins.web_fetch import WebFetchConfig
from vibe.core.tools.utils import matches_sensitive_pattern, resolve_path_permission
from vibe.core.utils import name_matches
from vibe.utils.http import VibeAsyncHTTPClient, build_ssl_context, get_user_agent
from vibe.utils.io import decode_safe, split_lines_verbatim

READ_FILE_TOOL = "read_file"
GREP_TOOL = "grep"
WEB_FETCH_TOOL = "web_fetch"

# The duck's read-only allowlist. ``web_search`` is deliberately absent: the
# runtime executes it in Rust and there is no Python client to reuse, so the
# catalog never advertises it (docs/design/rubber-duck-peer-review.md §5).
DEFAULT_TOOLS = frozenset({READ_FILE_TOOL, GREP_TOOL})

GREP_MAX_FILE_BYTES = 1_000_000
# read_file bounds the bytes pulled into memory the same way (the main tool's
# max_read_bytes); the MAX_BYTES output cap still applies on top.
READ_FILE_MAX_BYTES = 1_000_000
WEB_FETCH_MAX_BYTES = 120_000
WEB_FETCH_DEFAULT_TIMEOUT = 30.0
WEB_FETCH_MAX_TIMEOUT = 120.0
WEB_FETCH_MAX_REDIRECTS = 5

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_RIPGREP_STREAM_LIMIT = 1 << 20
_RIPGREP_ERROR_EXIT_CODE = 2
_RIPGREP_ARGS = (
    "--no-config",
    "--color=never",
    "--no-heading",
    "--with-filename",
    "--line-number",
    "--null",
    "--hidden",
    "--no-require-git",
    "--max-columns=500",
    "--max-columns-preview",
)

_HTML_TAG = re.compile(r"<[^>]+>")
_WHITESPACE = re.compile(r"\s+")


def _tool_config[ConfigT: BaseToolConfig](
    config: VibeConfigSchema, name: str, config_class: type[ConfigT]
) -> ConfigT:
    merged = {**config_class().model_dump(), **config.tools.get(name, {})}
    return config_class.model_validate(merged)


@dataclass(frozen=True, slots=True)
class PeerToolPolicy:
    """What the duck may run, derived from the session's effective tool config.

    The duck is non-interactive, so a tool that would prompt is not offered:
    only an explicit ``allow_web_tools`` opt-in stands in for ``web_fetch``'s
    prompt, and a ``never`` permission or the disabled/enabled tool lists
    still win over it.
    """

    tools: frozenset[str] = DEFAULT_TOOLS
    read_file: ReadFileConfig = field(default_factory=ReadFileConfig)
    grep: GrepToolConfig = field(default_factory=GrepToolConfig)

    @classmethod
    def from_config(cls, config: VibeConfigSchema) -> PeerToolPolicy:
        read_file = _tool_config(config, READ_FILE_TOOL, ReadFileConfig)
        grep = _tool_config(config, GREP_TOOL, GrepToolConfig)
        web_fetch = _tool_config(config, WEB_FETCH_TOOL, WebFetchConfig)
        offered = {
            READ_FILE_TOOL: read_file.permission is ToolPermission.ALWAYS,
            GREP_TOOL: grep.permission is ToolPermission.ALWAYS,
            WEB_FETCH_TOOL: config.peer_review.allow_web_tools
            and web_fetch.permission is not ToolPermission.NEVER,
        }
        names = {
            name
            for name, allowed in offered.items()
            if allowed
            and (not config.enabled_tools or name_matches(name, config.enabled_tools))
            and not name_matches(name, config.disabled_tools)
        }
        return cls(tools=frozenset(names), read_file=read_file, grep=grep)


class PeerReviewToolExecutor(Protocol):
    """The duck's read-only tool surface against the session workspace.

    The executor is the security boundary of peer review: it only ever runs
    the tools its policy allows, confined to the session workspace, and
    refuses anything else. The controller advertises at most
    ``supported_tools`` to the duck model.
    """

    @property
    def supported_tools(self) -> frozenset[str]:
        """Tool names this executor will run; the catalog never exceeds it."""
        ...

    def set_policy(self, policy: PeerToolPolicy) -> None:
        """Re-apply the policy derived from the current effective config."""
        ...

    async def execute(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one read-only tool call and return its JSON result text.

        Raises ``ToolError`` when the call is refused; the controller feeds the
        error text back to the duck so the review continues.
        """
        ...


def _find_ripgrep() -> str | None:
    return shutil.which("rg")


class WorkspacePeerToolExecutor:
    """Runs the duck's allowlist against the session workspace.

    The duck is non-interactive, so there is no approval path: anything the
    main model's tools would ask for is denied outright, reads that would trip
    the configured ``sensitive_patterns`` included (the flagged ADR 0004
    deviation, docs/design/rubber-duck-peer-review.md §7.8). ``grep`` runs
    ripgrep so a duck-chosen pattern cannot stall the app server's event loop.
    """

    def __init__(self, cwd: Path, *, policy: PeerToolPolicy | None = None) -> None:
        self._cwd = cwd.expanduser().resolve()
        self._policy = policy or PeerToolPolicy()

    @property
    def supported_tools(self) -> frozenset[str]:
        if _find_ripgrep() is None:
            return self._policy.tools - {GREP_TOOL}
        return self._policy.tools

    def set_policy(self, policy: PeerToolPolicy) -> None:
        self._policy = policy

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
        self._deny_restricted(resolved, self._policy.read_file)
        offset = arguments.get("offset")
        if offset is not None and (not isinstance(offset, int) or offset < 1):
            raise ToolError("read_file offset must be a positive integer")
        limit = arguments.get("limit")
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ToolError("read_file limit must be a positive integer")
        try:
            mode = (await anyio.Path(resolved).stat()).st_mode
            if not stat.S_ISREG(mode):
                raise ToolError(f"Cannot read '{raw_path}': not a regular file")
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
        ripgrep = _find_ripgrep()
        if ripgrep is None:
            raise ToolError("grep needs ripgrep (rg) on PATH")
        config = self._policy.grep
        raw_path = arguments.get("path")
        search_root = (
            self._resolve_workspace_path(raw_path)
            if isinstance(raw_path, str) and raw_path
            else self._cwd
        )
        self._deny_restricted(search_root, config)
        max_matches = arguments.get("max_matches")
        if max_matches is not None and (
            not isinstance(max_matches, int) or max_matches < 1
        ):
            raise ToolError("grep max_matches must be a positive integer")
        use_default_ignore = arguments.get("use_default_ignore", True)
        if not isinstance(use_default_ignore, bool):
            raise ToolError("grep use_default_ignore must be a boolean")

        command = [ripgrep, *_RIPGREP_ARGS, f"--max-filesize={GREP_MAX_FILE_BYTES}"]
        if not use_default_ignore:
            command.append("--no-ignore")
        codeignore = self._cwd / config.codeignore_file
        if await anyio.Path(codeignore).is_file():
            command.extend(["--ignore-file", str(codeignore)])
        for excluded in config.exclude_patterns:
            command.extend(["--glob", f"!{excluded}"])
        for sensitive in config.sensitive_patterns:
            command.extend(["--iglob", f"!{sensitive}"])
        command.extend([
            "-e",
            pattern,
            "--",
            search_root.relative_to(self._cwd).as_posix(),
        ])

        matches, was_truncated = await self._run_ripgrep(
            command, match_cap=max_matches or config.default_max_matches
        )
        return json.dumps({
            "matches": "\n".join(matches),
            "matchCount": len(matches),
            "wasTruncated": was_truncated,
        })

    async def _run_ripgrep(
        self, command: list[str], *, match_cap: int
    ) -> tuple[list[str], bool]:
        config = self._policy.grep
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self._cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_RIPGREP_STREAM_LIMIT,
        )
        stderr_task = asyncio.create_task(_read_stderr(process))
        matches: list[str] = []
        was_truncated = False
        try:
            async with asyncio.timeout(config.default_timeout):
                was_truncated = await self._collect_matches(
                    process, matches, match_cap=match_cap
                )
        except TimeoutError:
            was_truncated = True
        finally:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            error = await stderr_task
        if (
            process.returncode == _RIPGREP_ERROR_EXIT_CODE
            and not matches
            and not was_truncated
        ):
            raise ToolError(f"Invalid grep pattern: {error or 'ripgrep failed'}")
        return matches, was_truncated

    async def _collect_matches(
        self, process: asyncio.subprocess.Process, matches: list[str], *, match_cap: int
    ) -> bool:
        if process.stdout is None:
            return False
        output_bytes = 0
        async for raw in process.stdout:
            path, separator, rest = raw.rstrip(b"\r\n").partition(b"\0")
            if not separator:
                continue
            line_number, _, content = rest.partition(b":")
            relative = path.decode(errors="replace").removeprefix("./")
            if self._is_restricted(self._cwd / relative, self._policy.grep):
                continue
            entry = (
                f"{relative}:{line_number.decode(errors='replace')}: "
                f"{content.decode(errors='replace')}"
            )
            entry_bytes = len(entry.encode("utf-8", errors="replace")) + 1
            if (
                len(matches) >= match_cap
                or output_bytes + entry_bytes > self._policy.grep.max_output_bytes
            ):
                return True
            matches.append(entry)
            output_bytes += entry_bytes
        return False

    async def _web_fetch(self, arguments: dict[str, Any]) -> str:
        url = arguments.get("url")
        if not isinstance(url, str) or not url:
            raise ToolError("web_fetch requires a url")
        _parse_http_url(url)
        timeout = arguments.get("timeout")
        if timeout is not None and (
            not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ToolError("web_fetch timeout must be a positive number of seconds")
        timeout_seconds = min(
            float(timeout) if timeout is not None else WEB_FETCH_DEFAULT_TIMEOUT,
            WEB_FETCH_MAX_TIMEOUT,
        )
        try:
            async with asyncio.timeout(timeout_seconds):
                return await _fetch_public(url, timeout_seconds)
        except TimeoutError as exc:
            raise ToolError(
                f"web_fetch timed out after {timeout_seconds:g}s for '{url}'"
            ) from exc

    def _resolve_workspace_path(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self._cwd / path
        resolved = path.resolve()
        if not resolved.is_relative_to(self._cwd):
            raise ToolError(f"Path '{raw}' is outside the session workspace")
        return resolved

    def _is_restricted(
        self, resolved: Path, config: ReadFileConfig | GrepToolConfig
    ) -> bool:
        if matches_sensitive_pattern(str(resolved), config.sensitive_patterns):
            return True
        if not config.denylist or not resolved.is_relative_to(self._cwd):
            return False
        parts = resolved.relative_to(self._cwd).parts
        return any(
            resolve_path_permission(
                str(self._cwd.joinpath(*parts[:depth])),
                cwd=self._cwd,
                allowlist=[],
                denylist=config.denylist,
            )
            is not None
            for depth in range(1, len(parts) + 1)
        )

    def _deny_restricted(
        self, resolved: Path, config: ReadFileConfig | GrepToolConfig
    ) -> None:
        if self._is_restricted(resolved, config):
            raise ToolError(f"Path '{resolved}' is denied to peer review")


async def _read_stderr(process: asyncio.subprocess.Process) -> str:
    if process.stderr is None:
        return ""
    return (await process.stderr.read(8192)).decode(errors="replace").strip()


async def _fetch_public(url: str, timeout_seconds: float) -> str:
    current = url
    try:
        async with VibeAsyncHTTPClient(
            timeout=timeout_seconds, follow_redirects=False, verify=build_ssl_context()
        ) as client:
            for _ in range(WEB_FETCH_MAX_REDIRECTS + 1):
                await _require_public_host(current)
                async with client.stream(
                    "GET", current, headers={"user-agent": get_user_agent(None)}
                ) as response:
                    location = response.headers.get("location")
                    if response.status_code in _REDIRECT_STATUSES and location:
                        current = urljoin(current, location)
                        continue
                    return await _read_response(response, current)
    except ToolError:
        raise
    except Exception as exc:
        # httpx errors are not OSError subclasses; wrap anything the fetch
        # raises so the controller can feed it back to the duck.
        raise ToolError(f"web_fetch failed for '{url}': {exc}") from exc
    raise ToolError(
        f"web_fetch followed more than {WEB_FETCH_MAX_REDIRECTS} redirects for '{url}'"
    )


async def _read_response(response: Any, url: str) -> str:
    raw = bytearray()
    was_truncated = False
    async for chunk in response.aiter_bytes():
        raw.extend(chunk)
        if len(raw) > WEB_FETCH_MAX_BYTES:
            was_truncated = True
            break
    text = bytes(raw[:WEB_FETCH_MAX_BYTES]).decode(
        response.encoding or "utf-8", errors="replace"
    )
    if "html" in response.headers.get("content-type", ""):
        text = _WHITESPACE.sub(" ", _HTML_TAG.sub(" ", text)).strip()
        text = html.unescape(text)
    return json.dumps({
        "url": url,
        "statusCode": response.status_code,
        "content": text,
        "wasTruncated": was_truncated,
    })


def _parse_http_url(url: str) -> SplitResult:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ToolError(f"web_fetch only supports http(s) URLs: '{url}'")
    return parsed


async def _resolve_addresses(
    host: str, port: int
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    with suppress(ValueError):
        return [ipaddress.ip_address(host)]
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, port, type=socket.SOCK_STREAM
    )
    return [ipaddress.ip_address(str(info[4][0]).split("%")[0]) for info in infos]


def _is_public(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_global


async def _require_public_host(url: str) -> None:
    parsed = _parse_http_url(url)
    try:
        host = httpx.URL(url).raw_host.decode("ascii")
    except (httpx.InvalidURL, UnicodeDecodeError) as exc:
        raise ToolError(f"web_fetch cannot parse the host of '{url}'") from exc
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    addresses = await _resolve_addresses(host, port)
    if not addresses or not all(_is_public(address) for address in addresses):
        raise ToolError(f"web_fetch refuses non-public address for '{host}'")


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
