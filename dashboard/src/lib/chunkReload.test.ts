import { describe, expect, it } from "vitest";

import { ErrorBoundary } from "../components/ui/ErrorBoundary";
import { CHUNK_RELOAD_KEY, installChunkReload, shouldAutoReload } from "./chunkReload";

describe("reload after a deploy removed the page chunks", () => {
  it("reloads once a minute at most", () => {
    const now = 1_000_000;
    expect(shouldAutoReload(null, now)).toBe(true);
    expect(shouldAutoReload(now - 5_000, now)).toBe(false);
    expect(shouldAutoReload(now - 61_000, now)).toBe(true);
    expect(shouldAutoReload(Number.NaN, now)).toBe(true);
    expect(shouldAutoReload(Number("garbage"), now)).toBe(true);
    expect(shouldAutoReload(now + 10_000, now)).toBe(true); // a stamp from the future is unreadable
  });

  /** A window stand-in with the listener, sessionStorage and reload the helper uses. */
  function fakeWindow(stored: string | null, storageWorks = true) {
    let listener: ((e: { preventDefault: () => void }) => void) | null = null;
    const state = { reloads: 0, prevented: 0, stored };
    const win = {
      addEventListener: (_: string, fn: typeof listener) => {
        listener = fn;
      },
      sessionStorage: {
        getItem: () => {
          if (!storageWorks) throw new Error("blocked");
          return state.stored;
        },
        setItem: (_: string, v: string) => {
          if (!storageWorks) throw new Error("blocked");
          state.stored = v;
        },
      },
      location: { reload: () => (state.reloads += 1) },
    };
    installChunkReload(win as unknown as Window);
    const fire = () => listener?.({ preventDefault: () => (state.prevented += 1) });
    return { state, fire };
  }

  it("reloads on the first failure and leaves the second to the error boundary", () => {
    const w = fakeWindow(null);
    w.fire();
    expect(w.state).toMatchObject({ reloads: 1, prevented: 1 });
    expect(Number(w.state.stored)).toBeGreaterThan(0);
    w.fire(); // the reloaded page fails again within the minute
    expect(w.state.reloads).toBe(1);
    expect(w.state.prevented).toBe(1);
  });

  it("never reloads when the stamp cannot be stored (no loop)", () => {
    const w = fakeWindow(null, false);
    w.fire();
    expect(w.state.reloads).toBe(0);
    expect(CHUNK_RELOAD_KEY).toBe("ph-chunk-reload");
  });
});

describe("error boundary", () => {
  it("keeps the error it caught, also a thrown null", () => {
    const e = new Error("x");
    expect(ErrorBoundary.getDerivedStateFromError(e)).toEqual({ error: e });
    expect(ErrorBoundary.getDerivedStateFromError(null).error).not.toBeNull();
  });
});
