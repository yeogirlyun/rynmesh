import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { initializeDesktopConnection, nodeApiUrl } from "./nodeUrl";
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn().mockResolvedValue(18131) }));
beforeEach(() => { vi.stubEnv("VITE_RYN_NODE_BASE_URL", ""); });
afterEach(() => { vi.unstubAllEnvs(); delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__; });
it("keeps browser APIs same-origin", () => { expect(nodeApiUrl("/api/admin/info")).toBe("/api/admin/info"); });
it("sends desktop business APIs to the managed node port rather than tauri.localhost", async () => {
  (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
  await initializeDesktopConnection();
  expect(nodeApiUrl("/api/company-plugin/status")).toBe("http://127.0.0.1:18131/api/company-plugin/status");
  expect(nodeApiUrl("/api/admin/accounts")).toBe("http://127.0.0.1:18131/api/admin/accounts");
});
it("preserves an explicit node URL override", () => {
  vi.stubEnv("VITE_RYN_NODE_BASE_URL", "http://localhost:18132/api/local");
  expect(nodeApiUrl("/api/admin/info")).toBe("http://localhost:18132/api/admin/info");
});
