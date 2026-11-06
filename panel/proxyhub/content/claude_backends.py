"""The two ways the content supervisor reaches Claude (decisions D19 and D52), and the writer that
tries them in order.

**Bridge** (:class:`BridgeBackend`, primary): the host's bridge (deploy/claude-bridge/bridge.py) on
a unix socket the worker mounts. One JSON line out, one back; the bridge runs ``claude -p`` with no
tools and the owner's subscription. Its protocol:

* ``{"op": "health"}`` -> ``{"ok": true, "protocol": 1, "calls_24h", "daily_cap", "busy",
  "logged_in", "claude_version", "version_ok", "rate_limit", "last_outcome", ...}``;
* ``{"op": "review", "protocol": 1, "model", "system", "content": [text and image blocks],
  "timeout_s"}`` -> ``{"ok": true, "text", "model", "duration_ms", ...}`` or ``{"ok": false,
  "error": <code>}``.

**API** (:class:`ApiBackend`, backup): the Anthropic Messages API with the owner's key from the
vault, over httpx like every other client of the panel (proxyhub/cloudflare/client.py): one endpoint
and one request shape do not need the SDK, and the SDK's automatic retries would hide calls from the
daily cap. The request: ``anthropic-version: 2026-10-21``, the model (``claude-sonnet-5-5`` or
``claude-haiku-5-5``), the system prompt, one user message (the image block, then the text),
``output_config.format`` with the answer's JSON Schema (structured output) and ``effort: medium``;
no sampling parameters (refused since that API version). A ``refusal`` stop reason is NOT retried
on another model (no server-side fallback): for a safety reviewer a refusal is itself a reason to
reject (outcome ``refused``). Rule 3: the key is a SecretValue revealed only into the ``x-api-key``
header; errors carry a short code and the HTTP status only.

**:class:`ClaudeWriter`** asks the backends in order. A backend that cannot answer (not installed,
logged out, over its cap, rate-limited, down) hands over to the next; an answer, or an outcome about
the candidate itself (``refused``, ``empty``, ``too_long``), ends the review.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx

from proxyhub.content.qc_prompt import output_schema
from proxyhub.content.writer import Backend, Review, ReviewRequest
from proxyhub.vault import SecretValue

log = logging.getLogger(__name__)

PROTOCOL: Final = 1
API_URL: Final = "https://api.anthropic.com/v1/messages"
API_VERSION: Final = "2026-10-21"
API_MAX_TOKENS: Final = 8000
API_EFFORT: Final = "medium"
VAULT_NAME: Final = "anthropic_api_key"
MAX_ANSWER_BYTES: Final = 1024 * 1024
HEALTH_TIMEOUT_S: Final = 8.0

# Outcomes about the candidate (Claude was reached but gave no usable answer): the review ends and
# the candidate is rejected; trying another backend would only spend more.
UNUSABLE: Final = frozenset({"refused", "empty", "too_long", "max_turns"})
# Outcomes about our request (a bug or an odd image): the review ends, the candidate is tried again
# later (at most 3 times) and is never approved by it.
INPUT: Final = frozenset(
    {"bad_image", "too_large", "bad_request", "bad_model", "api_bad_request", "api_too_large"}
)
# Attempts that certainly never reached Claude, so they spend no quota and count toward no cap.
NOT_STARTED: Final = frozenset(
    {
        "bridge_missing",
        "bridge_denied",
        "bridge_refused",
        "busy",
        "cap",
        "peer_not_allowed",
        "bad_request",
        "bad_model",
        "too_large",
        "bad_image",
        "claude_missing",
        "unavailable",
        "api_auth",
        "api_unreachable",
    }
)


@dataclass(frozen=True)
class Attempt:
    backend: Backend
    outcome: str
    duration_ms: int | None = None
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


class ReviewBackend(Protocol):
    @property
    def kind(self) -> Backend: ...

    async def review(self, request: ReviewRequest) -> Attempt | tuple[Attempt, str]:
        """The attempt, with Claude's raw answer when the outcome is ``ok``."""
        ...


def _blocks(request: ReviewRequest) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    if request.image is not None:
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": request.image.mime,
                    "data": base64.b64encode(request.image.data).decode("ascii"),
                },
            }
        )
    blocks.append({"type": "text", "text": request.user_text})
    return blocks


