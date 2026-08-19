/** Small async and routing primitives, so no data or router library is needed. */

import { useCallback, useEffect, useState } from "react";

import { ApiError } from "./api";

export interface AsyncState<T> {
  data: T | undefined;
  loading: boolean;
  error: ApiError | undefined;
  reload: () => void;
}

/**
 * Run an async loader, tracking loading and error state.
 *
 * `deps` behaves like `useEffect`'s. A stale response is discarded on unmount so a
 * slow request cannot overwrite a newer screen's data.
 */
export function useAsync<T>(loader: () => Promise<T>, deps: unknown[]): AsyncState<T> {
  const [data, setData] = useState<T | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ApiError | undefined>(undefined);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(undefined);

    loader()
      .then((result) => {
        if (live) setData(result);
      })
      .catch((cause: unknown) => {
        if (!live) return;
        setError(
          cause instanceof ApiError
            ? cause
            : new ApiError(0, "unexpected", cause instanceof Error ? cause.message : String(cause)),
        );
      })
      .finally(() => {
        if (live) setLoading(false);
      });

    return () => {
      live = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, nonce]);

  const reload = useCallback(() => setNonce((value) => value + 1), []);
  return { data, loading, error, reload };
}

/** Wrap a mutating call so screens get consistent pending/error handling. */
export function useAction() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ApiError | undefined>(undefined);

  const run = useCallback(async (work: () => Promise<unknown>, after?: () => void) => {
    setPending(true);
    setError(undefined);
    try {
      await work();
      after?.();
    } catch (cause) {
      setError(
        cause instanceof ApiError
          ? cause
          : new ApiError(0, "unexpected", cause instanceof Error ? cause.message : String(cause)),
      );
    } finally {
      setPending(false);
    }
  }, []);

  return { run, pending, error, clearError: () => setError(undefined) };
}

export interface Route {
  screen: string;
  params: Record<string, string>;
}

function parseHash(): Route {
  const raw = window.location.hash.replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const segments = (path ?? "").split("/").filter(Boolean);
  const params: Record<string, string> = {};
  new URLSearchParams(query ?? "").forEach((value, key) => {
    params[key] = value;
  });
  if (segments[1]) params.id = segments[1];
  if (segments[2]) params.sub = segments[2];
  return { screen: segments[0] ?? "dashboard", params };
}

/** Hash routing: no dependency, and deep links survive a reload. */
export function useRoute(): [Route, (screen: string, params?: Record<string, string>) => void] {
  const [route, setRoute] = useState<Route>(parseHash);

  useEffect(() => {
    const onChange = () => setRoute(parseHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  const navigate = useCallback((screen: string, params: Record<string, string> = {}) => {
    const { id, sub, ...query } = params;
    const path = [screen, id, sub].filter(Boolean).join("/");
    const search = new URLSearchParams(query).toString();
    window.location.hash = `#/${path}${search ? `?${search}` : ""}`;
  }, []);

  return [route, navigate];
}

/** Remember the active candidate across reloads. */
export function useCandidateId(): [string | null, (id: string | null) => void] {
  const [candidateId, setCandidateId] = useState<string | null>(() =>
    window.localStorage.getItem("careerAgent.candidateId"),
  );

  const update = useCallback((id: string | null) => {
    if (id) window.localStorage.setItem("careerAgent.candidateId", id);
    else window.localStorage.removeItem("careerAgent.candidateId");
    setCandidateId(id);
  }, []);

  return [candidateId, update];
}
