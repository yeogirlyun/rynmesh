import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import i18n from "../i18n";
import { makeLiveNodeClient } from "../domain/liveNodeClient";
import Chat from "./Chat";

const state = vi.hoisted(() => ({
  client: {} as Record<string, unknown>, notify: vi.fn(),
  sources: [] as { onopen?: (event: Event) => void }[],
  peers: [{ id: "peer", name: "Peer", isSelf: false }],
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ client: state.client, peers: state.peers, notify: state.notify }) }));
vi.mock("../personal/model", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ devices: [], resolveName: (_id: string, name: string) => name }),
}));
beforeEach(async () => {
  await i18n.changeLanguage("zh-CN");
  state.sources = [];
  state.notify.mockReset();
  vi.stubGlobal("EventSource", class {
    onmessage = null;
    onopen?: (event: Event) => void;
    constructor() { state.sources.push(this); }
    close() {}
  });
  state.client = {
    mode: "live", messagesStreamUrl: () => "/api/local/messages/stream",
    listMessages: vi.fn().mockResolvedValue([]), getActivity: vi.fn().mockResolvedValue([]), sendMessage: vi.fn(),
  };
});
const view = () => render(<MemoryRouter><Chat /></MemoryRouter>);

it("T3 rejects an oversized attachment before reading it in the real composer", async () => {
  const rendered = view();
  const file = new File([], "large.bin");
  Object.defineProperty(file, "size", { value: 6 * 1024 * 1024 });
  const read = vi.fn();
  Object.defineProperty(file, "arrayBuffer", { value: read });
  const input = rendered.container.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => { fireEvent.change(input, { target: { files: [file] } }); });
  expect(state.notify).toHaveBeenCalledWith("danger", expect.stringContaining("5 MiB"));
  expect(screen.queryByText("large.bin")).not.toBeInTheDocument();
  expect(read).not.toHaveBeenCalled();
  expect(state.client.sendMessage).not.toHaveBeenCalled();
});

it("T3 translates the actual HTTP attachment_too_large rejection", async () => {
  server.use(http.post("*/api/local/messages/send", () => HttpResponse.json({ detail: "attachment_too_large" }, { status: 413 })));
  await expect(makeLiveNodeClient().sendMessage({ peer_id: "peer", text: "hello" })).rejects.toThrow("聊天附件最大 5 MiB");
});

it("T4 reloads persisted history when an SSE connection reopens", async () => {
  const history = vi.mocked(state.client.listMessages as () => Promise<unknown[]>);
  const first = { msg_id: "before", dir: "in", from: "peer", to: "self", text: "before overflow" };
  history.mockResolvedValue([first]);
  view();
  expect(await screen.findByText("before overflow")).toBeInTheDocument();
  await act(async () => { state.sources[0].onopen?.(new Event("open")); });
  history.mockResolvedValue([first, { ...first, msg_id: "missed", text: "recovered from history" }]);
  await act(async () => { state.sources[0].onopen?.(new Event("open")); });
  expect(await screen.findByText("recovered from history")).toBeInTheDocument();
});
