import { act, render, screen, waitFor, within } from "@testing-library/react";
import i18n from "../i18n";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { clearConversations } from "../domain/llmConversationStore";
import PrivateAIChat from "./PrivateAIChat";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { setNasHandoff } from "../domain/nas";
import type { LLMServiceRecord, NodeClient } from "../domain/nodeClient";

beforeEach(async () => {
  Object.keys(localStorage).filter(key => key.startsWith("ryn-ai-services-v1:")).forEach(key => localStorage.removeItem(key));
  setNasHandoff(null);
  await clearConversations("peer:fixture-llm-provider::fixture-local-llm");
});

it("keeps conversation history and user text intact when changing UI language", async () => {
  const { user } = renderChat();
  await screen.findByRole("heading", { name: "AI workspace" });
  await user.type(screen.getByLabelText("Message AI chat"), "My English question");
  await user.click(screen.getByRole("button", { name: "Send message" }));
  await screen.findByText(/Fixture response for: My English question/);
  expect(screen.getByRole("heading", { name: "Today" })).toBeInTheDocument();
  await act(async () => { await i18n.changeLanguage("zh-CN"); });
  expect(screen.getByRole("heading", { name: "今天" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "新建对话" })).toBeInTheDocument();
  expect(within(screen.getByRole("complementary", { name: "AI 对话" })).getByText("My English question", { selector: "strong" })).toBeInTheDocument();
  await act(async () => { await i18n.changeLanguage("en"); });
  expect(screen.getByRole("heading", { name: "Today" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "New conversation" })).toBeInTheDocument();
});

