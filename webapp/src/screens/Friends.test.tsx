import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import Chat from "./Chat";
import Friends from "./Friends";

vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn(async () => "data:image/png;base64,fixture") } }));
class EventSourceStub { onmessage: ((event: MessageEvent) => void) | null = null; close() {} }
vi.stubGlobal("EventSource", EventSourceStub);

function context(): AppOutletContext {
  return {
    client: makeFixtureNodeClient(),
    node: { node_name: "Test", peer_id: "self", daemon_running: true, registry: "connected", peer_count: 4, local_items: 1, fetched_items: 1, pending_recs: 0, version: "test", uptime_seconds: 1 },
    registry: { status: "connected", url: "local" }, peers: [], refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
}
function setup(path: "/friends" | "/chat") {
  const appContext = context(); const user = userEvent.setup();
  render(<MemoryRouter initialEntries={[path]}><Routes><Route element={<Outlet context={appContext} />}><Route path="friends" element={<Friends />} /><Route path="chat" element={<Chat />} /></Route></Routes></MemoryRouter>);
  return { appContext, user };
}
afterEach(() => cleanup());

describe("friend pairing and private chat", () => {
  it("creates a one-time local QR invite and reviews before joining", async () => {
    const { user } = setup("/friends");
    expect((await screen.findAllByText("Maya's Ryn")).length).toBeGreaterThan(0);
    await user.click(screen.getByRole("button", { name: "Create invite" }));
    expect(await screen.findByAltText("Scannable friend invite QR code")).toBeInTheDocument();
    expect(screen.getByText(/one use only/i)).toBeInTheDocument();
    await user.type(screen.getByLabelText("Friend invite"), "rynmesh://friend/invite?data=fixture");
    await user.click(screen.getByRole("button", { name: "Review invite" }));
    expect(await screen.findByText("Signed invite verified locally")).toBeInTheDocument();
    expect(screen.getByText("Alex's MacBook")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Add this friend" }));
    await waitFor(() => expect(screen.getByText("Alex's MacBook")).toBeInTheDocument());
  });

  it("chat lists active friends, delivery state, and content-card sharing", async () => {
    const { user, appContext } = setup("/chat");
    expect((await screen.findAllByText("Maya's Ryn")).length).toBeGreaterThan(0);
    expect(screen.getByText("Explicitly paired")).toBeInTheDocument();
    expect(screen.queryByText("stadt-archive")).not.toBeInTheDocument();
    expect(await screen.findByText("I sent you the reading list.")).toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Message your friend"), "Got it");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Got it")).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText("Choose content to share"), "import:imp_fixture_notes");
    await user.click(screen.getByRole("button", { name: "Share card" }));
    expect(appContext.notify).toHaveBeenCalledWith("ok", "Content card delivered");
    expect(screen.getByText("Practical Local AI")).toBeInTheDocument();
  });
});
