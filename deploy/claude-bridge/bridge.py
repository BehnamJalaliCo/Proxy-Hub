#!/usr/bin/env python3
"""Proxy-Hub Claude bridge (decisions D19 and D52): the panel's content supervisor asks Claude Code,
logged in with the OWNER's Claude subscription on the panel server, to review one candidate post.

The panel runs in containers and never holds the subscription login. This small host service does:

* runs as its own unprivileged user (``proxyhub-claude``, deploy/claude-bridge/install.sh), whose
  home holds the Claude Code install and its login, and nothing of the panel;
* listens on ONE unix socket (``/var/lib/proxy-hub/claude-bridge/bridge.sock``, group
  ``proxyhub-claude-sock``, mode 0660) that only the worker container mounts; the peer's uid is
  checked too (``SO_PEERCRED``: the panel's uid 10001, and root for the operator's checks);
* takes one JSON request per connection (one line) and gives one JSON line back:

  - ``{"op": "health"}``: protocol, calls in the last 24 h, the cap, busy, the last outcome, Claude
    Code's version, whether it is logged in, and the subscription's rate-limit state (numbers only);
  - ``{"op": "review", "model", "system", "content", "timeout_s"}``: runs ``claude -p`` once.

**How Claude Code runs** (flags checked against code.claude.com/docs/en/cli-reference and
/headless, and against a real run of Claude Code 2.1.319 on 2026-11-06):

* ``-p --input-format stream-json --output-format stream-json --verbose``: the only way to give an
  IMAGE to a headless run is a ``user`` message on stdin with ``image`` content blocks (base64);
  stream-json input requires stream-json output;
* ``--tools ""`` (no built-in tool at all), ``--disallowedTools "*"``, ``--strict-mcp-config``
  without ``--mcp-config`` (no MCP server), ``--permission-mode dontAsk`` and
  ``--permission-prompts none`` (anything that would ask is denied), ``--disable-slash-commands``,
  ``--safe-mode`` (no CLAUDE.md, hooks, skills, plugins or MCP of that user; the login still works,
  unlike ``--bare``), ``--no-session-persistence`` (no transcript on disk), ``--max-turns 1``;
* ``--json-schema`` is NOT used: Claude Code delivers it through a ``StructuredOutput`` tool, which
  the no-tools rule denies (seen in the real run). The answer is plain text and the panel validates
  it against its own schema (proxyhub/content/qc_prompt.py); anything else is rejected there;
* the system prompt (the panel's own text) goes through ``--system-prompt-file``, a 0600 file this
  bridge writes; ``--model`` takes a checked alias or model id. **Nothing of the candidate is ever
  in argv** (argv is visible in ``ps``): the candidate's text and image go through stdin only;
* the stream's ``system/init`` event must list NO tools and NO MCP server, else the run is killed
  and refused (``tools_present``): a future Claude Code that ignored a flag cannot act;
* the child gets a minimal environment: HOME, PATH, a UTF-8 locale, ``DISABLE_TELEMETRY``,
  ``DISABLE_ERROR_REPORTING`` and ``CLAUDE_CODE_SKIP_PROMPT_HISTORY``, and runs in an empty work
  directory, in its own process group (killed whole on timeout).

**Limits:** one review at a time (a second gets ``busy`` at once), a timeout per review (10 to 300
s), and a cap of reviews in any 24 hours (``--daily-cap``, kept in a state file across restarts and
counted when a run STARTS, so a failing loop cannot spend more). Requests are at most 9 MB (Claude
Code reads at most 10 MB from stdin). A review carries at most :data:`MAX_IMAGES` images (a photo, or
a video's frames in order, panel/proxyhub/content/frames.py), only JPEG and PNG checked by their
first bytes, each at most :data:`MAX_IMAGE_BYTES` and all together at most :data:`MAX_IMAGES_BYTES`
decoded (with base64 and the text that stays under the stdin cap).

**Logging:** counts and outcomes only (``review outcome=ok duration_ms=… calls_24h=…``). Never the
candidate, the prompt, Claude's answer or Claude Code's stderr (its length at most).

Standard library only (Python 3.11+). Tests: panel/tests/test_claude_bridge.py.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import contextlib
import json
import logging
import os
import re
import signal
import socket
import stat
import struct
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL = 1
MAX_REQUEST_BYTES = 9 * 1024 * 1024
STDIN_MAX_BYTES = 9_500_000  # Claude Code caps piped stdin at 10 MB
MAX_STDOUT_BYTES = 8 * 1024 * 1024
MAX_RESULT_CHARS = 20_000
MAX_SYSTEM_CHARS = 60_000
MAX_TEXT_CHARS = 30_000
MAX_IMAGES = 8  # a video's frames (the panel sends 6) or one photo
MAX_BLOCKS = MAX_IMAGES + 2
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # one image, decoded (the Messages API's own limit per image)
MAX_IMAGES_BYTES = 6 * 1024 * 1024  # all images, decoded: 8 MB as base64, under STDIN_MAX_BYTES
MIN_TIMEOUT_S = 10
MAX_TIMEOUT_S = 300
DEFAULT_TIMEOUT_S = 150
READ_REQUEST_TIMEOUT_S = 30
WINDOW_S = 24 * 3600
DEFAULT_CAP = 200
DEFAULT_ALLOWED_UIDS = (0, 10001)  # root (the operator's checks) and the panel container's user
STATUS_REFRESH_S = 600
MODEL_RE = re.compile(r"^[a-z][a-z0-9.\-]{1,63}$")
IMAGE_MAGIC = {"image/jpeg": (b"\xff\xd8\xff",), "image/png": (b"\x89PNG\r\n\x1a\n",)}
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")
MIN_CLAUDE_VERSION = (2, 1, 259)  # --permission-prompts needs v2.1.259 or later

log = logging.getLogger("proxyhub-claude-bridge")


class Refused(Exception):
    """A request the bridge does not run. ``code`` goes back to the panel."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the command line Claude Code gets ---------------------------------------------------------------


