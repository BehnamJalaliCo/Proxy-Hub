"""Media of a candidate: what may be downloaded, and what the bytes really are.

**When media is downloaded** (decision D51; owner review first, D20):

* a **photo** (and a **video's thumbnail**) when the candidate is fetched: the owner must SEE it in
  the review queue, and the panel shows it from its own API, never from the source (no hot-link:
  the owner's browser never contacts the source, and the source's CDN addresses expire, Telegram's
  within hours). Photos are small (a preview page's photo is usually 100 to 300 KB), so this costs
  little;
* a **video** only when it is needed: when the content supervisor picks the candidate for a review
  (decision D52: Claude judges frames sampled across the whole video, proxyhub/content/frames.py;
  the file is kept for the post if it is approved and removed at once if it is rejected), or once
  the owner approved a candidate that has no file yet. Most candidates are never reviewed, and a
  video is up to :data:`VIDEO_MAX_BYTES`. The thumbnail stands in for it in the queue.

**Limits.** A post may go out through the operator account (Telegram Premium) or, when that account
cannot take it, through the bot with the same file (proxyhub/posts/poster.py), so every file must
fit BOTH:

* Bot API, multipart upload (core.telegram.org/bots/api#sending-files): photos up to 10 MB, other
  files up to 50 MB; a photo's width + height at most 10000 and its aspect ratio at most 20
  (``sendPhoto``).
* MTProto user account (core.telegram.org/api/files): parts of at most 512 KB, ``saveBigFilePart``
  above 10 MB, at most ``upload_max_fileparts_default`` = 4000 parts (8000 with Premium), i.e. 2 GB
  (4 GB with Premium) per file (core.telegram.org/api/config).

The panel's own limits are lower still: :data:`PHOTO_MAX_BYTES` (5 MB), :data:`THUMB_MAX_BYTES`
(1 MB) and :data:`VIDEO_MAX_BYTES` (20 MB; the file is kept in the database until the post went out
and is uploaded in one piece by the worker).

**What the bytes are** (:func:`sniff`): the declared type is never trusted. A photo must be JPEG or
PNG by its magic bytes, with its size read from the file itself (JPEG SOF marker, PNG IHDR); a video
must be an MP4 (ISO base media file with an MP4 brand, not QuickTime) whose ``moov`` box gives its
duration and its picture size. Anything else is refused with a short code.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Final, Literal

PHOTO_MAX_BYTES: Final = 5 * 1024 * 1024
THUMB_MAX_BYTES: Final = 1024 * 1024
VIDEO_MAX_BYTES: Final = 20 * 1024 * 1024
# Telegram's hard limits (see the module doc); the panel's are below them.
TG_BOT_PHOTO_MAX: Final = 10 * 1024 * 1024
TG_BOT_FILE_MAX: Final = 50 * 1024 * 1024
TG_PHOTO_SIDES_MAX: Final = 10_000
TG_PHOTO_RATIO_MAX: Final = 20
MAX_VIDEO_SECONDS: Final = 3 * 3600

Mime = Literal["image/jpeg", "image/png", "video/mp4"]
_MP4_BRANDS: Final = frozenset(
    {
        b"isom",
        b"iso2",
        b"iso4",
        b"iso5",
        b"iso6",
        b"mp41",
        b"mp42",
        b"avc1",
        b"M4V ",
        b"dash",
        b"mmp4",
        b"MSNV",
    }
)


class MediaRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class MediaInfo:
    mime: Mime
    width: int | None
    height: int | None
    duration: int | None = None  # seconds (video)

    @property
    def is_image(self) -> bool:
        return self.mime != "video/mp4"


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9 or marker == 0xDA:  # end of image, or the image data before any size
            return None
        (length,) = struct.unpack(">H", data[i + 2 : i + 4])
        if length < 2:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if i + 9 > n:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + length
    return None


def _boxes(data: bytes, start: int, end: int) -> list[tuple[bytes, int, int]]:
    """(type, content start, content end) of the ISO-BMFF boxes in data[start:end]."""
    out: list[tuple[bytes, int, int]] = []
    i = start
    while i + 8 <= end and len(out) < 1000:
        size, kind = struct.unpack(">I4s", data[i : i + 8])
        header = 8
        if size == 1:
            if i + 16 > end:
                break
            (size,) = struct.unpack(">Q", data[i + 8 : i + 16])
            header = 16
        elif size == 0:
            size = end - i
        if size < header or i + size > end:
            break
        out.append((kind, i + header, i + size))
        i += size
    return out


def _mp4_info(data: bytes) -> tuple[int | None, int | None, int | None]:
    """(duration in seconds, width, height) from the moov box, or Nones."""
    top = _boxes(data, 0, len(data))
    moov = next(((s, e) for k, s, e in top if k == b"moov"), None)
    if moov is None:
        raise MediaRefused("video_no_moov")
    duration = width = height = None
    for kind, s, e in _boxes(data, *moov):
        if kind == b"mvhd" and e - s >= 20:
            version = data[s]
            if version == 1 and e - s >= 32:
                timescale, length = struct.unpack(">IQ", data[s + 20 : s + 32])
            else:
                timescale, length = struct.unpack(">II", data[s + 12 : s + 20])
            if timescale:
                duration = round(length / timescale)
        elif kind == b"trak" and width is None:
            for k2, s2, e2 in _boxes(data, s, e):
                if k2 == b"tkhd" and e2 - s2 >= 84:
                    w, h = struct.unpack(">II", data[e2 - 8 : e2])
                    if w >> 16 and h >> 16:
                        width, height = w >> 16, h >> 16
    return duration, width, height


def sniff(data: bytes) -> MediaInfo:
    """What ``data`` really is (see the module doc), or MediaRefused."""
    if data.startswith(b"\xff\xd8\xff"):
        size = _jpeg_size(data)
        if size is None:
            raise MediaRefused("image_broken")
        return _photo("image/jpeg", *size)
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 24 or data[12:16] != b"IHDR":
            raise MediaRefused("image_broken")
        width, height = struct.unpack(">II", data[16:24])
        return _photo("image/png", width, height)
    if len(data) >= 12 and data[4:8] == b"ftyp":
        if data[8:12] == b"qt  ":
            raise MediaRefused("video_not_mp4")
        brands = {data[8:12]} | {data[i : i + 4] for i in range(16, min(len(data), 64), 4)}
        if not brands & _MP4_BRANDS:
            raise MediaRefused("video_not_mp4")
        duration, width, height = _mp4_info(data)
        if duration is not None and duration > MAX_VIDEO_SECONDS:
            raise MediaRefused("video_too_long")
        return MediaInfo(mime="video/mp4", width=width, height=height, duration=duration)
    raise MediaRefused("unknown_type")


def _photo(mime: Mime, width: int, height: int) -> MediaInfo:
    if width < 1 or height < 1:
        raise MediaRefused("image_broken")
    if width + height > TG_PHOTO_SIDES_MAX or max(width, height) > TG_PHOTO_RATIO_MAX * min(width, height):
        raise MediaRefused("image_shape")
    return MediaInfo(mime=mime, width=width, height=height)


def check_for(kind: Literal["photo", "thumb", "video"], data: bytes) -> MediaInfo:
    """``data`` sniffed and checked for its role: a photo or thumbnail must be an image within its
    byte limit, a video an MP4 within :data:`VIDEO_MAX_BYTES`."""
    limit = {"photo": PHOTO_MAX_BYTES, "thumb": THUMB_MAX_BYTES, "video": VIDEO_MAX_BYTES}[kind]
    if not data:
        raise MediaRefused("empty")
    if len(data) > limit:
        raise MediaRefused("too_big")
    info = sniff(data)
    if (kind == "video") == info.is_image:
        raise MediaRefused("wrong_type")
    return info


_GENERIC_TYPES: Final = frozenset({"", "application/octet-stream", "binary/octet-stream"})


def declared_type_ok(kind: Literal["photo", "thumb", "video"], content_type: str) -> bool:
    """The server's own Content-Type must not contradict the role (an image for a photo or a
    thumbnail, a video for a video); a generic or missing type is left to :func:`sniff`."""
    if content_type in _GENERIC_TYPES:
        return True
    return content_type.startswith("video/" if kind == "video" else "image/")


def check_download(kind: Literal["photo", "thumb", "video"], content_type: str, data: bytes) -> MediaInfo:
    """A downloaded file: its declared type, then its real bytes (:func:`check_for`)."""
    if not declared_type_ok(kind, content_type):
        raise MediaRefused("wrong_type")
    return check_for(kind, data)


def file_name(info: MediaInfo) -> str:
    return {"image/jpeg": "photo.jpg", "image/png": "photo.png", "video/mp4": "video.mp4"}[info.mime]
