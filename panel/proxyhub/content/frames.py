"""Frames of a candidate's video for the content supervisor (decision D52): Claude judges a video by
:data:`FRAME_COUNT` frames sampled across its whole length, in order, not by its cover alone.

**Why.** A video can show what its cover does not: gore or nudity in the middle, another channel's
watermark that appears later, an advert at the end. D52 has no owner behind the supervisor, so the
reviewer must see the whole video.

**How** (:meth:`Sampler.sample`): the video's bytes (downloaded by the panel itself and checked as MP4
by proxyhub/content/media.py, at most :data:`~proxyhub.content.media.VIDEO_MAX_BYTES`) are written to
a fresh temporary directory (mode 0700); ffprobe reads the duration; ffmpeg takes
:data:`FRAME_COUNT` frames evenly spaced from the first second to the last second, both included
(:func:`plan`), each scaled down to at most :data:`MAX_SIDE` px, as JPEG. When the frames together
are over :data:`MAX_TOTAL_BYTES` (the room the bridge's request has, deploy/claude-bridge/bridge.py)
they are taken again at a lower JPEG quality (:data:`QUALITIES`). Outcomes, both a rejection by the
supervisor without asking Claude:

* ``video_too_long``: longer than :data:`MAX_SECONDS` (by the file's own header or by ffprobe);
* ``video_unreadable``: not an MP4 the panel accepts, ffprobe or ffmpeg cannot read it, a frame is
  missing, a run is over its time, or the frames do not fit even at the lowest quality.

**Running ffmpeg on untrusted bytes:**

* an argv, never a shell (:func:`probe_argv`, :func:`frame_argv`): fixed flags, numbers computed
  here, and our own file as ``file:<absolute path>``; never a URL, never anything of the source;
* ``-protocol_whitelist file``: the only protocol ffmpeg may open is a local file (no http, no tcp,
  no pipe, no concat or subfile); ``-f mov`` forces the MP4/MOV demuxer, so no other format is
  probed (an HLS playlist, an image sequence or a concat list that names other files), and
  ``-enable_drefs 0`` keeps it from following a track's reference to another file; ffprobe must
  report the MOV demuxer (:data:`MOV_DEMUXER`) and one real video stream;
* the decoding step may open only the usual video decoders of MP4 (``-codec_whitelist``,
  :data:`VIDEO_DECODERS`), takes only that one stream (``-map``; no audio, subtitle or data
  stream) and allocates at most :data:`MAX_ALLOC` at once (``-max_alloc``, ffprobe too);
* a timeout per run (:data:`PROBE_TIMEOUT_S`, :data:`FRAME_TIMEOUT_S`) and for the whole sampling
  (:data:`TOTAL_TIMEOUT_S`); a run over its time or its output cap is killed with its process group;
* caps: the input (:data:`~proxyhub.content.media.VIDEO_MAX_BYTES`, MP4 by its bytes), ffprobe's
  output (:data:`MAX_PROBE_BYTES`), one frame (:data:`MAX_FRAME_BYTES`) and all frames
  (:data:`MAX_TOTAL_BYTES`); every frame is checked as a JPEG of at most :data:`MAX_SIDE` px;
* the child gets stdin closed, its own session, the temporary directory as its working directory and
  a minimal environment (:data:`CHILD_ENV`: nothing of the worker's); stderr is read and dropped,
  never logged (it could echo the file's metadata). The directory is removed afterwards, always.

The binaries come from the panel image (Debian's ``ffmpeg`` package, panel/Dockerfile). A missing
binary is :class:`FramesUnavailable` (the panel's problem, not the candidate's).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shutil
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from proxyhub.content import media

FRAME_COUNT: Final = 6
MAX_SIDE: Final = 768
MAX_SECONDS: Final = 180
EDGE_S: Final = 0.5  # the first frame this far from the start, the last this far from the end
EARLIER_S: Final = (1.0, 3.0)  # a frame that does not come out is tried this much earlier
QUALITIES: Final = (5, 9, 15, 24)  # ffmpeg's -q:v for JPEG: 2 (best) to 31
MAX_FRAME_BYTES: Final = 1024 * 1024
MAX_TOTAL_BYTES: Final = 3 * 1024 * 1024
MAX_PROBE_BYTES: Final = 64 * 1024
MAX_ALLOC: Final = 256 * 1024 * 1024
THREADS: Final = 2
PROBE_TIMEOUT_S: Final = 15.0
FRAME_TIMEOUT_S: Final = 30.0
TOTAL_TIMEOUT_S: Final = 120.0
MOV_DEMUXER: Final = "mov,mp4,m4a,3gp,3g2,mj2"
VIDEO_DECODERS: Final = ("h264", "hevc", "mpeg4", "av1", "libdav1d", "libaom-av1", "vp9", "libvpx-vp9")
CHILD_ENV: Final = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LC_ALL": "C"}


class FramesRefused(Exception):
    """The video cannot be judged: ``video_too_long`` or ``video_unreadable``. The candidate is rejected."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class FramesUnavailable(Exception):
    """ffmpeg or ffprobe is missing or cannot start: a fault of the panel, not of the candidate."""

    def __init__(self, code: str = "ffmpeg_missing") -> None:
        super().__init__(code)
        self.code = code


