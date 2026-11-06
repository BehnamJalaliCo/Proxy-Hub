"""Video frames for the content supervisor (proxyhub/content/frames.py, decision D52).

* The pure parts: the sampling plan (timestamps), the exact argv of ffprobe and ffmpeg (no URL, a
  file-only protocol whitelist, the MOV demuxer forced, no shell), and the strict reading of
  ffprobe's answer (duration cap, one real video stream).
* The runner's limits with small local stand-in programs (our own scripts, not an imitation of
  ffmpeg's behaviour): a run over its time or its output cap is killed, the child gets a minimal
  environment, a 0700 working directory that is removed afterwards, and closed stdin.
* The real ffmpeg on a tiny MP4 made by ffmpeg itself (skipped without ffmpeg; CI and the test image
  set PROXYHUB_REQUIRE_FFMPEG=1, so there it must run).
"""

from __future__ import annotations

import itertools
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from test_content_support import mp4, png, real_mp4

from proxyhub.content import frames, media
from proxyhub.content.frames import FramesRefused, FramesUnavailable, Sampler, plan

VIDEO = Path("/run/proxyhub-frames-x/video.mp4")


# --- the plan --------------------------------------------------------------------------------------


def test_the_plan_spans_the_whole_video_from_the_first_second_to_the_last() -> None:
    assert plan(8.0) == [0.5, 1.9, 3.3, 4.7, 6.1, 7.5]
    long = plan(180.0)
    assert len(long) == frames.FRAME_COUNT and long[0] == 0.5 and long[-1] == 179.5
    assert long == sorted(long) and len(set(long)) == len(long)
    steps = {round(b - a, 3) for a, b in itertools.pairwise(long)}
    assert len(steps) == 1  # evenly spaced
    for length in (1.0, 2.5, 3.0, 8.0, 59.9, 180.0):
        times = plan(length)
        assert times[0] < 1.0 and times[-1] > length - 1.0, length  # the first and the last second
        assert all(0 <= t < length for t in times)


def test_a_short_clip_gets_about_one_frame_a_second() -> None:
    assert plan(0.6) == [0.3]
    assert plan(1.0) == [0.25, 0.75]
    assert len(plan(2.5)) == 3 and len(plan(5.0)) == 6
    assert plan(30.0, count=3) == [0.5, 15.0, 29.5]


@pytest.mark.parametrize("length", [0.0, -1.0, float("nan"), float("inf")])
def test_no_plan_without_a_real_duration(length: float) -> None:
    with pytest.raises(FramesRefused, match="video_unreadable"):
        plan(length)


# --- the command lines -----------------------------------------------------------------------------


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_ffprobe_reads_only_our_file_with_the_mov_demuxer() -> None:
    argv = frames.probe_argv("/usr/bin/ffprobe", VIDEO)
    assert argv[0] == "/usr/bin/ffprobe"
    assert _after(argv, "-protocol_whitelist") == "file"
    assert _after(argv, "-f") == "mov" and _after(argv, "-enable_drefs") == "0"
    assert _after(argv, "-i") == "file:/run/proxyhub-frames-x/video.mp4" and argv[-1] == "file:" + str(VIDEO)
    assert argv.index("-protocol_whitelist") < argv.index("-i") and argv.index("-f") < argv.index("-i")
    assert _after(argv, "-of") == "json" and _after(argv, "-max_alloc") == str(frames.MAX_ALLOC)
    flat = " ".join(argv)
    assert "http" not in flat and "pipe:" not in flat and "concat" not in flat


