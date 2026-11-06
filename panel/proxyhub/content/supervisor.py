"""The content supervisor (ناظر), decision D52: content goes out with no owner, but only after strong
automatic quality control; the owner is needed for nothing code can fix (D1).

One round a minute (loop ``content_qc``, proxyhub/reconcilers/content_qc.py), never with a
transaction open while Claude answers:

1. **Is a review needed?** Only for the next posting slot (proxyhub/posts/plan.py: the due slot not
   yet posted, else the next one), from ``lead_minutes`` before it, while no approved item is
   waiting, and while fewer than ``per_slot`` candidates were sent to Claude for that slot. This is
   what keeps Claude's use small: at most ``per_slot`` reviews per post.
2. **Rank** the pending candidates that passed phase A's deterministic gates (not a duplicate, not
   stale, media downloaded and checked, text cleaned) and whose source is on (:func:`score`): views
   per hour (log scale), freshness, how many other sources carry the story, an image. Two gates need
   no Claude and reject at once (backend ``rules``): a video with no cover frame (``no_preview``: the
   reviewer could not see it) and a photo too large for Claude to read (``image_too_large``).
3. **Ask Claude** about the best one (proxyhub/content/claude_backends.py): the bridge first, the API
   key as backup, each within its daily cap and after a pause when it failed. The prompt and its
   injection hardening: proxyhub/content/qc_prompt.py.
4. **Decide**: the answer is parsed strictly (unparsable = rejected), then checked again by code
   (proxyhub/content/qc_checks.py); approval only when Claude approves AND every check passes. In
   ``auto`` mode an approval takes Claude's title and body and status ``approved`` (the publisher
   posts it at the slot); a rejection is ``rejected``. In ``review`` mode nothing changes status:
   the decision, reasons and caption wait as a suggestion for the owner.
5. **Sources**: a source whose last ``source_window`` reviewed items were rejected at
   ``source_max_reject_pct`` or more (with at least ``source_min`` of them) is turned off, with a
   Persian reason on the sources page (:func:`judge_source`).
6. **Never a skipped post**: when nothing is approved in time (Claude down, over its cap, every
   candidate rejected, no source works) the publisher takes the evergreen content bank as before
   (proxyhub/posts/publisher.py). Nothing here can stop a post.
7. **Owner alerts**, only for what code cannot fix, each at most once per episode and then once a day
   (:func:`alert_due`): Claude unreachable for ``alert_after_hours`` (bridge gone, login expired,
   subscription limit, API key refused), and every source failing for as long.

Nothing of a candidate, the prompt or Claude's answer is logged; ``content_qc_calls`` keeps counts,
outcomes and token numbers only.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import and_, delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.orm import aliased

from proxyhub.content import candidates, qc_checks, qc_prompt, qc_settings, sources
from proxyhub.content.claude_backends import (
    API_URL,
    INPUT,
    NOT_STARTED,
    UNUSABLE,
    VAULT_NAME,
    ApiBackend,
    BridgeBackend,
    ClaudeWriter,
    ReviewBackend,
)
from proxyhub.content.messages import qc_check_fa, qc_outcome_fa
from proxyhub.content.qc_settings import QcConfig
from proxyhub.content.writer import Image, Review, ReviewRequest
from proxyhub.db.models_content import ContentCandidate, ContentMedia, ContentQcCall, ContentSource
from proxyhub.fleet.actions import iso, log_action
from proxyhub.posts import links, plan, publisher, views
from proxyhub.posts import settings as posting_settings
from proxyhub.telegram import store as tg_store
from proxyhub.telegram.templates import fa_digits
from proxyhub.vault import SecretValue, Vault, VaultError

log = logging.getLogger(__name__)

ACTOR: Final = "content_qc"
POOL: Final = 200
RULE_REJECTS_PER_ROUND: Final = 20
MAX_TRIES: Final = 3
MAX_IMAGE_SIDE: Final = 8000  # the Messages API's limit per image side
HEALTH_EVERY: Final = timedelta(minutes=5)
KEEP_CALLS: Final = timedelta(days=14)
ALERT_REPEAT: Final = timedelta(hours=24)
# Claude counts as unavailable only while it is still needed: a failure within this time.
STILL_NEEDED: Final = timedelta(hours=2)
COOLDOWN: Final[dict[str, timedelta]] = {
    "bridge_missing": timedelta(minutes=15),
    "bridge_denied": timedelta(minutes=15),
    "bridge_refused": timedelta(minutes=10),
    "claude_missing": timedelta(minutes=30),
    "auth": timedelta(minutes=30),
    "tools_present": timedelta(hours=6),
    "tools_unknown": timedelta(hours=1),
    "cap": timedelta(hours=1),
    "rate_limited": timedelta(minutes=30),
    "api_auth": timedelta(hours=1),
    "api_rate_limited": timedelta(minutes=10),
    "api_bad_model": timedelta(hours=1),
}
DEFAULT_COOLDOWN: Final = timedelta(minutes=3)

Clock = Callable[[], datetime]
HarvestFn = Callable[[AsyncConnection], Awaitable[links.Harvest]]


# --- ranking ------------------------------------------------------------------------------------------


def score(views: int | None, when: datetime, now: datetime, extra_sources: int, has_image: bool) -> float:
    """How much a candidate deserves a review (see the module doc). Deterministic."""
    age_h = max((now - when).total_seconds() / 3600, 0.0)
    hot = math.log10(1 + (views or 0) / max(age_h, 0.25))  # views per hour, the first quarter as one
    fresh = max(0.0, 1 - age_h / 24) * 2
    spread = 0.75 * min(max(extra_sources, 0), 3)
    return round(hot + fresh + spread + (0.25 if has_image else 0.0), 4)


@dataclass(frozen=True)
class Pick:
    id: int
    source_id: int
    when: datetime
    views: int | None
    media_kind: str | None
    media_state: str
    has_thumb: bool
    width: int | None
    height: int | None
    extra_sources: int = 0
    score: float = 0.0

    @property
    def image_role(self) -> str | None:
        """Which stored file Claude sees: the photo, or a video's cover frame."""
        if self.media_kind == "photo" and self.media_state == "ready":
            return "main"
        if self.media_kind == "video" and self.has_thumb:
            return "thumb"
        return None

    @property
    def post_media(self) -> str | None:
        """What the post will carry (a photo whose download failed goes as text)."""
        if self.media_kind == "photo":
            return "photo" if self.media_state == "ready" else None
        return self.media_kind