class TooSlow(Exception):
    """A run of ffmpeg or ffprobe over its time (it was killed)."""


class TooBig(Exception):
    """A run of ffmpeg or ffprobe over its output cap (it was killed)."""


@dataclass(frozen=True)
class Probe:
    duration: float  # seconds: the file's
    length: float  # seconds: the video stream's own (frames are taken within it)
    stream: int  # the video stream's index
    width: int
    height: int


@dataclass(frozen=True)
class Frame:
    at: float  # seconds from the start
    data: bytes  # JPEG
    width: int
    height: int


@dataclass(frozen=True)
class Sampled:
    frames: tuple[Frame, ...]
    duration: float
    quality: int

    @property
    def size(self) -> int:
        return sum(len(f.data) for f in self.frames)


# --- the plan and the command lines (pure) -------------------------------------------------------


def plan(length: float, count: int = FRAME_COUNT) -> list[float]:
    """When the frames are taken (seconds): ``count`` evenly spaced, the first :data:`EDGE_S` after
    the start and the last as long before the end (both inside the first and the last second), and
    at most about one per second of video (a 2-second clip gets 3)."""
    if not isinstance(length, int | float) or not math.isfinite(length) or length <= 0 or count < 1:
        raise FramesRefused("video_unreadable")
    n = max(1, min(count, int(length) + 1))
    if n == 1:
        return [round(length / 2, 3)]
    edge = min(EDGE_S, length / 4)
    step = (length - 2 * edge) / (n - 1)
    return [round(edge + i * step, 3) for i in range(n)]


def _input(path: Path) -> str:
    """Our own file, as ffmpeg's ``file:`` protocol: an absolute path, never a URL."""
    if not isinstance(path, Path) or not path.is_absolute():
        raise ValueError("ffmpeg reads only an absolute path of our own")
    return f"file:{path}"


def probe_argv(ffprobe: str, path: Path) -> list[str]:
    """ffprobe: the file's demuxer and duration, and each stream's kind, size and duration (JSON)."""
    return [
        ffprobe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-max_alloc",
        str(MAX_ALLOC),
        "-protocol_whitelist",
        "file",
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-show_entries",
        "format=format_name,duration:stream=index,codec_type,width,height,duration"
        ":stream_disposition=attached_pic",
        "-of",
        "json",
        "-i",
        _input(path),
    ]