def build_argv(claude_bin: str, model: str, system_file: str) -> list[str]:
    """The exact argv of one review. Only fixed flags, a checked model name and a path this bridge
    made; never anything of the candidate."""
    if not MODEL_RE.fullmatch(model):
        raise Refused("bad_model")
    return [
        claude_bin,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--tools",
        "",
        "--disallowedTools",
        "*",
        "--strict-mcp-config",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--disable-slash-commands",
        "--safe-mode",
        "--no-session-persistence",
        "--max-turns",
        "1",
        "--model",
        model,
        "--system-prompt-file",
        system_file,
    ]


def child_env(home: str) -> dict[str, str]:
    """The child's whole environment: nothing of this service's own environment leaks in."""
    return {
        "HOME": home,
        "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
    }


def _image_block(block: dict[str, Any]) -> tuple[dict[str, Any], int]:
    """One checked image block and its decoded size: base64 of a JPEG or PNG (by its first bytes,
    which must match the declared type), at most :data:`MAX_IMAGE_BYTES`."""
    source = block.get("source")
    if not isinstance(source, dict) or set(source) != {"type", "media_type", "data"}:
        raise Refused("bad_request")
    media_type, data = source.get("media_type"), source.get("data")
    if source.get("type") != "base64" or media_type not in IMAGE_MAGIC or not isinstance(data, str):
        raise Refused("bad_image")
    if len(data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise Refused("too_large")
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise Refused("bad_image") from None
    if not raw or not raw.startswith(IMAGE_MAGIC[media_type]):
        raise Refused("bad_image")
    if len(raw) > MAX_IMAGE_BYTES:
        raise Refused("too_large")
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}, len(raw)


