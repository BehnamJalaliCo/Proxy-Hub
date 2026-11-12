// What the protected area shows for the owner's session (GET /api/auth/me), as a pure function.

import { isApiError } from "./api";
import { queryPhase } from "./queries";

export type OwnerGateView = "splash" | "login" | "error" | "shell";

/** A 401 always goes to the login. A failed background refetch keeps the shell (and every unsaved
 * form in it): only a first load that failed shows the full-screen error. */
export function ownerGateView(me: { isPending: boolean; isError: boolean; error: unknown; data: unknown }): OwnerGateView {
  if (me.isPending) return "splash";
  if (me.isError && isApiError(me.error) && me.error.status === 401) return "login";
  const phase = queryPhase(me);
  return phase === "ready" ? "shell" : phase === "error" ? "error" : "splash";
}
