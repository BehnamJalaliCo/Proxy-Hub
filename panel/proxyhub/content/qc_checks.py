"""Deterministic checks of Claude's answer (decision D52, step 3). Code, not Claude, has the last
word: any check that fails rejects the item, whatever Claude decided.

Each check gives a short code (Persian in the dashboard, proxyhub/content/messages.py):

* **policy**: ``category_not_allowed`` (the owner's categories), ``flag:<name>`` (a flag the owner
  blocks, and ``manipulation`` always), ``approve_with_flag`` is folded into the same codes;
* **shape**: ``title_empty``, ``title_too_long``, ``title_emoji`` (exactly one emoji, at the very
  start), ``body_too_long``, ``caption_too_long`` (title line + body over the media caption's room),
  ``control_chars``;
* **nothing that leads elsewhere**: ``link`` (http(s), www, t.me, tg:, a bare domain), ``mention``
  (any ``@``), ``hashtag``, ``phone`` (eight or more digits in a row, Persian digits included),
  ``signature`` (an invitation to join or follow, a list of platforms, 🆔), ``source_name`` (the
  name or username of any of the owner's sources);
* **language**: ``not_persian`` (fewer than 80% of the letters are Persian/Arabic script);
* **the owner's words**: ``forbidden_word``.
"""

from __future__ import annotations

import re
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
_URL = re.compile(
    r"(?i)https?:|\bwww\.|\b(?:t|telegram)\.(?:me|dog)\b|\btg:|"
    r"\b[a-z0-9-]{2,63}\.(?:com|net|org|ir|io|me|co|info|news|tv|app|xyz|site|online|link|ly|gl|to|cc|biz|"
    r"uk|de|fr|ru|tr|ai|live|store|shop|vip|top|club)\b"
)
_HASHTAG = re.compile(r"#[\w‌]")
_DIGITS: Final = str.maketrans(
    {**{chr(0x06F0 + i): str(i) for i in range(10)}, **{chr(0x0660 + i): str(i) for i in range(10)}}
)
_PHONE = re.compile(r"(?:\+|00)?\d(?:[ \-]?\d){7,}")
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


def normalized(text: str) -> str:
    """Lower case, Arabic letter forms and digits unified, ZWNJ and punctuation as spaces."""
    return " ".join(clean.norm_text_keep_all(text).split())


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
    title = _SPACES.sub(" ", verdict.title).strip()
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
    if _PHONE.search(both.translate(_DIGITS)):
        codes.append("phone")
    if clean.looks_like_signature(both):
        codes.append("signature")
    text_norm = normalized(both)
    if any(_has_name(text_norm, name) for name in names):
        codes.append("source_name")
    if persian_share(both) < PERSIAN_SHARE:
        codes.append("not_persian")
    words = [normalized(w) for w in config.forbidden_words]
    if any(w and w in text_norm for w in words):
        codes.append("forbidden_word")
    return codes
