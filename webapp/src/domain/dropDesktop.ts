import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop } from "./nodeUrl";

// Desktop runtime: native pickers return absolute paths so the daemon can
// stream files from disk. The browser falls back to staged uploads.

export const canPickPaths = () => isTauriDesktop();

export async function chooseDropPaths(kind: "files" | "folder"): Promise<string[]> {
  if (!isTauriDesktop()) return [];
  return (await invoke<string[]>("choose_drop_paths", { kind })) || [];
}

export async function chooseDropDir(): Promise<string | null> {
  if (!isTauriDesktop()) return null;
  return (await invoke<string | null>("choose_drop_dir")) ?? null;
}

/** Opens a received path from the shell process. Executables are only revealed. */
export async function openDropPath(path: string, reveal: boolean): Promise<"open" | "reveal"> {
  return invoke<"open" | "reveal">("drop_open_path", { path, reveal });
}

export type NativeDropEvent =
  | { type: "over"; x: number; y: number }
  | { type: "drop"; x: number; y: number; paths: string[] }
  | { type: "leave" };

/** Subscribes to Tauri's window drag-drop (real paths). Returns an unsubscribe. */
export async function listenNativeFileDrop(handler: (event: NativeDropEvent) => void): Promise<() => void> {
  if (!isTauriDesktop()) return () => undefined;
  const { getCurrentWebview } = await import("@tauri-apps/api/webview");
  const ratio = () => window.devicePixelRatio || 1;
  return getCurrentWebview().onDragDropEvent(event => {
    const payload = event.payload as { type: string; paths?: string[]; position?: { x: number; y: number } };
    const x = (payload.position?.x ?? 0) / ratio();
    const y = (payload.position?.y ?? 0) / ratio();
    if (payload.type === "over" || payload.type === "enter") handler({ type: "over", x, y });
    else if (payload.type === "drop") handler({ type: "drop", x, y, paths: payload.paths || [] });
    else handler({ type: "leave" });
  });
}

export const baseName = (path: string) => path.replace(/[\\/]+$/, "").split(/[\\/]/).pop() || path;
