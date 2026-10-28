import { act, createEvent, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { dropApi, newClientId, isDoneTransfer, isIncomingRequest, progressPercent, type DropMessage, type PeerMeta, type Transfer } from "../../domain/dropClient";
import { applyConversation, applyEvents, applySnapshot, emptyDropData, DropContext, DropProvider, useDrop, type DropState } from "../dropStore";
import { DeviceActionsMenu } from "./DeviceActionsMenu";
import { DeviceConversation } from "./DeviceConversation";
import { DeviceSendSheet } from "./DeviceSendSheet";
import { DevicesWorkspace } from "./DevicesWorkspace";
import { DropNotices } from "./DropNotices";
import { buildDropDevices } from "./dropModel";
import * as dropDesktop from "../../domain/dropDesktop";

vi.mock("../model", () => ({ usePersonal: () => ({ devices: [
  { id: "self", name: "This PC", kind: "desktop", self: true, own: true, online: true },
], loading: false, demo: false, refresh: vi.fn() }), personalHref: (path: string) => path }));

const peer = (id: string, more: Partial<PeerMeta> = {}): PeerMeta => ({ peer_id: id, name: id, relationship: "own", online: true,
  route: "lan", address: "127.0.0.1", latency_ms: 1, sendable: true, send_mode: "send", reason: "",
  verification: { required: false, code: null, verified: true }, auto_accept: false, unread: 0,
  last: { kind: "text", text: "Last message", at: "2026-10-13" }, ...more });
const transfer = (more: Partial<Transfer> = {}): Transfer => ({ transfer_id: "t1", offer_id: "o1", peer_id: "NAS", direction: "out",
  kind: "file", name: "footage.zip", file_count: 1, total_bytes: 100_000_000_000, confirmed_bytes: 50_000_000_000, status: "transferring",
  route: "lan", speed_bps: null, eta_s: null, error: null, note: "", save_dir: "", final_path: "", verification_code: null,
  actions: ["pause", "cancel"], created_at: "2026-10-13", updated_at: "2026-10-13", completed_at: null, version: 2, files_preview: [], ...more });
const message = (more: Partial<DropMessage> = {}): DropMessage => ({ message_id: "m1", peer_id: "NAS", direction: "out", kind: "text",
  text: "Hello", created_at: "2026-10-13", status: "delivered", delivered_at: "2026-10-13", error: "", version: 2, ...more });
let state: DropState;
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const data = emptyDropData();
  data.peers = { NAS: peer("NAS"), Laptop: peer("Laptop"), Offline: peer("Offline", { online: false, route: null, send_mode: "queue" }) };
  data.convs.NAS = { loaded: true, refs: [] };
  state = { status: "ready", capabilities: null, data, transfers: [], incoming: [], incomingOpen: false,
    setIncomingOpen: vi.fn(), notify: vi.fn(), isPending: () => false, run: async (_key, fn) => fn(), refresh: vi.fn(),
    loadConversation: vi.fn().mockResolvedValue(undefined), markRead: vi.fn(), sendMessage: vi.fn(), retryMessage: vi.fn(), transferAction: vi.fn(),
    acceptTransfer: vi.fn(), openTransfer: vi.fn(), verifyPeer: vi.fn(), preflight: vi.fn().mockResolvedValue({ ok: true }),
    createTransfer: vi.fn().mockResolvedValue(transfer()), loadPrefs: vi.fn(), setAutoAccept: vi.fn(),
    notices: [], dismissNotice: vi.fn() };
});
const renderWithStore = (ui: React.ReactNode) => render(<MemoryRouter><DropContext.Provider value={state}>{ui}</DropContext.Provider></MemoryRouter>);

