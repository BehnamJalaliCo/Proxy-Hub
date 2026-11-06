"""The channel's own animated emoji pack (D38): internet and operator icons, drawn here in code.

No public pack fits the channel (Iranian internet and operators), and a pack made by someone else
shows THEIR title (often another channel's ad) when a reader taps an emoji. So the panel makes its
own pack, titled with the channel's @username, from the drawings in this module
(proxyhub/posts/own_pack.py uploads them through the bot).

Every drawing is a Lottie animation, sent as a ``.tgs`` file (gzip of the Lottie JSON). Telegram's
rules for it (core.telegram.org/stickers, "Animation Requirements"), all checked by
:func:`problems` and by the tests:

* canvas 512x512, 60 frames per second, at most 3 seconds, looped (the first and the last frame
  are the same picture), at most 64 KB;
* no expressions, masks, layer effects, images, solids, texts, 3D layers, merge paths, star
  shapes, gradient strokes, repeaters, time stretching, time remapping or auto-oriented layers;
* nothing leaves the canvas: in EVERY frame, with its stroke widths and every scale, turn and move
  of its groups, a drawing stays inside :data:`SAFE_MIN`..:data:`SAFE_MAX` (:func:`frame_box`
  computes what a frame really draws).

How a drawing reads at text size, beside a normal emoji (:func:`look_problems`, also in the tests).
The first pack (version 1) broke these, so in posts its emoji looked small or "half" next to a
normal one: most drawings spanned 55 to 84% of the canvas (🔒 70% tall and 52% wide), some sat off
centre, and parts faded to 25% or 0% for part of every loop (📡's waves, 📶's bars, 🔋's cells) or
flashed as a faint disc (⚡'s glow, 0 to 38%). Nothing left the canvas: rendered with rlottie
(Telegram's renderer) frame by frame, every drawing kept 25 px or more inside it.

* :func:`emoji` scales and centres every drawing so that what it draws over the whole loop spans
  :data:`FIT_SIZE` (about 90% of the canvas, like a font's emoji), and no frame shrinks below
  :data:`MIN_FRAME_SIZE`;
* no fill or stroke ever drops below :data:`MIN_OPACITY` percent (its own opacity times its groups'),
  except a light "shine" drawn over the glyph; an animation dims, changes colour or moves instead;
* no trim paths: a half-drawn line looks like a cut-off emoji.

The look: Persian turquoise and gold with a dark navy, solid shapes with a darker edge, so an
emoji still reads at text size on a light or a dark chat background. The operator SIM cards use
each operator's colour only, never a logo or a name.

``alt`` is the plain emoji a drawing stands for. The poster swaps a plain emoji in a post for the
drawing with the same alt (proxyhub/posts/entities.py), so the alts below are the emoji the post
templates use (📡 🔐 🔗 ⚡ ⏳ ⌛ 🔴 🚫 ⏸ ♻️) and the ones the channel's texts use most.

The output is deterministic: the same code gives the same bytes (gzip without a timestamp), so
the pack's contents only change when this file does. Telegram never replaces an emoji of a pack
(and the panel never asks it to, D43), so a changed drawing goes into a NEW pack:
**bump :data:`ART_VERSION` whenever a drawing changes.** own_pack.py then builds the new version
beside the old one and moves the posts over once it is complete.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
import itertools
import json
import math
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Any, Final

FPS: Final = 60
CANVAS: Final = 512
FRAMES: Final = 120  # 2 seconds; Telegram allows 3 (180 frames)
MAX_FRAMES: Final = 180
MAX_TGS_BYTES: Final = 64 * 1024
SAFE_MIN: Final = 24
SAFE_MAX: Final = CANVAS - SAFE_MIN
CENTER: Final = CANVAS / 2
# What a drawing spans over its whole loop (its larger side, strokes included), centred: about 90%
# of the canvas, as much as a font's emoji fills its square. 2 px inside the safe area on each side.
FIT_SIZE: Final = SAFE_MAX - SAFE_MIN - 4
# No frame of the loop is smaller than this (larger side), so the emoji never looks shrunken.
MIN_FRAME_SIZE: Final = 0.7 * CANVAS
# No part of a glyph fades below this (percent); only a "shine" over it may be lighter.
MIN_OPACITY: Final = 60
SHINE: Final = "shine"
# The pack's version: a new number makes the panel build a new pack (own_pack.py). Bump it whenever
# a drawing changes; version 1 is the first pack (2026-11-05), whose emoji looked small and "half".
ART_VERSION: Final = 2

Json = dict[str, Any]
Point = tuple[float, float]

# --- palette ------------------------------------------------------------------------------------
NAVY: Final = "#17324D"
STEEL: Final = "#6E8196"
TOWER: Final = "#7D93AB"  # light enough for a dark chat, dark enough for a light one
STEEL_D: Final = "#3F5266"
TURQ: Final = "#1FB5AD"
TURQ_D: Final = "#0E7A7E"
TURQ_L: Final = "#8FE8DE"
GOLD: Final = "#F5B731"
GOLD_D: Final = "#B97C0C"
GOLD_L: Final = "#FFE59A"
RED: Final = "#E5484D"
RED_D: Final = "#A3262B"
GREEN: Final = "#2DBE6C"
GREEN_D: Final = "#17804A"
LAPIS: Final = "#2F62D6"
LAPIS_D: Final = "#1D3F94"
WHITE: Final = "#FFFFFF"
GLASS: Final = "#E3F7F5"
EMPTY: Final = "#C9D3DE"  # an empty battery cell
# Operator colours only (no logos, no names): Irancell yellow, MCI blue, Rightel purple.
OP_YELLOW: Final = "#FFC61A"
OP_YELLOW_D: Final = "#B98A00"
OP_BLUE: Final = "#119FDD"
OP_BLUE_D: Final = "#0A6799"
OP_PURPLE: Final = "#8E2C91"
OP_PURPLE_D: Final = "#581A5B"


def _n(x: float) -> float | int:
    v = round(float(x), 2)
    return int(v) if v == int(v) else v


def rgba(hex_color: str) -> list[float | int]:
    h = hex_color.lstrip("#")
    return [_n(int(h[i : i + 2], 16) / 255) for i in (0, 2, 4)] + [1]


# --- paths --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Path:
    """A Bezier path: vertices with in/out tangents relative to their vertex (Lottie's form)."""

    v: tuple[Point, ...]
    i: tuple[Point, ...]
    o: tuple[Point, ...]
    closed: bool

    def json(self) -> Json:
        return {
            "i": [[_n(x), _n(y)] for x, y in self.i],
            "o": [[_n(x), _n(y)] for x, y in self.o],
            "v": [[_n(x), _n(y)] for x, y in self.v],
            "c": self.closed,
        }


def poly(*pts: Point, closed: bool = True) -> Path:
    zero = tuple((0.0, 0.0) for _ in pts)
    return Path(tuple(pts), zero, zero, closed)


def bez(*nodes: tuple[Point, Point, Point], closed: bool = True) -> Path:
    """Nodes of ``(vertex, in_tangent, out_tangent)``."""
    return Path(tuple(n[0] for n in nodes), tuple(n[1] for n in nodes), tuple(n[2] for n in nodes), closed)


def arc(cx: float, cy: float, r: float, a0: float, a1: float) -> Path:
    """An open circular arc from angle ``a0`` to ``a1`` (degrees; 0 = right, 90 = down)."""
    span = a1 - a0
    parts = max(1, math.ceil(abs(span) / 90))
    step = math.radians(span / parts)
    k = 4 / 3 * math.tan(step / 4) * r
    angles = [math.radians(a0) + step * n for n in range(parts + 1)]
    v = [(cx + r * math.cos(a), cy + r * math.sin(a)) for a in angles]
    i = [(k * math.sin(a), -k * math.cos(a)) for a in angles]
    o = [(-k * math.sin(a), k * math.cos(a)) for a in angles]
    i[0] = (0.0, 0.0)
    o[-1] = (0.0, 0.0)
    return Path(tuple(v), tuple(i), tuple(o), False)


# --- animated values ----------------------------------------------------------------------------
EASE: Final = ({"x": [0.42], "y": [0]}, {"x": [0.58], "y": [1]})
LINEAR: Final = ({"x": [0], "y": [0]}, {"x": [1], "y": [1]})


@dataclass(frozen=True)
class Anim:
    """Keyframes ``(frame, value, ease)``; ease is "ease", "linear" or "hold" (to the next key)."""

    keys: tuple[tuple[float, Any, str], ...]

    def json(self) -> Json:
        out: list[Json] = []
        for n, (t, value, ease) in enumerate(self.keys):
            key: Json = {"t": _n(t), "s": _kval(value)}
            if n < len(self.keys) - 1:
                if ease == "hold":
                    key["h"] = 1
                else:
                    o, i = EASE if ease == "ease" else LINEAR
                    key["o"] = o
                    key["i"] = i
            out.append(key)
        return {"a": 1, "k": out}

    def values(self) -> list[Any]:
        return [v for _, v, _ in self.keys]


def anim(*keys: tuple[Any, ...], ease: str = "ease") -> Anim:
    return Anim(tuple((k[0], k[1], k[2] if len(k) == 3 else ease) for k in keys))


def loop(*keys: tuple[Any, ...], ease: str = "ease") -> Anim:
    """Keyframes that end on the first value at :data:`FRAMES` (a seamless loop)."""
    first = keys[0][1]
    return anim(*keys, (FRAMES, first), ease=ease)


def tint(*keys: tuple[Any, ...], ease: str = "ease") -> Anim:
    """A looped colour: keys of ``(frame, "#RRGGBB"[, ease])``."""
    return loop(*((k[0], rgba(k[1]), *k[2:]) for k in keys), ease=ease)


def _kval(value: Any) -> Any:
    if isinstance(value, Path):
        return [value.json()]
    if isinstance(value, list | tuple):
        return [_n(x) for x in value]
    return [_n(value)]


def prop(value: Any) -> Json:
    if isinstance(value, Anim):
        return value.json()
    if isinstance(value, Path):
        return {"a": 0, "k": value.json()}
    if isinstance(value, list | tuple):
        return {"a": 0, "k": [_n(x) for x in value]}
    return {"a": 0, "k": _n(value)}


# --- shapes and styles --------------------------------------------------------------------------
def rect(cx: float, cy: float, w: Any, h: float | None = None, r: float = 0) -> Json:
    size = w if isinstance(w, Anim) else [w, h]
    return {"ty": "rc", "d": 1, "p": prop([cx, cy]), "s": prop(size), "r": prop(r)}


def ellipse(cx: float, cy: float, w: Any, h: float | None = None) -> Json:
    size = w if isinstance(w, Anim) else [w, h if h is not None else w]
    return {"ty": "el", "d": 1, "p": prop([cx, cy]), "s": prop(size)}


def shape(path: Path | Anim) -> Json:
    return {"ty": "sh", "d": 1, "ks": prop(path)}


def _color(color: str | Anim) -> Json:
    return prop(color if isinstance(color, Anim) else rgba(color))


def fill(color: str | Anim, opacity: Any = 100) -> Json:
    return {"ty": "fl", "c": _color(color), "o": prop(opacity), "r": 1, "bm": 0}


def stroke(color: str | Anim, width: float, opacity: Any = 100, *, round_cap: bool = True) -> Json:
    return {
        "ty": "st",
        "c": _color(color),
        "o": prop(opacity),
        "w": prop(width),
        "lc": 2 if round_cap else 1,
        "lj": 2,
        "ml": 4,
        "bm": 0,
    }


def transform(
    pivot: Point = (CENTER, CENTER),
    *,
    pos: Any = None,
    scale: Any = 100,
    rot: Any = 0,
    opacity: Any = 100,
) -> Json:
    if isinstance(scale, int | float):
        scale = [scale, scale]
    return {
        "ty": "tr",
        "p": prop(pos if pos is not None else list(pivot)),
        "a": prop(list(pivot)),
        "s": prop(scale),
        "r": prop(rot),
        "o": prop(opacity),
        "sk": prop(0),
        "sa": prop(0),
    }


def group(*items: Json, t: Json | None = None, name: str = "g") -> Json:
    """Items top first: a style applies to the shapes listed before it."""
    return {"ty": "gr", "it": [*items, t or transform()], "nm": name, "bm": 0}


def emoji(name: str, *back_to_front: Json) -> Json:
    """One shape layer (groups given back to front), scaled and centred to :data:`FIT_SIZE`."""
    layer = {
        "ddd": 0,
        "ind": 1,
        "ty": 4,
        "nm": name,
        "sr": 1,
        "ks": {
            "o": prop(100),
            "r": prop(0),
            "p": prop([0, 0, 0]),
            "a": prop([0, 0, 0]),
            "s": prop([100, 100, 100]),
        },
        "ao": 0,
        "shapes": list(reversed(back_to_front)),
        "ip": 0,
        "op": FRAMES,
        "st": 0,
        "bm": 0,
    }
    doc = {
        "tgs": 1,
        "v": "5.5.2",
        "fr": FPS,
        "ip": 0,
        "op": FRAMES,
        "w": CANVAS,
        "h": CANVAS,
        "nm": name,
        "ddd": 0,
        "assets": [],
        "layers": [layer],
    }
    return _fit(doc)


# --- reusable parts -----------------------------------------------------------------------------
def solid(path_or_shape: Json, color: str, edge: str, width: float = 18, *, t: Json | None = None) -> Json:
    """A filled shape with a darker edge."""
    return group(path_or_shape, stroke(edge, width), fill(color), t=t)


def line(path: Path | Anim, color: str, width: float, *, opacity: Any = 100, t: Json | None = None) -> Json:
    return group(shape(path), stroke(color, width, opacity), t=t)


def outlined_line(path: Path | Anim, color: str, edge: str, width: float, edge_w: float = 16) -> Json:
    """A thick line with a darker rim (the rim is a wider stroke under it)."""
    return group(shape(path), stroke(color, width), stroke(edge, width + edge_w))


def highlight(
    cx: float, cy: float, w: float, h: float, opacity: float = 35, *, t: Json | None = None
) -> Json:
    """A light shine over the glyph (the one thing that may be lighter than :data:`MIN_OPACITY`)."""
    return group(ellipse(cx, cy, w, h), fill(WHITE, opacity), t=t, name=SHINE)


WAVE_DIM: Final = 65  # percent: a wave dims to this, never further (MIN_OPACITY)


def waves(
    cx: float,
    cy: float,
    radii: tuple[float, ...],
    a0: float,
    a1: float,
    color: str,
    width: float,
    *,
    start: float = 0,
    gap: float = 14,
) -> list[Json]:
    """Arcs that dim together, then light up again one after another (signal / wifi)."""
    out = []
    for n, r in enumerate(radii):
        on = 56 + start + gap * n
        opacity = loop((0, 100), (40, 100), (52, WAVE_DIM), (on, WAVE_DIM, "linear"), (on + 10, 100))
        out.append(line(arc(cx, cy, r, a0, a1), color, width, opacity=opacity))
    return out


# --- the drawings -------------------------------------------------------------------------------
def antenna() -> Json:
    """📡 an operator's cell tower sending signal both ways."""
    legs = [
        line(poly((256, 168), (176, 440), closed=False), TOWER, 26),
        line(poly((256, 168), (336, 440), closed=False), TOWER, 26),
        line(poly((222, 286), (290, 286), (204, 370), (318, 370), closed=False), TOWER, 18),
        line(poly((176, 446), (336, 446), closed=False), TOWER, 28),
    ]
    pulse = transform((256, 150), scale=loop((0, [100, 100]), (14, [116, 116]), (28, [100, 100])))
    head = solid(ellipse(256, 150, 64), GOLD, GOLD_D, 16, t=pulse)
    right = waves(256, 150, (72, 116, 160), -42, 42, TURQ, 22, start=6)
    left = waves(256, 150, (72, 116, 160), 138, 222, TURQ, 22, start=6)
    return emoji("antenna", *legs, *right, *left, head)


def signal_bars() -> Json:
    """📶 four bars that turn grey and light up again one by one."""
    groups = []
    heights = (96, 168, 240, 312)
    for n, h in enumerate(heights):
        x = 112 + 96 * n
        on = 58 + 12 * n
        color = tint((0, TURQ), (36, TURQ), (48, TOWER), (on, TOWER, "linear"), (on + 8, TURQ))
        groups.append(group(rect(x, 440 - h / 2, 64, h, 18), stroke(TURQ_D, 14), fill(color)))
    return emoji("signal", *groups)


def globe() -> Json:
    """🌐 a turquoise globe turning."""
    r = 188
    body = solid(ellipse(256, 256, 2 * r), TURQ, TURQ_D, 20)
    lat = [
        line(poly((256 - r + 8, 256), (256 + r - 8, 256), closed=False), WHITE, 14, opacity=85),
        line(arc(256, 256 + 230, 290, -118, -62), WHITE, 12, opacity=70),
        line(arc(256, 256 - 230, 290, 62, 118), WHITE, 12, opacity=70),
    ]
    meridians = []
    keys = (0, 30, 60, 90, 120)
    for k in range(4):
        sizes = []
        for t in keys:
            lam = math.radians(45 * k + 45 * t / FRAMES)
            sizes.append((t, [max(2.0, 2 * r * abs(math.sin(lam))), 2 * r], "linear"))
        meridians.append(group(ellipse(256, 256, anim(*sizes)), stroke(WHITE, 12, 80)))
    axis = line(poly((256, 256 - r + 4), (256, 256 + r - 4), closed=False), WHITE, 12, opacity=80)
    return emoji("globe", body, *lat, *meridians, axis, highlight(196, 170, 120, 80, 22))


def _padlock(cx: float, cy: float, s: float, lift: tuple[tuple[float, float], ...]) -> list[Json]:
    """A gold padlock with its body centred on (cx, cy); ``s`` scales it (1 = full size). ``lift``:
    ``(frame, dy)`` keys of the shackle's offset (negative = up, open), looped."""
    w, h = 248 * s, 196 * s
    top = cy - h / 2
    sh_r = 74 * s
    shackle = arc(cx, top - 20 * s, sh_r, 180, 360)
    legs = poly((cx - sh_r, top + 10 * s), (cx - sh_r, top - 20 * s), closed=False)
    legs_r = poly((cx + sh_r, top - 20 * s), (cx + sh_r, top + 10 * s), closed=False)
    move = transform((cx, top), pos=loop(*((t, [cx, top + dy]) for t, dy in lift)))
    steel = group(
        shape(shackle),
        shape(legs),
        shape(legs_r),
        stroke(STEEL, 34 * s),
        stroke(STEEL_D, 52 * s),
        t=move,
    )
    body = solid(rect(cx, cy, w, h, 40 * s), GOLD, GOLD_D, 18 * s)
    hole = group(ellipse(cx, cy - 14 * s, 44 * s), rect(cx, cy + 22 * s, 20 * s, 56 * s, 10 * s), fill(NAVY))
    shine = highlight(cx - w / 4, cy - h / 4, w / 3, h / 6, 30)
    return [steel, body, hole, shine]


def padlock() -> Json:
    """🔒 a padlock whose shackle clicks shut."""
    lift = ((0, 0), (24, 0), (38, -34), (50, -34), (58, 6), (64, 0))
    return emoji("lock", *_padlock(256, 316, 1.0, lift))


def _key(cx: float, cy: float, s: float, rot: Any, color: str = GOLD, edge: str = GOLD_D) -> list[Json]:
    """A key lying along +x, its ring centred on (cx, cy); turned by ``rot`` around the ring."""
    t = transform((cx, cy), rot=rot)
    ring = group(ellipse(cx, cy, 116 * s), stroke(color, 36 * s), stroke(edge, 54 * s), t=t)
    blade = poly(
        (cx + 52 * s, cy - 18 * s),
        (cx + 236 * s, cy - 18 * s),
        (cx + 236 * s, cy + 18 * s),
        (cx + 216 * s, cy + 18 * s),
        (cx + 216 * s, cy + 62 * s),
        (cx + 178 * s, cy + 62 * s),
        (cx + 178 * s, cy + 18 * s),
        (cx + 150 * s, cy + 18 * s),
        (cx + 150 * s, cy + 48 * s),
        (cx + 116 * s, cy + 48 * s),
        (cx + 116 * s, cy + 18 * s),
        (cx + 52 * s, cy + 18 * s),
    )
    shaft = group(shape(blade), stroke(edge, 18 * s), fill(color), t=t)
    return [shaft, ring]


def key() -> Json:
    """🔑 a gold key turning in a lock."""
    rot = loop((0, -40), (30, -40), (50, -4), (70, -4), (90, -40))
    return emoji("key", *_key(150, 330, 1.0, rot))


def locked_with_key() -> Json:
    """🔐 a padlock with a turquoise key turning beside it."""
    lock = _padlock(216, 262, 0.82, ((0, 0), (30, 0), (42, -24), (54, 6), (62, 0)))
    rot = loop((0, -60), (40, -60), (60, -30), (80, -30), (96, -60))
    return emoji("locked_with_key", *lock, *_key(330, 392, 0.62, rot, TURQ, TURQ_D))


def link() -> Json:
    """🔗 two chain links pulling apart and snapping back."""

    def chain(dx: float) -> Json:
        c, apart = 256 + dx, 256 + dx * 1.2
        offset = loop((0, [c, c]), (30, [apart, apart]), (60, [c, c]))
        t = transform((c, c), pos=offset, rot=45)
        return group(rect(c, c, 230, 116, 58), stroke(TURQ, 38), stroke(TURQ_D, 58), t=t)

    return emoji("link", chain(-56), chain(56))


def _hourglass(t: Json, top_sand: Any, bottom_sand: Any, stream: Any = None) -> list[Json]:
    glass = poly((176, 128), (336, 128), (270, 256), (336, 384), (176, 384), (242, 256))
    parts = [
        group(shape(glass), stroke(NAVY, 16), fill(GLASS, 80), t=t),
        group(shape(top_sand), fill(GOLD), t=t),
        group(shape(bottom_sand), fill(GOLD), t=t),
    ]
    if stream is not None:
        parts.append(group(shape(stream), fill(GOLD), t=t))
    parts.append(
        group(rect(256, 100, 228, 40, 14), rect(256, 412, 228, 40, 14), stroke(GOLD_D, 14), fill(TURQ_D), t=t)
    )
    return parts


def hourglass_flowing() -> Json:
    """⏳ sand running down, then the glass turns over (a little smaller while it turns)."""
    full_top = poly((200, 168), (312, 168), (262, 246), (250, 246))
    empty_top = poly((253, 240), (259, 240), (257, 247), (255, 247))
    empty_bottom = poly((250, 380), (262, 380), (259, 376), (253, 376))
    # The bottom pile at the end is the top pile turned by 180 degrees, so the loop is seamless.
    full_bottom = poly((312, 344), (200, 344), (250, 266), (262, 266))
    top = anim((0, full_top), (84, empty_top), (120, empty_top), ease="linear")
    bottom = anim((0, empty_bottom), (84, full_bottom), (120, full_bottom), ease="linear")
    # The stream's last grains fall to the pile (no fading).
    flowing = poly((251, 256), (261, 256), (261, 380), (251, 380))
    stopped = poly((251, 380), (261, 380), (261, 380), (251, 380))
    stream = anim((0, flowing), (80, flowing), (86, stopped), (120, stopped))
    rot = anim((0, 0), (94, 0), (120, 180))
    size = anim((0, [100, 100]), (94, [100, 100]), (100, [82, 82]), (114, [82, 82]), (120, [100, 100]))
    return emoji("hourglass_flowing", *_hourglass(transform(rot=rot, scale=size), top, bottom, stream))


def hourglass_done() -> Json:
    """⌛ the sand has run out; the glass rocks."""
    empty_top = poly((253, 240), (259, 240), (257, 247), (255, 247))
    pile = bez(
        ((196, 378), (0, 0), (0, 0)),
        ((316, 378), (0, 0), (0, 0)),
        ((256, 316), (40, 0), (-40, 0)),
    )
    rot = loop((0, 0), (20, -10), (40, 8), (56, 0))
    return emoji("hourglass_done", *_hourglass(transform(rot=rot), empty_top, pile))


def red_circle() -> Json:
    """🔴 a red dot beating like a live light."""
    beat = loop((0, [100, 100]), (12, [108, 108]), (24, [100, 100]), (34, [104, 104]), (46, [100, 100]))
    dot = solid(ellipse(256, 256, 300), RED, RED_D, 22, t=transform(scale=beat))
    return emoji("red_circle", dot, highlight(204, 196, 104, 64, 40, t=transform(scale=beat)))


def no_entry_sign() -> Json:
    """🚫 the prohibited sign shaking its head."""
    rot = loop((0, 0), (8, -12), (18, 10), (28, -6), (36, 0))
    t = transform(rot=rot)
    ring = group(ellipse(256, 256, 352), stroke(RED, 46), stroke(RED_D, 66), t=t)
    bar = group(shape(poly((132, 132), (380, 380), closed=False)), stroke(RED, 46, round_cap=False), t=t)
    return emoji("no_entry", group(ellipse(256, 256, 352), fill(WHITE, 92), t=t), ring, bar)


def pause() -> Json:
    """⏸ a turquoise pause button breathing."""
    box = solid(rect(256, 256, 336, 336, 76), TURQ, TURQ_D, 20)
    s = loop((0, [100, 100]), (30, [100, 84]), (60, [100, 100]))
    bars = group(rect(204, 256, 46, 176, 16), rect(308, 256, 46, 176, 16), fill(WHITE), t=transform(scale=s))
    return emoji("pause", box, bars)


def _arrow_arc(r: float, a0: float, a1: float, color: str, edge: str, width: float) -> list[Json]:
    """A curved arrow along a circle around the centre, its head at ``a1`` (clockwise)."""
    body = outlined_line(arc(256, 256, r, a0, a1), color, edge, width, 14)
    a = math.radians(a1)
    tip = (256 + r * math.cos(a + 0.42), 256 + r * math.sin(a + 0.42))
    side = width * 1.25
    base_out = (256 + (r + side) * math.cos(a), 256 + (r + side) * math.sin(a))
    base_in = (256 + (r - side) * math.cos(a), 256 + (r - side) * math.sin(a))
    head = group(shape(poly(base_out, tip, base_in)), stroke(edge, 14), fill(color))
    return [body, head]


def recycle() -> Json:
    """♻️ three green arrows going round (renewed)."""
    parts: list[Json] = []
    for n in range(3):
        parts += _arrow_arc(150, 120 * n + 8, 120 * n + 84, GREEN, GREEN_D, 40)
    spin = anim((0, 0), (120, 120), ease="linear")
    return emoji("recycle", group(*reversed(parts), t=transform(rot=spin)))


def refresh() -> Json:
    """🔄 two white arrows turning on a blue tile."""
    tile = solid(rect(256, 256, 352, 352, 80), LAPIS, LAPIS_D, 20)
    parts: list[Json] = []
    for n in range(2):
        parts += _arrow_arc(104, 180 * n + 20, 180 * n + 150, WHITE, WHITE, 30)
    spin = anim((0, 0), (120, 180), ease="linear")
    return emoji("refresh", tile, group(*reversed(parts), t=transform(rot=spin)))


def high_voltage() -> Json:
    """⚡ a broad gold bolt that flashes bright and jumps a little."""
    bolt = poly((296, 48), (124, 292), (236, 292), (200, 468), (392, 200), (280, 200), (348, 48))
    flash = tint((0, GOLD), (8, GOLD_L), (22, GOLD), (30, GOLD_L), (44, GOLD))
    jump = loop((0, [100, 100]), (8, [106, 106]), (22, [100, 100]), (30, [103, 103]), (44, [100, 100]))
    body = group(shape(bolt), stroke(GOLD_D, 18), fill(flash), t=transform(scale=jump))
    streak = poly((300, 80), (182, 262), (214, 262), (318, 96))
    shine = group(shape(streak), fill(WHITE, 45), t=transform(scale=jump), name=SHINE)
    return emoji("bolt", body, shine)


def rocket() -> Json:
    """🚀 a rocket climbing with a flickering flame."""
    cx, cy = 256, 256
    body = bez(
        ((cx, cy - 172), (0, 0), (0, 0)),
        ((cx + 64, cy - 30), (12, -70), (0, 40)),
        ((cx + 48, cy + 96), (8, -30), (0, 0)),
        ((cx - 48, cy + 96), (0, 0), (-8, -30)),
        ((cx - 64, cy - 30), (0, 40), (-12, -70)),
    )
    fin_l = poly((cx - 56, cy + 10), (cx - 118, cy + 96), (cx - 112, cy + 128), (cx - 46, cy + 100))
    fin_r = poly((cx + 56, cy + 10), (cx + 118, cy + 96), (cx + 112, cy + 128), (cx + 46, cy + 100))
    flame = bez(
        ((cx - 36, cy + 100), (0, 0), (0, 0)),
        ((cx + 36, cy + 100), (0, 0), (0, 0)),
        ((cx, cy + 210), (22, -40), (-22, -40)),
    )
    flicker = loop(
        (0, [100, 100]),
        (6, [86, 118]),
        (12, [110, 88]),
        (18, [92, 112]),
        (24, [100, 100]),
        (30, [88, 116]),
        (36, [108, 92]),
        (42, [96, 108]),
        (48, [100, 100]),
    )
    bob = loop((0, [cx, cy]), (30, [cx + 8, cy - 8]), (60, [cx, cy]))
    whole = transform((cx, cy), pos=bob, rot=45, scale=88)
    parts = [
        group(shape(flame), stroke(RED, 18), fill(GOLD), t=transform((cx, cy + 100), scale=flicker)),
        group(shape(fin_l), shape(fin_r), stroke(RED_D, 16), fill(RED)),
        group(shape(body), stroke(NAVY, 18), fill(WHITE)),
        group(ellipse(cx, cy - 46, 66), stroke(NAVY, 16), fill(TURQ)),
        group(ellipse(cx - 12, cy - 58, 20, 14), fill(WHITE, 80)),
    ]
    return emoji("rocket", group(*reversed(parts), t=whole))


def _pop(cx: float, cy: float, at: float) -> Json:
    """A quick pop around (cx, cy) from frame ``at``: bigger, a little smaller, back."""
    return transform(
        (cx, cy),
        scale=loop(
            (0, [100, 100]),
            (at, [100, 100]),
            (at + 10, [118, 118]),
            (at + 20, [94, 94]),
            (at + 30, [100, 100]),
        ),
    )


def check_mark() -> Json:
    """✅ a green tile whose white tick pops."""
    tile = solid(rect(256, 256, 344, 344, 84), GREEN, GREEN_D, 20)
    path = poly((166, 262), (230, 326), (350, 200), closed=False)
    tick = group(shape(path), stroke(WHITE, 54), t=_pop(258, 263, 60))
    return emoji("check", tile, tick, highlight(186, 150, 140, 40, 20))


def warning() -> Json:
    """⚠️ a gold warning sign whose mark jumps twice."""
    tri = poly((256, 70), (448, 412), (64, 412))
    sign = group(shape(tri), stroke(GOLD, 44), stroke(GOLD_D, 64), fill(GOLD))
    s = loop(
        (0, [100, 100]),
        (20, [100, 100]),
        (28, [116, 116]),
        (36, [100, 100]),
        (44, [112, 112]),
        (52, [100, 100]),
    )
    mark = group(
        rect(256, 248, 42, 136, 21), ellipse(256, 360, 46), fill(NAVY), t=transform((256, 300), scale=s)
    )
    return emoji("warning", sign, mark)


def shield() -> Json:
    """🛡 a turquoise shield with a gold tick that pops."""
    body = bez(
        ((256, 56), (0, 0), (0, 0)),
        ((428, 116), (-60, 0), (0, 0)),
        ((420, 248), (0, -60), (-14, 120)),
        ((256, 460), (110, -60), (-110, -60)),
        ((92, 248), (14, 120), (0, -60)),
        ((84, 116), (0, 0), (60, 0)),
    )
    s = loop((0, [100, 100]), (20, [106, 106]), (40, [100, 100]))
    guard = group(shape(body), stroke(TURQ_D, 22), fill(TURQ), t=transform(scale=s))
    inner = group(shape(body), fill(WHITE, 18), t=transform(scale=72), name=SHINE)
    path = poly((178, 256), (236, 314), (338, 200), closed=False)
    tick = group(shape(path), stroke(GOLD, 46), t=_pop(258, 257, 60))
    return emoji("shield", guard, inner, tick)


def _phone(cx: float, cy: float) -> list[Json]:
    return [
        solid(rect(cx, cy, 216, 388, 48), NAVY, TOWER, 12),
        group(rect(cx, cy + 4, 176, 300, 18), fill(TURQ)),
        group(rect(cx, cy - 170, 56, 12, 6), fill(STEEL)),
        group(ellipse(cx, cy + 174, 18), fill(STEEL)),
    ]


def mobile_phone() -> Json:
    """📱 a phone with signal waves on its screen."""
    w = waves(256, 330, (44, 84, 124), 225, 315, WHITE, 22, start=0, gap=12)
    dot = group(ellipse(256, 330, 30), fill(WHITE))
    return emoji("phone", *_phone(256, 256), *w, dot)


def phone_with_arrow() -> Json:
    """📲 an arrow flying into a phone."""
    pos = loop((0, [0, 0]), (24, [0, 0]), (40, [-24, 0]), (64, [10, 0]), (76, [0, 0]))
    arrow = group(
        shape(poly((84, 230), (186, 230), (186, 190), (250, 256), (186, 322), (186, 282), (84, 282))),
        stroke(GOLD_D, 16),
        fill(GOLD),
        t=transform((0, 0), pos=pos),
    )
    bars = []
    for n, h in enumerate((40, 70, 100)):
        bars.append(group(rect(296 + 40 * n, 330 - h / 2, 24, h, 8), fill(WHITE)))
    return emoji("phone_arrow", *_phone(338, 256), *bars, arrow)


def information() -> Json:
    """ℹ️ a blue tile whose dot hops."""
    tile = solid(rect(256, 256, 344, 344, 84), LAPIS, LAPIS_D, 20)
    hop = loop((0, [256, 166]), (16, [256, 136]), (30, [256, 166]), (36, [256, 170]), (42, [256, 166]))
    dot = group(ellipse(256, 166, 60), fill(WHITE), t=transform((256, 166), pos=hop))
    stem = group(rect(256, 300, 52, 152, 24), fill(WHITE))
    return emoji("info", tile, stem, dot)


def battery() -> Json:
    """🔋 a battery charging cell by cell."""
    case = group(rect(240, 256, 352, 200, 40), stroke(STEEL_D, 26), fill(WHITE, 90))
    tip = group(rect(436, 256, 30, 84, 12), fill(TOWER))
    cells = []
    for n in range(4):
        on = 56 + 12 * n
        color = tint((0, GREEN), (34, GREEN), (44, EMPTY), (on, EMPTY, "linear"), (on + 8, GREEN))
        cells.append(group(rect(126 + 76 * n, 256, 58, 146, 14), fill(color)))
    return emoji("battery", case, tip, *cells)


def stopwatch() -> Json:
    """⏱ a stopwatch hand going round."""
    face = solid(ellipse(256, 284, 336), WHITE, STEEL_D, 28)
    crown = group(rect(256, 92, 76, 36, 12), rect(256, 120, 30, 30), fill(TOWER))
    button = group(rect(376, 138, 44, 30, 10), fill(TOWER), t=transform((376, 138), rot=45))
    ticks = [line(poly((256, 138), (256, 162), closed=False), NAVY, 14)]
    for a in (90, 180, 270):
        ticks.append(
            line(poly((256, 138), (256, 162), closed=False), NAVY, 14, t=transform((256, 284), rot=a))
        )
    spin = anim((0, 0), (120, 360), ease="linear")
    hand = group(
        shape(poly((256, 284), (256, 172), closed=False)), stroke(RED, 18), t=transform((256, 284), rot=spin)
    )
    hub = group(ellipse(256, 284, 34), fill(RED))
    return emoji("stopwatch", crown, button, face, *ticks, hand, hub)


def fire() -> Json:
    """🔥 a flame flickering."""
    outer = bez(
        ((256, 66), (0, 0), (0, 0)),
        ((388, 300), (-10, -110), (0, 90)),
        ((256, 452), (80, 0), (-80, 0)),
        ((124, 300), (0, 90), (10, -110)),
    )
    inner = bez(
        ((256, 210), (0, 0), (0, 0)),
        ((326, 352), (-6, -60), (0, 54)),
        ((256, 444), (44, 0), (-44, 0)),
        ((186, 352), (0, 54), (6, -60)),
    )
    s1 = loop((0, [100, 100]), (10, [96, 106]), (20, [104, 96]), (30, [98, 104]), (40, [100, 100]))
    s2 = loop((0, [100, 100]), (8, [108, 92]), (18, [94, 110]), (28, [104, 96]), (40, [100, 100]))
    r = loop((0, 0), (15, -3), (30, 3), (40, 0))
    return emoji(
        "fire",
        group(shape(outer), stroke(RED_D, 18), fill(RED), t=transform((256, 452), scale=s1, rot=r)),
        group(shape(inner), fill(GOLD), t=transform((256, 444), scale=s2)),
    )


def sim_card(color: str, edge: str, name: str) -> Callable[[], Json]:
    """🟡 🔵 🟣 an operator's SIM card (its colour only) with signal waves."""

    def draw() -> Json:
        card = poly((112, 132), (300, 132), (360, 192), (360, 460), (112, 460))
        body = group(shape(card), stroke(edge, 22), fill(color))
        chip = group(rect(236, 330, 156, 120, 22), stroke(GOLD_D, 12), fill(GOLD_L))
        lines = group(
            shape(poly((158, 330), (314, 330), closed=False)),
            shape(poly((236, 270), (236, 390), closed=False)),
            stroke(GOLD_D, 10),
        )
        w = waves(356, 148, (36, 66, 96), -90, 0, color, 22, start=0, gap=12)
        return emoji(name, body, chip, lines, *w)

    return draw


@dataclass(frozen=True)
class EmojiArt:
    name: str
    alt: str
    draw: Callable[[], Json]


# The pack, in its order. The alt is the plain emoji a post shows when this drawing is not used.
ART: Final[tuple[EmojiArt, ...]] = (
    EmojiArt("antenna", "📡", antenna),
    EmojiArt("signal", "📶", signal_bars),
    EmojiArt("globe", "🌐", globe),
    EmojiArt("locked_with_key", "🔐", locked_with_key),
    EmojiArt("lock", "🔒", padlock),
    EmojiArt("key", "🔑", key),
    EmojiArt("shield", "🛡️", shield),
    EmojiArt("link", "🔗", link),
    EmojiArt("bolt", "⚡", high_voltage),
    EmojiArt("rocket", "🚀", rocket),
    EmojiArt("fire", "🔥", fire),
    EmojiArt("check", "✅", check_mark),
    EmojiArt("warning", "⚠️", warning),
    EmojiArt("info", "ℹ️", information),
    EmojiArt("red_circle", "🔴", red_circle),
    EmojiArt("no_entry", "🚫", no_entry_sign),
    EmojiArt("pause", "⏸️", pause),
    EmojiArt("hourglass_flowing", "⏳", hourglass_flowing),
    EmojiArt("hourglass_done", "⌛", hourglass_done),
    EmojiArt("recycle", "♻️", recycle),
    EmojiArt("refresh", "🔄", refresh),
    EmojiArt("stopwatch", "⏱️", stopwatch),
    EmojiArt("phone", "📱", mobile_phone),
    EmojiArt("phone_arrow", "📲", phone_with_arrow),
    EmojiArt("battery", "🔋", battery),
    EmojiArt("sim_yellow", "🟡", sim_card(OP_YELLOW, OP_YELLOW_D, "sim_yellow")),
    EmojiArt("sim_blue", "🔵", sim_card(OP_BLUE, OP_BLUE_D, "sim_blue")),
    EmojiArt("sim_purple", "🟣", sim_card(OP_PURPLE, OP_PURPLE_D, "sim_purple")),
)


@functools.cache
def _built(art: EmojiArt) -> str:
    """The compact Lottie JSON of a drawing (made once per process: fitting it measures every frame)."""
    return json.dumps(art.draw(), separators=(",", ":"), ensure_ascii=False)


def lottie(art: EmojiArt) -> Json:
    return dict(json.loads(_built(art)))


def tgs(art: EmojiArt) -> bytes:
    """The ``.tgs`` file: gzip of the compact Lottie JSON, without a timestamp (same bytes each run)."""
    return gzip.compress(_built(art).encode(), compresslevel=9, mtime=0)


def digest(art: Iterable[EmojiArt] = ART) -> str:
    """One hash of every drawing's name, alt and file: it changes exactly when the pack would."""
    h = hashlib.sha256()
    for a in art:
        h.update(f"{a.name}\0{a.alt}\0".encode())
        h.update(hashlib.sha256(tgs(a)).digest())
    return h.hexdigest()


# --- what each frame draws ----------------------------------------------------------------------
Box = tuple[float, float, float, float]  # x0, y0, x1, y1 on the canvas
Matrix = tuple[float, float, float, float, float, float]  # x' = a x + c y + e, y' = b x + d y + f
Segment = tuple[Point, Point, Point, Point]  # one cubic Bezier
IDENTITY: Final[Matrix] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
KAPPA: Final = 0.5522847498  # a quarter circle as one cubic Bezier (as rlottie draws ellipses)


def _bezier1(p1: float, p2: float, t: float) -> float:
    """The 1-D cubic Bezier 0, p1, p2, 1 at ``t``."""
    mt = 1 - t
    return 3 * mt * mt * t * p1 + 3 * mt * t * t * p2 + t * t * t


def _eased(key: Json, x: float) -> float:
    """How far (0..1) a value has gone at time fraction ``x`` between two keyframes: Lottie's easing
    curve (0,0) (o.x,o.y) (i.x,i.y) (1,1), solved for x."""
    ox, oy = float(key["o"]["x"][0]), float(key["o"]["y"][0])
    ix, iy = float(key["i"]["x"][0]), float(key["i"]["y"][0])
    lo, hi = 0.0, 1.0
    for _ in range(48):
        mid = (lo + hi) / 2
        if _bezier1(ox, ix, mid) < x:
            lo = mid
        else:
            hi = mid
    return _bezier1(oy, iy, (lo + hi) / 2)


def _mix(a: Any, b: Any, u: float) -> Any:
    if isinstance(a, dict):
        return {k: _mix(a[k], b[k], u) if k in ("v", "i", "o") else a[k] for k in a}
    if isinstance(a, list):
        return [_mix(x, y, u) for x, y in zip(a, b, strict=True)]
    return a + (b - a) * u


def _unwrap(s: list[Any]) -> Any:
    return s[0] if len(s) == 1 else s


def value_at(p: Json, frame: float) -> Any:
    """A property's value at ``frame``: a number, a list of numbers or a path (Lottie's JSON form)."""
    if not p.get("a"):
        return p["k"]
    keys: list[Json] = p["k"]
    if frame <= keys[0]["t"]:
        return _unwrap(keys[0]["s"])
    for k0, k1 in itertools.pairwise(keys):
        if frame < k1["t"]:
            if k0.get("h"):
                return _unwrap(k0["s"])
            u = _eased(k0, (frame - k0["t"]) / (k1["t"] - k0["t"]))
            return _unwrap(_mix(k0["s"], k1["s"], u))
    return _unwrap(keys[-1]["s"])


def _then(outer: Matrix, inner: Matrix) -> Matrix:
    """``inner`` first, then ``outer``."""
    a, b, c, d, e, f = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def _matrix(tr: Json, frame: float) -> Matrix:
    """A group's (or the layer's) transform at ``frame``: scaled and turned around its anchor, which
    lands on its position. Positive angles turn clockwise (the canvas' y axis points down)."""
    px, py = value_at(tr["p"], frame)[:2]
    ax, ay = value_at(tr["a"], frame)[:2]
    sx, sy = (float(v) / 100 for v in value_at(tr["s"], frame)[:2])
    angle = math.radians(float(value_at(tr["r"], frame)))
    cos, sin = math.cos(angle), math.sin(angle)
    a, b, c, d = cos * sx, sin * sx, -sin * sy, cos * sy
    return (a, b, c, d, px - a * ax - c * ay, py - b * ax - d * ay)


def _apply(m: Matrix, p: Point) -> Point:
    a, b, c, d, e, f = m
    return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)


