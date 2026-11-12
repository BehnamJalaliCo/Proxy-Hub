// Visual check of the dashboard against a REAL running panel API (decision D10: nothing faked).
// Loads every page on a phone (390x844) and a desktop (1440x900), light and dark, fails on any
// console/page error, and writes PNG screenshots.
//
//   BASE_URL=http://localhost:8000 OUT=screenshots-raw [SETUP_TOKEN=<token>] node scripts/screenshots.mjs
//
// With SETUP_TOKEN (a fresh link from `python -m proxyhub setup-link` on a THROWAWAY database), it
// also walks the real setup flow, creates the owner, and screenshots the signed-in pages. Values
// that would be secrets on a real panel (TOTP QR/key, recovery codes) are masked in the images and
// never printed. Never point this at the production panel with a real token.
//
// PAGES_ONLY=1 STATE_FILE=<session saved by SAVE_STATE>: no setup flow and no public pages; shoots
// EVERY signed-in route (and the phone's drawer) on the four combinations, with the same checks. For
// a throwaway panel that already has data (scripts/seed_demo.py fills one), so each page is seen in
// its busy state. It only reads: nothing is clicked except the drawer button. PAGES=stats,nodes
// (names or paths, comma separated) shoots only those routes. A phone page taller than one image
// continues in <name>-part2-...; a phone page wider than the phone is reported as a problem.
//
// Playwright is not a dashboard dependency: it is resolved from the machine (global install).

import { createHmac, randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, resolve } from "node:path";

const require = createRequire(import.meta.url);
const { chromium } = require("playwright");

const BASE = (process.env.BASE_URL ?? "http://localhost:8000").replace(/\/$/, "");
const OUT = resolve(process.env.OUT ?? "screenshots-raw");
const TOKEN = process.env.SETUP_TOKEN ?? "";
const ONLY_PUBLIC = process.env.ONLY_PUBLIC === "1";
const PREFIX = process.env.PREFIX ?? "";
// ONLY_STATS=1: after the setup (or with STATE_FILE, a session saved by SAVE_STATE on a throwaway
// database), shoot only the «آمار» page, e.g. once empty and once after local data was added.
const ONLY_STATS = process.env.ONLY_STATS === "1";
// PAGES_ONLY=1 with STATE_FILE: every signed-in route, nothing else (see the top of this file).
const PAGES_ONLY = process.env.PAGES_ONLY === "1";
const PAGES = (process.env.PAGES ?? "").split(",").map((p) => p.trim()).filter(Boolean);
const STATE_FILE = process.env.STATE_FILE ?? "";
const SAVE_STATE = process.env.SAVE_STATE ?? "";
// A fresh random owner password per run (never printed, never in the repo): even a run pointed at
// the wrong panel by mistake can never create an owner whose password anyone else knows.
const PASSWORD = randomBytes(24).toString("base64url");

mkdirSync(OUT, { recursive: true });