def test_ffmpeg_takes_one_scaled_jpeg_frame_of_one_stream() -> None:
    argv = frames.frame_argv("/usr/bin/ffmpeg", VIDEO, 12.3456, 0, quality=5)
    assert argv[0] == "/usr/bin/ffmpeg" and "-nostdin" in argv
    head = argv[: argv.index("-i")]  # input options: they bind the input
    assert _after(head, "-protocol_whitelist") == "file"
    assert _after(head, "-f") == "mov" and _after(head, "-enable_drefs") == "0"
    assert _after(head, "-codec_whitelist") == ",".join(frames.VIDEO_DECODERS)
    assert _after(head, "-ss") == "12.346" and _after(head, "-max_alloc") == str(frames.MAX_ALLOC)
    assert _after(argv, "-i") == "file:" + str(VIDEO)
    assert _after(argv, "-map") == "0:0" and _after(argv, "-frames:v") == "1"
    assert {"-an", "-sn", "-dn"} <= set(argv)
    assert _after(argv, "-vf") == "scale='min(768,iw)':'min(768,ih)':force_original_aspect_ratio=decrease"
    assert _after(argv, "-c:v") == "mjpeg" and _after(argv, "-q:v") == "5"
    assert argv[-3:] == ["-f", "image2pipe", "pipe:1"]  # the frame comes back on stdout only
    assert "aac" not in _after(head, "-codec_whitelist")  # only video decoders are opened
    small = frames.frame_argv("ffmpeg", VIDEO, 0, 3, quality=24, side=512)
    assert _after(small, "-map") == "0:3" and "min(512,iw)" in _after(small, "-vf")


@pytest.mark.parametrize(
    "path",
    ["https://cdn.example/x.mp4", "/run/x.mp4", "concat:/a|/b", Path("relative/video.mp4")],
)
def test_the_input_is_never_a_url_or_anything_but_our_absolute_path(path: Any) -> None:
    with pytest.raises(ValueError):
        frames.probe_argv("ffprobe", path)
    with pytest.raises(ValueError):
        frames.frame_argv("ffmpeg", path, 1.0, 0, quality=5)


@pytest.mark.parametrize(
    ("at", "stream", "quality", "side"),
    [(-1.0, 0, 5, 768), (float("nan"), 0, 5, 768), (1.0, -1, 5, 768), (1.0, 0, 1, 768), (1.0, 0, 5, 9000)],
)
def test_frame_requests_are_checked(at: float, stream: int, quality: int, side: int) -> None:
    with pytest.raises(ValueError):
        frames.frame_argv("ffmpeg", VIDEO, at, stream, quality=quality, side=side)


# --- ffprobe's answer --------------------------------------------------------------------------------


def probe_json(**over: Any) -> bytes:
    data: dict[str, Any] = {
        "streams": [
            {"index": 0, "codec_type": "video", "width": 1280, "height": 720, "duration": "30.000000",
             "disposition": {"attached_pic": 0}},
            {"index": 1, "codec_type": "audio", "duration": "31.000000", "disposition": {"attached_pic": 0}},
        ],
        "format": {"format_name": frames.MOV_DEMUXER, "duration": "31.000000"},
    }  # fmt: skip
    data.update(over)
    return json.dumps(data).encode()


MOV = {"format_name": frames.MOV_DEMUXER}


def test_the_probe_is_read_strictly() -> None:
    probe = frames.parse_probe(probe_json())
    assert (probe.duration, probe.length, probe.stream, probe.width, probe.height) == (
        31.0,
        30.0,
        0,
        1280,
        720,
    )
    cover = {
        "index": 0,
        "codec_type": "video",
        "width": 300,
        "height": 300,
        "disposition": {"attached_pic": 1},
    }
    video = {"index": 2, "codec_type": "video", "width": 640, "height": 360, "duration": "9.5"}
    probe = frames.parse_probe(probe_json(streams=[cover, video], format={"format_name": frames.MOV_DEMUXER}))
    assert (probe.stream, probe.length, probe.duration) == (2, 9.5, 9.5)  # the cover picture is skipped
    bad = [
        b"not json",
        b"[]",
        probe_json(format={"format_name": "matroska,webm", "duration": "5"}),
        probe_json(format={"format_name": "hls", "duration": "5"}),
        probe_json(streams=[{"index": 1, "codec_type": "audio", "duration": "5"}]),
        probe_json(streams=[{**video, "width": 0}]),
        probe_json(streams=[cover]),
        probe_json(streams=[{**video, "duration": "N/A"}], format={"format_name": frames.MOV_DEMUXER}),
        probe_json(streams=[{**video, "duration": "nan"}], format={**MOV, "duration": "inf"}),
    ]  # fmt: skip
    for raw in bad:
        with pytest.raises(FramesRefused, match="video_unreadable"):
            frames.parse_probe(raw)