def _path_segments(path: Json) -> list[Segment]:
    v, i, o = path["v"], path["i"], path["o"]
    if len(v) == 1:
        p = (float(v[0][0]), float(v[0][1]))
        return [(p, p, p, p)]
    out: list[Segment] = []
    for k in range(len(v) if path["c"] else len(v) - 1):
        j = (k + 1) % len(v)
        p0, p3 = (float(v[k][0]), float(v[k][1])), (float(v[j][0]), float(v[j][1]))
        out.append((p0, (p0[0] + o[k][0], p0[1] + o[k][1]), (p3[0] + i[j][0], p3[1] + i[j][1]), p3))
    return out


def _ellipse_segments(cx: float, cy: float, rx: float, ry: float) -> list[Segment]:
    kx, ky = rx * KAPPA, ry * KAPPA
    top, right, bottom, left = (cx, cy - ry), (cx + rx, cy), (cx, cy + ry), (cx - rx, cy)
    return [
        (top, (cx + kx, cy - ry), (cx + rx, cy - ky), right),
        (right, (cx + rx, cy + ky), (cx + kx, cy + ry), bottom),
        (bottom, (cx - kx, cy + ry), (cx - rx, cy + ky), left),
        (left, (cx - rx, cy - ky), (cx - kx, cy - ry), top),
    ]


