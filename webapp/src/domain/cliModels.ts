/** CLI providers with local model discovery and per-request selection. */
export function modelSelectableCLI(adapter?: string): "codex_cli" | "antigravity_cli" | null {
  return adapter === "codex_cli" || adapter === "antigravity_cli" ? adapter : null;
}
