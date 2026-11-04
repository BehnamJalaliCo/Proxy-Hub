// A tiny toast store; <Toaster/> renders it with Radix Toast (accessible live region).

import { useSyncExternalStore } from "react";

export type ToastKind = "success" | "error" | "info" | "warning";

export interface ToastItem {
  id: number;
  kind: ToastKind;
  title: string;
  description?: string;
}

let items: ToastItem[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit(): void {
  for (const l of listeners) l();
}

function push(kind: ToastKind, title: string, description?: string): void {
  const item: ToastItem = description === undefined ? { id: nextId++, kind, title } : { id: nextId++, kind, title, description };
  items = [...items.slice(-3), item];
  emit();
}

export const toast = {
  success: (title: string, description?: string) => push("success", title, description),
  error: (title: string, description?: string) => push("error", title, description),
  info: (title: string, description?: string) => push("info", title, description),
  warning: (title: string, description?: string) => push("warning", title, description),
};

export function dismissToast(id: number): void {
  items = items.filter((t) => t.id !== id);
  emit();
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

export function useToasts(): ToastItem[] {
  return useSyncExternalStore(
    subscribe,
    () => items,
    () => items,
  );
}
