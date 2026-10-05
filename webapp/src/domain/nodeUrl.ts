import { invoke } from "@tauri-apps/api/core";
export function isTauriDesktop(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

let desktopPort = 8791;
export async function initializeDesktopConnection(): Promise<void> {
  if (!isTauriDesktop()) return;
  desktopPort = await invoke<number>("desktop_node_port");
}

export function nodeControlBaseUrl(): string {
  const explicit = import.meta.env.VITE_RYN_NODE_BASE_URL;
  if (explicit) return explicit.replace(/\/$/, "");
  return isTauriDesktop() ? `http://127.0.0.1:${desktopPort}/api/local` : "/api/local";
}

export function nodeControlUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${nodeControlBaseUrl()}${suffix}`;
}
