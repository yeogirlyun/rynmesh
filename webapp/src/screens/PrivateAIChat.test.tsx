import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { clearConversations } from "../domain/llmConversationStore";
import PrivateAIChat from "./PrivateAIChat";

beforeEach(async () => {
  await clearConversations("peer:fixture-llm-provider::fixture-local-llm");
});

function renderChat() {
  const client = makeFixtureNodeClient();
  const submit = vi.spyOn(client, "submitLLMOrder");
  const confirm = vi.fn();
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [], refreshShell: vi.fn(async () => undefined), confirm, notify: vi.fn(),
  };
  const result = render(
    <MemoryRouter initialEntries={["/services/private-ai/chat?peer=peer%3Afixture-llm-provider&service=fixture-local-llm&network=rynmesh-main"]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="/services/private-ai/chat" element={<PrivateAIChat />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { ...result, client, confirm, submit, user: userEvent.setup() };
}

describe("Private AI chat", () => {
  it("creates, switches, searches, and sends independent conversations", async () => {
    const { submit, user } = renderChat();
    expect(await screen.findByRole("heading", { name: "Private AI" })).toBeInTheDocument();

    const composer = screen.getByLabelText("Message Private AI");
    await user.type(composer, "Why is this request private?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/Fixture response for: Why is this request private/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await user.type(composer, "Draft a launch email");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/Fixture response for: Draft a launch email/)).toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(2);

    await user.type(screen.getByLabelText("Search conversations"), "private");
    expect(screen.getByText("Why is this request private?", { selector: "strong" })).toBeInTheDocument();
    expect(screen.queryByText("Draft a launch email", { selector: "strong" })).not.toBeInTheDocument();
  });

  it("includes prior messages in a follow-up and requests destructive confirmation before clearing", async () => {
    const { confirm, submit, user } = renderChat();
    expect(await screen.findByRole("heading", { name: "Private AI" })).toBeInTheDocument();
    const composer = screen.getByLabelText("Message Private AI");
    await user.type(composer, "First question");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Fixture response for: First question/);
    await user.type(composer, "Follow-up question");
    await user.click(screen.getByRole("button", { name: "Send message" }));

    await waitFor(() => expect(submit).toHaveBeenLastCalledWith(expect.objectContaining({
      prompt: expect.stringContaining("User: First question"),
    })));
    expect(submit.mock.calls.at(-1)?.[0].prompt).toContain("User: Follow-up question");

    await user.click(screen.getByRole("button", { name: "Clear history" }));
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ risk: "high", confirmLabel: "Clear history" }));
  });
});