def pre_gate(pick: Pick) -> str | None:
    """A rejection that needs no Claude, or None."""
    if pick.media_kind == "video" and not pick.has_thumb:
        return "no_preview"
    if pick.image_role == "main" and max(pick.width or 0, pick.height or 0) > MAX_IMAGE_SIDE:
        return "image_too_large"
    return None


async def eligible(db: AsyncConnection, now: datetime) -> list[Pick]:
    """Pending, never reviewed candidates of enabled sources that passed phase A's gates, best first."""
    when = func.coalesce(ContentCandidate.published_at, ContentCandidate.created_at)
    thumb = aliased(ContentMedia)
    main = aliased(ContentMedia)
    has_thumb = (
        select(thumb.id).where(thumb.candidate_id == ContentCandidate.id, thumb.role == "thumb").exists()
    )
    rows = (
        await db.execute(
            select(
                ContentCandidate.id,
                ContentCandidate.source_id,
                when.label("when"),
                ContentCandidate.views,
                ContentCandidate.media_kind,
                ContentCandidate.media_state,
                has_thumb.label("has_thumb"),
                main.width,
                main.height,
            )
            .join(ContentSource, ContentSource.id == ContentCandidate.source_id)
            .outerjoin(main, and_(main.candidate_id == ContentCandidate.id, main.role == "main"))
            .where(
                ContentSource.enabled.is_(True),
                ContentCandidate.status == "pending",
                ContentCandidate.qc_at.is_(None),
                ContentCandidate.qc_tries < MAX_TRIES,
                when >= now - candidates.MAX_AGE,
                or_(
                    ContentCandidate.media_kind.is_(None),
                    and_(
                        ContentCandidate.media_kind == "photo",
                        ContentCandidate.media_state.in_(("ready", "failed")),
                    ),
                    and_(
                        ContentCandidate.media_kind == "video",
                        or_(has_thumb, ContentCandidate.thumb_url.is_(None)),
                    ),
                ),
            )
            .order_by(ContentCandidate.created_at.desc(), ContentCandidate.id.desc())
            .limit(POOL)
        )
    ).all()
    ids = [int(r.id) for r in rows]
    also = await candidates.also_in(db, ids)
    picks = []
    for r in rows:
        base = Pick(
            id=int(r.id),
            source_id=int(r.source_id),
            when=r.when,
            views=r.views,
            media_kind=r.media_kind,
            media_state=r.media_state,
            has_thumb=bool(r.has_thumb),
            width=r.width,
            height=r.height,
            extra_sources=len(also.get(int(r.id), [])),
        )
        picks.append(
            replace(
                base, score=score(base.views, base.when, now, base.extra_sources, base.image_role is not None)
            )
        )
    return sorted(picks, key=lambda p: (-p.score, -p.id))