def test_a_video_over_the_cap_is_too_long() -> None:
    long = probe_json(format={"format_name": frames.MOV_DEMUXER, "duration": "181.2"})
    with pytest.raises(FramesRefused, match="video_too_long"):
        frames.parse_probe(long)
    stream_long = probe_json(
        streams=[{"index": 0, "codec_type": "video", "width": 9, "height": 9, "duration": "200"}],
        format={"format_name": frames.MOV_DEMUXER, "duration": "20"},
    )
    with pytest.raises(FramesRefused, match="video_too_long"):
        frames.parse_probe(stream_long)
    assert frames.parse_probe(probe_json(), max_seconds=31).duration == 31.0
    with pytest.raises(FramesRefused, match="video_too_long"):
        frames.parse_probe(probe_json(), max_seconds=30)


# --- the runner, with local stand-in programs ---------------------------------------------------------


STUB = r"""
import json, os, sys, time
here = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(here, "mode")).read().strip()
with open(os.path.join(here, "seen.json"), "w") as f:
    json.dump({"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd(),
               "cwd_mode": oct(os.stat(os.getcwd()).st_mode & 0o777),
               "files": os.listdir(os.getcwd()), "stdin": sys.stdin.read(), "pid": os.getpid()}, f)
if mode == "sleep":
    time.sleep(60)
if mode == "flood":
    sys.stdout.buffer.write(b"\xff\xd8\xff" + b"\x00" * (4 * 1024 * 1024)); sys.stdout.flush()
    time.sleep(60)
if "-show_entries" in sys.argv:
    print(json.dumps({"streams": [{"index": 0, "codec_type": "video", "width": 64, "height": 36,
                                   "duration": "4.0"}],
                      "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "4.0"}}))
    sys.exit(0)
if mode == "fail":
    sys.stderr.write("secret metadata of the file\n" * 5000); sys.exit(1)
quality = int(sys.argv[sys.argv.index("-q:v") + 1])
sys.stdout.buffer.write(open(os.path.join(here, "frame.jpg"), "rb").read() + b"\x00" * (3000 // quality))
"""


def stand_in(directory: Path, mode: str, frame: bytes = b"") -> Path:
    script = directory / "ff"
    script.write_text(f"#!{sys.executable}\n{STUB}", encoding="utf-8")
    script.chmod(0o755)
    (directory / "mode").write_text(mode, encoding="utf-8")
    (directory / "frame.jpg").write_bytes(frame)
    return script


def seen(directory: Path) -> dict[str, Any]:
    found: dict[str, Any] = json.loads((directory / "seen.json").read_text())
    return found


def leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.startswith("proxyhub-frames-")]


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    return False


JPEG_64 = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffc0000b080024004001011100ffd9"
)  # SOI, APP0, SOF0 64x36, EOI: a JPEG by its bytes and size


def sampler(directory: Path, **kw: Any) -> Sampler:
    script = str(directory / "ff")
    return Sampler(ffmpeg=script, ffprobe=script, tmp_root=directory, **kw)


