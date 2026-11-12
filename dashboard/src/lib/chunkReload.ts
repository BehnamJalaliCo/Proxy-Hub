// A tab opened before a deploy asks for page chunks that no longer exist (their names are content
// hashes). Vite then fires "vite:preloadError" on every browser (Safari's own message differs, so
// no message is matched). The page reloads once to get the new index.html; a second failure within
// a minute is left to the error boundary, which shows a Persian card, so it can never loop.

export const CHUNK_RELOAD_KEY = "ph-chunk-reload";
export const CHUNK_RELOAD_GAP_MS = 60_000;

/** Reload unless one was done less than a minute ago (an unreadable stamp counts as none). */
export function shouldAutoReload(lastMs: number | null, nowMs: number): boolean {
  if (lastMs === null || !Number.isFinite(lastMs) || lastMs > nowMs) return true;
  return nowMs - lastMs > CHUNK_RELOAD_GAP_MS;
}

export function installChunkReload(win: Window): void {
  win.addEventListener("vite:preloadError", (event) => {
    const now = Date.now();
    let last: number | null;
    try {
      const raw = win.sessionStorage.getItem(CHUNK_RELOAD_KEY);
      last = raw === null ? null : Number(raw);
    } catch {
      last = null;
    }
    if (!shouldAutoReload(last, now)) return;
    try {
      win.sessionStorage.setItem(CHUNK_RELOAD_KEY, String(now));
    } catch {
      return; // without the stamp a reload could repeat: the error boundary shows the card instead
    }
    event.preventDefault();
    win.location.reload();
  });
}
