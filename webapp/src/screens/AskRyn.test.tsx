import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import type { AppOutletContext } from "../appContext";
import AskRynPanel from "../components/AskRynPanel";
import AskRynWorkspace, { ASSISTANT_CONVERSATION_PAGE_SIZE, pageAssistantConversations } from "../components/AskRynWorkspace";
import { assistantSession, createAssistantConversation, saveAssistantConversation } from "../domain/assistantStore";
import { clearConversations } from "../domain/llmConversationStore";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import AskRyn from "./AskRyn";

function context(client = makeFixtureNodeClient()): AppOutletContext {
  return {
    client,
    node: { node_name: "Test", peer_id: "self", daemon_running: true, registry: "connected",
      peer_count: 0, local_items: 1, fetched_items: 1, pending_recs: 0, version: "test", uptime_seconds: 1 },
    registry: { status: "connected", url: "local" },
    peers: [],
    refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(),
    notify: vi.fn(),
  };
}

function renderAsk(client = makeFixtureNodeClient(), entry = "/ask") {
  const user = userEvent.setup();
  const appContext = context(client);
  render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route element={<Outlet context={appContext} />}>
          <Route path="ask" element={<AskRyn />} />
          <Route path="local-ai" element={<div>Local AI setup</div>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { user, client, appContext };
}

beforeEach(async () => {
  await clearConversations("assistant");
  assistantSession.setActive(null);
});

afterEach(() => cleanup());

describe("Ask Ryn workspace", () => {
  it("starts without demo messages and completes a device-local conversation", async () => {
    const { user } = renderAsk();
    expect(await screen.findByRole("heading", { name: "One private place to ask" })).toBeInTheDocument();
    expect(screen.queryByText("Search design references about urban gardens.")).not.toBeInTheDocument();
    const composer = screen.getByRole("textbox", { name: "Ask Ryn message" });
    await user.type(composer, "What can you do privately?");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText(/I’m ready on this device/, {}, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getAllByText("What can you do privately?").length).toBeGreaterThan(0);
    expect(screen.getByText(/This device · Ryn Light/)).toBeInTheDocument();
  }, 8000);

  it("shares a draft between the quick panel and full workspace", async () => {
    const client = makeFixtureNodeClient();
    const target = (await client.listAssistantTargets())[0];
    const conversation = createAssistantConversation(target);
    await saveAssistantConversation(conversation);
    assistantSession.setActive(conversation.id);
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/ask"]}>
        <Routes><Route element={<Outlet context={context(client)} />}>
          <Route path="ask" element={<><AskRynWorkspace /><AskRynPanel appContext={context(client)} /></>} />
        </Route></Routes>
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getAllByRole("textbox", { name: "Ask Ryn message" })).toHaveLength(2));
    const composers = screen.getAllByRole("textbox", { name: "Ask Ryn message" });
    await user.type(composers[0], "shared private draft");
    await waitFor(() => expect(composers[1]).toHaveValue("shared private draft"));
  });

  it("prepares a text file as bounded private context", async () => {
    const { user } = renderAsk();
    await screen.findByRole("heading", { name: "One private place to ask" });
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["Local context body"], "private-notes.md", { type: "text/markdown" }));
    expect(await screen.findByText("private-notes.md")).toBeInTheDocument();
    expect(screen.getByText(/Compute: on this device/)).toBeInTheDocument();
  });

  it("offers the in-app setup flow instead of a dead composer when no model exists", async () => {
    const client = makeFixtureNodeClient();
    vi.spyOn(client, "listAssistantTargets").mockResolvedValue([]);
    renderAsk(client);
    expect(await screen.findByRole("heading", { name: "Enable private AI to ask Ryn" })).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Ask Ryn message" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Enable local AI" })).toBeInTheDocument();
  });

  it("requires a separate conversation before crossing to a friend privacy boundary", async () => {
    const { user, appContext } = renderAsk();
    await screen.findByRole("heading", { name: "One private place to ask" });
    await user.selectOptions(screen.getByRole("combobox", { name: "Assistant compute target" }), "friend:fixture-alice");
    expect(appContext.confirm).toHaveBeenCalledOnce();
    const request = vi.mocked(appContext.confirm).mock.calls[0][0];
    expect(request.body).toContain("will not be copied across the privacy boundary");
    await request.onConfirm();
    expect(await screen.findByText("Leaves this device")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "One private place to ask" })).toBeInTheDocument();
  });

  it("renames, archives, restores, and deletes encrypted conversations", async () => {
    const { user, appContext } = renderAsk();
    await screen.findByRole("heading", { name: "One private place to ask" });
    const prompt = vi.spyOn(window, "prompt").mockReturnValue("Private research");
    await user.click(screen.getByRole("button", { name: "Rename conversation" }));
    expect(await screen.findByRole("heading", { name: "Private research" })).toBeInTheDocument();
    prompt.mockRestore();

    await user.click(screen.getByRole("button", { name: "Archive conversation" }));
    await user.click(screen.getByLabelText("Show archived"));
    expect(await screen.findByRole("button", { name: /Private research/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Delete conversation" }));
    const request = vi.mocked(appContext.confirm).mock.calls.at(-1)![0];
    expect(request.title).toContain("Delete");
    await request.onConfirm();
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Private research" })).not.toBeInTheDocument());
  });

  it("keeps a 500-conversation history bounded to 50-row pages", () => {
    const target = { key: "local:test", label: "This device", privacy_boundary: "device" as const,
      online: true, route_modes: ["local_chat" as const] };
    const conversations = Array.from({ length: 500 }, (_, index) => ({
      ...createAssistantConversation(target), id: `conversation-${index}`, title: `Research ${index}`,
    }));
    const first = pageAssistantConversations(conversations, "", false, ASSISTANT_CONVERSATION_PAGE_SIZE);
    expect(first.items).toHaveLength(50);
    expect(first.remaining).toBe(450);
    const filtered = pageAssistantConversations(conversations, "Research 42", false, 50);
    expect(filtered.total).toBe(11);
  });
});
