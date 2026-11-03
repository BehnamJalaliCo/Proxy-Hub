"""The managed-record rule (like CLAUDE.md rule 5, for DNS) and the DNS change planner.

A Cloudflare record may be updated or deleted by this panel only if BOTH are true:

1. its comment carries the marker ``managed-by=proxy-hub``, and
2. our table ``cf_records`` lists it (by its Cloudflare id, or, for a row written just before a
   crash, by the ``ref=<row id>`` in the comment).

:func:`owned_row` is the only decision function for that; the planner uses it, and the code that
applies a plan checks it again right before every update or delete. A record that fails either
condition is "foreign": it is never changed or deleted, and a desired name that a foreign record
already uses is reported as a conflict instead of being created next to it.

Everything here is pure (no I/O), so it is tested with real Cloudflare record JSON as data.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

from proxyhub.cloudflare.client import DnsRecord, RecordSpec

MARKER = "managed-by=proxy-hub"
SLOT_TTL = 60  # seconds: the lowest TTL a non-enterprise zone accepts (docs/10 section 3.10)


def managed_comment(ref: uuid.UUID) -> str:
    return f"{MARKER} ref={ref}"


def has_marker(comment: str | None) -> bool:
    return comment is not None and MARKER in comment.split()


def comment_ref(comment: str | None) -> uuid.UUID | None:
    if comment is None:
        return None
    for part in comment.split():
        if part.startswith("ref="):
            try:
                return uuid.UUID(part[4:])
            except ValueError:
                return None
    return None


@dataclass(frozen=True)
class OwnedRow:
    """A row of ``cf_records`` (what this panel created)."""

    ref: uuid.UUID
    zone_id: str
    cf_record_id: str | None
    name: str
    type: str
    content: str
    ttl: int
    proxied: bool


@dataclass(frozen=True)
class Desired:
    name: str
    type: str
    content: str
    ttl: int = SLOT_TTL
    proxied: bool = False  # MTProto cannot pass Cloudflare's proxy (orange cloud)
    node_id: uuid.UUID | None = None
    purpose: str = "slot"  # 'slot' (reconciler) or 'test' (real Cloudflare tests)

    @property
    def key(self) -> tuple[str, str]:
        return (self.name.lower(), self.type.upper())

    def spec(self, ref: uuid.UUID) -> RecordSpec:
        return RecordSpec(
            name=self.name.lower(),
            type=self.type.upper(),
            content=self.content,
            ttl=self.ttl,
            proxied=self.proxied,
            comment=managed_comment(ref),
        )


def owned_row(remote: DnsRecord, rows: Iterable[OwnedRow]) -> OwnedRow | None:
    """THE decision function: our row for this Cloudflare record, or None if it is not ours."""
    if not has_marker(remote.comment):
        return None
    ref = comment_ref(remote.comment)
    for row in rows:
        if row.zone_id != remote.zone_id:
            continue
        if row.cf_record_id is not None:
            if row.cf_record_id == remote.id:
                return row
        elif (
            ref == row.ref
            and row.name.lower() == remote.name.lower()
            and row.type.upper() == remote.type.upper()
        ):
            return row
    return None


Op = Literal["create", "update", "delete", "adopt", "sync", "forget", "conflict"]


@dataclass(frozen=True)
class Change:
    """One step of a plan.

    * ``create``: write ``desired`` (reusing ``row`` if a pending row exists for the name).
    * ``update``: change ``remote`` (ours) to ``desired``.
    * ``delete``: remove ``remote`` (ours) from Cloudflare, then its row.
    * ``adopt``: a pending row's record exists in Cloudflare; store its id (no API call).
    * ``sync``: our row's copy of content/ttl differs from Cloudflare; refresh the row (no API call).
    * ``forget``: our row's record is no longer in Cloudflare (or never got there); drop the row.
    * ``conflict``: a foreign record already uses a desired name; nothing is done.
    """

    op: Op
    zone_id: str
    name: str
    type: str
    desired: Desired | None = None
    remote: DnsRecord | None = None
    row: OwnedRow | None = None


def same_content(rtype: str, a: str, b: str) -> bool:
    """Content equality as DNS sees it: Cloudflare may return TXT content with surrounding quotes."""
    if rtype.upper() == "TXT":
        return _unquote(a) == _unquote(b)
    return a.strip().lower() == b.strip().lower()


def _unquote(value: str) -> str:
    v = value.strip()
    return v[1:-1] if len(v) >= 2 and v[0] == v[-1] == '"' else v


def _differs(remote: DnsRecord, want: Desired) -> bool:
    return (
        not same_content(remote.type, remote.content, want.content)
        or remote.ttl != want.ttl
        or remote.proxied != want.proxied
    )


def _row_differs(row: OwnedRow, remote: DnsRecord) -> bool:
    return (row.content, row.ttl, row.proxied) != (remote.content, remote.ttl, remote.proxied)


def plan_zone(
    zone_id: str, desired: Sequence[Desired], remote: Sequence[DnsRecord], rows: Sequence[OwnedRow]
) -> list[Change]:
    """Changes that make our records in one zone equal ``desired``. Foreign records are only read.

    ``remote`` must be the COMPLETE record list of the zone (the client refuses a partial list).
    """
    rows = [r for r in rows if r.zone_id == zone_id]
    remote = sorted((r for r in remote if r.zone_id == zone_id), key=lambda r: r.id)
    changes: list[Change] = []

    owned: dict[str, OwnedRow] = {}
    adopted_refs: set[uuid.UUID] = set()
    for rec in remote:
        row = owned_row(rec, rows)
        if row is None:
            continue
        if row.cf_record_id is None:
            if row.ref in adopted_refs:
                continue  # a second record claiming the same pending row is not ours
            adopted_refs.add(row.ref)
            changes.append(Change("adopt", zone_id, rec.name, rec.type, remote=rec, row=row))
        elif _row_differs(row, rec):
            changes.append(Change("sync", zone_id, rec.name, rec.type, remote=rec, row=row))
        owned[rec.id] = row

    remote_ids = {r.id for r in remote}
    pending_by_key: dict[tuple[str, str], OwnedRow] = {}
    for row in rows:
        if row.cf_record_id is not None and row.cf_record_id not in remote_ids:
            changes.append(Change("forget", zone_id, row.name, row.type, row=row))
        elif row.cf_record_id is None and row.ref not in adopted_refs:
            pending_by_key[(row.name.lower(), row.type.upper())] = row

    wanted: dict[tuple[str, str], Desired] = {}
    for d in desired:
        wanted.setdefault(d.key, d)

    for key, want in wanted.items():
        name, rtype = key
        foreign_same_name = [r for r in remote if r.name == name and r.id not in owned]
        ours = [r for r in remote if r.id in owned and (r.name, r.type) == key]
        if foreign_same_name:
            changes.append(Change("conflict", zone_id, name, rtype, desired=want))
            continue
        if ours:
            first, extras = ours[0], ours[1:]
            if _differs(first, want):
                changes.append(
                    Change("update", zone_id, name, rtype, desired=want, remote=first, row=owned[first.id])
                )
            for extra in extras:
                changes.append(Change("delete", zone_id, name, rtype, remote=extra, row=owned[extra.id]))
            continue
        changes.append(
            Change("create", zone_id, name, rtype, desired=want, row=pending_by_key.pop(key, None))
        )

    for rec in remote:
        if rec.id in owned and (rec.name, rec.type) not in wanted:
            changes.append(Change("delete", zone_id, rec.name, rec.type, remote=rec, row=owned[rec.id]))
    for row in pending_by_key.values():
        changes.append(Change("forget", zone_id, row.name, row.type, row=row))
    return changes
