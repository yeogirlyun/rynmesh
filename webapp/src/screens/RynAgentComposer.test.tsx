import { createRef, useRef, useState, type ComponentProps } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { AgentProject } from "../domain/rynAgent";
import RynAgentComposer from "./RynAgentComposer";
import "./rynAgentText";

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
