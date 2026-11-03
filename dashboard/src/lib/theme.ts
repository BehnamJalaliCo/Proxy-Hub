// Light / dark / system theme. The choice is a per-browser convenience in localStorage (wrapped in
// try/catch: storage can be blocked); public/theme-init.js applies it before the first paint.

import { useSyncExternalStore } from "react";

export type ThemeChoice = "light" | "dark" | "system";
export type ResolvedTheme = "light" | "dark";

const KEY = "ph-theme";
const listeners = new Set<() => void>();

function readChoice(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

function systemDark(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

let choice: ThemeChoice = typeof window === "undefined" ? "system" : readChoice();

function resolve(c: ThemeChoice): ResolvedTheme {
  return c === "dark" || (c === "system" && systemDark()) ? "dark" : "light";
}

function apply(): void {
  const resolved = resolve(choice);
  document.documentElement.setAttribute("data-theme", resolved);
  for (const meta of document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')) {
    meta.content = resolved === "dark" ? "#090b14" : "#f5f6fb";
    meta.removeAttribute("media");
  }
  for (const l of listeners) l();
}

export function setTheme(next: ThemeChoice): void {
  choice = next;
  try {
    if (next === "system") window.localStorage.removeItem(KEY);
    else window.localStorage.setItem(KEY, next);
  } catch {
    // Storage blocked (private mode): the choice still applies for this visit.
  }
  apply();
}

export function initTheme(): void {
  apply();
  if (typeof window.matchMedia === "function") {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (choice === "system") apply();
    });
  }
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

interface ThemeState {
  choice: ThemeChoice;
  resolved: ResolvedTheme;
}

let snapshot: ThemeState = { choice, resolved: typeof window === "undefined" ? "light" : resolve(choice) };

function getSnapshot(): ThemeState {
  const resolved = resolve(choice);
  if (snapshot.choice !== choice || snapshot.resolved !== resolved) snapshot = { choice, resolved };
  return snapshot;
}

export function useTheme(): ThemeState {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
