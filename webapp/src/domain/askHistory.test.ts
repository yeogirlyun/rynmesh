import { afterEach, expect, it, vi } from "vitest";
import { askHistory, AskRequestError, conversationRepository } from "./askHistory";
import * as legacy from "./llmConversationStore";

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });
const conversation = () => legacy.createConversation({ serviceKey: "provider::model", serviceName: "Model", providerPeerId: "provider", networkId: "network" });

it("times out a hung request after 30 seconds so the caller can recover", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const fetch = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((_resolve, reject) => {
    options.signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
  }));
  vi.stubGlobal("fetch", fetch);
  const pending = expect(askHistory.beginRun({
    task_id: "task_1", conversation_id: "conversation", expected_revision: 1, question: "Question", prompt_sha256: "sha",
  })).rejects.toMatchObject({
    status: 0, code: "ask_request_timeout",
    message: "The node did not confirm this request within 30 seconds. Check the original task before retrying the same reviewed request.",
  });
  await vi.advanceTimersByTimeAsync(30_000);
  await pending;
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("gives the export request a longer timeout than the 30s default", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  let resolveFetch!: (response: Response) => void;
  const fetch = vi.fn((_url: string, options: RequestInit) => new Promise<Response>((resolve, reject) => {
    options.signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
    resolveFetch = resolve;
  }));
  vi.stubGlobal("fetch", fetch);
  const pending = askHistory.export();
  // Past the 30s default request timeout, still under export's 120s allowance.
  await vi.advanceTimersByTimeAsync(90_000);
  resolveFetch(new Response(JSON.stringify({ version: "ryn.ask-export.v1", conversations: [] })));
  await expect(pending).resolves.toEqual({ version: "ryn.ask-export.v1", conversations: [] });
});

it("does not report a timeout for an ordinary fetch failure", async () => {
  vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
  const rejection: unknown = await askHistory.list().then(() => null, (cause) => cause);
  expect(rejection).not.toBeInstanceOf(AskRequestError);
  expect(rejection).not.toMatchObject({ code: "ask_request_timeout" });
});

it("uses node revisions and propagates conflicts without browser writes", async () => {
  const browserWrite = vi.spyOn(legacy, "saveConversation");
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "ask_revision_conflict" }), { status: 409 }));
  vi.stubGlobal("fetch", fetch);
  const row = { ...conversation(), revision: 3 };
  await expect(askHistory.save(row)).rejects.toThrow("changed in another view");
  expect(JSON.parse(fetch.mock.calls[0][1].body).expected_revision).toBe(3);
  expect(browserWrite).not.toHaveBeenCalled();
});

it("retains encrypted browser originals when migration partially fails", async () => {
  const first = conversation(), second = conversation();
  vi.spyOn(legacy, "readLegacyConversations").mockResolvedValue({ conversations: [first, second], unreadable: 1 });
  const remove = vi.spyOn(legacy, "deleteConversation");
  const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ status: "imported" })))
    .mockRejectedValueOnce(new Error("Lost connection"));
  vi.stubGlobal("fetch", fetch);
  expect(await askHistory.importLegacy()).toEqual({ imported: 1, alreadyPresent: 0, skippedDeleted: 0, retained: 2 });
  expect(remove).not.toHaveBeenCalled();
  const migrated = JSON.parse(fetch.mock.calls[0][1].body);
  expect(migrated.conversation.serviceKey).toBe(first.serviceKey);
  expect(migrated.source).toBe("ryn-private-ai-chat-v1");
});

it("clears only the selected provider service and network", async () => {
  const own = { ...conversation(), revision: 2 }, other = { ...conversation(), networkId: "another-network", revision: 3 };
  vi.spyOn(askHistory, "list").mockResolvedValue([own, other]);
  const remove = vi.spyOn(askHistory, "remove").mockResolvedValue({ removed: 1 });
  await conversationRepository("live").clear(own.serviceKey, own.networkId);
  expect(remove).toHaveBeenCalledExactlyOnceWith(own);
});

it("does not count existing, deleted or unrecognized migration receipts as new imports", async () => {
  vi.spyOn(legacy, "readLegacyConversations").mockResolvedValue({ conversations: Array.from({ length: 4 }, conversation), unreadable: 0 });
  const remove = vi.spyOn(legacy, "deleteConversation");
  vi.stubGlobal("fetch", vi.fn()
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: "imported" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: "already_imported" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: "deleted" })))
    .mockResolvedValueOnce(new Response(JSON.stringify({ status: "unknown" }))));
  expect(await askHistory.importLegacy()).toEqual({ imported: 1, alreadyPresent: 1, skippedDeleted: 1, retained: 1 });
  expect(remove).not.toHaveBeenCalled();
});

it("preserves the confirmed deletion code and sends explicit replacement intent", async () => {
  const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "ask_conversation_deleted" }), { status: 409 }));
  vi.stubGlobal("fetch", fetch);
  await expect(askHistory.restoreBranch("source", "choice", "next", "revision", "deleted"))
    .rejects.toMatchObject({ status: 409, code: "ask_conversation_deleted" });
  expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ conversation_id: "source", choice_id: "choice", new_id: "next", expected_revision: "revision", replaces: "deleted" });
});