async def test_the_child_gets_our_file_only_a_minimal_environment_and_a_private_directory(
    tmp_path: Path,
) -> None:
    stand_in(tmp_path, "ok", JPEG_64)
    os.environ["PROXYHUB_SECRET_FOR_TEST"] = "do-not-leak"
    try:
        got = await sampler(tmp_path).sample(mp4(64, 36, 4))
    finally:
        del os.environ["PROXYHUB_SECRET_FOR_TEST"]
    assert [f.at for f in got.frames] == plan(4.0) and all(f.data.startswith(JPEG_64) for f in got.frames)
    assert (got.frames[0].width, got.frames[0].height, got.quality) == (64, 36, frames.QUALITIES[0])
    child = seen(tmp_path)
    assert child["env"] == frames.CHILD_ENV  # nothing of the worker's environment
    assert child["cwd_mode"] == "0o700" and child["files"] == ["video.mp4"] and child["stdin"] == ""
    assert Path(child["cwd"]).parent == tmp_path and child["argv"][-1] == "pipe:1"
    assert _after(child["argv"], "-i") == "file:" + str(Path(child["cwd"]) / "video.mp4")
    assert leftovers(tmp_path) == []  # the directory is removed afterwards


async def test_a_run_over_its_time_is_killed_and_the_video_is_unreadable(tmp_path: Path) -> None:
    stand_in(tmp_path, "sleep")
    started = time.monotonic()
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await sampler(tmp_path, probe_timeout_s=0.5).sample(mp4(64, 36, 4))
    assert time.monotonic() - started < 10
    assert gone(seen(tmp_path)["pid"])
    assert leftovers(tmp_path) == []


async def test_the_whole_sampling_has_its_own_deadline(tmp_path: Path) -> None:
    stand_in(tmp_path, "sleep")
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await sampler(tmp_path, total_timeout_s=0.5).sample(mp4(64, 36, 4))
    assert gone(seen(tmp_path)["pid"])


async def test_output_over_the_cap_is_killed(tmp_path: Path) -> None:
    stand_in(tmp_path, "flood")
    started = time.monotonic()
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await sampler(tmp_path).sample(mp4(64, 36, 4))  # ffprobe's answer over 64 KiB
    assert time.monotonic() - started < 10 and gone(seen(tmp_path)["pid"])


async def test_a_failing_ffmpeg_rejects_and_its_stderr_is_dropped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    stand_in(tmp_path, "fail")
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await sampler(tmp_path).sample(mp4(64, 36, 4))
    assert "secret metadata" not in caplog.text


async def test_frames_that_do_not_fit_get_a_lower_quality_then_reject(tmp_path: Path) -> None:
    # The stand-in's frame is the JPEG plus 3000 // quality bytes: smaller at a lower quality.
    stand_in(tmp_path, "ok", JPEG_64)
    best = await sampler(tmp_path).sample(mp4(64, 36, 4))
    assert best.quality == 5 and best.size == 5 * (len(JPEG_64) + 600)
    fitted = await sampler(tmp_path, max_total=5 * (len(JPEG_64) + 200)).sample(mp4(64, 36, 4))
    assert fitted.quality == 15 and fitted.size == 5 * (len(JPEG_64) + 200)
    one_big = await sampler(tmp_path, max_frame=len(JPEG_64) + 400).sample(mp4(64, 36, 4))
    assert one_big.quality == 9  # a single frame over its cap also lowers the quality
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await sampler(tmp_path, max_total=len(JPEG_64) * 5).sample(mp4(64, 36, 4))
    assert _after(seen(tmp_path)["argv"], "-q:v") == str(frames.QUALITIES[-1])  # tried down to the lowest


async def test_a_frame_that_is_not_a_small_jpeg_rejects(tmp_path: Path) -> None:
    for frame in (
        png(64, 36),
        b"\xff\xd8\xff garbage",
        JPEG_64.replace(bytes.fromhex("00240040"), bytes.fromhex("04000400")),
    ):
        stand_in(tmp_path, "ok", frame)
        with pytest.raises(FramesRefused, match="video_unreadable"):
            await sampler(tmp_path).sample(mp4(64, 36, 4))  # fmt: skip


