import { QueryClient, QueryObserver } from "@tanstack/react-query";
import { describe, expect, it } from "vitest";

import { ApiError } from "./api";
import { ownerGateView } from "./ownerGate";
import { queryPhase, shouldWarnStale } from "./queries";

/** One query that answers `answers` in turn (an Error is thrown), observed like a page does. */
function observed<T>(key: string, answers: (T | Error)[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  let call = 0;
  const observer = new QueryObserver<T>(client, {
    queryKey: [key],
    queryFn: () => {
      const a = answers[Math.min(call++, answers.length - 1)];
      return a instanceof Error ? Promise.reject(a) : Promise.resolve(a as T);
    },
  });
  return observer;
}

describe("a page keeps its data after a failed refresh", () => {
  it("is ready after a success and stays ready when the refetch fails", async () => {
    const q = observed("status", [{ ok: true }, new ApiError(502, "http_502", "پاسخ نامعتبر")]);
    expect(queryPhase(q.getCurrentResult())).toBe("loading");
    await q.refetch();
    expect(queryPhase(q.getCurrentResult())).toBe("ready");
    await q.refetch();
    const r = q.getCurrentResult();
    expect(r.isError).toBe(true); // what the pages used to branch on first
    expect(r.isRefetchError).toBe(true);
    expect(r.isLoadingError).toBe(false);
    expect(r.data).toEqual({ ok: true });
    expect(queryPhase(r)).toBe("ready");
  });

  it("shows the error card only when nothing was ever loaded", async () => {
    const q = observed("first", [new ApiError(0, "network", "اتصال برقرار نشد")]);
    await q.refetch();
    const r = q.getCurrentResult();
    expect(r.isLoadingError).toBe(true);
    expect(queryPhase(r)).toBe("error");
  });
});

describe("the stats charts while a range loads or fails", () => {
  it("shows the previous range (placeholder) while pending and an error when the new range fails", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    type Series = { days: number };
    const answer = (days: number): Promise<Series> =>
      days === 90 ? Promise.reject(new ApiError(502, "http_502", "پاسخ نامعتبر")) : Promise.resolve({ days });
    const options = (days: number) => ({
      queryKey: ["series", days],
      queryFn: () => answer(days),
      placeholderData: (prev: Series | undefined) => prev,
    });
    const q = new QueryObserver<Series, Error, Series, Series, (string | number)[]>(client, options(30));
    const stop = q.subscribe(() => undefined);
    await q.refetch();
    expect(queryPhase(q.getCurrentResult())).toBe("ready");
    q.setOptions(options(90)); // the owner picks 90 days: the 30-day charts stay, dimmed
    const pending = q.getCurrentResult();
    expect(pending.isPlaceholderData).toBe(true);
    expect(queryPhase(pending)).toBe("ready");
    await q.refetch().catch(() => undefined);
    const failed = q.getCurrentResult();
    expect(failed.data).toBeUndefined(); // no data: the cards must not say "nothing recorded"
    expect(queryPhase(failed)).toBe("error");
    stop();
  });
});

describe("the owner's session guard", () => {
  it("keeps the shell when /api/auth/me fails after it had loaded", async () => {
    const me = observed("me", [{ username: "o" }, new ApiError(502, "http_502", "پاسخ نامعتبر")]);
    await me.refetch();
    await me.refetch();
    const r = me.getCurrentResult();
    expect(r.isError && r.isRefetchError).toBe(true);
    expect(ownerGateView(r)).toBe("shell");
  });

  it("shows the full-screen error for a first load that failed", async () => {
    const me = observed("me", [new ApiError(502, "http_502", "پاسخ نامعتبر")]);
    await me.refetch();
    expect(ownerGateView(me.getCurrentResult())).toBe("error");
  });

  it("goes to the login on a 401, with or without data", async () => {
    const fresh = observed("me", [new ApiError(401, "unauthorized", "وارد نشده‌اید")]);
    await fresh.refetch();
    expect(ownerGateView(fresh.getCurrentResult())).toBe("login");
    const expired = observed("me", [{ username: "o" }, new ApiError(401, "unauthorized", "وارد نشده‌اید")]);
    await expired.refetch();
    await expired.refetch();
    expect(ownerGateView(expired.getCurrentResult())).toBe("login");
  });

  it("waits while the first answer is pending", () => {
    const me = observed("me", [{ username: "o" }]);
    expect(ownerGateView(me.getCurrentResult())).toBe("splash");
  });
});

describe("the refresh-failed note", () => {
  it("is shown at most once a minute", () => {
    expect(shouldWarnStale(null, 1_000)).toBe(true);
    expect(shouldWarnStale(1_000, 30_000)).toBe(false);
    expect(shouldWarnStale(1_000, 61_000)).toBe(true);
    expect(shouldWarnStale(Number.NaN, 1_000)).toBe(true);
    expect(shouldWarnStale(90_000, 1_000)).toBe(true); // a clock that went back
  });
});