# --- alerts -------------------------------------------------------------------------------------------


def alert_due(
    entry: dict[str, Any] | None,
    since: datetime | None,
    now: datetime,
    after: timedelta,
    repeat: timedelta = ALERT_REPEAT,
) -> tuple[bool, dict[str, Any] | None]:
    """De-duplication of one kind of owner alert. ``since``: when the trouble began (None: no trouble).
    Returns (send now?, the entry to keep). One alert once the trouble is ``after`` old, then at most
    one per ``repeat`` while it lasts; when it ends the entry goes, so the next episode alerts again."""
    if since is None:
        return False, None
    if now - since < after:
        return False, entry
    sent = _time(entry.get("sent_at")) if entry else None
    if sent is not None and now - sent < repeat:
        return False, entry
    return True, {"sent_at": iso(now)}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


CLAUDE_HINT_FA: Final[dict[str, str]] = {
    "auth": "ورود اشتراک Claude روی سرور باید تازه شود (پرامپت P22، گام ورود).",
    "bridge_missing": "پل Claude روی سرور نصب یا روشن نیست (پرامپت P22).",
    "bridge_denied": "worker به پل Claude دسترسی ندارد (پرامپت P22، گام اتصال).",
    "claude_missing": "Claude Code برای کاربر پل نصب نیست (پرامپت P22).",
    "rate_limited": (
        "سقف مصرف اشتراک Claude پر است و خودش باز میشود؛ اگر زیاد تکرار شد سقف روزانه را کم کنید."
    ),
    "api_auth": "کلید API Anthropic در داشبورد را عوض کنید.",
}


def claude_alert_fa(reason: str | None, hours: int) -> str:
    why = qc_outcome_fa(reason) or "نامعلوم"
    hint = CLAUDE_HINT_FA.get(reason or "", "اگر ادامه داشت، وضعیت پل را در صفحه‌ی صف بازبینی ببینید.")
    return (
        f"🤖 <b>ناظر محتوا (Claude) بیش از {fa_digits(hours)} ساعت در دسترس نیست.</b>\n"
        f"دلیل: {why}\n"
        "پست‌ها بی‌وقفه با بانک محتوای همیشگی میروند و چیزی بی بررسی منتشر نمیشود.\n"
        f"{hint}"
    )


def sources_alert_fa(hours: int) -> str:
    return (
        f"📰 <b>هیچ منبع محتوایی بیش از {fa_digits(hours)} ساعت کار نکرده است.</b>\n"
        "پست‌ها بی‌وقفه با بانک محتوای همیشگی میروند.\n"
        "در صفحه‌ی «منبع‌های محتوا» منبع تازه اضافه کنید یا خطای منبع‌ها را ببینید."
    )


# --- the supervisor -----------------------------------------------------------------------------------


@dataclass
class Outcome:
    action: str = "idle"
    candidate_id: int | None = None
    decision: str | None = None
    review: str | None = None
    rule_rejected: list[int] = field(default_factory=list)
    disabled_sources: list[int] = field(default_factory=list)
    alerts: list[str] = field(default_factory=list)


async def calls_24h(db: AsyncConnection, backend: str, now: datetime) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ContentQcCall)
                .where(
                    ContentQcCall.backend == backend,
                    ContentQcCall.reached.is_(True),
                    ContentQcCall.at > now - timedelta(hours=24),
                )
            )
        ).scalar_one()
    )