const DEVICES = [
  { name: "mobile", viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  { name: "desktop", viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 },
];
const THEMES = ["light", "dark"];
const COMBOS = DEVICES.flatMap((d) => THEMES.map((t) => ({ device: d, theme: t })));

// The tallest page one image holds (CSS pixels); a taller page is cut there.
const MAX_SHOT_HEIGHT = 12000;

const problems = [];
const resourceErrors = [];
let shots = 0;

function base32(s) {
  const abc = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out = [];
  for (const ch of s.replace(/[\s=]/g, "").toUpperCase()) {
    value = (value << 5) | abc.indexOf(ch);
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 255);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** RFC 6238, SHA-1, 6 digits, 30 s (what the panel uses). */
function totp(secret, at = Date.now()) {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(at / 30000)));
  const h = createHmac("sha1", base32(secret)).update(counter).digest();
  const o = h[h.length - 1] & 15;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

async function newContext(browser, combo, storageState) {
  const ctx = await browser.newContext({
    ...combo.device,
    colorScheme: combo.theme,
    locale: "fa-IR",
    timezoneId: "Asia/Tehran",
    ...(storageState ? { storageState } : {}),
  });
  await ctx.addInitScript((theme) => {
    try {
      window.localStorage.setItem("ph-theme", theme);
    } catch {
      /* storage blocked: the OS colour scheme still applies */
    }
  }, combo.theme);
  return ctx;
}

function watch(page, label) {
  page.on("console", (msg) => {
    if (msg.type() !== "error") return;
    const text = msg.text();
    if (/^Failed to load resource: the server responded with a status of (401|404|409|422|429|503)/.test(text)) {
      resourceErrors.push(`${label}: ${text} ${msg.location().url ?? ""}`);
      return;
    }
    problems.push(`${label}: console.error: ${text}`);
  });
  page.on("pageerror", (err) => problems.push(`${label}: page error: ${err.message}`));
}

async function settle(page) {
  await page.waitForLoadState("networkidle").catch(() => undefined);
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(1300);
}

async function shot(page, name, combo, { full = true } = {}) {
  await settle(page);
  const file = join(OUT, `${PREFIX}${name}-${combo.device.name}-${combo.theme}.png`);
  // A focused field must never leave an overflow-hidden box scrolled (the page looks shifted on a
  // phone after the keyboard closes); decorative boxes use overflow: clip for that reason.
  const shifted = await page.evaluate(() =>
    [...document.querySelectorAll("*")]
      .filter((el) => (el.scrollTop !== 0 || el.scrollLeft !== 0) && /hidden/.test(getComputedStyle(el).overflow))
      .map((el) => `${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)}`),
  );
  if (shifted.length > 0) problems.push(`${name}/${combo.device.name}/${combo.theme}: scrolled overflow-hidden box: ${shifted.join(" | ")}`);
  // A whole page is shot by growing the viewport to the page height (instead of `fullPage`), so the
  // fixed top bar, sidebar and bottom navigation sit where a reader expects them.
  const base = combo.device.viewport;
  let height = base.height;
  if (full) {
    height = await page.evaluate(() => document.documentElement.scrollHeight);
    if (height > base.height) {
      await page.setViewportSize({ width: base.width, height: Math.min(height, MAX_SHOT_HEIGHT) });
      await page.waitForTimeout(500);
    }
  }
  const options = {
    animations: "disabled",
    mask: [page.locator("[data-ph-secret]")],
    maskColor: combo.theme === "dark" ? "#2a2f48" : "#d9dcea",
  };
  await page.screenshot({ path: file, ...options });
  shots += 1;
  console.log(`  saved ${file}`);
  // PAGES_ONLY: a page with data can be taller than one image holds (the statistics on a phone).
  // The rest follows as <name>-part2, -part3, ...: the page scrolled on, so the fixed bars show again.
  if (full && PAGES_ONLY) {
    for (let top = MAX_SHOT_HEIGHT, part = 2; top < height; top += MAX_SHOT_HEIGHT, part += 1) {
      const rest = Math.min(height - top, MAX_SHOT_HEIGHT);
      await page.setViewportSize({ width: base.width, height: Math.max(rest, base.height) });
      await page.evaluate((y) => window.scrollTo(0, y), top);
      await page.waitForTimeout(900);
      const more = join(OUT, `${PREFIX}${name}-part${String(part)}-${combo.device.name}-${combo.theme}.png`);
      await page.screenshot({ path: more, ...options });
      shots += 1;
      console.log(`  saved ${more}`);
    }
    if (height > MAX_SHOT_HEIGHT) await page.evaluate(() => window.scrollTo(0, 0));
  }
  await page.setViewportSize(base);
}

async function checkRtl(page, label) {
  const ok = await page.evaluate(() => document.documentElement.dir === "rtl" && document.documentElement.lang === "fa");
  if (!ok) problems.push(`${label}: page is not lang=fa dir=rtl`);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  if (overflow > 1) problems.push(`${label}: horizontal overflow of ${overflow}px`);
}

async function visit(page, path, label) {
  const res = await page.goto(BASE + path, { waitUntil: "domcontentloaded" });
  if (!res || res.status() !== 200) problems.push(`${label}: GET ${path} -> ${res?.status() ?? "no response"}`);
  const csp = res?.headers()["content-security-policy"] ?? "";
  if (!csp.includes("script-src 'self'")) problems.push(`${label}: no strict CSP on ${path}`);
}

async function publicPages(browser, combo) {
  const ctx = await newContext(browser, combo);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme}`;
  watch(page, label);
  await visit(page, "/login", label);
  await page.getByRole("heading", { name: "ورود به پنل" }).waitFor();
  await checkRtl(page, `${label} /login`);
  await shot(page, "login", combo);

  await visit(page, "/setup", label);
  await settle(page);
  await checkRtl(page, `${label} /setup`);
  await shot(page, "setup-no-link", combo);

  await visit(page, "/", label);
  await settle(page);
  await shot(page, "root-signed-out", combo);
  await ctx.close();
}

/** Setup steps 1-3 on every combo; the last call also finishes setup and returns the session. */
async function setupFlow(browser, combo, finish) {
  const ctx = await newContext(browser, combo);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme} setup`;
  watch(page, label);
  await visit(page, `/setup#${TOKEN}`, label);
  await page.getByRole("button", { name: "شروع راه‌اندازی" }).waitFor();
  if (await page.evaluate(() => window.location.hash !== "")) problems.push(`${label}: token stayed in the address bar`);
  await shot(page, "setup-1-intro", combo);

  await page.getByRole("button", { name: "شروع راه‌اندازی" }).click();
  await page.getByRole("heading", { name: "ثبت در برنامه‌ی Authenticator" }).waitFor();
  const secret = (await page.locator("code[data-ph-secret]").innerText()).replace(/\s+/g, "");
  await shot(page, "setup-2-authenticator", combo);

  await page.getByRole("button", { name: "ثبت کردم؛ ادامه" }).click();
  await page.locator("#setup-username").fill("owner");
  await page.locator("#setup-password").fill(PASSWORD);
  await page.locator("#setup-password2").fill(PASSWORD);
  await shot(page, "setup-3-account", combo);

  let state = null;
  if (finish) {
    await page.locator("#setup-totp").fill(totp(secret));
    await page.getByRole("button", { name: "ساخت حساب مالک" }).click();
    await page.getByRole("heading", { name: "کدهای بازیابی" }).waitFor();
    await shot(page, "setup-4-recovery-codes", combo);
    await page.getByRole("checkbox").check();
    await page.getByRole("button", { name: "ورود به پنل" }).click();
    await page.getByRole("heading", { name: "نمای کلی" }).waitFor({ timeout: 15000 });
    state = await ctx.storageState();
  }
  await ctx.close();
  return { state, secret };
}

