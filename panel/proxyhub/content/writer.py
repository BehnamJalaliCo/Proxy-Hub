"""The caption writer: where Claude will write a short Persian caption (decision D19). Not in phase A.

D19 wants the panel to call Claude Code headless (``claude -p``) with the owner's subscription on
the panel server. The panel runs in containers (api, worker) and that needs its own design first:
where the CLI and its login live (not in the panel image, not in the repository), how the worker
reaches it, a daily cap shared with development work, and a timeout. Until then every candidate's
caption is the source text cleaned by proxyhub/content/clean.py, which the owner edits in the review
queue (docs/10 section 3.12, TODO).

The engine calls :meth:`CaptionWriter.write` for each new candidate; ``None`` keeps the cleaned
source text. A writer never sees a secret and its answer is plain text, checked like the owner's
input (proxyhub/content/candidates.py :func:`check_caption`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Draft:
    title: str
    body: str


class CaptionWriter(Protocol):
    name: str

    async def write(self, *, text: str, source_label: str, media_kind: str | None) -> Draft | None:
        """A caption for the cleaned source ``text``, or None to keep the text as it is."""
        ...


class NoWriter:
    """Phase A: no writer; the cleaned source text is the caption the owner reviews."""

    name = "none"

    async def write(self, *, text: str, source_label: str, media_kind: str | None) -> Draft | None:
        return None


DEFAULT_WRITER: CaptionWriter = NoWriter()