def _rect_segments(cx: float, cy: float, hw: float, hh: float, r: float) -> list[Segment]:
    """A rectangle with round corners of radius ``r`` (at most half its side, as rlottie clamps it)."""
    r = max(0.0, min(r, hw, hh))
    x0, y0, x1, y1 = cx - hw, cy - hh, cx + hw, cy + hh
    k = r * (1 - KAPPA)

    def straight(p: Point, q: Point) -> Segment:
        return (p, p, q, q)

    a, b, c, d = (x1 - r, y0), (x1, y0 + r), (x1, y1 - r), (x1 - r, y1)
    e, f, g, h = (x0 + r, y1), (x0, y1 - r), (x0, y0 + r), (x0 + r, y0)
    return [
        straight(h, a),
        (a, (x1 - k, y0), (x1, y0 + k), b),
        straight(b, c),
        (c, (x1, y1 - k), (x1 - k, y1), d),
        straight(d, e),
        (e, (x0 + k, y1), (x0, y1 - k), f),
        straight(f, g),
        (g, (x0, y0 + k), (x0 + k, y0), h),
    ]


def _segments(item: Json, frame: float) -> list[Segment]:
    if item["ty"] == "sh":
        return _path_segments(value_at(item["ks"], frame))
    cx, cy = (float(v) for v in value_at(item["p"], frame))
    w, h = (float(v) for v in value_at(item["s"], frame))
    if item["ty"] == "el":
        return _ellipse_segments(cx, cy, w / 2, h / 2)
    return _rect_segments(cx, cy, w / 2, h / 2, float(value_at(item["r"], frame)))


