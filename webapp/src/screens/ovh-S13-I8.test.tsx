import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { askHistory, type AskPreview } from "../domain/askHistory";
import { createConversation, type LLMConversation } from "../domain/llmConversationStore";
import Chat from "./AskRynConversation";

afterEach(() => vi.restoreAllMocks());

function setup(available = true) {
  const client = makeFixtureNodeClient(); client.mode = "live";
  if (!available) vi.spyOn(client, "listLLMServices").mockResolvedValue([]);
  const row = { ...createConversation({ serviceKey: "peer:fixture-llm-provider::fixture-local-llm", serviceName: "Model",
    providerPeerId: "peer:fixture-llm-provider", networkId: "rynmesh-main" }), revision: 1,
    messages: [{ id: "old", role: "assistant" as const, status: "complete" as const, content: "Original history", createdAt: new Date().toISOString(), finishReason: "length" }] };
  const rows = new Map<string, LLMConversation>([[row.id, row]]);
  vi.spyOn(askHistory, "list").mockImplementation(async () => [...rows.values()]);
  const save = vi.spyOn(askHistory, "save").mockImplementation(async value => { const saved = { ...value, revision: (value.revision ?? 0) + 1 }; rows.set(value.id, saved); return saved; });
  const preview: AskPreview = { conversation_id: row.id, revision: 1, provider_peer_id: row.providerPeerId,
    service_id: "fixture-local-llm", prompt: "Reviewed prompt", prompt_sha256: "a".repeat(64), context_window: 16384,
    input_token_upper_estimate: 100, framing_reserve: 1024, max_output_tokens: 2048, history_messages_omitted: 1,
    sources: [{ library_id: "import:test", title: "Article", source_url: "", sha256: "hash", extraction_truncated: false, text_bytes: 100, included_bytes: 50, budget_truncated: true }],
    service_review: { token: "review", model_alias: "Model", pricing: { input_per_1k: 1, output_per_1k: 2, minimum: 1, maximum_per_task: 10, currency: "task_balance" } } };
  vi.spyOn(askHistory, "preview").mockResolvedValue(preview);
  const submit = vi.spyOn(client, "submitLLMOrder").mockResolvedValue({ task_id: "summary-task", state: "succeeded", output: "A useful summary", amount: 2 });
  const confirm = vi.fn();
  const context = { client, confirm, notify: vi.fn(), peers: [], refreshShell: vi.fn(), node: { node_name: "Test", peer_id: "self" }, registry: { status: "connected" } } as unknown as AppOutletContext;
  render(<MemoryRouter initialEntries={[`/chat?conversation=${row.id}&network=rynmesh-main`]}><Routes><Route element={<Outlet context={context} />}><Route path="/chat" element={<Chat />} /></Route></Routes></MemoryRouter>);
  return { user: userEvent.setup(), row, rows, save, preview, confirm, submit };
}

async function review() {
  const state = setup();
  await state.user.type(await screen.findByLabelText("Message Private AI"), "Continue");
  await state.user.click(screen.getByRole("button", { name: "Send message" }));
  await waitFor(() => expect(state.confirm).toHaveBeenCalled());
  return state;
}

it("T4/T7 visibly explains omitted history and clipped article above the composer", async () => {
  const { submit } = await review();
  expect(screen.getByText("已省略最早的 1 条消息")).toBeInTheDocument();
  expect(screen.getByText("文章只用了前一部分")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "带摘要开新对话" })).toBeEnabled();
  expect(submit).not.toHaveBeenCalled();
});

it("shows the provider's length finish reason on the archived answer", async () => {
  setup();
  expect(await screen.findByText("回答被长度上限截断")).toBeInTheDocument();
});

it("keeps the length warning when the original provider is unavailable", async () => {
  setup(false);
  expect(await screen.findByText("Original history")).toBeInTheDocument();
  expect(screen.getByText("回答被长度上限截断")).toBeInTheDocument();
});

it.each(["success", "network", "provider", "length"] as const)("T5 summary requires separate charge confirmation and preserves the original: %s", async outcome => {
  const state = await review();
  const before = structuredClone(state.row);
  const button = await screen.findByRole("button", { name: "带摘要开新对话" });
  const summary = { ...state.preview, prompt: "Only omitted history", sources: [], prompt_format: "chat_messages_v1",
    chat: { messages: [{ role: "user" as const, content: "Original history" }], max_tokens: 2048 } };
  vi.spyOn(askHistory, "summaryPreview").mockResolvedValue(summary);
  if (outcome === "network") state.submit.mockRejectedValue(new Error("Summary failed"));
  if (outcome === "provider") state.submit.mockResolvedValue({ task_id: "summary-task", state: "failed", output: "Summary failed" });
  if (outcome === "length") state.submit.mockResolvedValue({ task_id: "summary-task", state: "succeeded", output: "Partial summary", finish_reason: "length", amount: 2 });
  await state.user.click(button);
  await waitFor(() => expect(state.confirm).toHaveBeenCalledTimes(2));
  const dialog = state.confirm.mock.calls[1][0];
  expect(dialog.body).toContain("单独一次请求");
  expect(dialog.body).toContain("积分");
  expect(state.submit).not.toHaveBeenCalled();
  await dialog.onConfirm();
  expect(state.submit).toHaveBeenCalledWith(expect.objectContaining({ provider_peer_id: state.row.providerPeerId,
    service_id: "fixture-local-llm", prompt: "Only omitted history", max_tokens: 2048, service_review_token: "review",
    prompt_format: "text", chat: summary.chat }));
  expect(state.rows.get(state.row.id)).toEqual(before);
  if (outcome !== "success") {
    expect(await screen.findByRole("alert")).toHaveTextContent(outcome === "length" ? "摘要被长度上限截断" : "Summary failed");
    expect(state.save).not.toHaveBeenCalled();
  } else {
    await waitFor(() => expect(state.save).toHaveBeenCalledTimes(1));
    const fresh = state.save.mock.calls[0][0];
    expect(fresh.id).not.toBe(state.row.id);
    expect(fresh.messages[0].content).toContain("A useful summary");
    expect(fresh.messages[0].cost).toBe(2);
  }
});
