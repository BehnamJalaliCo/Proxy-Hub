"""Text of untrusted sources: HTML to plain text, the prefilled caption, and similarity.

* :func:`html_to_text`: an HTML fragment (a feed entry's description) to plain text with a real
  parser (the standard library's tokenizer); scripts and styles are dropped, nothing is executed.
* :func:`clean_source_text`: the source's text made fit for OUR post, deterministically:

  - every link is removed (``https://…``, ``www.…``, ``t.me/…``): it would send readers to someone
    else's site or channel, and could carry tracking; the post's own links are ours;
  - every ``@mention`` is removed: the source's own handle is an advert for another channel, and any
    other handle may be one too;
  - hashtags: a line of hashtags only (a source's tag line) is dropped; a hashtag of the source's own
    name is dropped; any other hashtag inside a sentence keeps its word (``#فوری`` -> ``فوری``,
    ``#جام_جهانی`` -> ``جام جهانی``), so the sentence still reads;
  - the source's signature is stripped: among the last lines, a short line that names the source
    (its label or username), invites to join or follow (``عضو شوید``, ``🆔``, ``join`` ...) or lists
    two or more platforms (``اینستاگرام | تلگرام``; one platform name alone is news, not a
    signature), and every line left with no letter or digit after the steps above (``➖➖➖``);
  - direction marks and invisible characters are removed (the zero-width non-joiner of Persian
    words stays); spaces and empty lines are collapsed.

* :func:`split_caption`: the cleaned text as a title (first line or first sentence) and a short
  body; the publisher shortens the content further when the post's links need the room (never the
  links).
* :func:`norm_text` / :func:`similar`: Persian text normalized (Arabic letter forms, diacritics,
  digits, punctuation, a few stop words) and compared by word pairs and word overlap, so the same
  news from two sources is kept once. Simple and deterministic on purpose.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from html.parser import HTMLParser
from itertools import pairwise
from typing import Final

TITLE_MAX: Final = 100
DEFAULT_BODY_CHARS: Final = 280
MAX_BODY_CHARS: Final = 1000
ELLIPSIS: Final = "…"

_URL = re.compile(
    r"(?i)(?:\bhttps?://|\bwww\.)[^\s<>\"']+|\b(?:t\.me|telegram\.me|telegram\.dog)/[^\s<>\"']*"
)
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{0,31}\b")
_HASHTAG = re.compile(r"(?<![\w#])#([\w‌]+)")
_INVISIBLE = re.compile("[​‎‏‪-‮⁠⁦-⁩؜﻿­]")
_SPACES = re.compile(r"[ \t  -   　]+")
_HAS_WORD = re.compile(r"[^\W_]")  # a letter or a digit, in any script
_JOIN_WORDS = re.compile(
    r"(?i)عضو\s*شو|عضویت|جوین|بپیوندید|دنبال\s*کنید|کانال\s*ما|لینک\s*کانال|🆔|"
    r"\bjoin\b|\bsubscribe\b|\bfollow\s+us\b"
)
_PLATFORMS = re.compile(
    r"(?i)instagram|اینستاگرام|youtube|یوتیوب|twitter|توییتر|توئیتر|telegram|تلگرام|facebook|فیسبوک|"
    r"aparat|آپارات|whatsapp|واتساپ|website|وبسایت|وب‌سایت"
)
_SENTENCE_END = re.compile(r"[.!?؟؛…](?=\s)")
SIGNATURE_LINES: Final = 3
SIGNATURE_MAX_CHARS: Final = 80


class _Text(HTMLParser):
    _BREAKS: Final = frozenset(
        {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote"}
    )
    _SKIP: Final = frozenset({"script", "style", "noscript", "template", "iframe", "object", "svg"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skipping = 0
        self.first_img: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self.skipping += 1
            return
        if tag in self._BREAKS:
            self.parts.append("\n")
        if tag == "img" and self.first_img is None:
            src = dict(attrs).get("src")
            if src:
                self.first_img = src.strip()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._SKIP:
            self.skipping = max(0, self.skipping - 1)
        elif tag in self._BREAKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.parts.append(data)


def html_to_text(fragment: str, limit: int = 20_000) -> tuple[str, str | None]:
    """Plain text of an HTML fragment (at most ``limit`` input characters are read), and the src
    of its first image (unchecked: the caller checks it like any URL)."""
    parser = _Text()
    parser.feed(fragment[:limit])
    parser.close()
    return tidy("".join(parser.parts)), parser.first_img


def tidy(text: str) -> str:
    """Invisible characters out, spaces collapsed, at most one empty line in a row, trimmed."""
    text = _INVISIBLE.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _names(source_names: Iterable[str]) -> list[str]:
    out = []
    for name in source_names:
        name = name.strip().lstrip("@").lower()
        if len(name) >= 3:
            out.append(name)
    return out


def _hashtag_word(match: re.Match[str], names: list[str]) -> str:
    word = match.group(1)
    if word.lower() in names:
        return ""
    return word.replace("_", " ")


def _only_hashtags(line: str) -> bool:
    words = line.split()
    return bool(words) and all(w.startswith("#") for w in words)


def _is_signature(line: str, names: list[str]) -> bool:
    if len(line) > SIGNATURE_MAX_CHARS:
        return False
    low = line.lower()
    if any(n in low for n in names) or _JOIN_WORDS.search(line):
        return True
    return len(_PLATFORMS.findall(line)) >= 2  # "اینستاگرام | تلگرام | یوتیوب": a list of pages


def clean_source_text(text: str, source_names: Iterable[str] = ()) -> str:
    """The source's text without links, mentions, tag lines and signature (see the module doc)."""
    names = _names(source_names)
    kept: list[str] = []
    for raw in tidy(text).split("\n"):
        if _only_hashtags(raw):
            continue
        line = _URL.sub(" ", raw)
        line = _MENTION.sub(" ", line)
        line = _HASHTAG.sub(lambda m: _hashtag_word(m, names), line)
        line = _SPACES.sub(" ", line).strip(" -–—|:•،,")
        if raw.strip() and not _HAS_WORD.search(line):
            continue  # nothing left but marks, emoji or separators
        kept.append(line)
    text = tidy("\n".join(kept))
    lines = text.split("\n")
    # The signature: walk up from the end over the last few non-empty lines.
    seen = 0
    while lines and seen < SIGNATURE_LINES:
        last = lines[-1].strip()
        if not last:
            lines.pop()
            continue
        if not _is_signature(last, names):
            break
        lines.pop()
        seen += 1
    return tidy("\n".join(lines))