def _int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


# --- the bridge --------------------------------------------------------------------------------------


async def bridge_call(socket_path: Path, request: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """One request to the bridge. Returns its answer, or ``{"ok": false, "error": <code>}``."""
    writer: asyncio.StreamWriter | None = None
    try:
        reader, connected = await asyncio.wait_for(
            asyncio.open_unix_connection(str(socket_path), limit=MAX_ANSWER_BYTES), 5
        )
        writer = connected
        connected.write((json.dumps(request, ensure_ascii=False) + "\n").encode())
        await connected.drain()
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout_s)
    except FileNotFoundError:
        return {"ok": False, "error": "bridge_missing"}
    except PermissionError:
        return {"ok": False, "error": "bridge_denied"}
    except ConnectionRefusedError:
        return {"ok": False, "error": "bridge_refused"}
    except TimeoutError:
        return {"ok": False, "error": "bridge_timeout"}
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, OSError):
        return {"ok": False, "error": "bridge_bad_answer"}
    finally:
        if writer is not None:
            with contextlib.suppress(OSError, ConnectionError):
                writer.close()
                await writer.wait_closed()
    try:
        answer = json.loads(line)
    except (ValueError, RecursionError):
        return {"ok": False, "error": "bridge_bad_answer"}
    if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
        return {"ok": False, "error": "bridge_bad_answer"}
    if not answer["ok"]:
        error = answer.get("error")
        return {"ok": False, "error": error[:40] if isinstance(error, str) and error else "bridge_bad_answer"}
    return answer


@dataclass(frozen=True)
class BridgeBackend:
    socket_path: Path
    model: str
    timeout_s: int
    kind: Backend = "bridge"

    async def review(self, request: ReviewRequest) -> Attempt | tuple[Attempt, str]:
        started = time.monotonic()
        answer = await bridge_call(
            self.socket_path,
            {
                "op": "review",
                "protocol": PROTOCOL,
                "model": self.model,
                "system": request.system,
                "content": _blocks(request),
                "timeout_s": self.timeout_s,
            },
            self.timeout_s + 20,
        )
        took = int((time.monotonic() - started) * 1000)
        if not answer["ok"]:
            return Attempt("bridge", str(answer["error"]), duration_ms=took)
        text = answer.get("text")
        if not isinstance(text, str) or not text.strip():
            return Attempt("bridge", "empty", duration_ms=took)
        cost = answer.get("cost_usd")
        model = answer.get("model")
        attempt = Attempt(
            "bridge",
            "ok",
            duration_ms=took,
            model=model[:64] if isinstance(model, str) else self.model,
            input_tokens=_int(answer.get("input_tokens")),
            output_tokens=_int(answer.get("output_tokens")),
            cost_usd=float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) else None,
        )
        return attempt, text

    async def health(self) -> dict[str, Any]:
        """The bridge's health, reduced to what the dashboard shows (numbers, booleans, a version)."""
        answer = await bridge_call(self.socket_path, {"op": "health"}, HEALTH_TIMEOUT_S)
        if not answer["ok"]:
            return {"ok": False, "error": answer["error"]}
        out: dict[str, Any] = {"ok": True}
        for key in ("calls_24h", "daily_cap", "protocol"):
            out[key] = _int(answer.get(key))
        for key in ("busy", "logged_in", "version_ok"):
            value = answer.get(key)
            out[key] = value if isinstance(value, bool) else None
        for key in ("claude_version", "last_outcome"):
            value = answer.get(key)
            out[key] = value[:40] if isinstance(value, str) else None
        rate = answer.get("rate_limit")
        if isinstance(rate, dict):
            out["rate_limit"] = {
                k: v
                for k, v in rate.items()
                if k in ("status", "type", "resets_at", "utilization")
                and isinstance(v, str | int | float)
                and not isinstance(v, bool)
            }
        else:
            out["rate_limit"] = None
        return out


# --- the Messages API --------------------------------------------------------------------------------


def api_body(request: ReviewRequest, model: str) -> dict[str, Any]:
    """The Messages API request (see the module doc)."""
    return {
        "model": model,
        "max_tokens": API_MAX_TOKENS,
        "system": request.system,
        "messages": [{"role": "user", "content": _blocks(request)}],
        "output_config": {"effort": API_EFFORT, "format": {"type": "json_schema", "schema": output_schema()}},
    }


