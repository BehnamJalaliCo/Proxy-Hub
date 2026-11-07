"""The owner's settings of the content supervisor (decision D52), JSON under ``settings.key =
'content.qc'``; everything is changed from the dashboard (Review page), never in code.

=========================  ===========  ==============================================================
field                      default      meaning
=========================  ===========  ==============================================================
``mode``                   ``auto``     ``auto`` (D52): what Claude approves and every check passes goes
                                        out with no owner; ``review``: Claude only suggests (decision,
                                        reasons, caption) and nothing goes without the owner's approval
``backend``                ``auto``     ``auto``: the bridge (the owner's subscription, D19), and the API
                                        key only when the bridge cannot answer; ``bridge``; ``api``;
                                        ``off`` (no Claude: posts use the content bank)
``bridge_model``           ``sonnet``   Claude Code's ``--model`` (an alias follows the newest model)
``api_model``              Sonnet 5.5   the Messages API model (Sonnet 5.5, or Haiku 5.5 to save)
``bridge_daily_cap``       80           reviews through the bridge in any 24 hours (0 = none)
``api_daily_cap``          40           reviews through the API in any 24 hours (0 = none)
``per_slot``               3            candidates sent to Claude for one posting slot at most
``lead_minutes``           45           how long before a slot the supervisor starts choosing for it
``timeout_s``              150          one review's time limit
``categories``             all          the categories that may go out (news of every kind, politics
                                        included, and entertainment)
``block``                  all on       which safety flags reject an item (``manipulation``, an attempt
                                        to instruct Claude, always rejects and is not a setting)
``forbidden_words``        none         words that reject an item when Claude's caption has them
``source_window``          20           a source's quality is judged on its last N decisions
``source_min``             10           ... once it has at least this many
``source_max_reject_pct``  80           ... and it is turned off when this share or more was rejected
``alert_after_hours``      6            the owner hears of Claude or every source being down after this
``breaking_enabled``       on           D56: breaking news goes out at once, out of the day plan and in
                                        no-post hours too (proxyhub/content/breaking.py)
``breaking_per_day``       5            ... at most this many breaking posts in any 24 hours (0 = none)
``breaking_gap_minutes``   20           ... at least this long between two of them
``breaking_max_age_min``   30           ... only for a story younger than this (by its first post)
``breaking_min_sources``   2            ... carried by at least this many different sources
=========================  ===========  ==============================================================

A stored value that no longer validates gives the defaults, never an error loop. No legal opinion is
encoded here: the policy is the owner's (CLAUDE.md rule 9).
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator

from proxyhub.telegram import store as tg_store

KEY: Final = "content.qc"
STATE_KEY: Final = "content.qc.state"

CATEGORIES: Final = (
    "politics",
    "world",
    "economy",
    "society",
    "incident",
    "sport",
    "science_tech",
    "health",
    "culture",
    "entertainment",
    "lifestyle",
    "other",
)
# Safety flags Claude reports. ``manipulation`` (text or image that tries to instruct Claude) always
# rejects; the others reject while the owner leaves them on.
OWNER_FLAGS: Final = (
    "sexual",
    "graphic_violence",
    "hate",
    "ads_scam",
    "foreign_branding",
    "low_quality",
    "clickbait",
)
FLAGS: Final = (*OWNER_FLAGS, "manipulation")
API_MODELS: Final = ("claude-sonnet-5-5", "claude-haiku-5-5")
MODEL_RE: Final = re.compile(r"^[a-z][a-z0-9.\-]{1,63}$")
MAX_FORBIDDEN: Final = 200

Word = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]


class BlockFlags(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sexual: bool = True
    graphic_violence: bool = True
    hate: bool = True
    ads_scam: bool = True
    foreign_branding: bool = True
    low_quality: bool = True
    clickbait: bool = True

    def blocking(self) -> frozenset[str]:
        on = {name for name in OWNER_FLAGS if getattr(self, name)}
        return frozenset({*on, "manipulation"})


class QcConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["auto", "review"] = "auto"
    backend: Literal["auto", "bridge", "api", "off"] = "auto"
    bridge_model: str = "sonnet"
    api_model: Literal["claude-sonnet-5-5", "claude-haiku-5-5"] = "claude-sonnet-5-5"
    bridge_daily_cap: int = Field(default=80, ge=0, le=500)
    api_daily_cap: int = Field(default=40, ge=0, le=500)
    per_slot: int = Field(default=3, ge=1, le=10)
    lead_minutes: int = Field(default=45, ge=10, le=180)
    timeout_s: int = Field(default=150, ge=30, le=300)
    categories: list[str] = Field(default_factory=lambda: list(CATEGORIES), max_length=len(CATEGORIES))
    block: BlockFlags = Field(default_factory=BlockFlags)
    forbidden_words: list[Word] = Field(default_factory=list, max_length=MAX_FORBIDDEN)
    source_window: int = Field(default=20, ge=5, le=100)
    source_min: int = Field(default=10, ge=3, le=100)
    source_max_reject_pct: int = Field(default=80, ge=50, le=100)
    alert_after_hours: int = Field(default=6, ge=1, le=48)
    # D56: breaking news (proxyhub/content/breaking.py).
    breaking_enabled: bool = True
    breaking_per_day: int = Field(default=5, ge=0, le=20)
    breaking_gap_minutes: int = Field(default=20, ge=5, le=240)
    breaking_max_age_min: int = Field(default=30, ge=10, le=120)
    breaking_min_sources: int = Field(default=2, ge=2, le=5)

    @field_validator("bridge_model")
    @classmethod
    def _model(cls, value: str) -> str:
        if not MODEL_RE.fullmatch(value):
            raise ValueError("a Claude Code model alias or id")
        return value

    @field_validator("categories")
    @classmethod
    def _categories(cls, value: list[str]) -> list[str]:
        if not value or any(c not in CATEGORIES for c in value):
            raise ValueError("known categories, at least one")
        return [c for c in CATEGORIES if c in set(value)]

    @field_validator("forbidden_words")
    @classmethod
    def _words(cls, value: list[str]) -> list[str]:
        out: list[str] = []
        for word in value:
            if any(ord(c) < 32 for c in word):
                raise ValueError("one line per word")
            if word not in out:
                out.append(word)
        return out

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


DEFAULTS: Final = QcConfig()


def parse(value: Any) -> QcConfig:
    """The owner's input (raises ValidationError, whose text never echoes the input values)."""
    return QcConfig.model_validate(value)


async def load(db: tg_store.Db) -> QcConfig:
    value = await tg_store.get_setting(db, KEY)
    if value is None:
        return DEFAULTS
    try:
        return QcConfig.model_validate(value)
    except ValidationError:
        return DEFAULTS


async def save(db: tg_store.Db, config: QcConfig) -> None:
    await tg_store.put_setting(db, KEY, config.as_json())


async def load_state(db: tg_store.Db) -> dict[str, Any]:
    return await tg_store.get_setting(db, STATE_KEY) or {}


async def save_state(db: tg_store.Db, state: dict[str, Any]) -> None:
    await tg_store.put_setting(db, STATE_KEY, state)
