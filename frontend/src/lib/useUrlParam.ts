"use client";

import { useCallback, useSyncExternalStore } from "react";

const EVENT = "urlparamchange";

function subscribe(cb: () => void) {
  window.addEventListener("popstate", cb);
  window.addEventListener(EVENT, cb);
  return () => {
    window.removeEventListener("popstate", cb);
    window.removeEventListener(EVENT, cb);
  };
}

/** Read/write one query-string param without a Next navigation (so a live stream is not remounted). */
export function useUrlParam(name: string): [string | null, (v: string | null, mode?: "push" | "replace") => void] {
  const value = useSyncExternalStore(
    subscribe,
    () => new URLSearchParams(window.location.search).get(name),
    () => null,
  );
  const set = useCallback(
    (v: string | null, mode: "push" | "replace" = "push") => {
      const url = new URL(window.location.href);
      if (v) url.searchParams.set(name, v);
      else url.searchParams.delete(name);
      window.history[mode === "push" ? "pushState" : "replaceState"](null, "", url);
      window.dispatchEvent(new Event(EVENT));
    },
    [name],
  );
  return [value, set];
}
