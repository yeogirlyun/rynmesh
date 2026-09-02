import { afterEach, describe, expect, it, vi } from "vitest";
import {
  consumeFriendInviteDeepLink,
  installDesktopFriendDeepLinks,
  queueFriendInviteDeepLink,
  validatedFriendInviteLink,
} from "./friendDeepLink";

const plugin = vi.hoisted(() => ({
  getCurrent: vi.fn<() => Promise<string[] | null>>(),
  onOpenUrl: vi.fn(),
}));

vi.mock("@tauri-apps/plugin-deep-link", () => plugin);

afterEach(() => {
  consumeFriendInviteDeepLink();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  vi.clearAllMocks();
});

describe("Friend Mesh desktop deep links", () => {
  it("accepts only the exact bounded rynmesh join bearer format", () => {
    expect(validatedFriendInviteLink("rynmesh://join/abc_DEF-123")).toBe("rynmesh://join/abc_DEF-123");
    expect(validatedFriendInviteLink(" rynmesh://join/abc ")).toBeNull();
    expect(validatedFriendInviteLink("rynmesh://other/abc")).toBeNull();
    expect(validatedFriendInviteLink("rynmesh://join/abc?leak=true")).toBeNull();
    expect(validatedFriendInviteLink("https://join/abc")).toBeNull();
    expect(validatedFriendInviteLink(`rynmesh://join/${"a".repeat(16_384)}`)).toBeNull();
  });

  it("keeps one accepted bearer in memory and consumes it exactly once", () => {
    expect(queueFriendInviteDeepLink("rynmesh://join/one-use-token")).toBe(true);
    expect(consumeFriendInviteDeepLink()).toBe("rynmesh://join/one-use-token");
    expect(consumeFriendInviteDeepLink()).toBeNull();
  });

  it("handles launch and running-instance URLs in Tauri but remains inert in a browser", async () => {
    const browserHandler = vi.fn();
    const browserRemove = await installDesktopFriendDeepLinks(browserHandler);
    browserRemove();
    expect(browserHandler).not.toHaveBeenCalled();
    expect(plugin.getCurrent).not.toHaveBeenCalled();

    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    plugin.getCurrent.mockResolvedValue(["rynmesh://other/no", "rynmesh://join/start-token"]);
    let runtimeHandler: ((urls: string[]) => void) | undefined;
    const unlisten = vi.fn();
    plugin.onOpenUrl.mockImplementation(async (handler: (urls: string[]) => void) => {
      runtimeHandler = handler;
      return unlisten;
    });
    const handler = vi.fn();
    const remove = await installDesktopFriendDeepLinks(handler);
    expect(handler).toHaveBeenCalledWith("rynmesh://join/start-token");
    runtimeHandler?.(["file:///not-an-invite", "rynmesh://join/runtime-token"]);
    expect(handler).toHaveBeenLastCalledWith("rynmesh://join/runtime-token");
    remove();
    expect(unlisten).toHaveBeenCalledOnce();
  });
});