def _cubic_range(p0: float, p1: float, p2: float, p3: float) -> tuple[float, float]:
    """The exact lowest and highest value of a 1-D cubic Bezier over 0..1."""
    lo, hi = min(p0, p3), max(p0, p3)
    if lo <= min(p1, p2) and max(p1, p2) <= hi:
        return lo, hi
    d0, d1, d2 = p1 - p0, p2 - p1, p3 - p2
    a, b, c = d0 - 2 * d1 + d2, 2 * (d1 - d0), d0  # the derivative / 3
    roots: list[float] = []
    if abs(a) < 1e-12:
        if abs(b) > 1e-12:
            roots.append(-c / b)
    elif (disc := b * b - 4 * a * c) >= 0:
        roots += [(-b + math.sqrt(disc)) / (2 * a), (-b - math.sqrt(disc)) / (2 * a)]
    for t in roots:
        if 0 < t < 1:
            mt = 1 - t
            value = mt**3 * p0 + 3 * mt * mt * t * p1 + 3 * mt * t * t * p2 + t**3 * p3
            lo, hi = min(lo, value), max(hi, value)
    return lo, hi


def _segment_box(seg: Segment, m: Matrix) -> Box:
    p = [_apply(m, q) for q in seg]  # an affine map of a Bezier is the Bezier of the mapped points
    x0, x1 = _cubic_range(p[0][0], p[1][0], p[2][0], p[3][0])
    y0, y1 = _cubic_range(p[0][1], p[1][1], p[2][1], p[3][1])
    return (x0, y0, x1, y1)