function renderChat(path = "/services/private-ai/chat?peer=peer%3Afixture-llm-provider&service=fixture-local-llm&network=rynmesh-main", services?: LLMServiceRecord[], configure?: (client: NodeClient) => void) {
  const client = makeFixtureNodeClient();
  if (services) client.listLLMServices = vi.fn(async () => services);
  configure?.(client);
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
    <MemoryRouter initialEntries={[path]}>
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
  it.each([false, true])("opens cached history before slow discovery and preserves new work (local=%s)", async (localOnly) => {
    const first = renderChat(undefined, undefined, client => {
      const discover = client.listLLMServices;
      client.listLLMServices = async network => (await discover(network)).map(service => ({ ...service, local_only: localOnly }));
    });
    await first.user.type(await screen.findByLabelText("Message AI chat"), "Cached private question");
    await first.user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText(/Fixture response for: Cached private question/);
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeInTheDocument());
    const services = await first.client.listLLMServices();
    first.unmount();

    let finish!: (services: LLMServiceRecord[]) => void;
    const pending = new Promise<LLMServiceRecord[]>(resolve => { finish = resolve; });
    const second = renderChat(undefined, undefined, client => { client.listLLMServices = vi.fn(() => pending); });
    await screen.findByText(/Fixture response for: Cached private question/);
    expect(screen.getByText("Refreshing service status…")).toBeInTheDocument();
    expect(screen.queryByText("Opening AI chat")).not.toBeInTheDocument();
    expect(JSON.stringify(localStorage)).not.toContain("Cached private question");
    await second.user.click(screen.getByRole("button", { name: "New conversation" }));
    await second.user.type(screen.getByLabelText("Message AI chat"), "Draft during refresh");
    await act(async () => { finish(services); });
    await screen.findByText("Service available");
    expect(screen.getByLabelText("Message AI chat")).toHaveValue("Draft during refresh");
    expect(screen.queryByText(/Fixture response for: Cached private question/)).not.toBeInTheDocument();
    expect(second.submit).not.toHaveBeenCalled();
  });

  it("opens a cached default workspace without waiting for settings", async () => {
    const path = "/services/private-ai/chat";
    const first = renderChat(path);
    await screen.findByLabelText("Message AI chat");
    first.unmount();
    const second = renderChat(path, undefined, client => {
      client.getSettings = vi.fn(() => new Promise<never>(() => {}));
      client.listLLMServices = vi.fn(() => new Promise<never>(() => {}));
    });
    await screen.findByLabelText("Message AI chat");
    expect(second.client.getSettings).toHaveBeenCalledTimes(1);
    expect(second.client.listLLMServices).not.toHaveBeenCalled();
    expect(screen.queryByText("Opening AI chat")).not.toBeInTheDocument();
  });

  it("keeps cached history when discovery fails and disables sending until recovery", async () => {
    const first = renderChat();
    await screen.findByLabelText("Message AI chat");
    first.unmount();
    const second = renderChat(undefined, undefined, client => {
      client.listLLMServices = vi.fn().mockRejectedValue(new Error("offline"));
    });
    await screen.findByLabelText("Message AI chat");
    await screen.findByText("Offline");
    await second.user.type(screen.getByLabelText("Message AI chat"), "Keep this draft");
    expect(screen.getByRole("button", { name: "Send message" })).toBeDisabled();
    expect(screen.queryByRole("heading", { name: "Could not load AI services" })).not.toBeInTheDocument();
    expect(second.submit).not.toHaveBeenCalled();
  });

  it("does not use cached services for another requested provider or network", async () => {
    const first = renderChat();
    await screen.findByLabelText("Message AI chat");
    first.unmount();
    const missing = renderChat("/services/private-ai/chat?peer=missing&service=missing&network=rynmesh-main", []);
    await screen.findByRole("heading", { name: "The selected AI service is unavailable" });
    expect(screen.queryByLabelText("Message AI chat")).not.toBeInTheDocument();
    missing.unmount();
    renderChat("/services/private-ai/chat?network=another-network", []);
    await screen.findByRole("heading", { name: "The selected AI service is unavailable" });
    expect(screen.queryByLabelText("Message AI chat")).not.toBeInTheDocument();
  });

  it("recovers from a discovery error with Retry without sending a request", async () => {
    const { client, submit, user } = renderChat();
    vi.spyOn(client, "listLLMServices").mockRejectedValueOnce(new Error("registry timeout"));
    expect(await screen.findByRole("heading", { name: "Could not load AI services" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "AI workspace" })).toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });

  it("automatically recovers when the requested service reappears", async () => {
    const { client, submit, unmount } = renderChat();
    const discover = vi.spyOn(client, "listLLMServices").mockResolvedValueOnce([]);
    const timeout = vi.spyOn(window, "setTimeout");
    try {
      await screen.findByRole("heading", { name: "The selected AI service is unavailable" });
      const retry = timeout.mock.calls.find(call => call[1] === 10000)?.[0];
      expect(retry).toBeTypeOf("function");
      await act(async () => { (retry as () => void)(); });
      expect(await screen.findByRole("heading", { name: "AI workspace" })).toBeInTheDocument();
      expect(discover).toHaveBeenCalledTimes(2);
      expect(submit).not.toHaveBeenCalled();
    } finally {
      unmount();
      timeout.mockRestore();
    }
  });

  it("keeps Codex context at the origin without creating a provider conversation", async () => {
    const service: LLMServiceRecord = {
      peer_id: "local-codex", node_name: "This PC", local_only: true, online: true,
      service: { package_id: "codex-cli", model_alias: "Codex CLI", adapter: "codex_cli",
        capabilities: ["text-generation"], context_window: 32768, max_output_tokens: 256,
        pricing: { currency: "DEV_TASK_BALANCE", input_per_1k: 0, output_per_1k: 0, minimum: 0, maximum_per_task: 0 },
        privacy: { compute_node_sees_plaintext: true } },
    };
    await clearConversations("local-codex::codex-cli");
    const { client, submit, user } = renderChat("/services/private-ai/chat?peer=local-codex&service=codex-cli", [service]);
    vi.spyOn(client, "getCLIModels").mockResolvedValue({ models: [
      { id: "model-a", name: "Model A", default: true }, { id: "model-b", name: "Model B", default: false },
    ] });
    submit.mockResolvedValue({ task_id: "private-task", state: "succeeded", output: "Private answer" });
    await waitFor(() => expect(screen.getByLabelText("Codex model")).toBeEnabled());
    await user.click(screen.getByLabelText("Codex model"));
    await user.click(screen.getByRole("option", { name: "Model B" }));
    expect(screen.getByLabelText("AI service")).toHaveTextContent("ChatGPT");
    await user.type(screen.getByLabelText("Message AI chat"), "Remember kumquat");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Private answer");
    await user.click(screen.getByText("Conversation details"));
    expect(screen.queryByText("Copy session ID")).not.toBeInTheDocument();
    const first = submit.mock.calls[0][0];
    expect(first.cli_model).toBe("model-b");
    expect(first.prompt).toContain("Remember kumquat");
    expect(first).not.toHaveProperty("conversation_id");
    await user.type(screen.getByLabelText("Message AI chat"), "Which word?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenLastCalledWith(expect.objectContaining({
      cli_model: "model-b", prompt: expect.stringContaining("Which word?"),
    })));
    expect(submit.mock.lastCall![0].prompt).toContain("Remember kumquat");
    expect(submit.mock.lastCall![0].prompt).toContain("Private answer");
    expect(submit.mock.lastCall![0]).not.toHaveProperty("conversation_id");
  });
  it("lets a selected CLI service use the encrypted LAN-first automatic route", async () => {
    const service: LLMServiceRecord = {
      peer_id: "home", node_name: "Home PC", online: true,
      service: { package_id: "codex-cli", model_alias: "Codex CLI", adapter: "codex_cli",
        capabilities: ["text-generation"], context_window: 32768, max_output_tokens: 256,
        pricing: { currency: "DEV_TASK_BALANCE", input_per_1k: 0.001, output_per_1k: 0.002, minimum: 0.001, maximum_per_task: 1 },
        privacy: { compute_node_sees_plaintext: true },
      },
    };
    const { client, submit, user } = renderChat("/services/private-ai/chat?peer=home&service=codex-cli", [service]);
    submit.mockResolvedValue({ task_id: "cli-task", state: "succeeded", output: "ready" });
    vi.spyOn(client, "getLLMOrder").mockResolvedValue({ task_id: "cli-task", state: "succeeded", output: "ready" });
    await screen.findByRole("heading", { name: "AI workspace" });
    await user.type(screen.getByLabelText("Message AI chat"), "hello");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({ service_id: "codex-cli", transport: "auto" })));
  });
  it("requires Send before passing a NAS document to the selected model and saves the result", async () => {
    const marker = "NAS_DOCUMENT_ACCEPTANCE_42";
    let saved = "";
    server.use(
      http.get("*/api/local/plugins/nas/sources/home/content", () => HttpResponse.json({ name: "report.txt", text: marker })),
      http.put("*/api/local/plugins/nas/sources/home/content", async ({ request }) => { saved = await request.text(); return HttpResponse.json({ ok: true }); }),
    );
    setNasHandoff({ sourceId: "home", sourceName: "Home NAS", path: "report.txt", text: marker, writable: true });
    const { submit, user, confirm } = renderChat();
    await screen.findByLabelText("Message AI chat");
    expect(submit).not.toHaveBeenCalled();
    expect(window.location.href).not.toContain(marker);
    expect(JSON.stringify(localStorage)).not.toContain(marker);
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({ prompt: expect.stringContaining(marker) })));
    await user.click(await screen.findByRole("button", { name: "Save to NAS" }));
    await confirm.mock.calls.at(-1)?.[0].onConfirm();
    expect(saved).toBe((await submit.mock.results[0].value).output);
  });
  it("does not send a NAS document after the backend disables access", async () => {
    server.use(http.get("*/api/local/plugins/nas/sources/home/content", () => HttpResponse.json({ detail: "nas_plugin_disabled" }, { status: 409 })));
    setNasHandoff({ sourceId: "home", sourceName: "Home NAS", path: "report.txt", text: "secret", writable: false });
    const { submit, user } = renderChat();
    await screen.findByLabelText("Message AI chat");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("nas plugin disabled");
    expect(submit).not.toHaveBeenCalled();
  });
  it("shows peer connection progress and explains a UDP timeout", async () => {
    const { client, submit, user } = renderChat();
    submit.mockResolvedValue({ task_id: "connecting-test", state: "running" });
    const poll = vi.spyOn(client, "getLLMOrder");
    poll.mockResolvedValueOnce({ task_id: "connecting-test", state: "running", connection_phase: "connecting_p2p" });
    poll.mockResolvedValue({ task_id: "connecting-test", state: "failed", error_code: "p2p_connection_timed_out" });
    await screen.findByRole("heading", { name: "AI workspace" });
    await user.type(screen.getByLabelText("Message AI chat"), "Connect to my home PC");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText("Establishing peer connection…")).toBeInTheDocument();
    expect(await screen.findByText(/Could not connect directly to this device/)).toBeInTheDocument();
    expect(await screen.findByText("Request failed")).toBeInTheDocument();
    expect(screen.queryByText("Connected")).not.toBeInTheDocument();
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({ transport: "auto" }));
  });
  it("replaces direct-route waiting with the returned answer and releases the composer", async () => {
    const { client, submit, user } = renderChat();
    submit.mockResolvedValue({ task_id: "lan-reply", state: "queued" });
    let complete = false;
    vi.spyOn(client, "getLLMOrder").mockImplementation(async () => complete
      ? { task_id: "lan-reply", state: "succeeded", output: "Reply received from the Mac", transport: "peer_http_direct" }
      : { task_id: "lan-reply", state: "running", connection_phase: "connecting_direct" });
    await user.type(await screen.findByLabelText("Message AI chat"), "Hello Mac");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    await screen.findByText("Waiting for the device's reply…");
    // The elapsed label ticks once per second. CI scheduling may skip the
    // exact "1s" frame; still require positive elapsed time before completing.
    await screen.findByText(/Waited [1-9]\d*s/, {}, { timeout: 3500 });
    complete = true;
    await screen.findByText("Reply received from the Mac");
    await waitFor(() => expect(screen.getByRole("button", { name: "Send message" })).toBeInTheDocument());
    expect(screen.queryByLabelText("AI is thinking")).not.toBeInTheDocument();
  });
  it("does not silently choose another provider when the requested device is missing", async () => {
    const { submit } = renderChat('/services/private-ai/chat?peer=missing&service=missing');
    expect(await screen.findByRole('heading', { name: 'The selected AI service is unavailable' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Send message' })).not.toBeInTheDocument();
    expect(submit).not.toHaveBeenCalled();
  });
  it("creates, switches, searches, and sends independent conversations", async () => {
    const { submit, user } = renderChat();
    expect(await screen.findByRole("heading", { name: "AI workspace" })).toBeInTheDocument();

    const composer = screen.getByLabelText("Message AI chat");
    await user.type(composer, "Why is this request private?");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/Fixture response for: Why is this request private/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "New conversation" }));
    await user.type(composer, "Draft a launch email");
    await user.click(screen.getByRole("button", { name: "Send message" }));
    expect(await screen.findByText(/Fixture response for: Draft a launch email/)).toBeInTheDocument();
    expect(submit).toHaveBeenCalledTimes(2);

    await user.type(screen.getByLabelText("Search conversations"), "private");
    expect(screen.getByText("Why is this request private?", { selector: "strong" })).toBeInTheDocument();
    expect(within(screen.getByRole("complementary", { name: "AI conversations" })).queryByText("Draft a launch email", { selector: "strong" })).not.toBeInTheDocument();
  });

  it("includes prior messages in a follow-up and requests destructive confirmation before clearing", async () => {
    const { confirm, submit, user } = renderChat();
    expect(await screen.findByRole("heading", { name: "AI workspace" })).toBeInTheDocument();
    const composer = screen.getByLabelText("Message AI chat");
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
