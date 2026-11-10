import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { initializeDesktopConnection, nodeApiUrl } from "./nodeUrl";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn().mockResolvedValue(18131) }));
beforeEach(() => { vi.stubEnv("VITE_RYN_NODE_BASE_URL", ""); });
afterEach(() => { vi.unstubAllEnvs(); delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__; });
it("keeps browser APIs same-origin", () => { expect(nodeApiUrl("/api/example/info")).toBe("/api/example/info"); });
it("sends desktop node APIs to the managed node port rather than tauri.localhost", async () => {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  await initializeDesktopConnection();
  expect(nodeApiUrl("/api/health/status")).toBe("http://127.0.0.1:18131/api/health/status");
  expect(nodeApiUrl("/api/example/accounts")).toBe("http://127.0.0.1:18131/api/example/accounts");
});
it("preserves an explicit node URL override", () => {
  vi.stubEnv("VITE_RYN_NODE_BASE_URL", "http://localhost:18132/api/local");
  expect(nodeApiUrl("/api/example/info")).toBe("http://localhost:18132/api/example/info");
});