describe("Drop state consistency", () => {
  it("keeps progress against delayed versions and uses lossless large offsets", () => {
    const data = applyEvents(emptyDropData(), [ { seq: 8, type: "transfer", transfer: transfer() },
      { seq: 7, type: "transfer", transfer: transfer({ confirmed_bytes: 2, version: 1 }) } ]);
    expect(data.transfers.t1.confirmed_bytes).toBe(50_000_000_000);
    expect(data.seq).toBe(8);
    expect(progressPercent("90071992547409920", "180143985094819840")).toBe(50);
  });
  it("keeps events that arrive while history is loading and reconciles optimistic messages", () => {
    const data = emptyDropData();
    data.convs.NAS = { loaded: true, refs: ["m:local:abc"] };
    data.messages["local:abc"] = message({ message_id: "local:abc", version: -1, client_id: "abc" });
    const withEvent = applyEvents(data, [{ seq: 1, type: "message", message: message({ client_id: "abc" }) }]);
    const afterHistory = applyConversation(withEvent, "NAS", []);
    expect(afterHistory.convs.NAS.refs).toEqual(["m:m1"]);
    expect(Object.keys(afterHistory.messages)).toEqual(["m1"]);
  });
  it("resets event sequence when daemon restarts", () => {
    const data = applySnapshot(emptyDropData(), { seq: 12, boot_id: "old", peers: [], transfers: [transfer()], incoming: 0 });
    const next = applySnapshot(data, { seq: 1, boot_id: "new", peers: [], transfers: [transfer({ version: 3, status: "paused" })], incoming: 0 });
    expect(next.seq).toBe(1);
    expect(next.transfers.t1.status).toBe("paused");
  });
  it("does not reconcile an optimistic message from another source", () => {
    const data = emptyDropData();
    data.messages["local:shared"] = message({ message_id: "local:shared", peer_id: "provider:first", client_id: "shared", version: -1 });
    data.convs["provider:first"] = { loaded: true, refs: ["m:local:shared"] };
    const next = applyEvents(data, [{ seq: 1, type: "message", message: message({ peer_id: "provider:second", client_id: "shared" }) }]);
    expect(next.messages["local:shared"]).toBeDefined();
    expect(next.convs["provider:first"].refs).toEqual(["m:local:shared"]);
  });
  it("does not put failures under Completed and keeps space errors reachable for acceptance", () => {
    expect(isDoneTransfer(transfer({ status: "failed" }))).toBe(false);
    expect(isDoneTransfer(transfer({ status: "completed" }))).toBe(true);
    expect(isIncomingRequest(transfer({ direction: "in", status: "insufficient_space" }))).toBe(true);
  });
  it("uses a staging-safe batch key", () => { expect(newClientId()).toMatch(/^[a-f0-9]{32}$/); });
});

it("restores keyboard focus after Escape and targets the chosen device", () => {
  const invoker = document.createElement("button"); document.body.appendChild(invoker); invoker.focus();
  const onClose = vi.fn(), onAction = vi.fn();
  const device = buildDropDevices([], state.data.peers)[0];
  renderWithStore(<DeviceActionsMenu device={device} anchor={invoker} onAction={onAction} onClose={onClose} />);
  expect(screen.getByRole("menuitem", { name: "Send files" })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole("menu"), { key: "ArrowDown" });
  expect(screen.getByRole("menuitem", { name: "Send folder" })).toHaveFocus();
  fireEvent.keyDown(screen.getByRole("menu"), { key: "Escape" });
  expect(onClose).toHaveBeenCalledOnce(); expect(invoker).toHaveFocus(); invoker.remove();
});

it("right-clicking another conversation does not retarget the draft, and IME Enter does not send", () => {
  const onActiveChange = vi.fn(), onMenu = vi.fn();
  renderWithStore(<DeviceConversation activeId="NAS" onActiveChange={onActiveChange} onMenu={onMenu} onSendFiles={vi.fn()} onDetails={vi.fn()}
    handlers={{}} mobileChat={false} onMobileBack={vi.fn()} />);
  const input = screen.getByRole("textbox", { name: "Message NAS" });
  fireEvent.change(input, { target: { value: "发给 NAS" } });
  fireEvent.contextMenu(screen.getByRole("button", { name: /Laptop.*Last message/ }), { clientX: 50, clientY: 50 });
  expect(onMenu.mock.calls[0][0].id).toBe("Laptop"); expect(onActiveChange).not.toHaveBeenCalled();
  expect(input).toHaveValue("发给 NAS");
  fireEvent.compositionStart(input); fireEvent.keyDown(input, { key: "Enter", keyCode: 229 });
  expect(state.sendMessage).not.toHaveBeenCalled();
  fireEvent.compositionEnd(input); fireEvent.keyDown(input, { key: "Enter" });
  expect(state.sendMessage).toHaveBeenCalledWith("NAS", "发给 NAS");
});