def union(boxes: Iterable[Box]) -> Box | None:
    out: Box | None = None
    for x0, y0, x1, y1 in boxes:
        out = (
            (x0, y0, x1, y1)
            if out is None
            else (min(out[0], x0), min(out[1], y0), max(out[2], x1), max(out[3], y1))
        )
    return out


def _marks(items: list[Json], outer: Matrix, frame: float, out: list[Box]) -> list[Box]:
    """Add to ``out`` the canvas box of everything this group draws at ``frame``; return its shapes'
    boxes for the styles after it in the groups around it. A stroke grows its shapes by half its
    width, scaled and turned like the group it is in (round joins and caps: exact)."""
    tr = next((it for it in items if it.get("ty") == "tr"), None)
    m = _then(outer, _matrix(tr, frame)) if tr is not None else outer
    shapes: list[Box] = []
    for item in items:
        ty = item.get("ty")
        if ty in ("sh", "el", "rc"):
            box = union(_segment_box(s, m) for s in _segments(item, frame))
            if box is not None:
                shapes.append(box)
                out.append(box)
        elif ty == "gr":
            shapes += _marks(item["it"], m, frame, out)
        elif ty == "st":
            half = float(value_at(item["w"], frame)) / 2
            gx, gy = half * math.hypot(m[0], m[2]), half * math.hypot(m[1], m[3])
            out += [(x0 - gx, y0 - gy, x1 + gx, y1 + gy) for x0, y0, x1, y1 in shapes]
    return shapes