def api_outcome(status: int) -> str:
    if status in (401, 403):
        return "api_auth"
    if status == 429:
        return "api_rate_limited"
    if status == 400:
        return "api_bad_request"
    if status == 404:
        return "api_bad_model"
    if status == 413:
        return "api_too_large"
    if status == 529:
        return "api_overloaded"
    return "api_unavailable" if status >= 500 else f"api_http_{status}"


def read_api_answer(data: Any) -> tuple[str, str | None, dict[str, Any]]:
    """A Messages API response body -> (outcome, the answer's text, usage facts)."""
    if not isinstance(data, dict) or data.get("type") != "message":
        return "api_bad_answer", None, {}
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    facts = {
        "model": data.get("model") if isinstance(data.get("model"), str) else None,
        "input_tokens": _int(usage.get("input_tokens")) if isinstance(usage, dict) else None,
        "output_tokens": _int(usage.get("output_tokens")) if isinstance(usage, dict) else None,
    }
    stop = data.get("stop_reason")
    if stop == "refusal":
        return "refused", None, facts
    if stop == "max_tokens":
        return "too_long", None, facts
    content = data.get("content")
    if not isinstance(content, list):
        return "api_bad_answer", None, facts
    text = "".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
    )
    if not text.strip():
        return "empty", None, facts
    return "ok", text, facts


@dataclass(frozen=True)
class ApiBackend:
    api_key: SecretValue
    model: str
    timeout_s: int
    url: str = API_URL
    kind: Backend = "api"

    async def review(self, request: ReviewRequest) -> Attempt | tuple[Attempt, str]:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout_s, connect=10.0)) as http:
                response = await http.post(
                    self.url,
                    json=api_body(request, self.model),
                    headers={
                        "x-api-key": self.api_key.reveal(),
                        "anthropic-version": API_VERSION,
                        "content-type": "application/json",
                    },
                )
        except httpx.TimeoutException:
            return Attempt("api", "api_timeout", duration_ms=int((time.monotonic() - started) * 1000))
        except httpx.HTTPError as exc:
            log.warning("content qc: the Anthropic API was not reached (%s)", type(exc).__name__)
            return Attempt("api", "api_unreachable", duration_ms=int((time.monotonic() - started) * 1000))
        took = int((time.monotonic() - started) * 1000)
        if response.status_code != 200:
            return Attempt("api", api_outcome(response.status_code), duration_ms=took)
        try:
            data = response.json()
        except ValueError:
            return Attempt("api", "api_bad_answer", duration_ms=took)
        outcome, text, facts = read_api_answer(data)
        attempt = Attempt(
            "api",
            outcome,
            duration_ms=took,
            model=(facts.get("model") or self.model)[:64],
            input_tokens=facts.get("input_tokens"),
            output_tokens=facts.get("output_tokens"),
        )
        return (attempt, text) if outcome == "ok" and text is not None else attempt


# --- the writer ------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaudeWriter:
    """Asks each backend in order until one answers (see the module doc)."""

    backends: Sequence[ReviewBackend]
    name: str = "claude"

    async def review(self, request: ReviewRequest) -> Review:
        attempts: list[Attempt] = []
        for backend in self.backends:
            got = await backend.review(request)
            attempt, text = got if isinstance(got, tuple) else (got, None)
            attempts.append(attempt)
            if attempt.outcome == "ok" or attempt.outcome in UNUSABLE or attempt.outcome in INPUT:
                return Review(
                    outcome=attempt.outcome,
                    backend=attempt.backend,
                    text=text,
                    model=attempt.model,
                    duration_ms=attempt.duration_ms,
                    input_tokens=attempt.input_tokens,
                    output_tokens=attempt.output_tokens,
                    cost_usd=attempt.cost_usd,
                    tried=tuple((a.backend, a.outcome) for a in attempts),
                )
        if not attempts:
            return Review(outcome="unavailable")
        last = attempts[-1]
        return Review(
            outcome=last.outcome, backend=last.backend, tried=tuple((a.backend, a.outcome) for a in attempts)
        )
