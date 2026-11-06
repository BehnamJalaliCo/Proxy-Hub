"""The caption writer and quality reviewer: Claude reads one candidate (its cleaned text and its
photo, or a video's thumbnail), decides whether it may go to the channel, and writes its short
Persian title and body (decisions D19, D51 and D52).

The supervisor (proxyhub/content/supervisor.py) calls :meth:`CaptionWriter.review` for the best few
candidates before each posting slot, with no transaction open. The answer is a :class:`Review`:

* ``outcome == "ok"``: Claude answered; ``text`` is its raw answer, which the supervisor validates
  against the schema (proxyhub/content/qc_prompt.py) and checks again deterministically
  (proxyhub/content/qc_checks.py). Anything unparsable is a rejection;
* any other outcome: Claude could not be asked (bridge down, login expired, over a cap, timeout). The
  candidate stays where it is and the post falls back to the content bank when nothing is ready.

:class:`~proxyhub.content.claude_writer.ClaudeWriter` is the real writer: Claude Code with the owner's
subscription through the host bridge (deploy/claude-bridge), and the Anthropic API as a backup when
the owner stored a key. :class:`NoWriter` is the writer when neither is configured.

A writer never sees a secret of the panel and never receives anything but the candidate itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

Backend = Literal["bridge", "api"]


@dataclass(frozen=True)
class Draft:
    title: str
    body: str


@dataclass(frozen=True)
class Image:
    mime: Literal["image/jpeg", "image/png"]
    data: bytes


@dataclass(frozen=True)
class ReviewRequest:
    """What Claude is given: the panel's instructions (``system``, built by qc_prompt from the owner's
    policy and the caption's room) and the candidate as data only (``user_text`` with the source text
    between nonce markers, and at most one image)."""

    system: str
    user_text: str
    image: Image | None = None


@dataclass(frozen=True)
class Review:
    outcome: str  # "ok", or why Claude could not be asked (a short code; Persian in the dashboard)
    backend: Backend | None = None
    text: str | None = None
    model: str | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    tried: tuple[tuple[str, str], ...] = field(default_factory=tuple)  # (backend, outcome) per try


class CaptionWriter(Protocol):
    name: str

    async def review(self, request: ReviewRequest) -> Review:
        """Ask Claude once. Never raises for a backend's failure: the outcome says what happened."""
        ...


class NoWriter:
    """No backend configured: every review is ``unavailable`` and posts use the content bank."""

    name = "none"

    async def review(self, request: ReviewRequest) -> Review:
        return Review(outcome="unavailable")


DEFAULT_WRITER: CaptionWriter = NoWriter()