async def test_what_is_not_our_mp4_never_reaches_ffmpeg(tmp_path: Path) -> None:
    stand_in(tmp_path, "ok", JPEG_64)
    for data, code in (
        (png(8, 8), "video_unreadable"),
        (b"", "video_unreadable"),
        (mp4(64, 36, 4, brand=b"qt  "), "video_unreadable"),
        (mp4(64, 36, 4) + b"\x00" * media.VIDEO_MAX_BYTES, "video_unreadable"),
        (mp4(64, 36, 200), "video_too_long"),
        (mp4(64, 36, 4 * 3600), "video_too_long"),
    ):
        with pytest.raises(FramesRefused) as err:
            await sampler(tmp_path).sample(data)
        assert err.value.code == code
    assert not (tmp_path / "seen.json").exists()


async def test_no_ffmpeg_is_the_panels_fault_not_the_candidates(tmp_path: Path) -> None:
    with pytest.raises(FramesUnavailable):
        await Sampler(ffmpeg=str(tmp_path / "missing"), ffprobe=str(tmp_path / "missing")).sample(
            mp4(64, 36, 4)
        )


# --- the real ffmpeg -----------------------------------------------------------------------------------


async def test_real_ffmpeg_takes_six_frames_across_a_real_video(tmp_path: Path) -> None:
    data = real_mp4(8, 320, 180)
    assert media.check_for("video", data).duration == 8
    got = await Sampler(tmp_root=tmp_path).sample(data)
    assert [f.at for f in got.frames] == plan(8.0) == [0.5, 1.9, 3.3, 4.7, 6.1, 7.5]
    assert got.duration == pytest.approx(8.0, abs=0.2) and got.quality == frames.QUALITIES[0]
    for frame in got.frames:
        info = media.sniff(frame.data)
        assert info.mime == "image/jpeg" and (info.width, info.height) == (frame.width, frame.height) == (
            320,
            180,
        )
    assert len({f.data for f in got.frames}) == len(got.frames)  # the pattern moves: six different pictures
    assert got.size <= frames.MAX_TOTAL_BYTES
    smaller = await Sampler(tmp_root=tmp_path, max_total=got.size - 1).sample(data)
    assert smaller.quality > got.quality and smaller.size < got.size and len(smaller.frames) == 6
    assert leftovers(tmp_path) == []  # the temporary directory is gone


async def test_real_ffmpeg_scales_a_large_video_down(tmp_path: Path) -> None:
    got = await Sampler(tmp_root=tmp_path).sample(real_mp4(3, 1280, 720, 2))
    assert len(got.frames) == 4 and {(f.width, f.height) for f in got.frames} == {(768, 432)}
    portrait = await Sampler(tmp_root=tmp_path, side=256).sample(real_mp4(2, 180, 320, 2))
    assert {(f.width, f.height) for f in portrait.frames} == {(144, 256)}


async def test_real_ffmpeg_rejects_a_long_or_unreadable_video(tmp_path: Path) -> None:
    data = real_mp4(8, 320, 180)
    with pytest.raises(FramesRefused, match="video_too_long"):
        await Sampler(tmp_root=tmp_path, max_seconds=5).sample(data)
    # An MP4 by its header (ftyp, moov with a duration) whose track ffprobe cannot read.
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await Sampler(tmp_root=tmp_path).sample(mp4(640, 360, 12))
    # A real video whose sample data is destroyed after the header.
    broken = data[: len(data) // 3] + b"\x00" * (len(data) - len(data) // 3)
    with pytest.raises(FramesRefused, match="video_unreadable"):
        await Sampler(tmp_root=tmp_path).sample(broken)
    assert leftovers(tmp_path) == []


async def test_real_ffmpeg_the_temporary_directory_is_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    modes: list[int] = []
    real_run = frames.run

    async def watching(argv: list[str], *, cwd: Path, timeout_s: float, limit: int) -> tuple[int, bytes]:
        modes.append(mode_of(cwd))
        assert mode_of(cwd / "video.mp4") == 0o600
        return await real_run(argv, cwd=cwd, timeout_s=timeout_s, limit=limit)

    monkeypatch.setattr(frames, "run", watching)
    got = await Sampler(tmp_root=tmp_path).sample(real_mp4(8, 320, 180))
    assert len(got.frames) == 6 and set(modes) == {0o700} and len(modes) == 7  # ffprobe and six frames