def build_input(content: Any) -> bytes:
    """The one stream-json ``user`` message on stdin, rebuilt from checked blocks only (text and
    JPEG/PNG images, several images in their order): no field of the request is passed through
    unchecked. At most :data:`MAX_IMAGES` images (else ``bad_request``), each and all together
    within their byte caps (else ``too_large``)."""
    if not isinstance(content, list) or not 1 <= len(content) <= MAX_BLOCKS:
        raise Refused("bad_request")
    blocks: list[dict[str, Any]] = []
    images = 0
    image_bytes = 0
    for block in content:
        if not isinstance(block, dict):
            raise Refused("bad_request")
        kind = block.get("type")
        if kind == "text" and set(block) == {"type", "text"}:
            text = block.get("text")
            if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
                raise Refused("bad_request")
            blocks.append({"type": "text", "text": text})
        elif kind == "image" and set(block) == {"type", "source"}:
            images += 1
            if images > MAX_IMAGES:
                raise Refused("bad_request")
            checked, size = _image_block(block)
            image_bytes += size
            if image_bytes > MAX_IMAGES_BYTES:
                raise Refused("too_large")
            blocks.append(checked)
        else:
            raise Refused("bad_request")
    message = {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}
    line = (json.dumps(message, ensure_ascii=False) + "\n").encode()
    if len(line) > STDIN_MAX_BYTES:
        raise Refused("too_large")
    return line


# --- what Claude Code printed ---------------------------------------------------------------------


@dataclass
class RunResult:
    outcome: str  # ok, or an error code
    text: str | None = None
    model: str | None = None
    num_turns: int | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    rate_limit: dict[str, Any] | None = None


def _rate_limit(event: dict[str, Any]) -> dict[str, Any] | None:
    info = event.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    out: dict[str, Any] = {}
    if isinstance(info.get("status"), str):
        out["status"] = info["status"][:24]
    if isinstance(info.get("rateLimitType"), str):
        out["type"] = info["rateLimitType"][:24]
    if isinstance(info.get("resetsAt"), int | float):
        out["resets_at"] = int(info["resetsAt"])
    windows = info.get("unifiedWindows")
    if isinstance(windows, dict):
        util = [w.get("utilization") for w in windows.values() if isinstance(w, dict)]
        nums = [float(u) for u in util if isinstance(u, int | float)]
        if nums:
            out["utilization"] = round(max(nums), 3)
    return out or None


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def interpret(lines: Sequence[bytes]) -> RunResult:
    """Claude Code's stream-json output -> one outcome. The init event must show no tool and no MCP
    server; the result event must be a success with text."""
    init_seen = False
    rate: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    for raw in lines:
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init_seen = True
            if event.get("tools") != [] or event.get("mcp_servers", []) != []:
                return RunResult("tools_present")
        elif kind == "rate_limit_event":
            rate = _rate_limit(event) or rate
        elif kind == "result":
            result = event
    if result is None:
        return RunResult("claude_failed", rate_limit=rate)
    if not init_seen:
        return RunResult("tools_unknown", rate_limit=rate)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    models = result.get("modelUsage")
    model = next(iter(models), None) if isinstance(models, dict) and models else None
    cost = result.get("total_cost_usd")
    out = RunResult(
        "ok",
        model=model[:64] if isinstance(model, str) else None,
        num_turns=_int(result.get("num_turns")),
        cost_usd=float(cost) if isinstance(cost, int | float) else None,
        input_tokens=_int(usage.get("input_tokens")) if isinstance(usage, dict) else None,
        output_tokens=_int(usage.get("output_tokens")) if isinstance(usage, dict) else None,
        rate_limit=rate,
    )
    text = result.get("result")
    if result.get("is_error") or result.get("subtype") != "success":
        status = result.get("api_error_status")
        lowered = text.lower() if isinstance(text, str) else ""
        if status in (401, 403) or "log in" in lowered or "login" in lowered or "/login" in lowered:
            out.outcome = "auth"
        elif status == 429 or (rate or {}).get("status") == "rejected":
            out.outcome = "rate_limited"
        elif result.get("subtype") == "error_max_turns":
            out.outcome = "max_turns"
        else:
            out.outcome = "claude_failed"
        return out
    if not isinstance(text, str) or not text.strip():
        out.outcome = "empty"
        return out
    if len(text) > MAX_RESULT_CHARS:
        out.outcome = "too_long"
        return out
    out.text = text
    return out


