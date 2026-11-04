"""The owner's posting settings, stored as JSON under ``settings.key = 'posting'``.

=========================  ==========  ===================================================================
field                      default     meaning
=========================  ==========  ===================================================================
``enabled``                true        the publisher posts by itself
``main_enabled``           false       posts go to the MAIN channel (else to the test channel); the owner
                                       turns it on in the dashboard, with a confirmation
``posts_per_day``          10          new posts per day (0 to 48), spread over the posting hours
``day_start``/``day_end``  08:00/00:00 posting hours, Tehran time (``00:00`` as end = midnight; an end
                                       before the start runs past midnight)
``max_mtproto``            12          MTProto link words per post (0 to 20)
``max_reality``            3           Reality configs per post (0 to 5)
``edit_window_hours``      48          status/host edits only for posts younger than this (1 to 720)
``recycle_after_days``     14          once every approved item was used, an item may come again after
                                       this many days (1 to 365)
``status_texts``           docs/04     the line a dead Reality config gets (decision D5)
=========================  ==========  ===================================================================

The no-post windows and the least gap between two posts are the Telegram team's
``telegram.posting`` (decision D21; default gap 60 minutes); the publisher obeys them for both
channels. A stored value that no longer validates gives the defaults (posting to the test channel
only), never an error loop.
"""

from __future__ import annotations

import re
from datetime import time
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from proxyhub.telegram import store as tg_store
from proxyhub.telegram.schedule import parse_hhmm
from proxyhub.telegram.templates import StatusTexts

KEY: Final = "posting"
STATE_KEY: Final = "posting.state"
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_ONE_LINE = re.compile(r"\s+")

StatusLine = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
HHMM = Annotated[str, StringConstraints(pattern=_HHMM)]


class StatusTextsIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    expired: StatusLine = StatusTexts.expired
    full: StatusLine = StatusTexts.full
    burned: StatusLine = StatusTexts.burned

    def texts(self) -> StatusTexts:
        def line(value: str) -> str:
            return _ONE_LINE.sub(" ", value).strip()

        return StatusTexts(expired=line(self.expired), full=line(self.full), burned=line(self.burned))


class PostingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    enabled: bool = True
    main_enabled: bool = False
    posts_per_day: int = Field(default=10, ge=0, le=48)
    day_start: HHMM = "08:00"
    day_end: HHMM = "00:00"
    max_mtproto: int = Field(default=12, ge=0, le=20)
    max_reality: int = Field(default=3, ge=0, le=5)
    edit_window_hours: int = Field(default=48, ge=1, le=720)
    recycle_after_days: int = Field(default=14, ge=1, le=365)
    status_texts: StatusTextsIn = Field(default_factory=StatusTextsIn)

    @model_validator(mode="after")
    def _window(self) -> PostingConfig:
        if self.day_start == self.day_end:
            raise ValueError("posting hours must not start and end at the same minute")
        if self.max_mtproto == 0 and self.max_reality == 0:
            raise ValueError("a post needs at least one MTProto link or one Reality config")
        return self

    @property
    def start(self) -> time:
        return parse_hhmm(self.day_start)

    @property
    def end(self) -> time:
        return parse_hhmm(self.day_end)

    @property
    def target_role(self) -> str:
        return "main" if self.main_enabled else "test"

    def texts(self) -> StatusTexts:
        return self.status_texts.texts()

    def as_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


DEFAULTS: Final = PostingConfig()


def parse(value: Any) -> PostingConfig:
    """The owner's input (raises ValidationError, whose text never echoes the input values)."""
    return PostingConfig.model_validate(value)


async def load(db: tg_store.Db) -> PostingConfig:
    value = await tg_store.get_setting(db, KEY)
    if value is None:
        return DEFAULTS
    try:
        return PostingConfig.model_validate(value)
    except ValidationError:
        return DEFAULTS


async def save(db: tg_store.Db, config: PostingConfig) -> None:
    await tg_store.put_setting(db, KEY, config.as_json())


async def load_state(db: tg_store.Db) -> dict[str, Any]:
    return await tg_store.get_setting(db, STATE_KEY) or {}


async def save_state(db: tg_store.Db, state: dict[str, Any]) -> None:
    await tg_store.put_setting(db, STATE_KEY, state)
