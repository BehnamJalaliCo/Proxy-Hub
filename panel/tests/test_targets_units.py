"""P15 (pure): the automatic REALITY target / fake-TLS domain choice among the owner's candidate
sites, from their TLS handshakes measured from Iran and our nodes' own checks. Globalping answers are
the real recorded shapes in tests/test_net_support.py (decision D10); nothing imitates a service.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError
from test_net_support import (
    HTTP_OK_GITHUB_RIGHTEL,
    HTTP_TLS_TIMEOUT_RIGHTEL_443,
    MCI_PROBE,
    RIGHTEL_PROBE,
    gp_https,
)

from proxyhub.mtproto.settings import MtprotoSettings
from proxyhub.probes.globalping import parse_http
from proxyhub.probes.settings import ProbeSettings
from proxyhub.probes.types import Measurement
from proxyhub.reality.settings import RealitySettings
from proxyhub.targets.choice import (
    Candidate,
    NetResult,
    choose_mtproto,
    choose_reality,
    confirm_due,
    iran_view,
    node_check,
    open_from_iran,
)
from proxyhub.targets.service import (
    Due,
    due_round,
    mtproto_switch_text,
    net_results,
    no_candidate_text,
    reality_switch_text,
    round_cost,
    target_locations,
)
from proxyhub.targets.settings import DEFAULT_CANDIDATES, TargetSettings

KEYS = ["asn:197207", "asn:57218"]
KEY_ASNS = [197207, 57218]
T0 = datetime(2026, 11, 5, 16, 0, tzinfo=UTC)


# --- the automatic target choice ------------------------------------------------------------------------


def cand(
    host: str,
    latest: dict[str, str] | None,
    prev: dict[str, str] | None = None,
    *,
    ms: float = 300.0,
    hours_ago: float = 1,
    node_ok: float | None = None,
    node_fail: float | None = None,
) -> Candidate:
    def nets(states: dict[str, str] | None) -> dict[str, NetResult]:
        return {k: NetResult(v, k, None, ms if v == "up" else None) for k, v in (states or {}).items()}  # type: ignore[arg-type]

    at = T0 - timedelta(hours=hours_ago)
    return Candidate(
        host=host,
        measured_at=at if latest is not None else None,
        iran=nets(latest),
        prev_measured_at=at - timedelta(hours=6) if prev is not None else None,
        iran_prev=nets(prev),
        node_ok_at=T0 - timedelta(hours=node_ok) if node_ok is not None else None,
        node_fail_at=T0 - timedelta(hours=node_fail) if node_fail is not None else None,
    )


UP = {"asn:197207": "up", "asn:57218": "up"}
MCI_DOWN = {"asn:197207": "down", "asn:57218": "up"}
LISTED = list(DEFAULT_CANDIDATES)


def states(*cs: Candidate) -> dict[str, Candidate]:
    return {c.host: c for c in cs}


def test_iran_view_and_node_check() -> None:
    c = cand("www.microsoft.com", MCI_DOWN, MCI_DOWN)
    v = iran_view(c, KEYS, T0)
    assert (v.open_on, v.down_on, v.blocked_on, v.ms, v.fresh) == (
        ("asn:57218",),
        ("asn:197207",),
        ("asn:197207",),
        300.0,
        True,
    )
    assert not open_from_iran(c, KEYS, T0) and open_from_iran(cand("a", UP), KEYS, T0)
    assert not open_from_iran(cand("a", UP, hours_ago=25), KEYS, T0)  # too old to choose by
    assert not iran_view(cand("a", MCI_DOWN, MCI_DOWN, hours_ago=25), KEYS, T0).blocked_on
    assert node_check(cand("a", UP, node_ok=1, node_fail=2), T0) == "passed"
    assert node_check(cand("a", UP, node_ok=2, node_fail=1), T0) == "failed"
    assert node_check(cand("a", UP, node_fail=24 * 8), T0) is None and node_check(None, T0) is None


def test_reality_auto_leaves_dl_google_for_the_best_open_candidate() -> None:
    st_ = states(
        cand("dl.google.com", UP, ms=2200),
        cand("www.apple.com", UP, ms=500),
        cand("github.com", UP, ms=400),
        cand("aws.amazon.com", {"asn:57218": "up"}, ms=100),
        cand("www.speedtest.net", MCI_DOWN, ms=50),
    )
    c = choose_reality("dl.google.com", LISTED, st_, KEYS, T0)
    assert (c.host, c.switch, c.why) == ("github.com", True, "not_a_candidate")  # open on both, fastest
    # A candidate our nodes already passed comes first; one they failed is never chosen.
    st2 = {**st_, "www.apple.com": cand("www.apple.com", UP, ms=500, node_ok=2)}
    assert choose_reality("dl.google.com", LISTED, st2, KEYS, T0).host == "www.apple.com"
    st3 = {**st_, "github.com": cand("github.com", UP, ms=400, node_fail=1)}
    assert choose_reality("dl.google.com", LISTED, st3, KEYS, T0).host == "www.apple.com"


def test_reality_auto_keeps_a_working_current_target() -> None:
    st_ = states(cand("www.apple.com", UP, node_ok=1), cand("github.com", UP, ms=10))
    assert choose_reality("www.apple.com", LISTED, st_, KEYS, T0).switch is False  # no churn
    once = states(cand("www.apple.com", MCI_DOWN, UP), cand("github.com", UP))
    assert choose_reality("www.apple.com", LISTED, once, KEYS, T0).switch is False  # one round: wait
    assert confirm_due(once["www.apple.com"], KEYS, T0, timedelta(minutes=30))
    twice = states(cand("www.apple.com", MCI_DOWN, MCI_DOWN), cand("github.com", UP))
    c = choose_reality("www.apple.com", LISTED, twice, KEYS, T0)
    assert (c.host, c.why, c.networks) == ("github.com", "blocked_from_iran", ("asn:197207",))
    assert not confirm_due(twice["www.apple.com"], KEYS, T0, timedelta(minutes=30))
    failed = states(cand("www.apple.com", UP, node_ok=3, node_fail=1), cand("github.com", UP))
    assert choose_reality("www.apple.com", LISTED, failed, KEYS, T0).why == "node_check_failed"
    # Nothing better: the current one stays, and the reason is kept for the owner's note.
    alone = states(cand("www.apple.com", MCI_DOWN, MCI_DOWN))
    c = choose_reality("www.apple.com", LISTED, alone, KEYS, T0)
    assert (c.host, c.switch, c.why) == ("www.apple.com", False, "blocked_from_iran")
    # No measurement at all yet: nothing moves.
    assert choose_reality("dl.google.com", LISTED, {}, KEYS, T0).switch is False


def test_mtproto_auto_replaces_only_a_failing_domain() -> None:
    st_ = states(
        cand("www.microsoft.com", MCI_DOWN), cand("www.apple.com", UP), cand("github.com", UP, ms=900)
    )
    assert choose_mtproto("www.microsoft.com", LISTED, st_, KEYS, T0).switch is False  # one round
    st_["www.microsoft.com"] = cand("www.microsoft.com", MCI_DOWN, MCI_DOWN)
    c = choose_mtproto("www.microsoft.com", LISTED, st_, KEYS, T0)
    assert (c.host, c.switch, c.why) == ("www.apple.com", True, "blocked_from_iran")
    # Not a candidate but working: kept (every ee link of the nodes without their own name carries it).
    assert (
        choose_mtproto("www.example.org", LISTED, {"www.example.org": cand("x", UP)}, KEYS, T0).switch
        is False
    )


def test_target_texts_are_persian_and_name_the_reason() -> None:
    blocked = choose_reality(
        "www.apple.com",
        LISTED,
        states(cand("www.apple.com", MCI_DOWN, MCI_DOWN), cand("github.com", UP)),
        KEYS,
        T0,
    )
    text = reality_switch_text("www.apple.com", "github.com", blocked, "ws_tls", False)
    assert "همراه اول" in text and "github.com" in text and "WebSocket" in text and "دستی" in text
    text = reality_switch_text(
        "dl.google.com",
        "github.com",
        choose_reality("dl.google.com", LISTED, states(cand("github.com", UP)), KEYS, T0),
        "reality",
        True,
    )
    assert "فهرست سایت‌های نامزد" in text and "قبول کرده‌اند" in text and "WebSocket" not in text
    assert "D42" in mtproto_switch_text("www.microsoft.com", "www.apple.com", blocked)
    assert "سایت هدف Reality و دامنه‌ی TLS" in no_candidate_text(["reality", "mtproto"])


def test_target_round_planning() -> None:
    s = TargetSettings()
    hosts = [*s.candidates, "dl.google.com", "www.microsoft.com"]
    current = ["dl.google.com", "www.microsoft.com"]
    assert due_round({}, s, hosts, current, {}, KEYS, T0) == Due(tuple(hosts), True, "schedule")
    done = {
        "last_full_at": (T0 - timedelta(hours=1)).isoformat(),
        "last_attempt_at": (T0 - timedelta(hours=1)).isoformat(),
    }
    all_known = {h: cand(h, UP, UP) for h in hosts}
    assert due_round(done, s, hosts, current, all_known, KEYS, T0) is None
    later = T0 + timedelta(hours=5, minutes=1)
    assert due_round(done, s, hosts, current, all_known, KEYS, later) == Due(tuple(hosts), True, "schedule")
    # A current target that looked closed once is looked at again after 30 minutes.
    suspicious = {**all_known, "www.microsoft.com": cand("www.microsoft.com", MCI_DOWN, UP)}
    assert due_round(done, s, hosts, current, suspicious, KEYS, T0) == Due(
        ("www.microsoft.com",), False, "confirm"
    )
    # A site the owner just added: within minutes.
    new = {h: c for h, c in all_known.items() if h != "www.samsung.com"}
    assert due_round(done, s, hosts, current, new, KEYS, T0) == Due(("www.samsung.com",), False, "new")
    # A failed attempt waits before the next one.
    tried = {**done, "last_attempt_at": (T0 - timedelta(minutes=5)).isoformat()}
    assert due_round(tried, s, hosts, current, new, KEYS, T0) is None
    failed_full = {
        "last_full_at": (T0 - timedelta(hours=7)).isoformat(),
        "last_attempt_at": (T0 - timedelta(minutes=12)).isoformat(),
    }
    assert due_round(failed_full, s, hosts, current, all_known, KEYS, T0) is None
    assert due_round(failed_full, s, hosts, current, all_known, KEYS, T0 + timedelta(minutes=20)) is not None
    assert round_cost(hosts, 5, "github.com") == 5 * len(hosts)  # the control's own result is reused
    assert target_locations(ProbeSettings()) == [{"asn": 197207, "limit": 3}, {"asn": 57218, "limit": 2}]


def test_net_results_count_only_where_the_control_worked() -> None:
    control_raw = gp_https("c", "github.com", 443, [(RIGHTEL_PROBE, HTTP_OK_GITHUB_RIGHTEL)], at="x")
    control = Measurement(
        "globalping", "https", "control", "github.com", 443, "ok", T0,
        networks=tuple(parse_http(control_raw, KEY_ASNS)),
    )  # fmt: skip
    raw = gp_https(
        "m",
        "www.microsoft.com",
        443,
        [
            (MCI_PROBE, {**HTTP_TLS_TIMEOUT_RIGHTEL_443, "resolvedAddress": "23.36.157.47"}),
            (RIGHTEL_PROBE, HTTP_OK_GITHUB_RIGHTEL),
        ],
        at="x",
    )
    m = Measurement(
        "globalping",
        "https",
        "target",
        "www.microsoft.com",
        443,
        "ok",
        T0,
        networks=tuple(parse_http(raw, KEY_ASNS)),
    )
    nets = net_results(m, control)
    assert nets["asn:57218"].state == "up" and nets["asn:57218"].ms == 548.0
    assert nets["asn:197207"].state == "nodata"  # the control had no MCI probe: proves nothing
    assert NetResult.from_json(nets["asn:57218"].to_json()) == nets["asn:57218"]
    assert NetResult.from_json("junk") == NetResult("nodata")


def test_target_settings_and_modes() -> None:
    s = TargetSettings.model_validate({"candidates": [" WWW.Apple.com. ", "www.apple.com", "github.com"]})
    assert s.candidates == ["www.apple.com", "github.com"]
    for bad in ([], ["1.1.1.1"], ["no spaces.com"], [f"s{i}.example.com" for i in range(13)]):
        with pytest.raises(ValidationError):
            TargetSettings.model_validate({"candidates": bad})
    assert "www.microsoft.com" not in DEFAULT_CANDIDATES and "dl.google.com" not in DEFAULT_CANDIDATES
    assert RealitySettings().target_mode == "auto" and MtprotoSettings().target_mode == "auto"
    with pytest.raises(ValidationError):
        RealitySettings.model_validate({"target_mode": "sometimes"})
    with pytest.raises(ValidationError):
        MtprotoSettings.model_validate({"target_mode": "sometimes"})
    assert RealitySettings.model_validate({"target_mode": "manual"}).target_mode == "manual"
