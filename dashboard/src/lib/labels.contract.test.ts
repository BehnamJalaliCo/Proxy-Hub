// The panel and the dashboard must agree on the actions_log codes: every action and actor the panel
// writes has a Persian word here, so the owner never reads a raw English code on the Events page.
// The panel's sources are read as text (no Python needed); a new log_action without a label fails.
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import { actionLabel, actorLabel, reasonLabel } from "./labels";

const PANEL = fileURLToPath(new URL("../../../panel/proxyhub", import.meta.url));

/** Writers of actions_log rows, with the position of the action among their arguments. */
const WRITERS: { name: string; actionArg: number | null }[] = [
  { name: "log_action", actionArg: null },
  { name: "_log", actionArg: 1 },
  { name: "_log_candidate", actionArg: 2 },
  { name: "_log_dns", actionArg: 2 },
  { name: "_cleared", actionArg: null },
  { name: "_refuse_owner_action", actionArg: null },
  { name: "audit.record", actionArg: 1 },
  { name: "audit", actionArg: 1 },
];

/** Actions built at run time (f-strings), spelled out. */
const DYNAMIC_ACTIONS = [
  "account_active",
  "account_preparing",
  "account_draining",
  "account_retired",
  "account_locked",
  "analytics.traffic_warning",
  "analytics.cpu_warning",
  "sponsor.campaign_active",
  "sponsor.campaign_finished",
];

function pythonFiles(dir: string): string[] {
  return readdirSync(dir, { recursive: true, encoding: "utf8" })
    .filter((f) => f.endsWith(".py"))
    .map((f) => join(dir, f));
}

/** The argument list of the call that starts at `open` (the index of its "("), split at top level. */
function callArgs(src: string, open: number): string[] {
  const args: string[] = [];
  let depth = 0;
  let quote: string | null = null;
  let start = open + 1;
  for (let i = open; i < src.length; i++) {
    const c = src[i];
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'") quote = c;
    else if (c === "(" || c === "[" || c === "{") depth++;
    else if (c === ")" || c === "]" || c === "}") {
      depth--;
      if (depth === 0) {
        args.push(src.slice(start, i).trim());
        return args.filter((a) => a !== "");
      }
    } else if (c === "," && depth === 1) {
      args.push(src.slice(start, i).trim());
      start = i + 1;
    }
  }
  return args;
}

/** The string values an argument expression can take: literals, module constants, `"a" if x else "b"`. */
function values(expr: string, consts: Map<string, string>): string[] {
  const branches = /^(.+?) if .+ else (.+)$/s.exec(expr);
  if (branches) return [...values(branches[1] ?? "", consts), ...values(branches[2] ?? "", consts)];
  const literal = /^["']([^"']+)["']$/.exec(expr);
  if (literal) return [literal[1] ?? ""];
  const name = /^[A-Za-z_]\w*$/.test(expr) ? consts.get(expr) : undefined;
  return name === undefined ? [] : [name];
}

function scan(): { actions: Map<string, string>; actors: Map<string, string> } {
  const actions = new Map<string, string>();
  const actors = new Map<string, string>();
  for (const file of pythonFiles(PANEL)) {
    const src = readFileSync(file, "utf8");
    const where = file.slice(PANEL.length + 1);
    const consts = new Map<string, string>();
    for (const m of src.matchAll(/^(_?[A-Z][A-Z0-9_]*)(?:: [\w[\]]+)? = "([^"]*)"$/gm)) {
      const [, name = "", value = ""] = m;
      consts.set(name, value);
      if (name.endsWith("ACTOR")) actors.set(value, where);
      if (name.endsWith("ACTION") || name.startsWith("ACTION_")) actions.set(value, where);
    }
    for (const w of WRITERS) {
      const re = new RegExp(`(?<![\\w.])(?<!def )${w.name.replace(".", "\\.")}\\(`, "g");
      for (const m of src.matchAll(re)) {
        const args = callArgs(src, m.index + m[0].length - 1);
        const kw = (k: string) => args.find((a) => a.startsWith(`${k}=`))?.slice(k.length + 1);
        const action = kw("action") ?? (w.actionArg === null ? undefined : args[w.actionArg]);
        for (const v of action === undefined ? [] : values(action, consts)) actions.set(v, where);
        const actor = kw("actor");
        for (const v of actor === undefined ? [] : values(actor, consts)) actors.set(v, where);
      }
    }
  }
  for (const a of DYNAMIC_ACTIONS) actions.set(a, "(built at run time)");
  return { actions, actors };
}

describe("actions_log codes of the panel", () => {
  it("finds the panel's sources", () => {
    expect(existsSync(PANEL)).toBe(true);
  });

  const { actions, actors } = scan();

  it("collects the writers (a sanity check of the scanner)", () => {
    for (const a of ["posts.queued", "reality_config_renewed", "sponsor.promotion_repair", "login_ok", "agent_register"]) {
      expect(actions.has(a), a).toBe(true);
    }
    for (const a of ["publisher", "sponsor", "sponsor:auto", "auth"]) expect(actors.has(a), a).toBe(true);
  });

  it("has a Persian word for every action the panel writes", () => {
    const missing = [...actions].filter(([a]) => !/[آ-ی]/.test(actionLabel(a))).map(([a, f]) => `${a} (${f})`);
    expect(missing).toEqual([]);
  });

  it("has a Persian word for every control loop that writes actions", () => {
    const missing = [...actors].filter(([a]) => !/[آ-ی]/.test(actorLabel(a))).map(([a, f]) => `${a} (${f})`);
    expect(missing).toEqual([]);
  });
});

describe("labels of the services' events", () => {
  it("names whose settings a settings_update changed", () => {
    expect(actionLabel("settings_update", "fleet")).toBe("تغییر تنظیمات ناوگان");
    expect(actionLabel("settings_update", "reality")).toContain("Reality");
    expect(actionLabel("settings_update", "mtproto")).toContain("MTProto");
    expect(actionLabel("settings_update", "targets")).toContain("سایت‌های پوششی");
    expect(actionLabel("settings_update", "something")).toBe("تغییر تنظیمات ناوگان");
    expect(actionLabel("settings_update")).toBe("تغییر تنظیمات ناوگان");
  });

  it("translates the posts, Reality, MTProto, sponsor and auth reasons", () => {
    const reasons = [
      "slot_due",
      "breaking",
      "link_status",
      "link_moved",
      "still_working",
      "fresh_config",
      "risky_sni",
      "tag_set",
      "cap_gb",
      "tls_domain",
      "interval_h",
      "set_promotion",
      "tag_received",
      "media_failed",
      "node_burned",
      "traffic_over_threshold",
      "factor:pending",
    ];
    for (const r of reasons) expect(reasonLabel(r), r).toMatch(/[آ-ی]/);
    expect(reasonLabel("from:registered")).toBe("از وضعیت: ثبت‌شده");
    expect(reasonLabel("scheduled->active")).toBe("از زمان‌بندی‌شده به در حال اجرا");
    expect(reasonLabel("odd->thing")).toBe("odd->thing");
  });

  it("keeps the fleet's own wording where a code is shared", () => {
    expect(reasonLabel("burned")).toBe("از ایران بسته شد (سوخته)");
    expect(reasonLabel("unknown")).toBe("هنوز معلوم نیست");
    expect(actorLabel("publisher")).toBe("چرخه‌ی انتشار پست");
  });
});