# --- the 24-hour cap --------------------------------------------------------------------------------


class CallLog:
    """Start times of the reviews of the last 24 hours, kept in a small JSON file (atomic writes)."""

    def __init__(self, path: Path, cap: int) -> None:
        self.path = path
        self.cap = cap
        self.calls: list[float] = []
        with contextlib.suppress(OSError, ValueError, TypeError):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.calls = [float(x) for x in data.get("calls", []) if isinstance(x, int | float)]

    def count(self, now: float) -> int:
        self.calls = [t for t in self.calls if now - WINDOW_S < t <= now + 60]
        return len(self.calls)

    def take(self, now: float) -> bool:
        """Record a review starting now, or False when the cap is reached."""
        if self.count(now) >= self.cap:
            return False
        self.calls.append(now)
        self._save()
        return True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".calls.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"calls": self.calls}, f)
            os.replace(tmp, self.path)
        except OSError:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            log.warning("could not save the call log")


# --- the service -----------------------------------------------------------------------------------


@dataclass
class Config:
    socket_path: Path
    claude_bin: str
    home: str
    workdir: Path
    state_file: Path
    daily_cap: int = DEFAULT_CAP
    allowed_uids: tuple[int, ...] = DEFAULT_ALLOWED_UIDS
    check_peer: bool = True