/** Real owner actions so the pages show real data: caps, kill switch off and on again. */
async function ownerActions(browser, combo, state) {
  const ctx = await newContext(browser, combo, state);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme} actions`;
  watch(page, label);
  await visit(page, "/settings", label);
  await page.locator("#s-budget").waitFor();
  await page.locator("#s-budget").fill("150");
  await page.locator("#s-nodecap").fill("10");
  await shot(page, "settings-unsaved", combo, { full: false });
  await page.getByRole("button", { name: "ذخیره", exact: true }).click();
  await page.getByText("تنظیمات ذخیره شد").first().waitFor();

  await page.getByRole("button", { name: "خاموش کردن" }).click();
  await page.getByRole("alertdialog").waitFor();
  await shot(page, "kill-switch-confirm", combo, { full: false });
  await page.getByRole("alertdialog").getByRole("checkbox").check();
  await page.getByRole("alertdialog").getByRole("button", { name: "خاموش کن" }).click();
  await page.getByText("کلید توقف خاموش شد").first().waitFor();
  await page.getByRole("button", { name: "روشن کردن (توقف)" }).click();
  await page.getByRole("alertdialog").getByRole("button", { name: "روشن کن" }).click();
  await page.getByText("کلید توقف روشن شد").first().waitFor();
  await ctx.close();
}

async function signedInPages(browser, combo, state) {
  const ctx = await newContext(browser, combo, state);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme}`;
  watch(page, label);
  for (const [path, name, heading] of [
    ["/", "overview", "نمای کلی"],
    ["/stats", "stats", "آمار"],
    ["/nodes", "nodes", "nodeها"],
    ["/telegram", "telegram", "تلگرام"],
    ["/cloudflare", "cloudflare", "Cloudflare و دامنه‌ها"],
    ["/accounts", "accounts", "حساب‌های هتزنر"],
    ["/settings", "settings", "تنظیمات ناوگان"],
    ["/events", "events", "رویدادها"],
    ["/security", "security", "امنیت"],
  ]) {
    await visit(page, path, label);
    await page.getByRole("heading", { name: heading, level: 1 }).waitFor({ timeout: 15000 });
    await checkRtl(page, `${label} ${path}`);
    await shot(page, name, combo);
  }
  await visit(page, "/accounts?add=1", label);
  await page.getByRole("dialog").waitFor();
  await shot(page, "accounts-add", combo, { full: false });
  await round2Dialogs(page, combo, label);
  if (combo.device.name === "mobile") {
    await visit(page, "/", label);
    await page.getByRole("button", { name: "منوی پنل" }).click();
    await page.getByRole("dialog").waitFor();
    await shot(page, "drawer", combo, { full: false });
  }
  await ctx.close();
}