def frame_argv(
    ffmpeg: str, path: Path, at: float, stream: int, *, quality: int, side: int = MAX_SIDE
) -> list[str]:
    """ffmpeg: ONE frame of stream ``stream`` at ``at`` seconds, at most ``side`` px, as JPEG on stdout."""
    if not math.isfinite(at) or at < 0 or not isinstance(stream, int) or stream < 0:
        raise ValueError("bad frame request")
    if not 2 <= quality <= 31 or not 16 <= side <= 4096:
        raise ValueError("bad frame request")
    return [
        ffmpeg,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-max_alloc",
        str(MAX_ALLOC),
        "-threads",
        str(THREADS),
        "-protocol_whitelist",
        "file",
        "-codec_whitelist",
        ",".join(VIDEO_DECODERS),
        "-f",
        "mov",
        "-enable_drefs",
        "0",
        "-ss",
        f"{at:.3f}",
        "-i",
        _input(path),
        "-map",
        f"0:{stream}",
        "-an",
        "-sn",
        "-dn",
        "-frames:v",
        "1",
        "-vf",
        f"scale='min({side},iw)':'min({side},ih)':force_original_aspect_ratio=decrease",
        "-c:v",
        "mjpeg",
        "-q:v",
        str(quality),
        "-f",
        "image2pipe",
        "pipe:1",
    ]


