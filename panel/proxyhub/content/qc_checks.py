"""Deterministic checks of Claude's answer (decision D52, step 3). Code, not Claude, has the last
word: any check that fails rejects the item, whatever Claude decided.

Each check gives a short code (Persian in the dashboard, proxyhub/content/messages.py):

* **policy**: ``category_not_allowed`` (the owner's categories), ``flag:<name>`` (a flag the owner
  blocks, and ``manipulation`` always), ``approve_with_flag`` is folded into the same codes;
* **shape**: ``title_empty``, ``title_too_long``, ``title_emoji`` (exactly one emoji, at the very
  start), ``body_too_long``, ``caption_too_long`` (title line + body over the media caption's room),
  ``control_chars``;
* **nothing that leads elsewhere**: ``link`` (http(s), www, t.me, tg:, any Latin ``name.tld``:
  Telegram makes every bare domain a link), ``mention`` (any ``@``), ``hashtag``, ``phone`` (eight
  or more digits, single spaces or hyphens between them allowed, Persian digits included; except a
  range of two years such as ۱۴۰۵-۱۴۰۶), ``signature`` (an invitation to join or follow, 🆔),
  ``source_name`` (the name or username of any of the owner's sources);
* **language**: ``not_persian`` (fewer than 80% of the letters are Persian/Arabic script);
* **the owner's words**: ``forbidden_word``.

Names and words are matched with invisible characters removed too (a zero-width character inside
``BBC`` must not hide it), and the title loses the same invisible and direction characters as the
body.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

from proxyhub.content import clean
from proxyhub.content.candidates import caption_html
from proxyhub.content.qc_prompt import Limits, Verdict
from proxyhub.content.qc_settings import QcConfig
from proxyhub.posts.entities import emoji_in
from proxyhub.telegram.templates import utf16_len, visible_text

PERSIAN_SHARE: Final = 0.8
MIN_LETTERS: Final = 6
# Any Latin name.tld (x.com, youtu.be, winbet.pro): ASCII lookarounds, not \b, so a domain glued to
# a Persian word is caught too.
_URL = re.compile(
    r"(?i)https?:|\bwww\.|\b(?:t|telegram)\.(?:me|dog)\b|\btg:|"
    r"(?<![a-z0-9-])[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\.(?:xn--[a-z0-9-]{2,59}|[a-z]{2,24})"
    r"(?![a-z0-9-])"
)
_HASHTAG = re.compile(r"#[\w‌]")
_DIGITS: Final = str.maketrans(
    {**{chr(0x06F0 + i): str(i) for i in range(10)}, **{chr(0x0660 + i): str(i) for i in range(10)}}
)
_PHONE = re.compile(r"(?:\+|00)?\d(?:[ \-]?\d){7,}")
_YEAR = r"(?:1[34]\d\d|19\d\d|20\d\d)"
_YEAR_PAIR = re.compile(rf"{_YEAR}[ \-]{_YEAR}")  # a school year or a season: ۱۴۰۵-۱۴۰۶
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
_SPACES = re.compile(r"\s+")


def _persian_letter(c: str) -> bool:
    o = ord(c)
    return 0x0600 <= o <= 0x06FF or 0x0750 <= o <= 0x077F or 0xFB50 <= o <= 0xFDFF or 0xFE70 <= o <= 0xFEFF


def persian_share(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if len(letters) < MIN_LETTERS:
        return 0.0
    return sum(1 for c in letters if _persian_letter(c)) / len(letters)


_ZWNJ: Final = "\u200c"
_FILLERS: Final = frozenset({0x034F, 0x115F, 0x1160, 0x3164, 0xFFA0})


def _invisible(c: str) -> bool:
    """A character that shows nothing: a format character (the Persian ZWNJ aside, it splits
    words), a variation selector, the combining grapheme joiner, a Hangul filler."""
    o = ord(c)
    return c != _ZWNJ and (
        unicodedata.category(c) == "Cf" or o in _FILLERS or 0xFE00 <= o <= 0xFE0F or 0xE0100 <= o <= 0xE01EF
    )


def visible(text: str) -> str:
    return "".join(c for c in text if not _invisible(c))


def normalized(text: str) -> str:
    """Lower case, Arabic letter forms and digits unified, invisible characters removed, ZWNJ and
    punctuation as spaces."""
    return _spaced(visible(text))


def _spaced(text: str) -> str:
    """Like :func:`normalized`, but an invisible character splits the word as punctuation does."""
    return " ".join(clean.norm_text_keep_all(text).split())


def _has_phone(text: str) -> bool:
    return any(not _YEAR_PAIR.fullmatch(m.group()) for m in _PHONE.finditer(text))


def _has_name(text_norm: str, name_norm: str) -> bool:
    return bool(name_norm) and re.search(rf"(?<!\w){re.escape(name_norm)}(?!\w)", text_norm) is not None


def source_names(refs_and_labels: Iterable[tuple[str, str, str]]) -> list[str]:
    """The names a caption must never carry: each Telegram source's username and every source's
    label (at least 3 characters), normalized."""
    out: list[str] = []
    for kind, ref, label in refs_and_labels:
        candidates = [label.lstrip("@")]
        if kind == "telegram":
            candidates.append(ref)
        for raw in candidates:
            name = normalized(raw)
            if len(name) >= 3 and name not in out:
                out.append(name)
    return out


@dataclass(frozen=True)
class Caption:
    title: str
    body: str


def tidy_caption(verdict: Verdict) -> Caption:
    title = _SPACES.sub(" ", clean.tidy(verdict.title)).strip()
    body = clean.tidy(verdict.body)
    return Caption(title, body)


def check(
    verdict: Verdict,
    caption: Caption,
    *,
    config: QcConfig,
    limits: Limits,
    names: Iterable[str],
) -> list[str]:
    """Every failed check's code (empty: the answer passes). Policy checks run on any decision;
    caption checks only on an approval (a rejection carries no caption)."""
    codes: list[str] = []
    if verdict.category not in config.categories:
        codes.append("category_not_allowed")
    blocking = config.block.blocking()
    codes += [f"flag:{name}" for name in sorted(verdict.flags & blocking)]
    if verdict.decision != "approve":
        return codes
    title, body = caption.title, caption.body
    if _CONTROL.search(verdict.title + verdict.body.replace("\n", " ")):
        codes.append("control_chars")
    if not title:
        codes.append("title_empty")
    elif len(title) > limits.title_max:
        codes.append("title_too_long")
    emojis = list(emoji_in(title))
    if len(emojis) != 1 or emojis[0].offset != 0:
        codes.append("title_emoji")
    if len(body) > limits.body_max:
        codes.append("body_too_long")
    if limits.room is not None:
        if utf16_len(visible_text(caption_html(title, body))) > limits.room:
            codes.append("caption_too_long")
    both = f"{title}\n{body}"
    if _URL.search(both):
        codes.append("link")
    if "@" in both or "＠" in both:
        codes.append("mention")
    if _HASHTAG.search(both) or "＃" in both:
        codes.append("hashtag")
    if _has_phone(both.translate(_DIGITS)):
        codes.append("phone")
    if clean.looks_like_signature(both):
        codes.append("signature")
    texts = (normalized(both), _spaced(both))  # either way of reading an invisible character
    if any(_has_name(t, name) for name in names for t in texts):
        codes.append("source_name")
    if persian_share(both) < PERSIAN_SHARE:
        codes.append("not_persian")
    words = [normalized(w) for w in config.forbidden_words]
    if any(w and w in t for w in words for t in texts):
        codes.append("forbidden_word")
    return codes
