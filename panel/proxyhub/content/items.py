"""The one shape every source plugin gives (proxyhub/content/plugins.py), and the input error."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

MediaKind = Literal["photo", "video"]
MAX_TEXT_CHARS: Final = 4000
MAX_EXTERNAL_ID: Final = 200


class SourceInvalid(ValueError):
    """The owner's input for a source is not acceptable. ``code`` is shown in Persian."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MediaRef:
    """A photo or video of a post, by its https URL (checked again when it is downloaded)."""

    kind: MediaKind
    url: str | None  # None: the source shows the media but gives no address for it
    thumb_url: str | None = None  # a video's poster image
    duration: int | None = None  # seconds, when the source says


@dataclass(frozen=True)
class FetchedItem:
    """One post of a source, in the one shape every plugin gives."""

    external_id: str  # unique within the source (a message id, a feed entry's id or link)
    url: str | None  # the original post, https
    published_at: datetime | None
    text: str  # plain text, no markup; line breaks kept
    views: int | None = None
    media: MediaRef | None = None
