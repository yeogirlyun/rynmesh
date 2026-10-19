import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { LLMServiceRecord } from "../domain/nodeClient";
import type { NodeStatus, Peer } from "../domain/types";
import { PersonalProvider, serviceAvailability, usePersonal } from "./model";

const node: NodeStatus = {
  node_name: "Local PC", peer_id: "local", daemon_running: true, registry: "connected",
  peer_count: 1, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 1,
};
const peer: Peer = {
  id: "remote", name: "ziling", slug: "remote", endpoint: "", network: "test", tier: "unverified",
  credits: 0, weight: 0, lastSeen: "now", served: 0, fetched: 0, trustedRoot: false,
};
const record = (more: Partial<LLMServiceRecord> = {}): LLMServiceRecord => ({
  peer_id: "remote", node_name: "ziling", online: true, binding_verified: false,
  service: {
    package_id: "codex-cli", model_alias: "Codex CLI", adapter: "codex_cli", capabilities: [],
    context_window: 1, max_output_tokens: 1, privacy: {},
    pricing: { currency: "credit", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 0 },
  },
  ...more,
});

describe("serviceAvailability", () => {
  const base = { selfId: "local", own: false };
  it("does not offer a bare advertisement from an unrelated device", () => {
    expect(serviceAvailability(record(), base)).toBe("needs_connection");
    // The provider's own online claim never upgrades an advertisement.
    expect(serviceAvailability(record({ online: true }), { ...base, probed: true })).toBe("needs_connection");
  });
  it("is ready for an owned device, a bound catalog record, a friend or this node", () => {
    expect(serviceAvailability(record(), { ...base, own: true })).toBe("ready");
    expect(serviceAvailability(record({ binding_verified: true }), base)).toBe("ready");
    expect(serviceAvailability(record({ access: "friend" }), base)).toBe("ready");
    expect(serviceAvailability(record({ peer_id: "local" }), base)).toBe("ready");
    expect(serviceAvailability(record({ local_only: true }), base)).toBe("ready");
  });
  it("uses the node's own view model when the record carries one", () => {
    // The local node already checked entitlement and presence; the UI must not second-guess it.
    expect(serviceAvailability(
      record({ availability: { state: "ready", reason: "space_member" } }), base)).toBe("ready");
    expect(serviceAvailability(
      record({ availability: { state: "needs_connection", reason: "space_ai_access_off" } }),
      { ...base, own: true })).toBe("needs_connection");
    expect(serviceAvailability(
      record({ availability: { state: "offline", reason: "peer_unreachable" } }),
      { ...base, own: true, probed: true })).toBe("offline");
  });
  it("lets this node's probe override the provider's online claim", () => {
    expect(serviceAvailability(record(), { ...base, own: true, probed: false })).toBe("offline");
    expect(serviceAvailability(record({ online: false }), { ...base, own: true })).toBe("offline");
    // No probe yet: fall back to the provider's claim rather than hiding the service.
    expect(serviceAvailability(record(), { ...base, own: true, probed: undefined })).toBe("ready");
  });
});

let records: LLMServiceRecord[];
let health: { peerId: string; online: boolean; checkedAt: string }[];
beforeEach(() => {
  localStorage.clear();
  records = [record()];
  health = [{ peerId: "remote", online: true, checkedAt: "" }];
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }));
});
afterEach(() => vi.unstubAllGlobals());

function setup(members: { peer_id: string; name: string; role: "device"; removed: boolean }[] = []) {
  const client = makeFixtureNodeClient();
  client.mode = "live";
  vi.spyOn(client, "listLLMServices").mockImplementation(async () => records);
  vi.spyOn(client, "peersHealth").mockImplementation(async () => health);
  if (members.length) {
    return import("../domain/space").then(({ fixtureSpace }) => {
      const status = fixtureSpace();
      status.members = members;
      vi.spyOn(client, "spaceStatus").mockResolvedValue(status);
      return mount(client);
    });
  }
  return Promise.resolve(mount(client));
}
function mount(client: ReturnType<typeof makeFixtureNodeClient>) {
  const wrapper = ({ children }: { children: ReactNode }) => (
    <PersonalProvider client={client} node={node} peers={[peer]} refreshShell={async () => undefined}>
      {children}
    </PersonalProvider>
  );
  return renderHook(usePersonal, { wrapper });
}

describe("PersonalProvider services", () => {
  it("keeps a device and its advertised services consistent: visible, unowned, not ready", async () => {
    const { result } = await setup();
    await waitFor(() => expect(result.current.services).toHaveLength(1));
    const device = result.current.devices.find((item) => item.id === "remote");
    expect(device).toBeDefined();
    expect(device?.own).toBe(false);
    expect(result.current.services[0].deviceId).toBe("remote");
    expect(result.current.services[0].availability).toBe("needs_connection");
    expect(result.current.services[0].online).toBe(false);
  });
  it("creates the device for a provider known only from its service advertisement, unconfirmed", async () => {
    records = [record({ peer_id: "ad-only", node_name: "Advertiser" })];
    health = [];
    const { result } = await setup();
    await waitFor(() => expect(result.current.services).toHaveLength(1));
    const device = result.current.devices.find((item) => item.id === "ad-only");
    expect(device?.own).toBe(false);
    expect(device?.online).toBeNull();
  });
  it("makes the service ready once the device is an active space member", async () => {
    const { result } = await setup([{ peer_id: "remote", name: "ziling", role: "device", removed: false }]);
    await waitFor(() => expect(result.current.services[0]?.availability).toBe("ready"));
    expect(result.current.services[0].online).toBe(true);
  });
  it("marks an owned device's service offline when this node's probe fails", async () => {
    health = [{ peerId: "remote", online: false, checkedAt: "" }];
    const { result } = await setup([{ peer_id: "remote", name: "ziling", role: "device", removed: false }]);
    await waitFor(() => expect(result.current.services[0]?.availability).toBe("offline"));
    await act(async () => undefined);
    expect(result.current.services[0].online).toBe(false);
  });
});
