import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { Peer } from "../domain/types";
import { PersonalProvider } from "../personal/model";
import Chat from "./Chat";

beforeAll(() => {
  vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  class FakeEventSource { onmessage: ((event: MessageEvent) => void) | null = null; close() {} }
  Object.defineProperty(window, "EventSource", { configurable: true, value: FakeEventSource });
});

const peer = (id: string, name: string): Peer => ({ id, slug: id, name, endpoint: `http://${id}:8791`, network: "rynmesh-main", tier: "attested",
  credits: 0, weight: 1, lastSeen: "2026-09-03T10:00:00Z", served: 0, fetched: 0 });
const HOME = peer("peer:home-device-0001", "Home PC");
const WORK = peer("peer:work-device-0002", "Work PC");

function renderChat(peers: Peer[], history: Record<string, Record<string, unknown>[]> = {}) {
  const client = makeFixtureNodeClient();
  const listMessages = vi.spyOn(client, "listMessages").mockImplementation(async (id: string) => history[id] ?? []);
  const context: AppOutletContext = {
    client,
    node: { node_name: "Test Ryn", peer_id: "peer:self", daemon_running: true, registry: "connected", peer_count: peers.length, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 60 },
    registry: { status: "connected", url: "https://registry.test" }, peers, refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
  const view = render(
    <MemoryRouter initialEntries={["/chat"]}>
      <PersonalProvider client={client} node={context.node} peers={context.peers} refreshShell={context.refreshShell}>
        <Routes><Route element={<Outlet context={context} />}><Route path="/chat" element={<Chat />} /></Route></Routes>
      </PersonalProvider>
    </MemoryRouter>,
  );
  return { ...view, listMessages };
}

describe("Messages page layout", () => {
  it("renders the redesigned root, the conversation list and the open conversation", async () => {
    const { container } = renderChat([HOME, WORK], { [HOME.id]: [
      { msg_id: "m1", dir: "out", from: "peer:self", to: HOME.id, text: "Is the backup finished?", ts: "2026-09-03T10:00:00Z", kind: "text", delivered: true, via: "direct" },
      { msg_id: "m2", dir: "in", from: HOME.id, to: "peer:self", text: "Yes, all folders are copied.", ts: "2026-09-03T10:01:00Z", kind: "text" },
    ] });
    expect(container.querySelector(".rd-chat")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Messages" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Conversations" })).toBeInTheDocument();
    const homeRow = screen.getByRole("button", { name: /Home PC/ });
    expect(homeRow).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Work PC/ })).toHaveAttribute("aria-pressed", "false");
    expect(await screen.findByText("Is the backup finished?")).toBeInTheDocument();
    expect(screen.getByText("Yes, all folders are copied.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 2, name: "Home PC" })).toBeInTheDocument();
    expect(screen.getByText("Encrypted conversation")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Type a message")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Friends" })).toHaveAttribute("href", "/friends");
  });

  it("opens another device's conversation from the list and filters the list by name", async () => {
    const { listMessages } = renderChat([HOME, WORK], { [WORK.id]: [{ msg_id: "w1", dir: "in", from: WORK.id, to: "peer:self", text: "Slides are on the share.", ts: "2026-09-03T10:00:00Z", kind: "text" }] });
    const user = userEvent.setup();
    await waitFor(() => expect(listMessages).toHaveBeenCalledWith(HOME.id));
    await user.click(screen.getByRole("button", { name: /Work PC/ }));
    expect(await screen.findByText("Slides are on the share.")).toBeInTheDocument();
    expect(listMessages).toHaveBeenCalledWith(WORK.id);
    await user.type(screen.getByRole("textbox", { name: "Search devices" }), "home");
    expect(screen.queryByRole("button", { name: /Work PC/ })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Home PC/ })).toBeInTheDocument();
  });

  it("shows the empty conversation prompt and the no-devices state", async () => {
    renderChat([HOME]);
    expect(await screen.findByText("No messages yet")).toBeInTheDocument();
    expect(within(screen.getByRole("heading", { level: 2, name: "Home PC" }).closest(".rq-conv") as HTMLElement).getByText(/Messages are sealed for this peer only/)).toBeInTheDocument();
  });

  it("offers Open Devices when no other device has been discovered", () => {
    renderChat([]);
    expect(screen.getByRole("heading", { name: "No devices discovered yet." })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Devices" })).toHaveAttribute("href", "/devices");
    expect(screen.queryByRole("heading", { name: "Conversations" })).not.toBeInTheDocument();
  });
});