def _seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _positive(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def parse_probe(raw: bytes, max_seconds: float = MAX_SECONDS) -> Probe:
    """ffprobe's JSON -> :class:`Probe`, or FramesRefused: the MOV demuxer, one real video stream
    (not a cover picture) with a size, a duration, and not longer than ``max_seconds``."""
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise FramesRefused("video_unreadable") from None
    fmt = data.get("format") if isinstance(data, dict) else None
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(fmt, dict) or fmt.get("format_name") != MOV_DEMUXER or not isinstance(streams, list):
        raise FramesRefused("video_unreadable")
    video: dict[str, Any] | None = None
    for s in streams:
        if not isinstance(s, dict) or s.get("codec_type") != "video":
            continue
        disposition = s.get("disposition")
        if isinstance(disposition, dict) and disposition.get("attached_pic"):
            continue
        index = s.get("index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            if _positive(s.get("width")) and _positive(s.get("height")):
                video = s
                break
    if video is None:
        raise FramesRefused("video_unreadable")
    duration = _seconds(fmt.get("duration"))
    length = _seconds(video.get("duration")) or duration
    duration = duration or length
    if duration is None or length is None:
        raise FramesRefused("video_unreadable")
    if max(duration, length) > max_seconds:
        raise FramesRefused("video_too_long")
    return Probe(
        duration=duration,
        length=min(length, duration),
        stream=int(video["index"]),
        width=int(video["width"]),
        height=int(video["height"]),
    )


# --- running ffmpeg ------------------------------------------------------------------------------------


def _kill(proc: asyncio.subprocess.Process) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()


async def _drain(stream: asyncio.StreamReader) -> None:
    while await stream.read(65536):
        pass  # dropped, never logged: it could echo the file's metadata


async def run(argv: list[str], *, cwd: Path, timeout_s: float, limit: int) -> tuple[int, bytes]:
    """One ffmpeg or ffprobe (see the module doc): (exit code, stdout). Raises :class:`TooSlow` or
    :class:`TooBig` after killing a run over its time or its output cap, and
    :class:`FramesUnavailable` when the binary cannot start."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=dict(CHILD_ENV),
            start_new_session=True,
        )
    except OSError:
        raise FramesUnavailable() from None
    assert proc.stdout is not None and proc.stderr is not None
    stderr = asyncio.create_task(_drain(proc.stderr))
    out = bytearray()
    try:
        async with asyncio.timeout(timeout_s):
            while chunk := await proc.stdout.read(65536):
                out += chunk
                if len(out) > limit:
                    raise TooBig
            code = await proc.wait()
    except TimeoutError:
        raise TooSlow from None
    finally:
        if proc.returncode is None:
            _kill(proc)
            await proc.wait()
        stderr.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await stderr
    return code, bytes(out)


@dataclass(frozen=True)
class Sampler:
    """Takes a video's frames (see the module doc). The fields are the limits; tests make them small."""

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    count: int = FRAME_COUNT
    side: int = MAX_SIDE
    max_seconds: float = MAX_SECONDS
    max_total: int = MAX_TOTAL_BYTES
    max_frame: int = MAX_FRAME_BYTES
    qualities: tuple[int, ...] = QUALITIES
    probe_timeout_s: float = PROBE_TIMEOUT_S
    frame_timeout_s: float = FRAME_TIMEOUT_S
    total_timeout_s: float = TOTAL_TIMEOUT_S
    tmp_root: Path | None = None

    def binaries(self) -> tuple[str, str]:
        ffmpeg, ffprobe = shutil.which(self.ffmpeg), shutil.which(self.ffprobe)
        if ffmpeg is None or ffprobe is None:
            raise FramesUnavailable()
        return ffmpeg, ffprobe

    async def sample(self, data: bytes) -> Sampled:
        """The frames of ``data`` (an MP4), or FramesRefused / FramesUnavailable."""
        try:
            info = media.check_for("video", data)
        except media.MediaRefused as exc:
            raise FramesRefused(
                "video_too_long" if exc.code == "video_too_long" else "video_unreadable"
            ) from None
        if info.duration is not None and info.duration > self.max_seconds:
            raise FramesRefused("video_too_long")
        ffmpeg, ffprobe = self.binaries()
        tmp = Path(tempfile.mkdtemp(prefix="proxyhub-frames-", dir=self.tmp_root))
        try:
            os.chmod(tmp, 0o700)
            path = tmp / "video.mp4"
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            async with asyncio.timeout(self.total_timeout_s):
                probe = await self._probe(ffprobe, path, tmp)
                return await self._frames(ffmpeg, path, tmp, probe)
        except (TimeoutError, TooSlow, TooBig):
            raise FramesRefused("video_unreadable") from None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def _probe(self, ffprobe: str, path: Path, tmp: Path) -> Probe:
        code, out = await run(
            probe_argv(ffprobe, path), cwd=tmp, timeout_s=self.probe_timeout_s, limit=MAX_PROBE_BYTES
        )
        if code != 0:
            raise FramesRefused("video_unreadable")
        return parse_probe(out, self.max_seconds)

    async def _frames(self, ffmpeg: str, path: Path, tmp: Path, probe: Probe) -> Sampled:
        times = plan(probe.length, self.count)
        for quality in self.qualities:
            frames: list[Frame] = []
            total = 0
            try:
                for at in times:
                    frame = await self._frame(ffmpeg, path, tmp, probe.stream, at, quality)
                    frames.append(frame)
                    total += len(frame.data)
                    if total > self.max_total:
                        break
            except TooBig:  # one frame over its cap: try a lower quality
                continue
            if total <= self.max_total:
                return Sampled(frames=tuple(frames), duration=probe.duration, quality=quality)
        raise FramesRefused("video_unreadable")

    async def _frame(self, ffmpeg: str, path: Path, tmp: Path, stream: int, at: float, quality: int) -> Frame:
        """One frame at ``at``, or a little earlier when nothing comes out there (the last frame of a
        stream can end before the file); none at all is FramesRefused."""
        tries = dict.fromkeys(round(t, 3) for t in (at, *(max(0.0, at - back) for back in EARLIER_S)))
        for when in tries:
            argv = frame_argv(ffmpeg, path, when, stream, quality=quality, side=self.side)
            code, out = await run(argv, cwd=tmp, timeout_s=self.frame_timeout_s, limit=self.max_frame)
            if code != 0:
                raise FramesRefused("video_unreadable")
            if out:
                return _checked(out, when, self.side)
        raise FramesRefused("video_unreadable")


def _checked(data: bytes, at: float, side: int) -> Frame:
    """A frame must be a JPEG (by its bytes) within the size it was scaled to."""
    try:
        info = media.sniff(data)
    except media.MediaRefused:
        raise FramesRefused("video_unreadable") from None
    if info.mime != "image/jpeg" or info.width is None or info.height is None:
        raise FramesRefused("video_unreadable")
    if max(info.width, info.height) > side:
        raise FramesRefused("video_unreadable")
    return Frame(at=at, data=data, width=info.width, height=info.height)
