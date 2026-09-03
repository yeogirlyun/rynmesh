import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { clearConversations, listConversations } from "../domain/llmConversationStore";
import type { LLMOrderStreamHandlers, NodeClient } from "../domain/nodeClient";
import PrivateAIChat from "./PrivateAIChat";

beforeEach(async () => {
  await clearConversations("peer:fixture-llm-provider::fixture-local-llm");
});

function renderChat(configureClient?: (client: NodeClient) => void) {
  const client = makeFixtureNodeClient();
  configureClient?.(client);
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

  it("renders verified deltas from local SSE but persists the assistant only at terminal", async () => {
    let handlers: LLMOrderStreamHandlers | undefined;
    const close = vi.fn();
    const { client, submit, user } = renderChat((configured) => {
      configured.submitLLMOrder = vi.fn(async () => ({ task_id: "task-stream", state: "queued" }));
      configured.subscribeLLMOrder = vi.fn((_taskId, next) => {
        handlers = next;
        return close;
      });
    });
    await screen.findByRole("heading", { name: "Private AI" });
    await user.type(screen.getByLabelText("Message Private AI"), "Stream this answer");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(client.subscribeLLMOrder).toHaveBeenCalledWith(
      "task-stream", expect.any(Object), -1,
    ));
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({ response_mode: "stream-v1" }));

    act(() => handlers?.onEvent({ event: "delta", sequence: 0, delta: "Hello " }));
    act(() => handlers?.onEvent({ event: "delta", sequence: 1, delta: "world" }));
    act(() => handlers?.onEvent({ event: "delta", sequence: 1, delta: " duplicated" }));
    expect(await screen.findByRole("status", { name: "Private AI streaming response" })).toHaveTextContent("Hello world");
    const beforeTerminal = await listConversations("peer:fixture-llm-provider::fixture-local-llm");
    expect(beforeTerminal[0].messages.filter((message) => message.role === "assistant")).toEqual([]);

    await act(async () => handlers?.onEvent({
      event: "complete",
      task_id: "task-stream",
      state: "succeeded",
      output: "Hello world",
      output_tokens: 2,
      amount: 0.001,
    }));
    await waitFor(() => expect(screen.queryByRole("status", { name: "Private AI streaming response" })).not.toBeInTheDocument());
    expect((await screen.findAllByText("Hello world")).length).toBeGreaterThan(0);
    const afterTerminal = await listConversations("peer:fixture-llm-provider::fixture-local-llm");
    expect(afterTerminal[0].messages.filter((message) => message.role === "assistant")).toHaveLength(1);
    expect(close).toHaveBeenCalledOnce();
  });

  it("reconnects from the last sequence, applies a snapshot, and finishes the same task", async () => {
    const subscriptions: Array<{ after: number; handlers: LLMOrderStreamHandlers }> = [];
    const { client, user } = renderChat((configured) => {
      configured.submitLLMOrder = vi.fn(async () => ({ task_id: "task-reconnect", state: "queued" }));
      configured.subscribeLLMOrder = vi.fn((_taskId, handlers, after = -1) => {
        subscriptions.push({ after, handlers });
        return vi.fn();
      });
      configured.getLLMOrder = vi.fn(configured.getLLMOrder);
    });
    await screen.findByRole("heading", { name: "Private AI" });
    await user.type(screen.getByLabelText("Message Private AI"), "Recover this answer");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));
    act(() => subscriptions[0].handlers.onEvent({ event: "delta", sequence: 0, delta: "old" }));
    act(() => subscriptions[0].handlers.onDisconnect());
    expect(await screen.findByText(/recovering the same task/)).toBeInTheDocument();
    await waitFor(() => expect(subscriptions).toHaveLength(2));
    expect(subscriptions[1].after).toBe(0);
    act(() => subscriptions[1].handlers.onEvent({
      event: "delta", sequence: 2, delta: "recovered snapshot", snapshot: true,
    }));
    expect(screen.getByRole("status", { name: "Private AI streaming response" })).toHaveTextContent("recovered snapshot");
    await act(async () => subscriptions[1].handlers.onEvent({
      event: "complete", task_id: "task-reconnect", state: "succeeded", output: "recovered snapshot",
    }));
    expect((await screen.findAllByText("recovered snapshot")).length).toBeGreaterThan(0);
    expect(client.getLLMOrder).not.toHaveBeenCalled();
  });

  it("cancels a running stream and marks the partial answer incomplete", async () => {
    let handlers: LLMOrderStreamHandlers | undefined;
    const { client, user } = renderChat((configured) => {
      configured.submitLLMOrder = vi.fn(async () => ({ task_id: "task-stop", state: "queued" }));
      configured.subscribeLLMOrder = vi.fn((_taskId, next) => {
        handlers = next;
        return vi.fn();
      });
      configured.cancelLLMOrder = vi.fn(configured.cancelLLMOrder);
    });
    await screen.findByRole("heading", { name: "Private AI" });
    await user.type(screen.getByLabelText("Message Private AI"), "Stop this answer");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(handlers).toBeDefined());
    act(() => handlers?.onEvent({ event: "delta", sequence: 0, delta: "Partial answer" }));
    expect((await listConversations("peer:fixture-llm-provider::fixture-local-llm"))[0].messages
      .filter((message) => message.role === "assistant")).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(client.cancelLLMOrder).toHaveBeenCalledWith("task-stop");
    await act(async () => handlers?.onEvent({ event: "error", state: "cancelled", error_code: "cancelled" }));
    expect(await screen.findByText(/Generation stopped — this response is incomplete/)).toBeInTheDocument();
    expect(screen.getAllByText("incomplete", { exact: false }).length).toBeGreaterThan(0);
    const stored = await listConversations("peer:fixture-llm-provider::fixture-local-llm");
    const assistants = stored[0].messages.filter((message) => message.role === "assistant");
    expect(assistants).toHaveLength(1);
    expect(assistants[0]).toMatchObject({ status: "cancelled", content: expect.stringContaining("Partial answer") });
  });

  it("polls the same task after a second disconnect and persists one terminal assistant", async () => {
    const subscriptions: LLMOrderStreamHandlers[] = [];
    const { client, submit, user } = renderChat((configured) => {
      configured.submitLLMOrder = vi.fn(async () => ({ task_id: "task-poll-reconnect", state: "queued" }));
      configured.subscribeLLMOrder = vi.fn((_taskId, handlers) => {
        subscriptions.push(handlers);
        return vi.fn();
      });
      configured.getLLMOrder = vi.fn(async () => ({
        task_id: "task-poll-reconnect", state: "succeeded", output: "Recovered terminal answer",
      }));
    });
    await screen.findByRole("heading", { name: "Private AI" });
    await user.type(screen.getByLabelText("Message Private AI"), "Recover by polling");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(subscriptions).toHaveLength(1));
    act(() => subscriptions[0].onDisconnect());
    await waitFor(() => expect(subscriptions).toHaveLength(2));
    act(() => subscriptions[1].onDisconnect());
    await waitFor(() => expect(client.getLLMOrder).toHaveBeenCalledWith("task-poll-reconnect"));
    expect(await screen.findByText("Recovered terminal answer")).toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(1);
    const stored = await listConversations("peer:fixture-llm-provider::fixture-local-llm");
    expect(stored[0].messages.filter((message) => message.role === "assistant")).toHaveLength(1);
  });

  it("falls back to terminal polling on a sequence gap without creating another order", async () => {
    let handlers: LLMOrderStreamHandlers | undefined;
    const { client, submit, user } = renderChat((configured) => {
      configured.submitLLMOrder = vi.fn(async () => ({ task_id: "task-gap", state: "queued" }));
      configured.subscribeLLMOrder = vi.fn((_taskId, next) => {
        handlers = next;
        return vi.fn();
      });
      configured.getLLMOrder = vi.fn(async () => ({
        task_id: "task-gap", state: "succeeded", output: "Gap recovered terminal",
      }));
    });
    await screen.findByRole("heading", { name: "Private AI" });
    await user.type(screen.getByLabelText("Message Private AI"), "Recover a gap");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(handlers).toBeDefined());
    act(() => handlers?.onEvent({ event: "delta", sequence: 1, delta: "missing zero" }));
    await waitFor(() => expect(client.getLLMOrder).toHaveBeenCalledWith("task-gap"));
    expect(await screen.findByText("Gap recovered terminal")).toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it("accepts a complete-response fallback through the same local event surface", async () => {
    let handlers: LLMOrderStreamHandlers | undefined;
    const { user } = renderChat((configured) => {
      configured.submitLLMOrder = vi.fn(async () => ({ task_id: "task-fallback", state: "queued" }));
      configured.subscribeLLMOrder = vi.fn((_taskId, next) => {
        handlers = next;
        return vi.fn();
      });
    });
    await screen.findByRole("heading", { name: "Private AI" });
    await user.type(screen.getByLabelText("Message Private AI"), "Use fallback");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(handlers).toBeDefined());
    act(() => handlers?.onEvent({ event: "state", state: "running" }));
    expect(screen.getByLabelText("Private AI is thinking")).toBeInTheDocument();
    await act(async () => handlers?.onEvent({
      event: "complete",
      task_id: "task-fallback",
      state: "succeeded",
      output: "Complete fallback answer",
    }));
    expect(await screen.findByText("Complete fallback answer")).toBeInTheDocument();
  });
});
