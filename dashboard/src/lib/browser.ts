// Small browser helpers: clipboard with a fallback, and saving a text file locally.

export async function copyText(text: string): Promise<boolean> {
  try {
    if (window.isSecureContext && "clipboard" in navigator) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the legacy path
  }
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.appendChild(area);
  area.select();
  let done: boolean;
  try {
    // Deprecated, but the only option outside a secure context.
    // eslint-disable-next-line @typescript-eslint/no-deprecated
    done = document.execCommand("copy");
  } catch {
    done = false;
  }
  area.remove();
  return done;
}

/** Saves `text` as a file on the owner's device (nothing leaves the browser). */
export function downloadText(filename: string, text: string): void {
  const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function cn(...parts: (string | false | null | undefined)[]): string {
  return parts.filter(Boolean).join(" ");
}
