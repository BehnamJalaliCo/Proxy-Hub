// request() against a real local HTTP server (node:http), not a stubbed fetch.

import { createServer, type IncomingHttpHeaders, type Server } from "node:http";
import type { AddressInfo } from "node:net";

import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

import { ApiError, errorList, isEndpointMissing, request, safeNext, setUnauthorizedHandler, toApiError } from "./api";

describe("error bodies", () => {
  it("gives round-2 codes a Persian sentence when the server sends none", () => {
    expect(toApiError(409, { error: "gate_refused", reasons: ["kill_switch_on"] }).messageFa).toMatch(/قفل‌های ایمنی/);
    expect(toApiError(400, { error: "bot_not_admin" }).messageFa).toMatch(/ادمین/);
    expect(toApiError(400, { error: "cf_permissions_missing", missing: ["DNS Write"] }).messageFa).toMatch(/Cloudflare/);
    expect(toApiError(400, { error: "cf_token_multiple_accounts" }).messageFa).toMatch(/All zones from an account/);
    // The server's own sentence always wins.
    expect(toApiError(400, { error: "bot_not_admin", message_fa: "متن سرور" }).messageFa).toBe("متن سرور");
  });

  it("tells a missing route from a contract 404", () => {
    expect(isEndpointMissing(toApiError(404, { detail: "Not Found" }))).toBe(true);
    expect(isEndpointMissing(toApiError(404, null))).toBe(true);
    expect(isEndpointMissing(toApiError(404, { error: "not_found", message_fa: "پیدا نشد" }))).toBe(false);
    expect(isEndpointMissing(toApiError(500, null))).toBe(false);
    expect(isEndpointMissing(new Error("x"))).toBe(false);
  });

  it("reads string lists of the error body", () => {
    const e = toApiError(409, { error: "gate_refused", reasons: ["kill_switch_on", 3, "node_cap_not_set"] });
    expect(errorList(e, "reasons")).toEqual(["kill_switch_on", "node_cap_not_set"]);
    expect(errorList(e, "missing")).toEqual([]);
    expect(errorList("nope", "reasons")).toEqual([]);
  });

  it("reads the contract shape", () => {
    const e = toApiError(429, { error: "locked", message_fa: "بسته است", retry_after_s: 42 });
    expect(e.code).toBe("locked");
    expect(e.messageFa).toBe("بسته است");
    expect(e.num("retry_after_s")).toBe(42);
    expect(e.num("missing")).toBeNull();
  });

  it("falls back to a Persian sentence for non-contract errors", () => {
    const e = toApiError(503, { detail: "authentication not installed yet" });
    expect(e.code).toBe("http_503");
    expect(e.messageFa).toMatch(/سرویس پنل/);
    expect(toApiError(404, null).messageFa).toMatch(/فعال نشده/);
  });
});

describe("post-login destination", () => {
  it("only allows same-site paths", () => {
    expect(safeNext("/accounts")).toBe("/accounts");
    expect(safeNext("/events?x=1")).toBe("/events?x=1");
    expect(safeNext("//evil.example")).toBe("/");
    expect(safeNext("/\\evil.example")).toBe("/");
    expect(safeNext("https://evil.example")).toBe("/");
    expect(safeNext("/login")).toBe("/");
    expect(safeNext(null)).toBe("/");
  });
});

describe("request() over real HTTP", () => {
  let server: Server;
  let base = "";
  const seen: { method: string; url: string; headers: IncomingHttpHeaders; body: string }[] = [];

  beforeAll(async () => {
    server = createServer((req, res) => {
      let body = "";
      req.on("data", (c: Buffer) => (body += c.toString("utf8")));
      req.on("end", () => {
        seen.push({ method: req.method ?? "", url: req.url ?? "", headers: req.headers, body });
        if (req.url === "/api/kill-switch") {
          res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify({ kill_switch: true }));
        } else if (req.url === "/api/sessions/x") {
          res.writeHead(204).end();
        } else if (req.url === "/api/broken") {
          res.writeHead(502, { "Content-Type": "text/html" }).end("<html>bad gateway</html>");
        } else {
          res.writeHead(401, { "Content-Type": "application/json" }).end(JSON.stringify({ error: "unauthenticated", message_fa: "دوباره وارد شوید" }));
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    base = `http://127.0.0.1:${String((server.address() as AddressInfo).port)}`;
  });

  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  afterEach(() => {
    seen.length = 0;
    setUnauthorizedHandler(null);
  });

  it("sends the CSRF header and a JSON body", async () => {
    const res = await request<{ kill_switch: boolean }>("POST", `${base}/api/kill-switch`, { on: true });
    expect(res.kill_switch).toBe(true);
    const req = seen[0];
    expect(req?.headers["x-requested-with"]).toBe("proxyhub");
    expect(req?.headers["content-type"]).toBe("application/json");
    expect(req?.body).toBe('{"on":true}');
  });

  it("calls the 401 handler for protected routes only", async () => {
    let hits = 0;
    setUnauthorizedHandler(() => {
      hits += 1;
    });
    await expect(request("GET", `${base}/api/overview`)).rejects.toBeInstanceOf(ApiError);
    expect(hits).toBe(1);
    const err = await request("POST", `${base}/api/auth/login`, {}, { noAuthRedirect: true }).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).messageFa).toBe("دوباره وارد شوید");
    expect(hits).toBe(1);
  });

  it("returns undefined for 204 and a Persian error for non-JSON failures", async () => {
    await expect(request("DELETE", `${base}/api/sessions/x`)).resolves.toBeUndefined();
    const err = await request("GET", `${base}/api/broken`).catch((e: unknown) => e);
    expect((err as ApiError).status).toBe(502);
    expect((err as ApiError).messageFa).toMatch(/سرویس بیرونی/);
  });

  it("turns a refused connection into a Persian network error", async () => {
    const err = await request("GET", "http://127.0.0.1:9/api/overview").catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(0);
  });
});
