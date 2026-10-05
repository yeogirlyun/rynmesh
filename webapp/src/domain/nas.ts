import { tr } from "../uiI18n";
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
};
export type NasStatus = {
  enabled: boolean;
  max_file_bytes: number;
  sources: NasSource[];
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
};
export const nasEvent = "ryn-nas-changed";
export function nasChanged() {
  window.dispatchEvent(new Event(nasEvent));
}
export async function nasRequest(
  path = "",
  options?: RequestInit,
): Promise<Response> {
  const response = await fetch(nodeControlUrl(`/plugins/nas${path}`), {
    credentials: "include",
    ...options,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      String(error.detail || tr("Request failed ({{v0}})", { v0: response.status })).replaceAll(
        "_",
        " ",
      ),
    );
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
  const [status, setStatus] = useState<NasStatus | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    const refresh = () => {
      if (demo) return;
      void nasJson<NasStatus>()
        .then((value) => {
          if (active) {
            setStatus(value);
            setError("");
          }
        })
        .catch((e) => {
          if (active) {
            setStatus(null);
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
