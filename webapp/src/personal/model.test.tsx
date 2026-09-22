import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { NodeStatus, Peer } from "../domain/types";
import {
  metadataKey,
  personalHref,
  PersonalProvider,
  useAppearance,
  usePersonal,
  validateDeviceDetails,
} from "./model";

const node: NodeStatus = {
  node_name: "Local PC",
  peer_id: "local",
  daemon_running: true,
  registry: "connected",
  peer_count: 1,
  local_items: 0,
  fetched_items: 0,
  pending_recs: 0,
  version: "test",
  uptime_seconds: 1,
};
const peer: Peer = {
  id: "remote",
  name: "Owner’s PC",
  slug: "remote",
  endpoint: "",
  network: "test",
  tier: "proven",
  credits: 0,
  weight: 0,
  lastSeen: "now",
  served: 0,
  fetched: 0,
  trustedRoot: true,
};
let dark = false;
let listeners: Set<() => void>;
beforeEach(() => {
  localStorage.clear();
  dark = false;
  listeners = new Set();
  vi.stubGlobal("matchMedia", () => ({
    get matches() {
      return dark;
    },
    addEventListener: (_event: string, handler: () => void) =>
      listeners.add(handler),
    removeEventListener: (_event: string, handler: () => void) =>
      listeners.delete(handler),
  }));
});
afterEach(() => vi.unstubAllGlobals());

function setup() {
  const client = makeFixtureNodeClient();
  client.mode = "live";
  vi.spyOn(client, "listLLMServices").mockResolvedValue([]);
  vi.spyOn(client, "peersHealth").mockResolvedValue([
    { peerId: peer.id, online: true, checkedAt: "" },
  ]);
  const update = vi.spyOn(client, "updateSettings");
  const refreshShell = vi.fn(async () => undefined);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <PersonalProvider
      client={client}
      node={node}
      peers={[peer]}
      refreshShell={refreshShell}
    >
      {children}
    </PersonalProvider>
  );
  return { ...renderHook(usePersonal, { wrapper }), update, refreshShell };
}

describe("personal device details", () => {
  it("never infers ownership from trust or discovery and saves remote notes locally", async () => {
    const { result, update } = setup();
    await waitFor(() => expect(result.current.loading).toBe(false));
    const remote = result.current.devices.find(
      (device) => device.id === "remote",
    )!;
    expect(remote.own).toBe(false);
    await act(() =>
      result.current.saveDevice(
        remote,
        "Studio",
        "Only for my reference",
        "desktop",
      ),
    );
    expect(update).not.toHaveBeenCalled();
    expect(result.current.resolveName("remote")).toBe("Studio");
    expect(
      JSON.parse(localStorage.getItem(metadataKey("live", "local"))!)["remote"]
        .note,
    ).toBe("Only for my reference");
    expect(peer.name).toBe("Owner’s PC");
  });
  it("sends only the owned device name to the node, keeping notes private", async () => {
    const { result, update, refreshShell } = setup();
    await waitFor(() => expect(result.current.loading).toBe(false));
    await act(() =>
      result.current.saveDevice(
        result.current.devices[0],
        "  My laptop  ",
        "Sensitive local note",
        "laptop",
      ),
    );
    expect(update).toHaveBeenCalledWith({ node_name: "My laptop" });
    expect(refreshShell).toHaveBeenCalledOnce();
    expect(result.current.devices[0].id).toBe("local");
    expect(result.current.devices[0].kind).toBe("laptop");
  });
  it("removes a nickname by falling back to the owner name", async () => {
    const { result } = setup();
    await waitFor(() => expect(result.current.loading).toBe(false));
    const remote = result.current.devices[1];
    await act(() => result.current.saveDevice(remote, "Studio", "", "desktop"));
    await act(() => result.current.saveDevice(remote, "  ", "", "desktop"));
    expect(result.current.resolveName("remote")).toBe(peer.name);
  });
  it("rejects empty owned names and excessive metadata while supporting Unicode", () => {
    expect(validateDeviceDetails(true, "   ", "")).toBeTruthy();
    expect(validateDeviceDetails(false, "", "")).toBe("");
    expect(validateDeviceDetails(true, "💻".repeat(32), "中文备注")).toBe("");
    expect(validateDeviceDetails(true, "💻".repeat(33), "")).toBeTruthy();
    expect(validateDeviceDetails(false, "Work", "a".repeat(201))).toBeTruthy();
    expect(metadataKey("fixture", "local")).not.toBe(
      metadataKey("live", "local"),
    );
  });
});
describe("appearance and navigation", () => {
  it("follows system changes until an explicit override is chosen and persists that override", () => {
    const { result } = renderHook(useAppearance);
    expect(document.documentElement.dataset.theme).toBe("light");
    act(() => {
      dark = true;
      listeners.forEach((listener) => listener());
    });
    expect(document.documentElement.dataset.theme).toBe("dark");
    act(() => result.current.setAppearance("light"));
    expect(localStorage.getItem("ryn.appearance")).toBe("light");
    act(() => listeners.forEach((listener) => listener()));
    expect(document.documentElement.dataset.theme).toBe("light");
    act(() => result.current.setAppearance("system"));
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
  it("preserves preview mode before fragments and alongside existing queries", () => {
    expect(personalHref("/services/manage#inference-api", true)).toBe(
      "/services/manage?client=fixture#inference-api",
    );
    expect(personalHref("/devices?device=abc", true)).toBe(
      "/devices?device=abc&client=fixture",
    );
    expect(personalHref("/devices", false)).toBe("/devices");
  });
});
