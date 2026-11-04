// Reality endpoints and the page's query (GET /api/reality, PUT /api/reality/settings).

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing, request } from "./api";
import type { RealitySettings, RealitySettingsView, RealitySummary } from "./reality";

export const realityKey = ["reality"] as const;

export const getReality = (signal?: AbortSignal) =>
  request<RealitySummary>("GET", "/api/reality", undefined, signal ? { signal } : {});

export const putRealitySettings = (s: RealitySettings) => request<RealitySettingsView>("PUT", "/api/reality/settings", s);

/** Refreshes every 30 s (configs count down), but not while the route does not exist yet. */
export const useReality = () =>
  useQuery({
    queryKey: realityKey,
    queryFn: ({ signal }) => getReality(signal),
    refetchInterval: (query) => (isEndpointMissing(query.state.error) ? false : 30_000),
  });
