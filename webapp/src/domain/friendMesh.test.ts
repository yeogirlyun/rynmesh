import { describe, expect, it, vi } from "vitest";
import { endpointAddressClass, splitEndpoints } from "./friendMesh";
import { friendInviteQrDataUrl } from "./friendMeshQr";

describe("Friend Mesh offline helpers", () => {
  it("classifies every endpoint without DNS or network access", () => {
    expect(endpointAddressClass("https://192.168.1.20:8791")).toBe("private LAN");
    expect(endpointAddressClass("https://203.0.113.20:8791")).toBe("public IP literal");
    expect(endpointAddressClass("https://friend.example:8791")).toBe("unresolved hostname");
    expect(endpointAddressClass("http://127.0.0.1:8791")).toBe("blocked local/link-local");
    expect(endpointAddressClass("https://metadata.google.internal/computeMetadata/v1"))
      .toBe("blocked local/link-local");
    expect(endpointAddressClass("https://user:secret@friend.example")).toBe("invalid");
    expect(endpointAddressClass("ftp://friend.example/file")).toBe("invalid");
    expect(splitEndpoints("https://a.example\nhttps://b.example, https://a.example"))
      .toEqual(["https://a.example", "https://b.example"]);
  });

  it("generates a local SVG QR and never calls fetch", () => {
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const result = friendInviteQrDataUrl("rynmesh://join/signed-private-invite");

    expect(result).toMatch(/^data:image\/svg\+xml/);
    expect(decodeURIComponent(result)).toContain("<svg");
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});