def _layer_items(layer: Json) -> list[Json]:
    """A shape layer as one group: its shapes, then its own transform."""
    return [*layer.get("shapes", []), {"ty": "tr", **layer["ks"]}]


def frame_box(doc: Json, frame: float) -> Box | None:
    """The canvas box of everything ``doc`` draws at ``frame``: every shape after all its groups'
    and its layer's transforms, grown by its strokes (None when it draws nothing)."""
    out: list[Box] = []
    for layer in doc.get("layers", []):
        _marks(_layer_items(layer), IDENTITY, frame, out)
    return union(out)


def frame_boxes(doc: Json) -> list[Box | None]:
    """:func:`frame_box` of every frame the animation shows (both ends included)."""
    return [frame_box(doc, f) for f in range(int(doc.get("ip", 0)), int(doc.get("op", 0)) + 1)]


def _size(box: Box) -> float:
    return max(box[2] - box[0], box[3] - box[1])


def _fit(doc: Json) -> Json:
    """Scale the drawing so that what it draws over the whole loop spans :data:`FIT_SIZE`, and
    centre it (one uniform scale around the middle of that span: the boxes scale exactly)."""
    whole = union(b for b in frame_boxes(doc) if b is not None)
    if whole is None:
        return doc
    centre = ((whole[0] + whole[2]) / 2, (whole[1] + whole[3]) / 2)
    t = transform(centre, pos=[CENTER, CENTER], scale=100 * FIT_SIZE / _size(whole))
    for layer in doc["layers"]:
        layer["shapes"] = [group(*layer["shapes"], t=t, name="fit")]
    return doc