/** Every route of the signed-in panel (dashboard/src/App.tsx), in the order of the menu, with the
 * h1 that shows the page really loaded. */
const ALL_PAGES = [
  ["/", "overview", "نمای کلی"],
  ["/stats", "stats", "آمار"],
  ["/nodes", "nodes", "nodeها"],
  ["/telegram", "telegram", "تلگرام"],
  ["/mtproto", "mtproto", "پروکسی‌های MTProto"],
  ["/sponsor", "sponsor", "اسپانسر"],
  ["/content", "content", "محتوا و انتشار"],
  ["/review", "review", "صف بازبینی"],
  ["/sources", "sources", "منبع‌های محتوا"],
  ["/reality", "reality", "Reality"],
  ["/accounts", "accounts", "حساب‌های هتزنر"],
  ["/cloudflare", "cloudflare", "Cloudflare و دامنه‌ها"],
  ["/events", "events", "رویدادها"],
  ["/settings", "settings", "تنظیمات ناوگان"],
  ["/security", "security", "امنیت"],
];

class SessionExpired extends Error {}

/** A phone page that is wider than the phone. Chromium's phone emulation then lays the page out in
 * a wider viewport and zooms out, so `scrollWidth - innerWidth` (checkRtl) stays 0 and a screenshot
 * shows the page shrunk into a corner. Returns null, or how far the page sticks out (in an RTL page:
 * on the left) and the outermost elements that do, none of them clipped by a parent. */
async function phoneOverflow(page) {
  return page.evaluate(() => {
    const width = document.documentElement.clientWidth;
    const off = window.innerWidth - width;
    if (off <= 1) return null;
    const clips = (el) => /(hidden|clip|auto|scroll)/.test(getComputedStyle(el).overflowX);
    const inside = (r) => r.left >= off - 1 && r.right <= off + width + 1;
    const found = [];
    for (const el of document.querySelectorAll("body *")) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || inside(r)) continue;
      let skip = false;
      for (let a = el; a && a !== document.documentElement && !skip; a = a.parentElement) {
        if (getComputedStyle(a).position === "fixed") skip = true;
        else if (a !== el && (found.includes(a) || (clips(a) && inside(a.getBoundingClientRect())))) skip = true;
      }
      if (!skip) found.push(el);
    }
    const say = (el) => {
      const r = el.getBoundingClientRect();
      const text = (el.textContent ?? "").trim().replace(/\s+/g, " ").slice(0, 40);
      return `<${el.tagName.toLowerCase()} class="${String(el.className).slice(0, 60)}"> ${String(Math.round(r.width))}px wide «${text}»`;
    };
    return { by: Math.round(off), layout: window.innerWidth, elements: found.slice(0, 3).map(say), more: Math.max(0, found.length - 3) };
  });
}