async def ready_count(db: AsyncConnection, now: datetime) -> int:
    when = func.coalesce(ContentCandidate.published_at, ContentCandidate.created_at)
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(ContentCandidate)
                .where(ContentCandidate.status == "approved", when >= now - candidates.MAX_AGE)
            )
        ).scalar_one()
    )


async def quality_by_source(db: AsyncConnection, window: int) -> dict[int, dict[str, int]]:
    """Each source's last ``window`` reviewed items (since its quality reset): how many, how many
    rejected. For the sources page and :func:`judge_source`."""
    ranked = (
        select(
            ContentCandidate.source_id,
            ContentCandidate.qc_decision,
            func.row_number()
            .over(
                partition_by=ContentCandidate.source_id,
                order_by=(ContentCandidate.qc_at.desc(), ContentCandidate.id.desc()),
            )
            .label("n"),
        )
        .join(ContentSource, ContentSource.id == ContentCandidate.source_id)
        .where(
            ContentCandidate.qc_decision.is_not(None),
            ContentCandidate.qc_error.is_(None),
            or_(
                ContentSource.quality_reset_at.is_(None),
                ContentCandidate.qc_at >= ContentSource.quality_reset_at,
            ),
        )
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                ranked.c.source_id,
                func.count().label("decided"),
                func.count().filter(ranked.c.qc_decision == "reject").label("rejected"),
            )
            .where(ranked.c.n <= window)
            .group_by(ranked.c.source_id)
        )
    ).all()
    return {int(r.source_id): {"decided": int(r.decided), "rejected": int(r.rejected)} for r in rows}


async def judge_source(db: AsyncConnection, config: QcConfig, source_id: int | None, now: datetime) -> bool:
    """Turn the source off when its recent items are mostly rejected (see the module doc)."""
    if source_id is None:
        return False
    found = await sources.get(db, source_id)
    if found is None or not found.enabled:
        return False
    stats = (await quality_by_source(db, config.source_window)).get(source_id)
    if stats is None or stats["decided"] < config.source_min:
        return False
    if stats["rejected"] * 100 < config.source_max_reject_pct * stats["decided"]:
        return False
    detail = {"rejected": stats["rejected"], "of": stats["decided"]}
    if not await sources.auto_disable(db, source_id, now, "qc_rejects", detail):
        return False
    await log_action(
        db,
        actor=ACTOR,
        action="content.source_auto_disabled",
        target=f"source:{source_id}",
        dry_run=False,
        allowed=True,
        reasons=["qc_rejects"],
        context={"kind": found.kind, "ref": found.ref, **detail},
    )
    return True


@dataclass(frozen=True)
class Prepared:
    pick: Pick
    request: ReviewRequest
    limits: qc_prompt.Limits
    names: list[str]