@dataclass
class Bridge:
    config: Config
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_outcome: str | None = None
    last_at: float | None = None
    last_ok_at: float | None = None
    rate_limit: dict[str, Any] | None = None
    claude_version: str | None = None
    logged_in: bool | None = None
    status_at: float = 0.0
    calls: CallLog = field(init=False)
    _child: asyncio.subprocess.Process | None = None

    def __post_init__(self) -> None:
        self.calls = CallLog(self.config.state_file, self.config.daily_cap)

    # -- requests --

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            answer = await self._answer(reader, writer)
        except Refused as exc:
            answer = {"ok": False, "error": exc.code}
        except Exception as exc:  # never the message: it could hold request content
            log.error("request failed (%s)", type(exc).__name__)
            answer = {"ok": False, "error": "internal"}
        with contextlib.suppress(OSError, ConnectionError):
            writer.write((json.dumps(answer, ensure_ascii=False) + "\n").encode())
            await writer.drain()
        with contextlib.suppress(OSError, ConnectionError):
            writer.close()
            await writer.wait_closed()

    def _peer_ok(self, writer: asyncio.StreamWriter) -> bool:
        if not self.config.check_peer:
            return True
        sock = writer.get_extra_info("socket")
        if sock is None:
            return False
        try:
            creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        except OSError:
            return False
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid in self.config.allowed_uids

    async def _answer(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> dict[str, Any]:
        if not self._peer_ok(writer):
            log.warning("refused a connection from a uid that is not allowed")
            raise Refused("peer_not_allowed")
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), READ_REQUEST_TIMEOUT_S)
        except asyncio.LimitOverrunError:
            raise Refused("too_large") from None
        except (asyncio.IncompleteReadError, TimeoutError):
            raise Refused("bad_request") from None
        try:
            request = json.loads(raw)
        except (ValueError, RecursionError):
            raise Refused("bad_request") from None
        if not isinstance(request, dict):
            raise Refused("bad_request")
        op = request.get("op")
        if op == "health":
            return await self.health()
        if op == "review":
            return await self.review(request)
        raise Refused("bad_request")

    async def health(self) -> dict[str, Any]:
        now = time.time()
        if not self.lock.locked() and now - self.status_at > STATUS_REFRESH_S:
            async with self.lock:
                await self._refresh_status()
        return {
            "ok": True,
            "protocol": PROTOCOL,
            "calls_24h": self.calls.count(now),
            "daily_cap": self.config.daily_cap,
            "busy": self.lock.locked(),
            "last_outcome": self.last_outcome,
            "last_at": self.last_at,
            "last_ok_at": self.last_ok_at,
            "claude_version": self.claude_version,
            "version_ok": version_ok(self.claude_version),
            "logged_in": self.logged_in,
            "rate_limit": self.rate_limit,
        }

    async def _refresh_status(self) -> None:
        self.status_at = time.time()
        out = await self._run_small([self.config.claude_bin, "--version"])
        self.claude_version = out.strip()[:40] if out is not None else None
        out = await self._run_small([self.config.claude_bin, "auth", "status"])
        logged_in: bool | None = None
        if out is not None:
            with contextlib.suppress(ValueError, RecursionError):
                data = json.loads(out)
                if isinstance(data, dict) and isinstance(data.get("loggedIn"), bool):
                    logged_in = data["loggedIn"]
        self.logged_in = logged_in

    async def _run_small(self, argv: list[str]) -> str | None:
        """A short Claude Code command (version, login state); its output is read, never logged."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.config.workdir,
                env=child_env(self.config.home),
                start_new_session=True,
            )
        except OSError:
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 20)
        except TimeoutError:
            _kill(proc)
            await proc.wait()
            return None
        return out[:65536].decode(errors="replace") if proc.returncode == 0 else None

    async def review(self, request: dict[str, Any]) -> dict[str, Any]:
        model = request.get("model")
        system = request.get("system")
        if not isinstance(model, str) or not MODEL_RE.fullmatch(model):
            raise Refused("bad_model")
        if not isinstance(system, str) or not system.strip() or len(system) > MAX_SYSTEM_CHARS:
            raise Refused("bad_request")
        timeout = request.get("timeout_s", DEFAULT_TIMEOUT_S)
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            raise Refused("bad_request")
        timeout = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout))
        stdin = build_input(request.get("content"))
        if self.lock.locked():
            raise Refused("busy")
        async with self.lock:
            started = time.time()
            if not self.calls.take(started):
                self._record("cap", started)
                log.info(
                    "review outcome=cap calls_24h=%d cap=%d", self.calls.count(started), self.config.daily_cap
                )
                raise Refused("cap")
            result = await self._run_review(model, system, stdin, timeout)
            took = int((time.time() - started) * 1000)
            self._record(result.outcome, started)
            if result.rate_limit is not None:
                self.rate_limit = result.rate_limit
            log.info(
                "review outcome=%s duration_ms=%d calls_24h=%d cap=%d",
                result.outcome,
                took,
                self.calls.count(time.time()),
                self.config.daily_cap,
            )
        if result.outcome != "ok":
            return {"ok": False, "error": result.outcome, "duration_ms": took}
        return {
            "ok": True,
            "text": result.text,
            "model": result.model,
            "num_turns": result.num_turns,
            "cost_usd": result.cost_usd,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "duration_ms": took,
        }

    def _record(self, outcome: str, at: float) -> None:
        self.last_outcome, self.last_at = outcome, at
        if outcome == "ok":
            self.last_ok_at = at

    async def _run_review(self, model: str, system: str, stdin: bytes, limit_s: int) -> RunResult:
        self.config.workdir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, prompt_path = tempfile.mkstemp(prefix="system-", suffix=".txt", dir=self.config.workdir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(system)
            argv = build_argv(self.config.claude_bin, model, prompt_path)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=self.config.workdir,
                    env=child_env(self.config.home),
                    start_new_session=True,
                    limit=MAX_STDOUT_BYTES,
                )
            except OSError:
                return RunResult("claude_missing")
            self._child = proc
            try:
                return await asyncio.wait_for(self._talk(proc, stdin), limit_s)
            except TimeoutError:
                _kill(proc)
                await proc.wait()
                return RunResult("timeout")
            finally:
                self._child = None
        finally:
            with contextlib.suppress(OSError):
                os.unlink(prompt_path)

    async def _talk(self, proc: asyncio.subprocess.Process, stdin: bytes) -> RunResult:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        async def feed() -> None:
            assert proc.stdin is not None
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.write(stdin)
                await proc.stdin.drain()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                proc.stdin.close()

        async def drain_stderr() -> int:
            assert proc.stderr is not None
            size = 0
            while chunk := await proc.stderr.read(65536):
                size += len(chunk)  # never kept: it could echo the candidate
            return size

        async def read_stdout() -> list[bytes] | None:
            assert proc.stdout is not None
            lines: list[bytes] = []
            total = 0
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    return None
                if not line:
                    return lines
                total += len(line)
                if total > MAX_STDOUT_BYTES:
                    return None
                lines.append(line)
                if b'"init"' in line and interpret([line]).outcome == "tools_present":
                    return [line]  # stop at once: this run could act

        helpers = [asyncio.create_task(feed()), asyncio.create_task(drain_stderr())]
        try:
            lines = await read_stdout()
            if lines is None:
                _kill(proc)
                await proc.wait()
                return RunResult("too_long")
            result = interpret(lines)
            if result.outcome == "tools_present":
                _kill(proc)
            await proc.wait()
            return result
        finally:  # also on the timeout's cancellation: no task outlives the run
            for task in helpers:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    def stop_child(self) -> None:
        if self._child is not None:
            _kill(self._child)


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def version_ok(version: str | None) -> bool | None:
    if version is None:
        return None
    m = VERSION_RE.search(version)
    if m is None:
        return None
    return tuple(int(x) for x in m.groups()) >= MIN_CLAUDE_VERSION


# --- start -------------------------------------------------------------------------------------------


def _prepare_socket(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit(f"{path} exists and is not a socket")
        path.unlink()


async def serve(config: Config, ready: asyncio.Event | None = None, *, handle_signals: bool = True) -> None:
    bridge = Bridge(config)
    _prepare_socket(config.socket_path)
    server = await asyncio.start_unix_server(
        bridge.handle, path=str(config.socket_path), limit=MAX_REQUEST_BYTES + 1024
    )
    os.chmod(config.socket_path, 0o660)
    log.info("listening (protocol %d, cap %d per 24 h)", PROTOCOL, config.daily_cap)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT) if handle_signals else ():
        with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)
    if ready is not None:
        ready.set()
    async with server:
        try:
            await stop.wait()
        finally:
            bridge.stop_child()
            server.close()
            with contextlib.suppress(OSError):
                config.socket_path.unlink()
    log.info("stopped")


def parse_args(argv: Sequence[str] | None = None) -> Config:
    home = os.environ.get("HOME", "/var/lib/proxyhub-claude")
    p = argparse.ArgumentParser(description="Proxy-Hub Claude bridge (one unix socket, claude -p, no tools)")
    p.add_argument("--socket", default="/var/lib/proxy-hub/claude-bridge/bridge.sock")
    p.add_argument("--claude", default=os.path.join(home, ".local/bin/claude"))
    p.add_argument("--workdir", default=os.path.join(home, "work"))
    p.add_argument("--state", default=os.path.join(home, "state/calls.json"))
    p.add_argument("--daily-cap", type=int, default=DEFAULT_CAP)
    p.add_argument("--allowed-uids", default=",".join(str(u) for u in DEFAULT_ALLOWED_UIDS))
    args = p.parse_args(argv)
    if not 1 <= args.daily_cap <= 5000:
        p.error("--daily-cap must be 1 to 5000")
    try:
        uids = tuple(int(x) for x in args.allowed_uids.split(",") if x.strip())
    except ValueError:
        p.error("--allowed-uids takes numbers separated by commas")
    return Config(
        socket_path=Path(args.socket),
        claude_bin=args.claude,
        home=home,
        workdir=Path(args.workdir),
        state_file=Path(args.state),
        daily_cap=args.daily_cap,
        allowed_uids=uids,
    )


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    config = parse_args(argv)
    asyncio.run(serve(config))


if __name__ == "__main__":
    main()
