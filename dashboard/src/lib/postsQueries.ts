// TanStack Query hooks of the posts page (read-only data; writes go through the endpoints directly).

import { useQuery } from "@tanstack/react-query";

import { isEndpointMissing } from "./api";
import { getContent, getPostingSettings, getPostPreview, getPostsStatus, getRecentPosts } from "./posts";

export const postsKeys = {
  all: ["posts"] as const,
  status: ["posts", "status"] as const,
  settings: ["posts", "settings"] as const,
  preview: ["posts", "preview"] as const,
  recent: ["posts", "recent"] as const,
  content: ["posts", "content"] as const,
};

function every(ms: number) {
  return (query: { state: { error: unknown } }) => (isEndpointMissing(query.state.error) ? false : ms);
}

export const usePostsStatus = () =>
  useQuery({ queryKey: postsKeys.status, queryFn: ({ signal }) => getPostsStatus(signal), refetchInterval: every(30_000) });

/** The form edits a copy: background refetches must not overwrite what the owner types. */
export const usePostingSettings = () =>
  useQuery({
    queryKey: postsKeys.settings,
    queryFn: ({ signal }) => getPostingSettings(signal),
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });

export const usePostPreview = () =>
  useQuery({ queryKey: postsKeys.preview, queryFn: ({ signal }) => getPostPreview(signal), refetchInterval: every(60_000) });

export const useRecentPosts = () =>
  useQuery({ queryKey: postsKeys.recent, queryFn: ({ signal }) => getRecentPosts(signal), refetchInterval: every(30_000) });

export const useContent = () =>
  useQuery({ queryKey: postsKeys.content, queryFn: ({ signal }) => getContent(signal), refetchInterval: every(60_000) });
