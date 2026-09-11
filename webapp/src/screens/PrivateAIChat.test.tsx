import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { clearConversations, type LLMConversation } from "../domain/llmConversationStore";
import PrivateAIChat from "./PrivateAIChat";
import { askHistory } from "../domain/askHistory";

beforeEach(async () => {
  await clearConversations("peer:fixture-llm-provider::fixture-local-llm");
});
afterEach(() => vi.restoreAllMocks());

function liveHistory() {
  const rows = new Map<string, LLMConversation>();
  vi.spyOn(askHistory, "list").mockImplementation(async () => [...rows.values()]);
  return vi.spyOn(askHistory, "save").mockImplementation(async (row) => {
    const saved = { ...row, revision: (row.revision ?? 0) + 1 }; rows.set(row.id, saved); return saved;
  });
}

function renderChat(mode: "fixture" | "live" = "fixture") {
  const client = makeFixtureNodeClient();
  client.mode = mode;
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
  it("keeps input and does not submit when node history cannot be saved", async () => {
    const save = liveHistory();
    const { submit, user } = renderChat("live");
    await screen.findByRole("heading", { name: "Ask Ryn" });
    save.mockRejectedValue(new Error("Node history unavailable"));
    await user.type(screen.getByLabelText("Message Private AI"), "Keep this draft");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Node history unavailable");
    expect(screen.getByLabelText("Message Private AI")).toHaveValue("Keep this draft");
    expect(submit).not.toHaveBeenCalled();
    expect(save).toHaveBeenCalledTimes(2);
  });

  it("persists task identity before submitting and checks it after a lost response", async () => {
    const save = liveHistory();
    const { submit, client, user } = renderChat("live");
    submit.mockRejectedValue(new Error("Response lost"));
    const check = vi.spyOn(client, "getLLMOrder").mockRejectedValue(new Error("Unreachable"));
    await screen.findByRole("heading", { name: "Ask Ryn" });
    await user.type(screen.getByLabelText("Message Private AI"), "Original request");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    const retry = await screen.findByRole("button", { name: "Check original task" });
    const task = submit.mock.calls[0][0].task_id;
    expect(task).toMatch(/^task_/);
    expect(save.mock.calls[1][0].messages[0].taskId).toBe(task);
    expect(submit.mock.calls[0][0].idempotency_key).toBe(task);
    await user.click(retry);
    expect(check).toHaveBeenCalledWith(task);
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it("creates, switches, searches, and sends independent conversations", async () => {
    const { submit, user } = renderChat();
    expect(await screen.findByRole("heading", { name: "Ask Ryn" })).toBeInTheDocument();

    const composer = screen.getByLabelText("Message Private AI");
    await user.type(composer, "Why is this request private?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/Fixture response for: Why is this request private/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New chat" }));
    await user.type(await screen.findByLabelText("Message Private AI"), "Draft a launch email");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/Fixture response for: Draft a launch email/)).toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(2);

    await user.type(screen.getByLabelText("Search conversations"), "private");
    expect(screen.getByText("Why is this request private?", { selector: "strong" })).toBeInTheDocument();
    expect(screen.queryByText("Draft a launch email", { selector: "strong" })).not.toBeInTheDocument();
  });

  it("includes prior messages in a follow-up and requests destructive confirmation before clearing", async () => {
    const { confirm, submit, user } = renderChat();
    expect(await screen.findByRole("heading", { name: "Ask Ryn" })).toBeInTheDocument();
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