def _lowest(p: Json) -> float:
    """The lowest value a number property takes (eased keys never overshoot: it is a key's value)."""
    if not p.get("a"):
        return float(p["k"])
    return min(float(_unwrap(key["s"])) for key in p["k"])


def faint_parts(doc: Json) -> list[str]:
    """Every fill or stroke that can drop below :data:`MIN_OPACITY` percent (its opacity times its
    groups' and its layer's), except in a :data:`SHINE` group; and every trim path."""
    out: list[str] = []

    def walk(items: list[Json], floor: float, where: str, shine: bool) -> None:
        tr = next((it for it in items if it.get("ty") == "tr"), None)
        here = floor * (_lowest(tr["o"]) / 100 if tr is not None else 1.0)
        for item in items:
            ty = item.get("ty")
            if ty == "gr":
                name = str(item.get("nm", "g"))
                walk(item["it"], here, f"{where}/{name}", shine or name == SHINE)
            elif ty == "tm":
                out.append(f"{where}: trim path (a half-drawn line)")
            elif ty in ("fl", "st") and not shine and (lowest := here * _lowest(item["o"])) < MIN_OPACITY:
                out.append(f"{where}: {'fill' if ty == 'fl' else 'stroke'} fades to {lowest:.0f}%")

    for layer in doc.get("layers", []):
        walk(_layer_items(layer), 1.0, str(layer.get("nm", "layer")), False)
    return out


