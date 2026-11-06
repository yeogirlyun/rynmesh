import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { DirectoryPeer, DirectorySnapshot } from "../domain/directory";
import { NodeClientError, type LLMServiceRecord } from "../domain/nodeClient";
import type { NodeStatus, Peer } from "../domain/types";
import { PersonalProvider, usePersonal } from "./model";

const node: NodeStatus = {
  node_name: "Local PC", peer_id: "local", daemon_running: true, registry: "connected",
  peer_count: 1, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 1,
};
const record = (peerId: string, more: Partial<LLMServiceRecord> = {}): LLMServiceRecord => ({
  peer_id: peerId, node_name: peerId, online: true, binding_verified: false,
  service: {
    package_id: "codex-cli", model_alias: "Codex CLI", adapter: "codex_cli", capabilities: [],
    context_window: 1, max_output_tokens: 1, privacy: {},
    pricing: { currency: "c", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 0 },
  },
  ...more,
});
const entry = (id: string, more: Partial<DirectoryPeer> = {}): DirectoryPeer => ({
  id, slug: id, name: id, endpoint: "", network: "net", tier: "unverified", credits: 0, weight: 0,
  lastSeen: "", served: 0, fetched: 0, relationship: "discovered",
  presence: { state: "unconfirmed", basis: "none" }, services: [], ...more,
});
const snapshot = (peers: DirectoryPeer[]): DirectorySnapshot => ({
  revision: "r1", generated_at: "", network_id: "net", self_id: "local", peers,
});

beforeEach(() => {
  localStorage.clear();
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener: () => undefined, removeEventListener: () => undefined }));
});
afterEach(() => vi.unstubAllGlobals());

function setup(snap: DirectorySnapshot | Error, shellPeers: Peer[] = []) {
  const client = makeFixtureNodeClient();
  client.mode = "live";
  const services = vi.spyOn(client, "listLLMServices").mockResolvedValue([]);
  const health = vi.spyOn(client, "peersHealth").mockResolvedValue([]);
  const directory = vi.fn(async () => { if (snap instanceof Error) throw snap; return snap; });
  client.directory = directory;
  const wrapper = ({ children }: { children: ReactNode }) => (
    <PersonalProvider client={client} node={node} peers={shellPeers} refreshShell={async () => undefined}>
      {children}
    </PersonalProvider>
  );
  return { ...renderHook(usePersonal, { wrapper }), services, health, directory };
}

describe("PersonalProvider reading the node's directory", () => {
  it("builds devices and services from one snapshot without polling the separate lists", async () => {
    const advertised = record("ziling", { availability: { state: "needs_connection", reason: "advertisement_only" } });
    const { result, services, health, directory } = setup(snapshot([
      entry("local", { isSelf: true, relationship: "self", presence: { state: "online", basis: "self" } }),
      entry("ziling", { presence: { state: "online", basis: "http" }, services: [advertised] }),
    ]));
    await waitFor(() => expect(result.current.services).toHaveLength(1));
    expect(directory).toHaveBeenCalled();
    expect(services).not.toHaveBeenCalled();
    expect(health).not.toHaveBeenCalled();
    const device = result.current.devices.find((d) => d.id === "ziling");
    expect(device).toMatchObject({ own: false, online: true });
    expect(result.current.services[0]).toMatchObject({ deviceId: "ziling", availability: "needs_connection", online: false });
  });

  it("shows a provider that only advertised capacity as a device with unconfirmed presence", async () => {
    const { result } = setup(snapshot([
      entry("local", { isSelf: true, relationship: "self" }),
      entry("capacity-only", { services: [record("capacity-only")] }),
    ]));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const device = result.current.devices.find((d) => d.id === "capacity-only");
    expect(device).toBeDefined();
    expect(device?.online).toBeNull();
    expect(device?.own).toBe(false);
  });

  it("falls back to the separate lists when the node has no usable directory", async () => {
    const { result, services, health } = setup(new NodeClientError("Local Ryn node returned 404", 404), [
      { id: "remote", slug: "r", name: "Remote", endpoint: "", network: "net", tier: "unverified", credits: 0,
        weight: 0, lastSeen: "now", served: 0, fetched: 0 },
    ]);
    services.mockResolvedValue([record("remote")]);
    health.mockResolvedValue([{ peerId: "remote", online: true, checkedAt: "" }]);
    await waitFor(() => expect(result.current.services).toHaveLength(1));
    expect(result.current.devices.find((d) => d.id === "remote")?.online).toBe(true);
  });

  it.each([0, 500, 503])("keeps the last snapshot and adds no requests when /directory fails with %s", async (status) => {
    const { result, services, health, directory } = setup(new NodeClientError("Local Ryn node returned an error", status));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(directory).toHaveBeenCalled();
    expect(services).not.toHaveBeenCalled();
    expect(health).not.toHaveBeenCalled();
    expect(result.current.error).toBe("Some device or service information could not be refreshed.");
  });

  it("keeps one /directory request in flight while it hangs, and a manual refresh joins it", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
    try {
      let inFlight = 0;
      let maxInFlight = 0;
      const { result, services, health, directory } = setup(snapshot([]));
      directory.mockImplementation((options?: { signal?: AbortSignal }) => {
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        return new Promise<DirectorySnapshot>((_, reject) => {
          options?.signal?.addEventListener("abort", () => { inFlight -= 1; reject(options.signal!.reason); }, { once: true });
        });
      });
      await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
      void result.current.refresh();
      void result.current.refresh();
      await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });
      expect(maxInFlight).toBe(1);
      // Every hung run is bounded by its 25 s timeout before the next one starts.
      expect(directory.mock.calls.length).toBeLessThanOrEqual(4);
      expect(services).not.toHaveBeenCalled();
      expect(health).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("keeps shell peers the snapshot does not list", async () => {
    const { result } = setup(snapshot([entry("local", { isSelf: true })]), [
      { id: "shell-only", slug: "s", name: "Shell only", endpoint: "", network: "net", tier: "unverified",
        credits: 0, weight: 0, lastSeen: "now", served: 0, fetched: 0 },
    ]);
    await waitFor(() => expect(result.current.devices.some((d) => d.id === "shell-only")).toBe(true));
  });
});