@dataclass
class Supervisor:
    engine: AsyncEngine
    clock: Clock
    bridge_socket: Path
    vault: Callable[[], Vault | None]
    harvest: HarvestFn = links.harvest
    api_url: str = API_URL

    # -- one round --

    async def run_round(self) -> Outcome:
        out = Outcome()
        now = self.clock()
        async with self.engine.begin() as conn:
            config = await qc_settings.load(conn)
            state = await qc_settings.load_state(conn)
            await conn.execute(delete(ContentQcCall).where(ContentQcCall.at < now - KEEP_CALLS))
            await self._sources_alert(conn, config, state, now, out)
        if config.backend in ("auto", "bridge") and self._health_due(state, now):
            health = await BridgeBackend(self.bridge_socket, config.bridge_model, config.timeout_s).health()
            state["health"] = {**health, "at": iso(now)}
        try:
            await self._work(config, state, now, out)
        finally:
            async with self.engine.begin() as conn:
                await self._claude_alert(conn, config, state, now, out)
                await qc_settings.save_state(conn, state)
        return out

    def _health_due(self, state: dict[str, Any], now: datetime) -> bool:
        health = state.get("health")
        at = _time(health.get("at")) if isinstance(health, dict) else None
        return at is None or now - at >= HEALTH_EVERY

    async def _work(self, config: QcConfig, state: dict[str, Any], now: datetime, out: Outcome) -> None:
        async with self.engine.begin() as conn:
            why = await self._not_needed(conn, config, state, now)
            if why is not None:
                out.action = why
                return
            picks = await eligible(conn, now)
            for pick in list(picks):
                code = pre_gate(pick)
                if code is not None and len(out.rule_rejected) < RULE_REJECTS_PER_ROUND:
                    await self._decide(
                        conn,
                        config,
                        pick,
                        now,
                        decision="reject",
                        reasons=[qc_check_fa(code)],
                        checks=[code],
                        backend="rules",
                    )
                    out.rule_rejected.append(pick.id)
                    if await judge_source(conn, config, pick.source_id, now):
                        out.disabled_sources.append(pick.source_id)
                if code is not None:
                    picks.remove(pick)
            picks = [p for p in picks if p.source_id not in out.disabled_sources]
            if not picks:
                out.action = "no_candidate"
                return
            backends, blocked = await self._backends(conn, config, state, now)
            if not backends:
                out.action = blocked
                if blocked in ("unavailable", "cooling"):
                    self._unavailable(state, now, state.get("unavailable_reason") or "unavailable")
                return
            prepared = await self._prepare(conn, config, picks[0], now)
        review = await ClaudeWriter(backends).review(prepared.request)
        async with self.engine.begin() as conn:
            await self._apply(conn, config, state, prepared, review, now, out)

    async def _not_needed(
        self, conn: AsyncConnection, config: QcConfig, state: dict[str, Any], now: datetime
    ) -> str | None:
        """Why no review is needed this round, or None."""
        if config.backend == "off":
            return "off"
        posting = await posting_settings.load(conn)
        if not posting.enabled or posting.posts_per_day == 0:
            return "posting_off"
        quiet = (await tg_store.posting_settings(conn)).quiet_windows
        count, start, end = posting.posts_per_day, posting.start, posting.end
        slot = plan.due_slot(now, count=count, start=start, end=end, quiet=quiet)
        if slot is None or await publisher.slot_taken(conn, posting.target_role, slot):
            upcoming = plan.upcoming(now, count=count, start=start, end=end, quiet=quiet, limit=1)
            slot = upcoming[0] if upcoming else None
        if slot is None:
            return "no_slot"
        if slot.at - now > timedelta(minutes=config.lead_minutes):
            return "waiting_slot"
        if await ready_count(conn, now) > 0:
            return "enough_ready"
        key = f"{slot.day.isoformat()}#{slot.index}"
        if state.get("slot") != key:
            state["slot"], state["slot_calls"] = key, 0
        if int(state.get("slot_calls") or 0) >= config.per_slot:
            return "slot_budget_used"
        return None

    async def _api_key(self, conn: AsyncConnection) -> SecretValue | None:
        vault = self.vault()
        if vault is None:
            return None
        try:
            return await vault.get(conn, VAULT_NAME)
        except VaultError as exc:
            log.warning("content qc: the API key could not be read (%s)", type(exc).__name__)
            return None

    async def _backends(
        self, conn: AsyncConnection, config: QcConfig, state: dict[str, Any], now: datetime
    ) -> tuple[list[ReviewBackend], str]:
        order = {"auto": ("bridge", "api"), "bridge": ("bridge",), "api": ("api",)}.get(config.backend, ())
        stored = state.get("cooldown")
        cooldown: dict[str, Any] = stored if isinstance(stored, dict) else {}
        out: list[ReviewBackend] = []
        why: list[str] = []
        for kind in order:
            cap = config.bridge_daily_cap if kind == "bridge" else config.api_daily_cap
            if await calls_24h(conn, kind, now) >= cap:
                why.append("over_cap")
                continue
            until = _time(cooldown.get(kind))
            if until is not None and until > now:
                why.append("cooling")
                continue
            if kind == "bridge":
                out.append(BridgeBackend(self.bridge_socket, config.bridge_model, config.timeout_s))
                continue
            key = await self._api_key(conn)
            if key is None:
                if config.backend == "api":
                    why.append("unavailable")
                continue
            out.append(ApiBackend(key, config.api_model, config.timeout_s, url=self.api_url))
        if out:
            return out, "ok"
        if why and all(w == "over_cap" for w in why):
            return [], "over_cap"
        return [], "cooling" if "cooling" in why else "unavailable"

    async def _prepare(self, conn: AsyncConnection, config: QcConfig, pick: Pick, now: datetime) -> Prepared:
        found = await candidates.get(conn, pick.id)
        assert found is not None
        image: Image | None = None
        role = pick.image_role
        if role is not None:
            stored = (await candidates.media_rows(conn, [pick.id])).get(pick.id, {}).get(role)
            if stored is not None and stored.mime in ("image/jpeg", "image/png"):
                data = await candidates.media_data(conn, stored.id)
                if data is not None:
                    image = Image(mime=stored.mime, data=data[1])  # type: ignore[arg-type]
        post_media = pick.post_media
        room: int | None = None
        if post_media is not None:
            try:
                room = await views.caption_budget(conn, now, await self.harvest(conn))
            except Exception as exc:  # the room is a refinement; a failure must not stop the review
                log.warning("content qc: caption room unknown (%s)", type(exc).__name__)
        limits = qc_prompt.limits_for(room, media=post_media is not None)
        nonce = qc_prompt.new_nonce()
        request = ReviewRequest(
            system=qc_prompt.system_prompt(
                config, limits, nonce, media_kind=post_media, image_sent=image is not None
            ),
            user_text=qc_prompt.user_text(found.source_text, nonce, media_kind=post_media if image else None),
            image=image,
        )
        names = qc_checks.source_names((s.kind, s.ref, s.label) for s in await sources.list_all(conn))
        return Prepared(pick, request, limits, names)

    def _unavailable(self, state: dict[str, Any], now: datetime, reason: str) -> None:
        state.setdefault("unavailable_since", iso(now))
        state["unavailable_last"] = iso(now)
        state["unavailable_reason"] = reason

    async def _apply(
        self,
        conn: AsyncConnection,
        config: QcConfig,
        state: dict[str, Any],
        prepared: Prepared,
        review: Review,
        now: datetime,
        out: Outcome,
    ) -> None:
        pick = prepared.pick
        out.candidate_id, out.review = pick.id, review.outcome
        state["last_outcome"], state["last_outcome_at"] = review.outcome, iso(now)
        await self._log_calls(conn, review, pick.id, now)
        cooldown = dict(state.get("cooldown") or {})
        for backend, outcome in review.tried:
            if outcome != "ok" and outcome not in UNUSABLE:
                cooldown[backend] = iso(now + COOLDOWN.get(outcome, DEFAULT_COOLDOWN))
            else:
                cooldown.pop(backend, None)
        state["cooldown"] = cooldown
        if review.outcome in INPUT:  # our request was refused: the candidate is tried again later
            out.action = "input_refused"
            await conn.execute(
                update(ContentCandidate)
                .where(ContentCandidate.id == pick.id)
                .values(qc_tries=ContentCandidate.qc_tries + 1, qc_error=review.outcome[:40], updated_at=now)
            )
            return
        if review.outcome != "ok" and review.outcome not in UNUSABLE:  # Claude was not reached
            out.action = "unavailable"
            self._unavailable(state, now, review.outcome)
            return
        state["slot_calls"] = int(state.get("slot_calls") or 0) + 1
        state["last_ok_at"] = iso(now)
        for key in ("unavailable_since", "unavailable_last", "unavailable_reason"):
            state.pop(key, None)
        out.action = "reviewed"
        if review.outcome != "ok":  # reached, but no usable answer
            refused = review.outcome == "refused"
            out.decision = "reject"
            await self._decide(
                conn,
                config,
                pick,
                now,
                decision="reject",
                reasons=[qc_check_fa("refused")] if refused else [],
                checks=["refused" if refused else "unparsable"],
                backend=review.backend,
                model=review.model,
                error=None if refused else review.outcome,
            )
        else:
            try:
                verdict = qc_prompt.parse_verdict(review.text)
            except qc_prompt.VerdictInvalid:
                out.decision = "reject"
                await self._decide(
                    conn,
                    config,
                    pick,
                    now,
                    decision="reject",
                    reasons=[],
                    checks=["unparsable"],
                    backend=review.backend,
                    model=review.model,
                    error="unparsable",
                )
            else:
                caption = qc_checks.tidy_caption(verdict)
                checks = qc_checks.check(
                    verdict, caption, config=config, limits=prepared.limits, names=prepared.names
                )
                decision = "approve" if verdict.decision == "approve" and not checks else "reject"
                out.decision = decision
                await self._decide(
                    conn,
                    config,
                    pick,
                    now,
                    decision=decision,
                    reasons=list(verdict.reasons),
                    checks=checks,
                    backend=review.backend,
                    model=review.model,
                    flags=sorted(verdict.flags),
                    category=verdict.category,
                    caption=caption if decision == "approve" else None,
                    claude_decision=verdict.decision,
                )
        if await judge_source(conn, config, pick.source_id, now):
            out.disabled_sources.append(pick.source_id)

    async def _log_calls(
        self, conn: AsyncConnection, review: Review, candidate_id: int, now: datetime
    ) -> None:
        rows = []
        last = len(review.tried) - 1
        for i, (backend, outcome) in enumerate(review.tried):
            final = i == last
            rows.append(
                {
                    "at": now,
                    "backend": backend,
                    "outcome": outcome[:40],
                    "reached": outcome not in NOT_STARTED,
                    "candidate_id": candidate_id,
                    "duration_ms": review.duration_ms if final else None,
                    "model": review.model if final else None,
                    "input_tokens": review.input_tokens if final else None,
                    "output_tokens": review.output_tokens if final else None,
                    "cost_usd": review.cost_usd if final else None,
                }
            )
        if rows:
            await conn.execute(insert(ContentQcCall), rows)

    async def _decide(
        self,
        conn: AsyncConnection,
        config: QcConfig,
        pick: Pick,
        now: datetime,
        *,
        decision: str,
        reasons: list[str],
        checks: list[str],
        backend: str | None,
        model: str | None = None,
        flags: list[str] | None = None,
        category: str | None = None,
        caption: qc_checks.Caption | None = None,
        error: str | None = None,
        claude_decision: str | None = None,
    ) -> None:
        """Record the review on the candidate and, in ``auto`` mode, act on it (see the module doc).
        The status changes only if the candidate is still pending and unreviewed (the owner may have
        acted meanwhile)."""
        qc: dict[str, Any] = {
            "qc_decision": decision,
            "qc_reasons": [r[: qc_prompt.MAX_REASON_CHARS] for r in reasons][: qc_prompt.MAX_REASONS],
            "qc_flags": flags or [],
            "qc_checks": checks,
            "qc_category": category,
            "qc_error": error,
            "qc_backend": backend,
            "qc_model": (model or "")[:64] or None,
            "qc_at": now,
            "qc_tries": ContentCandidate.qc_tries + 1,
            "updated_at": now,
        }
        act: dict[str, Any] = {}
        if caption is not None:
            act.update(title=caption.title, body=caption.body)
        if config.mode == "auto":
            act.update(
                status="approved" if decision == "approve" else "rejected", reviewed_at=now, decided_by="qc"
            )
        done = (
            await conn.execute(
                update(ContentCandidate)
                .where(
                    ContentCandidate.id == pick.id,
                    ContentCandidate.status == "pending",
                    ContentCandidate.qc_at.is_(None),
                )
                .values(**qc, **act)
                .returning(ContentCandidate.id)
            )
        ).first()
        if done is None:  # the owner decided meanwhile: keep the review for the record only
            await conn.execute(update(ContentCandidate).where(ContentCandidate.id == pick.id).values(**qc))
        await log_action(
            conn,
            actor=ACTOR,
            action="content.qc_approved" if decision == "approve" else "content.qc_rejected",
            target=f"candidate:{pick.id}",
            dry_run=config.mode != "auto",
            allowed=decision == "approve",
            reasons=checks or [decision],
            context={
                "mode": config.mode,
                "backend": backend,
                "claude": claude_decision,
                "category": category,
                "flags": flags or [],
                "applied": done is not None,
            },
        )

    # -- alerts --

    async def _sources_alert(
        self, conn: AsyncConnection, config: QcConfig, state: dict[str, Any], now: datetime, out: Outcome
    ) -> None:
        after = timedelta(hours=config.alert_after_hours)
        found = await sources.list_all(conn)
        enabled = [s for s in found if s.enabled]
        working = any(s.last_ok_at is not None and now - s.last_ok_at <= after for s in enabled)
        supervisor_off = any(not s.enabled and s.disabled_reason for s in found)
        down = bool(found) and not working and (bool(enabled) or supervisor_off)
        if down:
            state.setdefault("sources_down_since", iso(now))
        else:
            state.pop("sources_down_since", None)
        since = _time(state.get("sources_down_since"))
        text_html = sources_alert_fa(config.alert_after_hours)
        await self._alert(conn, state, "sources_down", since, now, after, text_html, out)

    async def _claude_alert(
        self, conn: AsyncConnection, config: QcConfig, state: dict[str, Any], now: datetime, out: Outcome
    ) -> None:
        after = timedelta(hours=config.alert_after_hours)
        if config.backend == "off":  # the owner's choice: nothing to tell
            for key in ("unavailable_since", "unavailable_last", "unavailable_reason"):
                state.pop(key, None)
        since = _time(state.get("unavailable_since"))
        last = _time(state.get("unavailable_last"))
        if since is not None and (last is None or now - last > STILL_NEEDED):
            return  # Claude was not needed lately (between slots, overnight): wait, forget nothing
        text_html = claude_alert_fa(state.get("unavailable_reason"), config.alert_after_hours)
        await self._alert(conn, state, "claude_unavailable", since, now, after, text_html, out)

    async def _alert(
        self,
        conn: AsyncConnection,
        state: dict[str, Any],
        key: str,
        since: datetime | None,
        now: datetime,
        after: timedelta,
        text_html: str,
        out: Outcome,
    ) -> None:
        alerts = dict(state.get("alerts") or {})
        send, entry = alert_due(alerts.get(key), since, now, after)
        if entry is None:
            alerts.pop(key, None)
        else:
            alerts[key] = entry
        state["alerts"] = alerts
        if not send:
            return
        await tg_store.enqueue_owner_alert(conn, text_html)
        await log_action(
            conn,
            actor=ACTOR,
            action="content.owner_alert",
            target=key,
            dry_run=False,
            allowed=True,
            reasons=[key],
            context={"since": iso(since)},
        )
        out.alerts.append(key)