it("shows provider members as explicit device targets and only allows text", () => {
  const id = "provider:source-scoped-device";
  state.data.peers[id] = peer(id, { relationship: "external", name: "Work laptop", member_name: "Alex",
    plugin_id: "Sample plugin", device_peer_id: "actual-node-key", provider_resource: "https://provider.test/connect",
    file_sendable: false, last: null, online: null });
  state.data.convs[id] = { loaded: true, refs: [] };
  const files = vi.fn(), dropped = vi.fn();
  renderWithStore(<DeviceConversation activeId={id} onActiveChange={vi.fn()} onMenu={vi.fn()}
    onSendFiles={files} onDropFiles={dropped} onDetails={vi.fn()} handlers={{}} mobileChat={false} onMobileBack={vi.fn()} />);
  expect(screen.getByRole("button", { name: /Alex · Work laptop.*Sample plugin/ })).toBeInTheDocument();
  expect(screen.getByText(/Text to this device only/)).toBeInTheDocument();
  for (const button of screen.getAllByRole("button", { name: /^(Send files|Files)$/ })) expect(button).toBeDisabled();
  const input = screen.getByRole("textbox", { name: "Message Alex · Work laptop" });
  fireEvent.change(input, { target: { value: "hello target device" } });
  fireEvent.keyDown(input, { key: "Enter" });
  expect(state.sendMessage).toHaveBeenCalledWith(id, "hello target device");
  fireEvent.drop(input.closest("form")!, { dataTransfer: { types: ["Files"], files: [new File(["x"], "file.txt")] } });
  expect(files).not.toHaveBeenCalled();
  expect(dropped).not.toHaveBeenCalled();
});