/** Reports a phone page that is wider than the phone (once per page: `label` null only repairs),
 * then clips the overflow for the picture: without that the emulation zooms out and the image is
 * useless. The image then shows what the phone shows before anyone pans sideways; the problem list
 * says what sticks out. */
async function fitPhone(page, label) {
  await settle(page);
  const wide = await phoneOverflow(page);
  if (!wide) return;
  if (label) {
    const rest = wide.more > 0 ? ` (+${String(wide.more)} more)` : "";
    problems.push(
      `${label}: wider than the phone by ${String(wide.by)}px (laid out ${String(wide.layout)}px wide): ${wide.elements.join(" | ")}${rest}`,
    );
  }
  await page.evaluate(() => document.documentElement.style.setProperty("overflow-x", "clip", "important"));
  await page.waitForTimeout(400);
}

/** PAGES_ONLY=1: every route once per combination, then the drawer on the phone. Read-only. */
async function allPages(browser, combo, state) {
  const ctx = await newContext(browser, combo, state);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme}`;
  watch(page, label);
  try {
    const wanted = ALL_PAGES.filter(([path, name]) => PAGES.length === 0 || PAGES.includes(name) || PAGES.includes(path));
    for (const [path, name, heading] of wanted) {
      await visit(page, path, label);
      const h1 = page.getByRole("heading", { name: heading, level: 1, exact: true });
      const login = page.getByRole("heading", { name: "ورود به پنل" });
      await h1.or(login).first().waitFor({ timeout: 20000 });
      if (new URL(page.url()).pathname.startsWith("/login")) {
        throw new SessionExpired(`the saved session is no longer signed in (${path} went to /login)`);
      }
      await checkRtl(page, `${label} ${path}`);
      if (combo.device.isMobile) await fitPhone(page, `${label} ${path}`);
      await shot(page, name, combo);
    }
    if (combo.device.name === "mobile" && (PAGES.length === 0 || PAGES.includes("drawer"))) {
      await visit(page, "/", label);
      await settle(page);
      await fitPhone(page, null); // "/" was reported above
      await page.getByRole("button", { name: "منوی پنل" }).click();
      await page.getByRole("dialog").waitFor();
      await shot(page, "drawer", combo, { full: false });
    }
  } finally {
    await ctx.close();
  }
}

/** Only the «آمار» page (ONLY_STATS=1). */
async function statsPage(browser, combo, state) {
  const ctx = await newContext(browser, combo, state);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme} stats`;
  watch(page, label);
  await visit(page, "/stats", label);
  await page.getByRole("heading", { name: "آمار", level: 1 }).waitFor({ timeout: 15000 });
  await checkRtl(page, `${label} /stats`);
  await shot(page, "stats", combo);
  await ctx.close();
}

/** Round-2 dialogs and editors, only where the panel has the route (without it, the pages show a
 * calm "not active yet" state, which the page shots above already cover). Nothing is confirmed or
 * saved: no node is requested, no token or setting is sent. */
async function round2Dialogs(page, combo, label) {
  await visit(page, "/nodes", label);
  await page.getByRole("heading", { name: "nodeها", level: 1 }).waitFor({ timeout: 15000 });
  await settle(page);
  const create = page.getByRole("button", { name: "ساخت node آزمایشی" }).first();
  if (await create.isEnabled()) {
    await create.click();
    await page.getByRole("alertdialog").waitFor();
    await shot(page, "nodes-test-confirm", combo, { full: false });
    await page.getByRole("alertdialog").getByRole("button", { name: "انصراف" }).click();
    await page.getByRole("alertdialog").waitFor({ state: "detached" });
  }
  const events = page.getByRole("button", { name: "رویدادها", exact: true }).first();
  if ((await events.count()) > 0) {
    await events.click();
    await page.getByRole("dialog").waitFor();
    await shot(page, "nodes-events", combo, { full: false });
    await page.keyboard.press("Escape");
  }

  await visit(page, "/telegram", label);
  await page.getByRole("heading", { name: "تلگرام", level: 1 }).waitFor({ timeout: 15000 });
  await settle(page);
  const add = page.getByRole("button", { name: "افزودن بازه" });
  if ((await add.count()) > 0) {
    await add.click();
    await add.click();
    await page.getByRole("button", { name: "ذخیره", exact: true }).waitFor();
    await shot(page, "telegram-schedule-edit", combo);
  }
}

