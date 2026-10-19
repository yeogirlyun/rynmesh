import { createRef, useRef, useState, type ComponentProps } from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AgentProject } from "../domain/rynAgent";
import type { AgentDraftAttachment } from "../domain/agentAttachments";
import type { NativeDropEvent } from "../domain/dropDesktop";
import RynAgentComposer from "./RynAgentComposer";
import "./rynAgentText";

const desktopDrop = vi.hoisted(() => ({ handler: undefined as ((event: NativeDropEvent) => void) | undefined }));
vi.mock("../domain/dropDesktop", () => ({ listenNativeFileDrop: vi.fn(async (handler: (event: NativeDropEvent) => void) => {
  desktopDrop.handler = handler;
  return () => { if (desktopDrop.handler === handler) desktopDrop.handler = undefined; };
}) }));

type ComposerProps = ComponentProps<typeof RynAgentComposer>;
const project: AgentProject = { id: "rynmesh", name: "rynmesh", path: "D:\\code\\rynmesh", generation: 1, entry_files: [], manifests: [], directories: [] };

function props(overrides: Partial<ComposerProps> = {}): ComposerProps {
  return {
    project, value: "", inputRef: createRef<HTMLTextAreaElement>(), inputDisabled: false, busy: false,
    onChange: vi.fn(), onSend: vi.fn(), onProject: vi.fn(), onSettings: vi.fn(), settingsDisabled: false,
    permissionMode: "workspace", permissionLocked: false, onPermission: vi.fn(),
    modelPicker: <button type="button">Select model</button>, options: <button type="button">Choose skills</button>,
    sendButton: <button type="button">Send task</button>, ...overrides,
  };
}

function EditableComposer({ initial = "", ...overrides }: Partial<ComposerProps> & { initial?: string }) {
  const [value, setValue] = useState(initial);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  return <RynAgentComposer {...props(overrides)} value={value} inputRef={inputRef} onChange={next => { setValue(next); overrides.onChange?.(next); }} />;
}

function AttachmentComposer() {
  const [attachments, setAttachments] = useState<AgentDraftAttachment[]>([]);
  return <RynAgentComposer {...props()} attachments={attachments} onAttachmentsChange={setAttachments} />;
}

beforeEach(() => {
  vi.stubGlobal("SpeechRecognition", undefined);
  vi.stubGlobal("webkitSpeechRecognition", undefined);
});
afterEach(() => vi.unstubAllGlobals());