# --- the dashboard's view -------------------------------------------------------------------------------


async def status(db: AsyncConnection, now: datetime, api_key_set: bool) -> dict[str, Any]:
    """The supervisor's settings and state for the dashboard (no secret: whether a key is set only)."""
    config = await qc_settings.load(db)
    state = await qc_settings.load_state(db)
    decided = (
        await db.execute(
            select(ContentCandidate.qc_decision, func.count())
            .where(
                ContentCandidate.qc_at > now - timedelta(hours=24), ContentCandidate.qc_decision.is_not(None)
            )
            .group_by(ContentCandidate.qc_decision)
        )
    ).all()
    counts = {str(r[0]): int(r[1]) for r in decided}
    health = state.get("health") if isinstance(state.get("health"), dict) else None
    return {
        "config": config.as_json(),
        "defaults": qc_settings.DEFAULTS.as_json(),
        "options": {
            "categories": list(qc_settings.CATEGORIES),
            "flags": list(qc_settings.OWNER_FLAGS),
            "api_models": list(qc_settings.API_MODELS),
        },
        "api_key_set": api_key_set,
        "calls_24h": {"bridge": await calls_24h(db, "bridge", now), "api": await calls_24h(db, "api", now)},
        "decisions_24h": {"approve": counts.get("approve", 0), "reject": counts.get("reject", 0)},
        "ready": await ready_count(db, now),
        "bridge": health,
        "bridge_error_fa": qc_outcome_fa(health.get("error")) if health and health.get("error") else None,
        "last_ok_at": state.get("last_ok_at"),
        "last_outcome": state.get("last_outcome"),
        "last_outcome_fa": qc_outcome_fa(state.get("last_outcome")),
        "last_outcome_at": state.get("last_outcome_at"),
        "unavailable_since": state.get("unavailable_since"),
        "unavailable_reason_fa": qc_outcome_fa(state.get("unavailable_reason")),
        "slot": {"key": state.get("slot"), "calls": int(state.get("slot_calls") or 0)},
        "alerts": sorted((state.get("alerts") or {}).keys()),
    }
