import { describe, expect, it, vi } from "vitest";
import { normalizeInviteLink, subscribeInviteLinks, type DeepLinkSource } from "./inviteDeepLinks";

describe("invite link boundary", () => {
  it("normalizes the ryn prefix without decoding or changing the signed envelope", () => {
    expect(normalizeInviteLink("ryn://join/Abc_123-XYZ")).toBe("rynmesh://join/Abc_123-XYZ");
    expect(normalizeInviteLink("rynmesh://join/Abc_123-XYZ")).toBe("rynmesh://join/Abc_123-XYZ");
  });
  it.each(["https://join/abc", "ryn://evil/abc", "ryn://join@evil/abc", "ryn://join/abc?token=x", "ryn://join/abc#x", "ryn://join/../abc", "ryn://join/abc%2fdef", "ryn://join/", "ryn://join/abc\n", "ryn://join/" + "a".repeat(16384)])("rejects malformed or unsupported input %s", (uri) => {
    expect(normalizeInviteLink(uri)).toBeNull();
  });
  it("subscribes before loading startup links and ignores a stale startup result", async () => {
    let event!: (urls: string[]) => void;
    let resolve!: (urls: string[]) => void;
    const stop = vi.fn(), receive = vi.fn(), invalid = vi.fn();
    const source: DeepLinkSource = {
      onOpenUrl: vi.fn(async (callback) => { event = callback; return stop; }),
      getCurrent: vi.fn(() => new Promise<string[]>(done => { resolve = done; })),
    };
    const cancel = subscribeInviteLinks(source, receive, invalid);
    await vi.waitFor(() => expect(source.getCurrent).toHaveBeenCalled());
    event(["ryn://join/new"]);
    resolve(["ryn://join/old"]);
    await Promise.resolve();
    expect(receive.mock.calls).toEqual([["rynmesh://join/new"]]);
    event(["ryn://join/one", "ryn://join/two"]);
    expect(invalid).toHaveBeenCalledTimes(1);
    cancel();
    event(["ryn://join/late"]);
    expect(receive).toHaveBeenCalledTimes(1);
    expect(stop).toHaveBeenCalledTimes(1);
  });
  it("delivers a cold-start link and reports source failure without exposing its error", async () => {
    const receive = vi.fn(), invalid = vi.fn();
    const source = { onOpenUrl: vi.fn(async () => () => {}), getCurrent: vi.fn(async () => ["ryn://join/cold"]) };
    const cancel = subscribeInviteLinks(source, receive, invalid);
    await vi.waitFor(() => expect(receive).toHaveBeenCalledWith("rynmesh://join/cold"));
    cancel();
    source.getCurrent.mockRejectedValueOnce(new Error("SECRET_MARKER"));
    const cleanup = subscribeInviteLinks(source, receive, invalid);
    await vi.waitFor(() => expect(invalid).toHaveBeenCalledWith());
    cleanup();
  });
});
