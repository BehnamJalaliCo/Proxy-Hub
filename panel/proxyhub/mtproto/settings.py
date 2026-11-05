"""The owner's MTProto settings, stored under key ``mtproto`` in the ``settings`` table.

Defaults (no row yet):

* ``port`` 8443: every node also runs Xray VLESS Reality on 443 (cross-team contract), so Telemt
  takes 8443; the node firewall opens both.
* ``tls_domain`` ``www.microsoft.com``. In fake-TLS (``ee``) mode this name is the SNI that DPI sees
  and the site Telemt fronts: a client without the secret is spliced to the real site, and Telemt
  copies that site's TLS profile (Telemt 3.5.16 FAQ "Recognizability for DPI and crawler",
  CONFIG_PARAMS ``tls_domain``). Telemt's FAQ asks for an innocuous name that is not blocked, and
  the name is part of every ``ee`` link, so changing it later breaks every link already posted.
  Hence: a huge, stable HTTPS site on a global CDN with TLS 1.3, reachable from the nodes, and NOT
  filtered in Iran. Checked 2026-11-04 with Globalping DNS from 4 Iranian networks (AbrArvan,
  Sefroyek Pardaz, Parsian, HostIran): real answers, none of the filter's 10.10.34.x (the control,
  www.youtube.com, was hijacked on 2 of 4).
* modes: fake-TLS on; ``dd`` (secure) and classic off. The link uses the first mode that is on, in
  the order tls, secure, classic (one link per proxy).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from proxyhub.agent_api.validation import valid_domain
from proxyhub.db.models import Setting

MTPROTO_KEY = "mtproto"
DEFAULT_PORT = 8443
DEFAULT_TLS_DOMAIN = "www.microsoft.com"
# Never a proxy port: SSH (22, dropped by the node firewall), Xray VLESS Reality (443 on every node),
# Xray VLESS + XHTTP + REALITY (2083, proxyhub/reality/settings.py XHTTP_PORT), Telemt's own
# metrics and API (9090/9091, loopback only and dropped by the node firewall).
FORBIDDEN_PORTS = frozenset({22, 443, 2083, 9090, 9091})

Mode = Literal["tls", "secure", "classic"]
Db = AsyncConnection | AsyncSession


class SettingsCorrupt(RuntimeError):
    """The stored value no longer validates (callers then change nothing on the nodes)."""


class MtprotoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535)
    tls_domain: str = DEFAULT_TLS_DOMAIN
    mode_tls: bool = True
    mode_secure: bool = False
    mode_classic: bool = False

    @field_validator("port")
    @classmethod
    def _port(cls, v: int) -> int:
        if v in FORBIDDEN_PORTS:
            raise ValueError("port is reserved on the nodes")
        return v

    @field_validator("tls_domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        v = v.strip().lower().rstrip(".")
        if not valid_domain(v):
            raise ValueError("tls_domain must be a DNS name (not an IP address)")
        return v

    @model_validator(mode="after")
    def _one_mode(self) -> MtprotoSettings:
        if not (self.mode_tls or self.mode_secure or self.mode_classic):
            raise ValueError("at least one mode must be on")
        return self

    @property
    def link_mode(self) -> Mode:
        return link_mode(tls=self.mode_tls, secure=self.mode_secure, classic=self.mode_classic)


def link_mode(*, tls: bool, secure: bool, classic: bool) -> Mode:
    """The mode of the one public link of a proxy: the first mode that is on (tls, secure, classic)."""
    if tls:
        return "tls"
    if secure:
        return "secure"
    if classic:
        return "classic"
    raise ValueError("no Telemt mode is on")


def settings_json(s: MtprotoSettings) -> dict[str, Any]:
    return s.model_dump(mode="json")


def parse_settings(value: Any) -> MtprotoSettings:
    try:
        return MtprotoSettings.model_validate(value)
    except ValidationError as exc:
        raise SettingsCorrupt(f"stored mtproto settings are invalid ({exc.error_count()} errors)") from None


async def load_settings(db: Db) -> MtprotoSettings:
    row = (await db.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": MTPROTO_KEY})).first()
    return MtprotoSettings() if row is None else parse_settings(row[0])


async def load_settings_for_update(db: Db) -> MtprotoSettings | None:
    """Lock the row (created with the defaults if missing). ``None`` if the stored value is corrupt."""
    await db.execute(
        insert(Setting)
        .values(key=MTPROTO_KEY, value=settings_json(MtprotoSettings()))
        .on_conflict_do_nothing(index_elements=[Setting.key])
    )
    row = (
        await db.execute(text("SELECT value FROM settings WHERE key = :k FOR UPDATE"), {"k": MTPROTO_KEY})
    ).one()
    try:
        return parse_settings(row[0])
    except SettingsCorrupt:
        return None


async def save_settings(db: Db, s: MtprotoSettings) -> None:
    stmt = insert(Setting).values(key=MTPROTO_KEY, value=settings_json(s))
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[Setting.key], set_={"value": stmt.excluded.value, "updated_at": text("now()")}
        )
    )


def settings_diff(old: MtprotoSettings | None, new: MtprotoSettings) -> dict[str, list[Any]]:
    b = settings_json(new)
    if old is None:
        return {"*": ["corrupt", "replaced"]}
    a = settings_json(old)
    return {k: [a[k], b[k]] for k in b if a.get(k) != b[k]}