def look_problems(doc: Json) -> list[str]:
    """Why a drawing would read badly beside a normal emoji (empty when it reads well): it is
    small or off centre over its loop, a frame shrinks below :data:`MIN_FRAME_SIZE`, a frame is
    empty, or a part fades (:func:`faint_parts`)."""
    out = faint_parts(doc)
    boxes = frame_boxes(doc)
    drawn = [b for b in boxes if b is not None]
    whole = union(drawn)
    if whole is None or len(drawn) < len(boxes):
        return [*out, "an empty frame"]
    if _size(whole) < FIT_SIZE - 1:
        out.append(f"small: {_size(whole):.0f} of {CANVAS} px")
    if max(abs((whole[0] + whole[2]) / 2 - CENTER), abs((whole[1] + whole[3]) / 2 - CENTER)) > 2:
        out.append("off centre")
    smallest = min(range(len(drawn)), key=lambda f: _size(drawn[f]))
    if _size(drawn[smallest]) < MIN_FRAME_SIZE:
        out.append(f"frame {smallest} shrinks to {_size(drawn[smallest]):.0f} px")
    return out


# --- Telegram's rules ---------------------------------------------------------------------------
_FORBIDDEN_SHAPES: Final = {
    "mm": "merge paths",
    "sr": "star shape",
    "gs": "gradient stroke",
    "rp": "repeater",
}
_FORBIDDEN_LAYERS: Final = {0: "precomposition", 1: "solid", 2: "image", 5: "text"}


def _walk(node: Any) -> Iterator[Json]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def problems(doc: Json, data: bytes) -> list[str]:
    """What breaks Telegram's animated-emoji rules (empty when the file is fine)."""
    out: list[str] = []
    if doc.get("tgs") != 1:
        out.append("tgs flag missing")
    if doc.get("w") != CANVAS or doc.get("h") != CANVAS:
        out.append("canvas is not 512x512")
    if doc.get("fr") != FPS:
        out.append("not 60 fps")
    if not 0 < doc.get("op", 0) - doc.get("ip", 0) <= MAX_FRAMES:
        out.append("longer than 3 seconds")
    if len(data) > MAX_TGS_BYTES:
        out.append(f"{len(data)} bytes > 64 KB")
    if doc.get("assets"):
        out.append("assets (images or precompositions)")
    for layer in doc.get("layers", []):
        if layer.get("ty") in _FORBIDDEN_LAYERS:
            out.append(f"{_FORBIDDEN_LAYERS[layer['ty']]} layer")
        if layer.get("ddd"):
            out.append("3D layer")
        if layer.get("hasMask") or layer.get("masksProperties"):
            out.append("mask")
        if layer.get("ef"):
            out.append("layer effect")
        if layer.get("tm") is not None:
            out.append("time remapping")
        if layer.get("sr", 1) != 1:
            out.append("time stretching")
        if layer.get("ao"):
            out.append("auto-oriented layer")
        if layer.get("ip", 0) > doc["ip"] or layer.get("op", 0) < doc["op"]:
            out.append("layer shorter than the animation")
    for box in frame_boxes(doc):
        if box is not None and not (
            SAFE_MIN <= box[0] and SAFE_MIN <= box[1] and box[2] <= SAFE_MAX and box[3] <= SAFE_MAX
        ):
            out.append("drawn outside the canvas")
            break
    for node in _walk(doc):
        if "x" in node and isinstance(node.get("x"), str):
            out.append("expression")
        if node.get("ty") in _FORBIDDEN_SHAPES:
            out.append(_FORBIDDEN_SHAPES[node["ty"]])
    return sorted(set(out))
