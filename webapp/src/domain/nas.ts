import i18n from "../i18n";
import { useEffect, useState } from "react";
import { nodeControlUrl } from "./nodeUrl";

export type NasSource = {
  id: string;
  name: string;
  system: string;
  protocol: string;
  root: string;
  username: string;
  host?: string;
  share?: string;
  port?: number;
  url?: string;
  writable: boolean;
  allow_ai: boolean;
  write_authorized?: boolean;
};
export type NasStatus = {
  browser_location?: { source_id?: string; path?: string; names?: Record<string, string> };
  enabled: boolean;
  max_file_bytes: number;
  sources: NasSource[];
  cloud_providers?: { id: string; configured: boolean; client_id: string }[];
  systems: {
    id: string;
    name: string;
    protocols: string[];
    integration: string;
  }[];
};
export type NasEntry = {
  name: string;
  path: string;
  directory: boolean;
  size: number;
  modified: string | number;
  mime_type?: string;
};
export const nasEvent = "ryn-nas-changed";
export function nasChanged() {
  clearNasMemory();
  window.dispatchEvent(new Event(nasEvent));
}

export type NasListing = { entries: NasEntry[] | null; updated_at?: number; next_cursor?: string; scan?: string };
const directories = new Map<string, NasListing>();
const listingRequests = new Map<string, { controller: AbortController; promise: Promise<NasListing>; users: number; timer?: ReturnType<typeof setTimeout> }>();
let memoryEpoch = 0;
let lastStatus: { key: string; value: NasStatus } | undefined;
const directoryKey = (source: string, path: string) => JSON.stringify([nodeControlUrl("/plugins/nas"), source, path]);
export function clearNasMemory() {
  memoryEpoch++;
  directories.clear();
  lastStatus = undefined;
  for (const request of listingRequests.values()) request.controller.abort();
  listingRequests.clear();
}
export function cachedNasDirectory(source: string, path: string) {
  return directories.get(directoryKey(source, path));
}
export function forgetNasDirectory(source: string, path: string) {
  directories.delete(directoryKey(source, path));
}
export function addNasCachedEntry(source: string, path: string, entry: NasEntry) {
  const key = directoryKey(source, path);
  const previous = directories.get(key)?.entries || [];
  const entries = [...previous.filter(item => item.path !== entry.path), entry];
  directories.set(key, { entries, updated_at: 0 });
  return entries;
}
export function rememberNasLocation(location: NonNullable<NasStatus["browser_location"]>) {
  if (lastStatus?.key === nodeControlUrl("/plugins/nas")) lastStatus.value = { ...lastStatus.value, browser_location: location };
  return nasJson("/browser-location", location, "PUT");
}
export function acquireNasListing(source: string, path: string, cached: boolean, cursor = "", scan = "") {
  const key = directoryKey(source, path);
  const requestKey = JSON.stringify([key, cached, cursor, scan]);
  let request = listingRequests.get(requestKey);
  if (!request) {
    const controller = new AbortController();
    const epoch = memoryEpoch;
    request = { controller, users: 0, promise: Promise.resolve({ entries: null }) };
    const current = request;
    request.promise = nasRequest(`/sources/${source}/files?path=${encodeURIComponent(path)}${cached ? "&cached=true" : ""}${cursor ? `&cursor=${encodeURIComponent(cursor)}&scan=${encodeURIComponent(scan)}` : ""}`, { signal: controller.signal })
      .then(response => response.json() as Promise<NasListing>)
      .then(value => {
        if (value.entries && epoch === memoryEpoch) {
          value.updated_at ||= cached ? 0 : Date.now() / 1000;
          directories.delete(key);
          directories.set(key, value);
          // File bodies and credentials never enter this cache or browser storage.
          while (directories.size > 100 || [...directories.values()].reduce((n, v) => n + (v.entries?.length || 0), 0) > 20000) directories.delete(directories.keys().next().value!);
        }
        return value;
      }).finally(() => { if (listingRequests.get(requestKey) === current) listingRequests.delete(requestKey); });
    listingRequests.set(requestKey, request);
  }
  request.users++;
  clearTimeout(request.timer);
  const current = request;
  let released = false;
  return { promise: current.promise, release: () => {
    if (released) return;
    released = true;
    current.users--;
    current.timer = setTimeout(() => {
      if (!current.users && listingRequests.get(requestKey) === current) {
        listingRequests.delete(requestKey);
        current.controller.abort();
      }
    }, 0);
  } };
}
export async function nasRequest(
  path = "",
  options?: RequestInit,
): Promise<Response> {
  const response = await fetch(nodeControlUrl(`/plugins/nas${path}`), {
    credentials: "include",
    ...options,
  }).catch((error: unknown) => {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new Error(i18n.t("drive.errors.network_error"));
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const key = `drive.errors.${String(error.detail || "")}`;
    if (i18n.exists(key)) throw Object.assign(new Error(i18n.t(key)), { status: response.status });
    const providerCode = /^drive_provider_error_(-?\d+)$/.exec(String(error.detail || ""));
    if (providerCode) throw new Error(i18n.t("drive.errors.provider_error", { code: providerCode[1] }));
    throw new Error(i18n.t("drive.errors.request_failed", { status: response.status }));
  }
  return response;
}
export async function nasJson<T>(
  path = "",
  body?: unknown,
  method = "POST",
): Promise<T> {
  return (
    await nasRequest(
      path,
      body === undefined
        ? undefined
        : {
            method,
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          },
    )
  ).json() as Promise<T>;
}
export function useNasStatus(demo = false) {
  const [status, setStatus] = useState<NasStatus | null>(() => !demo && lastStatus?.key === nodeControlUrl("/plugins/nas") ? lastStatus.value : null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const refresh = () => {
      if (demo) return;
      void nasJson<NasStatus>()
        .then((value) => {
          if (active) {
            lastStatus = { key: nodeControlUrl("/plugins/nas"), value };
            setStatus(value);
            setError("");
          }
        })
        .catch((e) => {
          if (active) {
            setError(e.message);
          }
        });
    };
    refresh();
    window.addEventListener(nasEvent, refresh);
    window.addEventListener("focus", refresh);
    return () => {
      active = false;
      window.removeEventListener(nasEvent, refresh);
      window.removeEventListener("focus", refresh);
    };
  }, [demo]);
  return { status, error };
}

// The selected text never goes into URLs or browser persistent storage.
export type NasHandoff = {
  sourceId: string;
  sourceName: string;
  path: string;
  text: string;
  writable: boolean;
};
let handoff: NasHandoff | null = null;
export function setNasHandoff(value: NasHandoff | null) {
  handoff = value;
}
export function getNasHandoff() {
  return handoff;
}
