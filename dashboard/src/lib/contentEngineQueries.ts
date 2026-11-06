// TanStack Query hooks of the content engine's pages (read-only data; writes go through the endpoints).

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing } from "./api";
import { getQc, getQueue, getSources, type CandidateStatus, type QueueSort } from "./contentEngine";

export const contentKeys = {
  all: ["content-engine"] as const,
  sources: ["content-engine", "sources"] as const,
  queue: (status: CandidateStatus, sort: QueueSort) => ["content-engine", "queue", status, sort] as const,
  qc: ["content-engine", "qc"] as const,
};

function every(ms: number) {
  return (query: { state: { error: unknown } }) => (isEndpointMissing(query.state.error) ? false : ms);
}

export const useSources = () =>
  useQuery({ queryKey: contentKeys.sources, queryFn: ({ signal }) => getSources(signal), refetchInterval: every(30_000) });

/** The queue refreshes every minute; a caption being edited keeps its own copy (see ReviewPage). */
export const useQueue = (status: CandidateStatus, sort: QueueSort) =>
  useQuery({
    queryKey: contentKeys.queue(status, sort),
    queryFn: ({ signal }) => getQueue(status, sort, signal),
    refetchInterval: every(60_000),
  });

/** The content supervisor (D52): settings, bridge health, calls and caps. */
export const useQc = () =>
  useQuery({ queryKey: contentKeys.qc, queryFn: ({ signal }) => getQc(signal), refetchInterval: every(60_000) });