it("sends with Enter without inserting a newline", () => {
  const onSend = vi.fn();
  render(<RynAgentComposer {...props({ value: "Inspect this project", onSend })} />);
  expect(fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", code: "Enter", keyCode: 13 })).toBe(false);
  expect(onSend).toHaveBeenCalledOnce();
});

it("inserts a newline with Shift+Enter without sending", async () => {
  const onSend = vi.fn(), user = userEvent.setup();
  render(<EditableComposer initial="First line" onSend={onSend} />);
  const input = screen.getByRole("textbox");
  await user.click(input);
  await user.keyboard("{End}{Shift>}{Enter}{/Shift}Second line");
  expect(input).toHaveValue("First line\nSecond line");
  expect(onSend).not.toHaveBeenCalled();
});

it.each([
  { key: "Enter", code: "Enter", keyCode: 13, isComposing: true },
  { key: "Enter", code: "Enter", keyCode: 229 },
])("keeps IME confirmation out of the send path: %j", event => {
  const onSend = vi.fn();
  render(<RynAgentComposer {...props({ value: "Chinese input", onSend })} />);
  expect(fireEvent.keyDown(screen.getByRole("textbox"), event)).toBe(true);
  expect(onSend).not.toHaveBeenCalled();
});

it("keeps the next draft editable while an earlier request is busy", async () => {
  const onChange = vi.fn(), user = userEvent.setup();
  render(<EditableComposer busy initial="Next" onChange={onChange} />);
  const input = screen.getByRole("textbox");
  expect(input).toBeEnabled();
  await user.type(input, " request");
  expect(input).toHaveValue("Next request");
  expect(onChange).toHaveBeenLastCalledWith("Next request");
});

it("shows one project path in the context strip and opens its project and settings controls", async () => {
  const onProject = vi.fn(), onSettings = vi.fn(), user = userEvent.setup();
  const view = render(<RynAgentComposer {...props({ onProject, onSettings })} />);
  const context = view.container.querySelector(".ra-composer-context") as HTMLElement;
  const composer = view.container.querySelector(".ra-composer") as HTMLElement;
  expect(screen.getAllByText(project.path)).toHaveLength(1);
  expect(within(context).getByText(project.path)).toBeInTheDocument();
  expect(within(composer).queryByText(project.path)).not.toBeInTheDocument();
  expect(within(context).getByText("This computer")).toBeInTheDocument();
  await user.click(within(context).getByRole("button", { name: project.path }));
  await user.click(within(context).getByRole("button", { name: "Conversation settings" }));
  expect(onProject).toHaveBeenCalledOnce();
  expect(onSettings).toHaveBeenCalledOnce();
});

it("changes permissions through the toolbar menu and closes it after selection", async () => {
  const onPermission = vi.fn(), user = userEvent.setup();
  render(<RynAgentComposer {...props({ onPermission })} />);
  const trigger = screen.getByLabelText("Tool permissions", { selector: "summary" });
  await user.click(trigger);
  const menu = screen.getByRole("menu", { name: "Tool permissions" });
  expect(within(menu).getByRole("menuitemradio", { name: "Work autonomously" })).toHaveAttribute("aria-checked", "true");
  await user.click(within(menu).getByRole("menuitemradio", { name: "Read only" }));
  expect(onPermission).toHaveBeenCalledExactlyOnceWith("read_only");
  expect(trigger.closest("details")).not.toHaveAttribute("open");
});

it("keeps existing conversation permissions visible and locked", async () => {
  const onPermission = vi.fn(), user = userEvent.setup();
  render(<RynAgentComposer {...props({ permissionLocked: true, permissionMode: "confirm_changes", onPermission })} />);
  await user.click(screen.getByLabelText("Tool permissions", { selector: "summary" }));
  const menu = screen.getByRole("menu", { name: "Tool permissions" });
  expect(within(menu).getByText("This conversation keeps its original permissions.")).toBeInTheDocument();
  for (const item of within(menu).getAllByRole("menuitemradio")) expect(item).toBeDisabled();
  expect(within(menu).getByRole("menuitemradio", { name: "Confirm changes" })).toHaveAttribute("aria-checked", "true");
  await user.click(within(menu).getByRole("menuitemradio", { name: "Read only" }));
  expect(onPermission).not.toHaveBeenCalled();
});

it("opens the supplied options from the plus control and restores focus on Escape", async () => {
  const user = userEvent.setup();
  render(<RynAgentComposer {...props()} />);
  const trigger = screen.getByLabelText("Options", { selector: "summary" });
  expect(trigger.closest("details")).not.toHaveAttribute("open");
  await user.click(trigger);
  expect(trigger.closest("details")).toHaveAttribute("open");
  const option = screen.getByRole("button", { name: "Choose skills" });
  expect(option).toBeInTheDocument();
  option.focus();
  await user.keyboard("{Escape}");
  expect(trigger.closest("details")).not.toHaveAttribute("open");
  expect(trigger).toHaveFocus();
});

it("disables voice input when speech recognition is unavailable", () => {
  render(<RynAgentComposer {...props()} />);
  expect(screen.getByRole("button", { name: "Voice input" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Voice input" })).toHaveAttribute("title", "Voice input is not available on this device");
});

it("adds picked files through the plus menu and lets the owner remove them", async () => {
  const user = userEvent.setup();
  render(<AttachmentComposer />);
  await user.click(screen.getByLabelText("Options", { selector: "summary" }));
  await user.click(screen.getByRole("button", { name: "Attach images or files" }));
  const picker = screen.getByLabelText("Attach images or files", { selector: "input" });
  await user.upload(picker, [new File(["Quarter 2 sales"], "sales.txt", { type: "text/plain" }), new File(["notes"], "notes.md", { type: "text/markdown" })]);
  expect(await screen.findByText("sales.txt")).toBeInTheDocument();
  expect(screen.getByText("notes.md")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Remove sales.txt" }));
  expect(screen.queryByText("sales.txt")).not.toBeInTheDocument();
  expect(screen.getByText("notes.md")).toBeInTheDocument();
});

it("accepts dropped files while keeping the next draft editable during a run", async () => {
  const onAttachmentsChange = vi.fn();
  const view = render(<RynAgentComposer {...props({ busy: true, onAttachmentsChange })} />);
  const composer = view.container.querySelector(".ra-composer-shell") as HTMLElement;
  const file = new File(["drop content"], "dropped.txt", { type: "text/plain" });
  fireEvent.dragOver(composer, { dataTransfer: { types: ["Files"], files: [file] } });
  expect(screen.getByText("Drop files to attach")).toBeInTheDocument();
  fireEvent.drop(composer, { dataTransfer: { types: ["Files"], files: [file] } });
  await waitFor(() => expect(onAttachmentsChange).toHaveBeenCalledOnce());
  expect(onAttachmentsChange.mock.calls[0][0][0]).toMatchObject({ name: "dropped.txt", data_base64: btoa("drop content") });
  expect(screen.queryByText("Drop files to attach")).not.toBeInTheDocument();
});

it("pastes images as visible removable attachments", async () => {
  const view = render(<AttachmentComposer />);
  const file = new File([new Uint8Array([137, 80, 78, 71])], "clipboard.png", { type: "image/png" });
  fireEvent.paste(screen.getByRole("textbox"), { clipboardData: { files: [file] } });
  expect(await screen.findByText("clipboard.png")).toBeInTheDocument();
  expect(view.container.querySelector(".ra-composer-attachment img")).toHaveAttribute("src", "data:image/png;base64,iVBORw==");
  fireEvent.click(screen.getByRole("button", { name: "Remove clipboard.png" }));
  expect(screen.queryByText("clipboard.png")).not.toBeInTheDocument();
});

it("blocks excessive attachments before reading their contents", async () => {
  const onAttachmentsChange = vi.fn();
  const view = render(<RynAgentComposer {...props({ onAttachmentsChange })} />);
  const files = Array.from({ length: 9 }, (_, index) => new File(["text"], `${index}.txt`, { type: "text/plain" }));
  fireEvent.drop(view.container.querySelector(".ra-composer-shell") as HTMLElement, { dataTransfer: { types: ["Files"], files } });
  expect(await screen.findByRole("alert")).toHaveTextContent("Attach up to 8 files");
  expect(onAttachmentsChange).not.toHaveBeenCalled();
});

it("does not take dropped files when the composer is disabled", () => {
  const onAttachmentsChange = vi.fn();
  const view = render(<RynAgentComposer {...props({ inputDisabled: true, onAttachmentsChange })} />);
  fireEvent.drop(view.container.querySelector(".ra-composer-shell") as HTMLElement, { dataTransfer: { types: ["Files"], files: [new File(["text"], "private.txt")] } });
  expect(onAttachmentsChange).not.toHaveBeenCalled();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("accepts desktop native drops only inside the composer", async () => {
  const onAttachmentsChange = vi.fn();
  const view = render(<RynAgentComposer {...props({ onAttachmentsChange })} />);
  const composer = view.container.querySelector(".ra-composer-shell") as HTMLElement;
  vi.spyOn(composer, "getBoundingClientRect").mockReturnValue({ x: 10, y: 100, left: 10, right: 500, top: 100, bottom: 400, width: 490, height: 300, toJSON: () => ({}) });
  await waitFor(() => expect(desktopDrop.handler).toBeDefined());
  act(() => desktopDrop.handler?.({ type: "drop", x: 900, y: 200, paths: ["D:\\outside.txt"] }));
  expect(onAttachmentsChange).not.toHaveBeenCalled();
  act(() => desktopDrop.handler?.({ type: "over", x: 100, y: 200 }));
  expect(screen.getByText("Drop files to attach")).toBeInTheDocument();
  act(() => desktopDrop.handler?.({ type: "drop", x: 100, y: 200, paths: ["D:\\Quarter 2\\sales.xlsx"] }));
  expect(onAttachmentsChange).toHaveBeenCalledExactlyOnceWith([expect.objectContaining({ name: "sales.xlsx", path: "D:\\Quarter 2\\sales.xlsx" })]);
  expect(screen.queryByText("Drop files to attach")).not.toBeInTheDocument();
});

it("does not clear a new chat's attachment preparation when an old reader finishes", async () => {
  const readers: Array<{ result: string; onload: (() => void) | null }> = [];
  class DeferredReader {
    result = "data:text/plain;base64,dGV4dA==";
    onload: (() => void) | null = null;
    readAsDataURL() { readers.push(this); }
  }
  vi.stubGlobal("FileReader", DeferredReader);
  const onAttachmentsChange = vi.fn(), onAttachmentsBusyChange = vi.fn();
  const first = render(<RynAgentComposer {...props({ onAttachmentsChange, onAttachmentsBusyChange })} />);
  fireEvent.change(screen.getByLabelText("Attach images or files", { selector: "input" }), { target: { files: [new File(["text"], "first.txt")] } });
  first.unmount();
  render(<RynAgentComposer {...props({ onAttachmentsChange, onAttachmentsBusyChange })} />);
  fireEvent.change(screen.getByLabelText("Attach images or files", { selector: "input" }), { target: { files: [new File(["text"], "second.txt")] } });
  await act(async () => { readers[0].onload?.(); });
  expect(onAttachmentsBusyChange.mock.calls).toEqual([[true], [true]]);
  expect(onAttachmentsChange).not.toHaveBeenCalled();
  await act(async () => { readers[1].onload?.(); });
  expect(onAttachmentsBusyChange.mock.calls).toEqual([[true], [true], [false]]);
  expect(onAttachmentsChange.mock.calls[0][0][0].name).toBe("second.txt");
});

it("appends final voice results to the newest draft and stops recognition on unmount", async () => {
  type ResultEvent = { resultIndex: number; results: Array<{ isFinal: boolean; 0: { transcript: string } }> };
  let active: FakeRecognition | undefined;
  class FakeRecognition {
    lang = ""; continuous = false; interimResults = true;
    onresult: ((event: ResultEvent) => void) | null = null;
    onend: (() => void) | null = null;
    onerror: (() => void) | null = null;
    start = vi.fn(); stop = vi.fn();
    constructor() { active = this; }
  }
  vi.stubGlobal("SpeechRecognition", FakeRecognition);
  const user = userEvent.setup();
  const view = render(<EditableComposer initial="Original" />);
  await user.click(screen.getByRole("button", { name: "Voice input" }));
  expect(active?.start).toHaveBeenCalledOnce();
  expect(screen.getByRole("button", { name: "Stop voice input" })).toHaveAttribute("aria-pressed", "true");
  await user.type(screen.getByRole("textbox"), " edited");
  act(() => active?.onresult?.({ resultIndex: 1, results: [
    { isFinal: true, 0: { transcript: "already handled" } },
    { isFinal: false, 0: { transcript: "interim" } },
    { isFinal: true, 0: { transcript: "spoken words" } },
  ] }));
  expect(screen.getByRole("textbox")).toHaveValue("Original edited spoken words");
  view.unmount();
  expect(active?.stop).toHaveBeenCalledOnce();
  expect(active?.onresult).toBeNull();
});