it("network and offline context menus open device history with the correct filter", () => {
  renderWithStore(<DevicesWorkspace />);
  fireEvent.contextMenu(screen.getByRole("button", { name: /^Offline/ }), { clientX: 20, clientY: 30 });
  fireEvent.click(screen.getByRole("menuitem", { name: "Transfer history" }));
  expect(screen.getByRole("tab", { name: "Transfers" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByText(/Device: Offline/)).toBeInTheDocument();
});

it("dropping multiple files on the composer opens confirmation for that peer and preserves the draft", async () => {
  vi.spyOn(dropApi, "upload").mockImplementation(async file => ({ upload_id: `batch:${file.name}`, name: file.name, rel_path: file.name, size: file.size }));
  renderWithStore(<DevicesWorkspace />);
  fireEvent.click(screen.getByRole("tab", { name: "Device messages" }));
  const input = screen.getByRole("textbox", { name: "Message NAS" });
  fireEvent.change(input, { target: { value: "Keep this draft" } });
  const composer = input.closest("form")!;
  const files = [new File(["abc"], "first.txt"), new File(["def"], "second.txt")];
  const dataTransfer = { types: ["Files"], files, items: [], dropEffect: "none" };
  fireEvent.dragOver(input, { dataTransfer });
  expect(composer).toHaveClass("drop-target");
  expect(dataTransfer.dropEffect).toBe("copy");
  const leave = createEvent.dragLeave(input);
  Object.defineProperty(leave, "relatedTarget", { value: composer.querySelector("button") });
  fireEvent(input, leave);
  expect(composer).toHaveClass("drop-target");
  fireEvent.drop(input, { dataTransfer });
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("NAS");
  expect(dialog).toHaveTextContent("first.txt");
  expect(dialog).toHaveTextContent("second.txt");
  expect(composer).not.toHaveClass("drop-target");
  expect(input).toHaveValue("Keep this draft");
  expect(state.createTransfer).not.toHaveBeenCalled();
  fireEvent.click(within(dialog).getByRole("button", { name: /^Send$/ }));
  await waitFor(() => expect(state.createTransfer).toHaveBeenCalledOnce());
  expect(dropApi.upload).toHaveBeenCalledTimes(2);
  expect(state.createTransfer).toHaveBeenCalledWith(expect.objectContaining({ peer_id: "NAS", upload_ids: ["batch:first.txt", "batch:second.txt"] }));
});

it("dropping a browser folder on the composer preserves relative paths", async () => {
  renderWithStore(<DevicesWorkspace />);
  fireEvent.click(screen.getByRole("tab", { name: "Device messages" }));
  const file = new File(["abc"], "notes.txt");
  const fileEntry = { isFile: true, isDirectory: false, file: (resolve: (file: File) => void) => resolve(file) };
  const readEntries = vi.fn().mockImplementationOnce(resolve => resolve([fileEntry])).mockImplementation(resolve => resolve([]));
  const folder = { name: "documents", isFile: false, isDirectory: true, createReader: () => ({ readEntries }) };
  fireEvent.drop(screen.getByRole("textbox", { name: "Message NAS" }), {
    dataTransfer: { types: ["Files"], files: [], items: [{ kind: "file", webkitGetAsEntry: () => folder }] },
  });
  const dialog = await screen.findByRole("dialog");
  expect(dialog).toHaveTextContent("documents");
  fireEvent.click(within(dialog).getByRole("button", { name: "Show files" }));
  expect(dialog).toHaveTextContent("notes.txt");
  expect(state.createTransfer).not.toHaveBeenCalled();
});

it("composer leaves text drags alone and clears the file highlight when leaving", () => {
  const onDropFiles = vi.fn();
  renderWithStore(<DeviceConversation activeId="NAS" onActiveChange={vi.fn()} onMenu={vi.fn()} onSendFiles={vi.fn()}
    onDropFiles={onDropFiles} onDetails={vi.fn()} handlers={{}} mobileChat={false} onMobileBack={vi.fn()} />);
  const input = screen.getByRole("textbox", { name: "Message NAS" });
  const composer = input.closest("form")!;
  fireEvent.dragOver(input, { dataTransfer: { types: ["text/plain"] } });
  fireEvent.drop(input, { dataTransfer: { types: ["text/plain"] } });
  expect(composer).not.toHaveClass("drop-target");
  expect(onDropFiles).not.toHaveBeenCalled();
  fireEvent.dragOver(input, { dataTransfer: { types: ["Files"] } });
  expect(composer).toHaveClass("drop-target");
  fireEvent.dragLeave(composer, { relatedTarget: null });
  expect(composer).not.toHaveClass("drop-target");
});

it("native file drops over the composer open the path-based confirmation sheet", async () => {
  vi.spyOn(dropDesktop, "canPickPaths").mockReturnValue(true);
  let handler: (event: dropDesktop.NativeDropEvent) => void;
  const stop = vi.fn();
  vi.spyOn(dropDesktop, "listenNativeFileDrop").mockImplementation(async callback => { handler = callback; return stop; });
  const { unmount } = renderWithStore(<DevicesWorkspace />);
  fireEvent.click(screen.getByRole("tab", { name: "Device messages" }));
  const input = screen.getByRole("textbox", { name: "Message NAS" });
  const composer = input.closest("form")!;
  const original = document.elementFromPoint;
  document.elementFromPoint = vi.fn(() => input);
  try {
    await act(async () => handler!({ type: "over", x: 500, y: 700 }));
    expect(composer).toHaveClass("drop-target");
    await act(async () => handler!({ type: "leave" }));
    expect(composer).not.toHaveClass("drop-target");
    await act(async () => handler!({ type: "drop", x: 500, y: 700, paths: ["C:\\files\\report.txt", "C:\\files\\photos"] }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("NAS");
    expect(dialog).toHaveTextContent("report.txt");
    expect(dialog).toHaveTextContent("photos");
    expect(state.createTransfer).not.toHaveBeenCalled();
    unmount();
    expect(stop).toHaveBeenCalledOnce();
  } finally {
    document.elementFromPoint = original;
  }
});

it("browser selection streams the File with a valid batch id before creating a transfer", async () => {
  vi.spyOn(dropApi, "upload").mockResolvedValue({ upload_id: "abcd1234:file.txt", name: "file.txt", rel_path: "file.txt", size: 3 });
  const file = new File(["abc"], "file.txt");
  renderWithStore(<DeviceSendSheet device={buildDropDevices([], state.data.peers)[0]} init={{ kind: "files", files: [{ file, rel: file.name }] }} onClose={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));
  await waitFor(() => expect(state.createTransfer).toHaveBeenCalledOnce());
  expect(dropApi.upload).toHaveBeenCalledWith(file, "file.txt", expect.stringMatching(/^[a-f0-9]{32}$/), expect.any(AbortSignal));
});

it("retry after a lost HTTP response preserves the original idempotency key", async () => {
  vi.spyOn(dropApi, "snapshot").mockResolvedValue({ seq: 0, peers: [], transfers: [], incoming: 0 });
  vi.spyOn(dropApi, "capabilities").mockRejectedValue(new Error("offline"));
  const send = vi.spyOn(dropApi, "sendMessage").mockRejectedValueOnce(new Error("lost response")).mockResolvedValueOnce(message());
  let live: DropState;
  function Probe() { live = useDrop(); return <span>{live.status}</span>; }
  render(<DropProvider mode="live"><Probe /></DropProvider>);
  await screen.findByText("ready");
  await act(async () => { await live.sendMessage("NAS", "hello").catch(() => undefined); });
  const key = send.mock.calls[0][2];
  await act(async () => { await live.retryMessage(`local:${key}`); });
  expect(send.mock.calls[1]).toEqual(["NAS", "hello", key]);
  expect(Object.keys(live!.data.messages)).toEqual(["m1"]);
});

describe("receive notices and opening", () => {
  const liveStore = async () => {
    vi.spyOn(dropApi, "capabilities").mockRejectedValue(new Error("offline"));
    let live: DropState;
    function Probe() { live = useDrop(); return <span>{live.status}</span>; }
    render(<DropProvider mode="live"><Probe /></DropProvider>);
    await screen.findByText("ready");
    return () => live!;
  };

  it("announces new requests and receives once per peer, never items from the first snapshot", async () => {
    const old = transfer({ transfer_id: "old", direction: "in", status: "completed", actions: ["open", "reveal"] });
    const snapshot = vi.spyOn(dropApi, "snapshot").mockResolvedValue({ seq: 1, peers: [], transfers: [old], incoming: 0 });
    const live = await liveStore();
    expect(live().notices).toEqual([]);
    const a = transfer({ transfer_id: "a", direction: "in", status: "awaiting_accept", actions: ["accept", "reject"], version: 3 });
    const b = transfer({ transfer_id: "b", direction: "in", status: "awaiting_accept", actions: ["accept", "reject"], version: 3 });
    const done = transfer({ transfer_id: "c", peer_id: "Laptop", direction: "in", status: "completed", actions: ["open", "reveal"], version: 3 });
    snapshot.mockResolvedValue({ seq: 2, peers: [], transfers: [old, a, b, done], incoming: 2 });
    await act(async () => { await live().refresh(); });
    expect(live().notices.map(n => [n.kind, n.peerId, n.transferIds])).toEqual([
      ["incoming", "NAS", ["a", "b"]], ["received", "Laptop", ["c"]],
    ]);
    // Accepting elsewhere clears the request card; repeating the same state adds nothing.
    snapshot.mockResolvedValue({ seq: 3, peers: [], transfers: [old, { ...a, status: "transferring", actions: [], version: 4 },
      { ...b, status: "rejected", actions: [], version: 4 }, done], incoming: 0 });
    await act(async () => { await live().refresh(); });
    expect(live().notices.map(n => n.kind)).toEqual(["received"]);
  });

  it("desktop open goes through the shell and explains when a program was only revealed", async () => {
    vi.spyOn(dropApi, "snapshot").mockResolvedValue({ seq: 1, peers: [], transfers: [], incoming: 0 });
    vi.spyOn(dropDesktop, "canPickPaths").mockReturnValue(true);
    const resolve = vi.spyOn(dropApi, "openFile").mockResolvedValue({ ok: true, path: "C:/in/setup.exe", how: "reveal" });
    const shell = vi.spyOn(dropDesktop, "openDropPath").mockResolvedValue("reveal");
    const notify = vi.fn();
    vi.spyOn(dropApi, "capabilities").mockRejectedValue(new Error("offline"));
    let live: DropState;
    function Probe() { live = useDrop(); return <span>{live.status}</span>; }
    render(<DropProvider mode="live" notify={notify}><Probe /></DropProvider>);
    await screen.findByText("ready");
    await act(async () => { await live!.openTransfer(transfer({ direction: "in", status: "completed" }), "open"); });
    expect(resolve).toHaveBeenCalledWith("t1", "open", true);
    expect(shell).toHaveBeenCalledWith("C:/in/setup.exe", true);
    expect(notify).toHaveBeenCalledWith("info", expect.stringMatching(/aren't run directly/));
  });

  it("a received card opens the file and closes itself", async () => {
    const done = transfer({ direction: "in", status: "completed", actions: ["open", "reveal"], name: "report.pdf" });
    state.data.transfers = { t1: done };
    state.notices = [{ id: "received:t1", kind: "received", peerId: "NAS", transferIds: ["t1"], at: Date.now() }];
    renderWithStore(<DropNotices />);
    expect(screen.getByText("report.pdf from NAS")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open file" }));
    await waitFor(() => expect(state.openTransfer).toHaveBeenCalledWith(done, "open"));
    expect(state.dismissNotice).toHaveBeenCalledWith("received:t1");
  });
});