async function loginError(browser, combo) {
  const ctx = await newContext(browser, combo);
  const page = await ctx.newPage();
  const label = `${combo.device.name}/${combo.theme} login-error`;
  watch(page, label);
  await visit(page, "/login?next=%2Fevents", label);
  await page.locator("#login-username").fill("owner");
  await page.locator("#login-password").fill("not-the-password");
  await page.locator("#login-totp").fill("000000");
  await page.getByRole("button", { name: "ورود", exact: true }).click();
  await page.getByRole("alert").waitFor();
  await shot(page, "login-error", combo);
  await ctx.close();
}

const browser = await chromium.launch();
try {
  for (const combo of ONLY_STATS || PAGES_ONLY ? [] : COMBOS) {
    console.log(`public pages: ${combo.device.name}/${combo.theme}`);
    await publicPages(browser, combo);
  }
  if (PAGES_ONLY) {
    if (!STATE_FILE) throw new Error("PAGES_ONLY=1 needs STATE_FILE (a session saved with SAVE_STATE)");
    const saved = JSON.parse(readFileSync(STATE_FILE, "utf8"));
    for (const combo of COMBOS) {
      console.log(`all pages: ${combo.device.name}/${combo.theme}`);
      await allPages(browser, combo, saved);
    }
  } else if (ONLY_STATS && STATE_FILE) {
    const saved = JSON.parse(readFileSync(STATE_FILE, "utf8"));
    for (const combo of COMBOS) await statsPage(browser, combo, saved);
  } else if (TOKEN && !ONLY_PUBLIC) {
    let state = null;
    for (const [i, combo] of COMBOS.entries()) {
      console.log(`setup flow: ${combo.device.name}/${combo.theme}`);
      const last = i === COMBOS.length - 1;
      const r = await setupFlow(browser, combo, last);
      if (last) state = r.state;
    }
    if (!state) throw new Error("setup did not produce a session");
    if (SAVE_STATE) writeFileSync(SAVE_STATE, JSON.stringify(state), { mode: 0o600 });
    if (ONLY_STATS) {
      for (const combo of COMBOS) await statsPage(browser, combo, state);
    } else {
      await ownerActions(browser, COMBOS[0], state);
      for (const combo of COMBOS) {
        console.log(`signed-in pages: ${combo.device.name}/${combo.theme}`);
        await signedInPages(browser, combo, state);
      }
      await loginError(browser, COMBOS[0]);
      await loginError(browser, COMBOS[3]);
    }
  }
} catch (err) {
  if (!(err instanceof SessionExpired)) throw err;
  // Nothing is repaired here: a new session needs a new setup link on a throwaway database.
  problems.push(`${err.message}; make a new STATE_FILE (SETUP_TOKEN + SAVE_STATE) and run again`);
} finally {
  await browser.close();
}

console.log(`\n${String(shots)} screenshots in ${OUT}`);
console.log(`expected HTTP error responses seen by the browser: ${String(resourceErrors.length)}`);
for (const r of resourceErrors) console.log(`  - ${r}`);
if (problems.length > 0) {
  console.error(`\n${String(problems.length)} problem(s):`);
  for (const p of problems) console.error(`  - ${p}`);
  process.exit(1);
}
console.log("no console errors, no page errors, RTL everywhere, no horizontal overflow");
