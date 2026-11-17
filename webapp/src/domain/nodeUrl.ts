import { invoke } from "@tauri-apps/api/core";
export function isTauriDesktop(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

let desktopPort = 8791;
let desktopToken: string | undefined;

/** The credential stays in WebView memory and is never stored in browser storage. */
export async function localControlHeaders(): Promise<Record<string, string>> {
  if (!isTauriDesktop()) return {};
  desktopToken ??= await invoke<string>("local_token");
  return { "X-Ryn-Local-Token": desktopToken };
}

export async function initializeDesktopConnection(): Promise<void> {
  if (!isTauriDesktop()) return;
  desktopPort = await invoke<number>("desktop_node_port");
  await localControlHeaders();
  // Older domain clients also use fetch directly. Cover the entire trusted
  // WebView, while keeping the credential away from plugins and remote URLs.
  const originalFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = async (input, init) => {
    const url = new URL(input instanceof Request ? input.url : String(input), document.baseURI);
    const local = new URL(nodeControlBaseUrl(), document.baseURI);
    if (isTauriDesktop() && url.origin === local.origin && url.pathname.startsWith("/api/local/")) {
      const headers = new Headers(init?.headers ?? (input instanceof Request ? input.headers : undefined));
      headers.set("X-Ryn-Local-Token", desktopToken!);
      return originalFetch(input, { ...init, headers });
    }
    return originalFetch(input, init);
  };
}

export function nodeControlBaseUrl(): string {
  if (typeof document !== "undefined" && document.documentElement.dataset.nodeAppsHost === "true") return "/api/local";
  const explicit = import.meta.env.VITE_RYN_NODE_BASE_URL;
  if (explicit) return explicit.replace(/\/$/, "");
  return isTauriDesktop() ? `http://127.0.0.1:${desktopPort}/api/local` : "/api/local";
}

export function nodeControlUrl(path: string): string {
  const suffix = path.startsWith("/") ? path : `/${path}`;
  return `${nodeControlBaseUrl()}${suffix}`;
}

/** Resolve node API paths against the managed desktop node. */
export function nodeApiUrl(path: string): string {
  return `${nodeControlBaseUrl().replace(/\/api\/local$/, "")}${path.startsWith("/") ? path : `/${path}`}`;
}
