import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { clearConversations, type LLMConversation } from "../domain/llmConversationStore";
import PrivateAIChat from "./PrivateAIChat";
import { askHistory, AskRequestError } from "../domain/askHistory";

beforeEach(async () => {
  await clearConversations("peer:fixture-llm-provider::fixture-local-llm");
});
afterEach(() => vi.restoreAllMocks());

function liveHistory() {
  const rows = new Map<string, LLMConversation>();
  vi.spyOn(askHistory, "list").mockImplementation(async () => [...rows.values()]);
  vi.spyOn(askHistory, "run").mockImplementation(async (taskId) => ({ task_id: taskId, conversation_id: "original", state: "succeeded", cancel_requested: false }));
  vi.spyOn(askHistory, "beginRun").mockImplementation(async (request) => {
    const prior = rows.get(request.conversation_id)!;
    rows.set(prior.id, { ...prior, revision: (prior.revision ?? 0) + 1, messages: [
      { id: "question", taskId: request.task_id, role: "user", content: request.question, status: "complete", createdAt: prior.createdAt },
      { id: "answer", taskId: request.task_id, role: "assistant", content: "An answer with [1].", status: "complete", createdAt: prior.createdAt, contextIds: ["import:imp_" + "b".repeat(64)], contextBytes: [1000], promptSha256: request.prompt_sha256 },
    ] });
    return { task_id: request.task_id, conversation_id: prior.id, state: "succeeded", cancel_requested: false };
  });
  vi.spyOn(askHistory, "preview").mockImplementation(async (row, question) => ({ conversation_id: row.id, revision: row.revision ?? 1,
    provider_peer_id: row.providerPeerId, service_id: row.serviceKey.slice(row.providerPeerId.length + 2), prompt: question, prompt_sha256: "fixture",
    context_window: 4096, input_token_upper_estimate: question.length, framing_reserve: 1024, max_output_tokens: 256, history_messages_omitted: 0, sources: [] }));
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

// A running task resumed from history (as opposed to one just submitted in
// this session): save a "running" conversation directly, then unmount and
// remount so the fresh render's initial load picks it up, matching how a
// reopened tab would observe it.
async function setupResumedRunningTask(taskId = "task_original") {
  const save = liveHistory();
  const first = renderChat("live");
  await screen.findByRole("heading", { name: "Ask Ryn" });
  const prior = save.mock.calls[0][0];
  const running: LLMConversation = { ...prior, messages: [{ id: "running-answer", role: "assistant", content: "Waiting on the node", status: "running", taskId, createdAt: prior.createdAt }] };
  await save(running);
  first.unmount();
  const rendered = renderChat("live");
  expect(await screen.findByText("Waiting on the node")).toBeInTheDocument();
  return { save, ...rendered };
}

describe("Private AI chat", () => {
  it("restores a node-owned running task and reads its archived answer after reopening", async () => {
    const save = liveHistory();
    const first = renderChat("live");
    await screen.findByRole("heading", { name: "Ask Ryn" });
    const prior = save.mock.calls[0][0];
    const running: LLMConversation = { ...prior, messages: [{ id: "running-answer", role: "assistant", content: "Waiting on the node", status: "running", taskId: "task_original", createdAt: prior.createdAt }] };
    await save(running);
    first.unmount();
    const reopened = renderChat("live");
    expect(await screen.findByText("Waiting on the node")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeInTheDocument();
    await save({ ...running, messages: [{ ...running.messages[0], content: "Archived while the page was closed", status: "complete" }] });
    expect(await screen.findByText("Archived while the page was closed", {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeInTheDocument();
    expect(reopened.submit).not.toHaveBeenCalled();
    expect(askHistory.beginRun).not.toHaveBeenCalled();
  });

  it("shows source truncation and confirms its fingerprint before node dispatch", async () => {
    liveHistory();
    const { submit, user, confirm } = renderChat("live");
    submit.mockImplementation(async (request) => ({ task_id: request.task_id!, state: "succeeded", output: "An answer with [1]." }));
    await screen.findByRole("heading", { name: "Ask Ryn" });
    vi.mocked(askHistory.preview).mockImplementation(async (row) => ({ conversation_id: row.id, revision: row.revision!, provider_peer_id: row.providerPeerId,
      service_id: row.serviceKey.slice(row.providerPeerId.length + 2), prompt: "A verified prompt with bounded untrusted article material", prompt_sha256: "a".repeat(64),
      context_window: 4096, input_token_upper_estimate: 2000, framing_reserve: 1024, max_output_tokens: 256, history_messages_omitted: 3,
      ai_permission: { relationship_id: "d".repeat(32), revision: 7 },
      sources: [{ library_id: "import:imp_" + "b".repeat(64), title: "Article", source_url: "https://example.test/article", sha256: "c".repeat(64), extraction_truncated: false,
        text_bytes: 10000, source_number: 1, included_bytes: 1000, budget_truncated: true }] }));
    await user.type(screen.getByLabelText("Message Private AI"), "Summarize this article");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    const review = confirm.mock.calls[0][0];
    expect(review.details[0]).toEqual({ label: "Question", value: "Summarize this article" });
    expect(review.details[1].value).toContain("TRUNCATED");
    expect(review.body).toContain("3 older history messages omitted");
    expect(submit).not.toHaveBeenCalled();
    await act(() => review.onConfirm());
    expect(askHistory.beginRun).toHaveBeenCalledWith(expect.objectContaining({ prompt_sha256: "a".repeat(64), question: "Summarize this article", ai_permission: { relationship_id: "d".repeat(32), revision: 7 } }));
    expect(submit).not.toHaveBeenCalled();
    expect(await screen.findByText("An answer with [1].")).toBeInTheDocument();
    expect(screen.getByText("Sources supplied for this answer")).toBeInTheDocument();
  });

  it("keeps input and does not submit when node history cannot be saved", async () => {
    const save = liveHistory();
    const { submit, user, confirm } = renderChat("live");
    await screen.findByRole("heading", { name: "Ask Ryn" });
    vi.mocked(askHistory.beginRun).mockRejectedValue(new AskRequestError(409, "Node history unavailable"));
    await user.type(screen.getByLabelText("Message Private AI"), "Keep this draft");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    await act(() => confirm.mock.calls[0][0].onConfirm());
    expect(await screen.findByRole("alert")).toHaveTextContent("Node history unavailable");
    expect(screen.getByLabelText("Message Private AI")).toHaveValue("Keep this draft");
    expect(submit).not.toHaveBeenCalled();
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("keeps the reviewed request retryable and frees the composer when the node times out", async () => {
    const save = liveHistory();
    const { submit, user, confirm } = renderChat("live");
    await screen.findByRole("heading", { name: "Ask Ryn" });
    vi.mocked(askHistory.beginRun).mockRejectedValue(new AskRequestError(0,
      "The node did not confirm this request within 30 seconds. Check the original task before retrying the same reviewed request.", "ask_request_timeout"));
    await user.type(screen.getByLabelText("Message Private AI"), "Question that times out");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    await act(() => confirm.mock.calls[0][0].onConfirm());
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The node did not confirm this request within 30 seconds. Check the original task before retrying the same reviewed request.",
    );
    expect(screen.getByRole("button", { name: "Retry same reviewed request" })).toBeEnabled();
    expect(submit).not.toHaveBeenCalled();
    expect(save).toHaveBeenCalledTimes(1);
  });

  it("reuses the reviewed task identity after a lost response and never calls legacy submission", async () => {
    liveHistory();
    const { submit, user, confirm } = renderChat("live");
    vi.mocked(askHistory.beginRun).mockRejectedValue(new Error("Response lost"));
    const check = vi.mocked(askHistory.run).mockRejectedValue(new Error("Unreachable"));
    await screen.findByRole("heading", { name: "Ask Ryn" });
    await user.type(screen.getByLabelText("Message Private AI"), "Original request");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(confirm).toHaveBeenCalled());
    expect(submit).not.toHaveBeenCalled();
    await act(() => confirm.mock.calls[0][0].onConfirm());
    const retry = await screen.findByRole("button", { name: "Check original task" });
    const request = vi.mocked(askHistory.beginRun).mock.calls[0][0];
    const task = request.task_id;
    expect(task).toMatch(/^task_/);
    await user.click(retry);
    expect(check).toHaveBeenCalledWith(task);
    await user.click(screen.getByRole("button", { name: "Retry same reviewed request" }));
    expect(askHistory.beginRun).toHaveBeenCalledTimes(2);
    expect(vi.mocked(askHistory.beginRun).mock.calls[1][0]).toEqual(request);
    expect(submit).not.toHaveBeenCalled();
  });

  it("recovers the composer when the node has no record of the task being cancelled", async () => {
    const { save, user } = await setupResumedRunningTask();
    await user.type(screen.getByLabelText("Message Private AI"), "Ready to retry");
    vi.spyOn(askHistory, "cancelRun").mockRejectedValue(new AskRequestError(404, "The node has no saved receipt for this task. Retry the same reviewed request to confirm it; do not create a different task.", "ask_run_not_found"));
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The node has no record of this task, so nothing is running there. The request was not confirmed; you can send it again.");
    // A resumed-from-history running task keeps `isSending` true via the
    // stale message status alone; recovery must clear that too, or the
    // composer stays stuck on "Stop generating" forever.
    expect(await screen.findByText("The node has no record of this task; nothing is running there.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
    // The rewrite went through the normal save path (revision-checked),
    // not a purely local patch.
    expect(save).toHaveBeenCalled();
  });

  it("frees the composer once a confirmed cancellation is reflected in refreshed history", async () => {
    const { save, user } = await setupResumedRunningTask("task_running");
    await user.type(screen.getByLabelText("Message Private AI"), "Ready to retry");
    const running = save.mock.calls[1][0] as LLMConversation;
    const cancelRun = vi.spyOn(askHistory, "cancelRun").mockImplementation(async (taskId) => {
      await save({ ...running, messages: [{ ...running.messages[0], status: "cancelled",
        content: "Cancellation was recorded. The provider may still be finishing computation." }] });
      return { task_id: taskId, conversation_id: running.id, state: "cancelled", cancel_requested: true };
    });
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(cancelRun).toHaveBeenCalledWith("task_running");
    expect(await screen.findByText("Cancellation was recorded. The provider may still be finishing computation.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the cancellation as unconfirmed and leaves Stop generating in place when cancelRun fails for a reason other than a missing task", async () => {
    const { user } = await setupResumedRunningTask("task_running");
    const cancelRun = vi.spyOn(askHistory, "cancelRun").mockRejectedValue(new AskRequestError(500, "The node is unreachable"));
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(cancelRun).toHaveBeenCalledWith("task_running");
    expect(await screen.findByRole("alert")).toHaveTextContent("Cancellation has not been confirmed. The task may still be running; check it again.");
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeInTheDocument();
    expect(screen.getByText("Waiting on the node")).toBeInTheDocument();
  });

  it("recovers a stale running task after one revision conflict, by retrying against the refreshed revision", async () => {
    const { save, user } = await setupResumedRunningTask();
    await user.type(screen.getByLabelText("Message Private AI"), "Ready to retry");
    vi.spyOn(askHistory, "cancelRun").mockRejectedValue(new AskRequestError(404, "The node has no saved receipt for this task.", "ask_run_not_found"));
    save.mockRejectedValueOnce(new AskRequestError(409, "This conversation changed in another view.", "ask_revision_conflict"));
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The node has no record of this task, so nothing is running there. The request was not confirmed; you can send it again.");
    expect(screen.queryByRole("button", { name: "Stop generating" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send message" })).toBeEnabled();
  });

  it("shows an explicit error and leaves Stop generating in place when recovery fails twice", async () => {
    const { save, user } = await setupResumedRunningTask();
    vi.spyOn(askHistory, "cancelRun").mockRejectedValue(new AskRequestError(404, "The node has no saved receipt for this task.", "ask_run_not_found"));
    save
      .mockRejectedValueOnce(new AskRequestError(409, "This conversation changed in another view.", "ask_revision_conflict"))
      .mockRejectedValueOnce(new AskRequestError(409, "This conversation changed in another view.", "ask_revision_conflict"));
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The node could not confirm clearing this task. Reload history and try again.");
    // Honest state: recovery never confirmed, so the task is still shown running.
    expect(screen.getByRole("button", { name: "Stop generating" })).toBeInTheDocument();
  });

  it("does not throw out of the click handler when the recovery refresh fails, and keeps the prior view", async () => {
    const { user } = await setupResumedRunningTask();
    vi.spyOn(askHistory, "cancelRun").mockRejectedValue(new AskRequestError(404, "The node has no saved receipt for this task.", "ask_run_not_found"));
    vi.mocked(askHistory.list).mockRejectedValueOnce(new Error("The node could not be reached"));
    await user.click(screen.getByRole("button", { name: "Stop generating" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The node could not confirm clearing this task. Reload history and try again.");
    expect(screen.getByText("Waiting on the node")).toBeInTheDocument();
  });

  it("clears the poll-unreachable error once the node responds again, without touching other errors", async () => {
    liveHistory();
    renderChat("live");
    await screen.findByRole("heading", { name: "Ask Ryn" });
    await screen.findByLabelText("Message Private AI");
    // The 1.5s poll runs on a real interval created at mount; drive it with
    // real time (as the existing node-owned-task test above does) rather
    // than fake timers, since the interval is already scheduled before this
    // test gets a chance to install fake timers.
    vi.mocked(askHistory.list).mockImplementationOnce(async () => { throw new Error("offline"); });
    expect(await screen.findByRole("alert", {}, { timeout: 3000 })).toHaveTextContent(
      "The node could not be reached. Saved tasks continue on the node; no new request was submitted.",
    );
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument(), { timeout: 3000 });
  }, 10_000);

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
