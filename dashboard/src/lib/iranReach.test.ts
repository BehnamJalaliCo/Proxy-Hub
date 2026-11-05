import { describe, expect, it } from "vitest";

import {
  LOCATION_CLASS,
  candidateState,
  candidatesProblem,
  closedAllowed,
  failureText,
  locationClassInfo,
  locationRow,
  networkLabel,
  nodeCheckInfo,
  normalizeHost,
  portsText,
  targetCell,
} from "./iranReach";
import type { LocationReach, LocationNetwork, TargetCandidate, TargetNetwork } from "./types";

const PERSIAN = /[آ-ی]/;
const KEYS = ["asn:197207", "asn:57218"];

function net(key: string, state: "open" | "closed", over: Partial<LocationNetwork> = {}): LocationNetwork {
  return { key, network: key, asn: null, state, at: "2026-11-05T16:00:00Z", nodes_open: state === "open" ? 1 : 0, nodes_closed: state === "closed" ? 1 : 0, key_network: KEYS.includes(key), ...over };
}

function loc(location: string, cls: LocationReach["class"], networks: LocationNetwork[], allowed = true): LocationReach {
  return { location, allowed, class: cls, networks };
}

function tnet(key: string, state: TargetNetwork["state"], ms: number | null = null): TargetNetwork {
  return { key, state, network: key, asn: null, ms, key_network: KEYS.includes(key) };
}

function cand(over: Partial<TargetCandidate>): TargetCandidate {
  return {
    host: "www.apple.com",
    listed: true,
    current_for: [],
    measured_at: "2026-11-05T16:00:00Z",
    networks: [tnet("asn:197207", "up", 540), tnet("asn:57218", "up", 548)],
    prev_measured_at: null,
    prev_networks: [],
    open: true,
    blocked_on: [],
    ms: 544,
    node_check: null,
    node_ok_at: null,
    node_fail_at: null,
    ...over,
  };
}

describe("location table", () => {
  it("has a Persian word and a tone for every class", () => {
    for (const info of Object.values(LOCATION_CLASS)) {
      expect(info.label).toMatch(PERSIAN);
      expect(info.hint).toMatch(PERSIAN);
    }
    expect(locationClassInfo("bad").tone).toBe("danger");
    expect(locationClassInfo("something-new")).toBe(LOCATION_CLASS.unknown);
  });

  it("shows the key networks, then the others together", () => {
    const fsn = loc("fsn1", "bad", [net("asn:197207", "closed"), net("asn:57218", "closed", { nodes_closed: 2 }), net("asn:202468", "open", { network: "AbrArvan" })]);
    const { cells, others } = locationRow(fsn, KEYS);
    expect(cells.map((c) => [c.text, c.tone])).toEqual([
      ["بسته", "danger"],
      ["بسته", "danger"],
    ]);
    expect(cells[1]?.title).toContain("۲ node بسته");
    expect([others.text, others.tone]).toEqual(["باز", "success"]);
    expect(others.title).toContain("AbrArvan: باز");
    const empty = locationRow(loc("hil", "unknown", []), KEYS);
    expect(empty.cells.every((c) => c.state === null && c.text === "—")).toBe(true);
    expect(empty.others.state).toBeNull();
  });

  it("names the allowed locations that are closed", () => {
    const rows = [loc("fsn1", "bad", []), loc("hel1", "good", []), loc("ash", "bad", [], false)];
    expect(closedAllowed(rows)).toEqual(["fsn1"]);
  });

  it("names operators in Persian", () => {
    expect(networkLabel("asn:197207")).toBe("همراه اول");
    expect(networkLabel("asn:57218")).toBe("رایتل");
    expect(networkLabel("asn:202468", "AbrArvan")).toBe("AbrArvan");
    expect(networkLabel("ch:ir1")).toBe("ch:ir1");
  });
});

describe("TLS handshakes of a node", () => {
  it("says why a handshake failed and on which ports", () => {
    expect(failureText("tls_timeout")).toMatch(PERSIAN);
    expect(failureText("tcp_timeout")).not.toBe(failureText("tls_timeout"));
    expect(failureText("new-kind")).toBe("خطا");
    expect(failureText(null)).toBe("");
    const n = (port: number | null) => ({ network: "x", asn: null, ok: 1, fail: 0, at: "", port });
    expect(portsText([n(8443), n(443), n(443), n(null)])).toBe("۴۴۳ و ۸۴۴۳");
  });
});

describe("candidate sites", () => {
  it("says what Iran says about each one", () => {
    expect(candidateState(cand({}))).toEqual({ label: "باز از ایران", tone: "success" });
    expect(candidateState(cand({ blocked_on: ["asn:197207"], open: false })).label).toBe("بسته از همراه اول");
    expect(candidateState(cand({ measured_at: null, networks: [], open: false })).tone).toBe("neutral");
    const once = cand({ open: false, networks: [tnet("asn:197207", "down"), tnet("asn:57218", "up", 500)] });
    expect(candidateState(once)).toEqual({ label: "یک بار بسته دیده شد", tone: "warning" });
    expect(candidateState(cand({ open: false, networks: [tnet("asn:57218", "nodata")] })).tone).toBe("neutral");
  });

  it("shows time, closed, or nothing per network", () => {
    expect(targetCell(tnet("asn:57218", "up", 548.4))).toEqual({ tone: "success", text: "۵۴۸ ms" });
    expect(targetCell(tnet("asn:57218", "up"))).toEqual({ tone: "success", text: "باز" });
    expect(targetCell(tnet("asn:197207", "down"))).toEqual({ tone: "danger", text: "بسته" });
    expect(targetCell(undefined).text).toBe("—");
    expect(targetCell(tnet("asn:197207", "nodata")).tone).toBe("neutral");
  });

  it("shows the nodes' own check", () => {
    expect(nodeCheckInfo(cand({ node_check: "passed" })).tone).toBe("success");
    expect(nodeCheckInfo(cand({ node_check: "failed" })).tone).toBe("danger");
    expect(nodeCheckInfo(cand({})).label).toBe("—");
  });

  it("checks the list like the server", () => {
    expect(normalizeHost(" WWW.Apple.com. ")).toBe("www.apple.com");
    expect(candidatesProblem(["www.apple.com", "github.com"])).toBeNull();
    expect(candidatesProblem([])).toMatch(PERSIAN);
    expect(candidatesProblem(["1.1.1.1"])).toContain("1.1.1.1");
    expect(candidatesProblem(["no spaces.com"])).not.toBeNull();
    expect(candidatesProblem(Array.from({ length: 13 }, (_, i) => `s${String(i)}.example.com`))).toContain("۱۲");
  });
});