def shorten(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters (ellipsis included) at a sentence end when one is late
    enough, else at a word boundary."""
    text = text.strip()
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS[:limit]
    head = text[: limit - 1]
    ends = [m.end() for m in _SENTENCE_END.finditer(head + " ")]
    if ends and ends[-1] > limit // 2:
        return head[: ends[-1]].rstrip()
    space = max(head.rfind(" "), head.rfind("\n"))
    if space > limit // 2:
        head = head[:space]
    return head.rstrip(" ,،؛:-") + ELLIPSIS


def split_caption(text: str, body_chars: int = DEFAULT_BODY_CHARS) -> tuple[str, str]:
    """(title, body) prefilled from the cleaned text: the first line as the title when it is short,
    else its first sentence, else its start with an ellipsis (the rest goes to the body)."""
    text = text.strip()
    if not text:
        return "", ""
    first, _, rest = text.partition("\n")
    first, rest = first.strip(), rest.strip()
    if len(first) <= TITLE_MAX:
        return first, shorten(rest, body_chars)
    ends = [m.end() for m in _SENTENCE_END.finditer(first[:TITLE_MAX] + " ")]
    if ends and ends[-1] >= 20:
        title, remainder = first[: ends[-1]].strip(), first[ends[-1] :].strip()
    else:
        cut = first.rfind(" ", 0, TITLE_MAX - 1)
        cut = cut if cut > TITLE_MAX // 2 else TITLE_MAX - 1
        title, remainder = first[:cut].rstrip() + ELLIPSIS, ELLIPSIS + first[cut:].strip()
    body = "\n".join(x for x in (remainder, rest) if x)
    return title, shorten(body, body_chars)


# --- similarity ------------------------------------------------------------------------------------

_LETTERS: Final = str.maketrans(
    {
        "ي": "ی",
        "ى": "ی",
        "ئ": "ی",
        "ك": "ک",
        "ة": "ه",
        "ۀ": "ه",
        "أ": "ا",
        "إ": "ا",
        "آ": "ا",
        "ٱ": "ا",
        "ؤ": "و",
        "‌": " ",
        **{chr(0x06F0 + i): str(i) for i in range(10)},
        **{chr(0x0660 + i): str(i) for i in range(10)},
    }
)
_DIACRITICS = re.compile("[ً-ٰٟـ]")
_NON_WORD = re.compile(r"[^\w\s]|_")
_STOP: Final = frozenset(
    "و در به از که این را با است برای آن یک تا ها های می شد شده بود کرد خود هم بر نیز اند ای "
    "the a an of to in and or is are for on".split()
)
MAX_NORM_CHARS: Final = 1500
MIN_WORDS: Final = 8


def norm_text(text: str) -> str:
    """The words of ``text`` for comparison (see the module doc)."""
    t = _URL.sub(" ", text)
    t = _MENTION.sub(" ", t)
    t = _DIACRITICS.sub("", t.translate(_LETTERS)).lower()
    t = _NON_WORD.sub(" ", t)
    words = [w for w in t.split() if len(w) > 1 and w not in _STOP]
    return " ".join(words)[:MAX_NORM_CHARS]


def similar(a: str, b: str) -> bool:
    """Two normalized texts tell the same news: half their word pairs are shared, or nearly every
    word of the shorter one is in the longer one. Short texts (under :data:`MIN_WORDS` words) are
    never called similar (a photo's one-line caption says too little)."""
    wa, wb = a.split(), b.split()
    if len(wa) < MIN_WORDS or len(wb) < MIN_WORDS:
        return False
    pa, pb = set(pairwise(wa)), set(pairwise(wb))
    if pa and pb and len(pa & pb) / len(pa | pb) >= 0.5:
        return True
    sa, sb = set(wa), set(wb)
    return min(len(sa), len(sb)) >= 10 and len(sa & sb) / min(len(sa), len(sb)) >= 0.85
