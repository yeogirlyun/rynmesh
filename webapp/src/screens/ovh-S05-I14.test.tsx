import { createRef, useRef, useState, type ComponentProps } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import { AGENT_ATTACHMENT_LIMITS, type AgentDraftAttachment } from "../domain/agentAttachments";
import type { AgentProject } from "../domain/rynAgent";
import { agentTaskCache, resetAgentWorkspaceMemory } from "../domain/agentWorkspaceCache";
import RynAgent from "./RynAgent";
import RynAgentComposer from "./RynAgentComposer";
import "./rynAgentText";

const tauriMocks = vi.hoisted(() => ({
  invoke: vi.fn(),
  dropHandler: undefined as ((event: { payload: { type: string; paths?: string[]; position?: { x: number; y: number } } }) => void) | undefined,
}));

vi.mock("@tauri-apps/api/core", () => ({
  invoke: (...args: unknown[]) => tauriMocks.invoke(...args),
}));

vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({
    onDragDropEvent: vi.fn(async (cb: typeof tauriMocks.dropHandler) => {
      tauriMocks.dropHandler = cb;
      return () => {
        if (tauriMocks.dropHandler === cb) tauriMocks.dropHandler = undefined;
      };
    }),
  }),
}));

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({
    demo: false,
    appearance: "light",
    setAppearance: vi.fn(),
    devices: [],
    services: [],
    space: null,
  }),
}));

type ComposerProps = ComponentProps<typeof RynAgentComposer>;
const project: AgentProject = {
  id: "proj-1",
  name: "workspace",
  path: "/workspace",
  generation: 1,
  entry_files: [],
  manifests: [],
  directories: [],
};

function makeComposerProps(overrides: Partial<ComposerProps> = {}): ComposerProps {
  return {
    project,
    value: "",
    inputRef: createRef<HTMLTextAreaElement>(),
    inputDisabled: false,
    busy: false,
    onChange: vi.fn(),
    onSend: vi.fn(),
    onProject: vi.fn(),
    onSettings: vi.fn(),
    settingsDisabled: false,
    permissionMode: "workspace",
    permissionLocked: false,
    onPermission: vi.fn(),
    modelPicker: <button type="button">Select model</button>,
    options: <button type="button">Choose skills</button>,
    sendButton: <button type="button">Send task</button>,
    ...overrides,
  };
}

function StatefulComposer({ initial = "", onAttachmentsChange, onChange }: {
  initial?: string;
  onAttachmentsChange?: (files: AgentDraftAttachment[]) => void;
  onChange?: (val: string) => void;
}) {
  const [value, setValue] = useState(initial);
  const [attachments, setAttachments] = useState<AgentDraftAttachment[]>([]);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  return (
    <RynAgentComposer
      {...makeComposerProps()}
      value={value}
      inputRef={inputRef}
      attachments={attachments}
      onChange={next => {
        setValue(next);
        onChange?.(next);
      }}
      onAttachmentsChange={next => {
        setAttachments(next);
        onAttachmentsChange?.(next);
      }}
    />
  );
}

beforeEach(() => {
  sessionStorage.clear();
  localStorage.clear();
  agentTaskCache().clear();
  tauriMocks.invoke.mockReset();
  tauriMocks.dropHandler = undefined;
  vi.stubGlobal("SpeechRecognition", undefined);
  vi.stubGlobal("webkitSpeechRecognition", undefined);
  Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
    configurable: true,
    value() { this.setAttribute("open", ""); },
  });
  Object.defineProperty(HTMLDialogElement.prototype, "close", {
    configurable: true,
    value() { this.removeAttribute("open"); },
  });
});

afterEach(async () => {
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  await act(async () => {
    await i18n.changeLanguage("en");
  });
});

describe("S05-I14 attachment pre-validation, path picker, dynamic limits, and folder drop", () => {
  it("T3: immediately reports offending filename and limit on native drag-and-drop exceeding total_bytes", async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    await act(async () => {
      await i18n.changeLanguage("zh-CN");
    });

    const size8MB = 8 * 1024 * 1024;
    tauriMocks.invoke.mockImplementation(async (cmd: string, args?: { paths?: string[] }) => {
      if (cmd === "inspect_drop_paths") {
        return (args?.paths || []).map(p => ({ path: p, size: size8MB, kind: "file" }));
      }
      return [];
    });

    const onAttachmentsChange = vi.fn();
    const view = render(<RynAgentComposer {...makeComposerProps({ onAttachmentsChange })} />);
    const shell = view.container.querySelector(".ra-composer-shell") as HTMLElement;
    vi.spyOn(shell, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, right: 600, top: 0, bottom: 400, width: 600, height: 400, toJSON: () => ({}),
    });

    await waitFor(() => expect(tauriMocks.dropHandler).toBeDefined());
    await act(async () => {
      tauriMocks.dropHandler?.({
        payload: {
          type: "drop",
          position: { x: 100, y: 100 },
          paths: ["/workspace/part1.csv", "/workspace/part2.csv", "/workspace/overflow-part3.csv"],
        },
      });
    });

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("overflow-part3.csv");
    expect(alert.textContent).toContain("20");
    expect(onAttachmentsChange).not.toHaveBeenCalled();
    view.unmount();

    // Also verify RynAgent banner renders offending filename and limit when backend rejects with details.
    server.use(
      http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
      http.get("*/api/local/agent/settings", () =>
        HttpResponse.json({
          enabled: true,
          model_id: "verified-model",
          permission_mode: "automatic",
          allowed_write_tools: [],
          max_steps: 16,
          max_seconds: 180,
          max_context_chars: 60000,
          max_output_chars: 20000,
        }),
      ),
      http.get("*/api/local/agent/models", () =>
        HttpResponse.json({
          models: [{ id: "verified-model", name: "Verified", source: "local_api", available: true, agent_supported: true }],
        }),
      ),
      http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
      http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
      http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [] })),
      http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
      http.get("*/api/local/agent/extensions", () => HttpResponse.json({ extensions: [], mcp_servers: [], skills: [] })),
      http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ restricted: false, sources: [] })),
      http.post("*/api/local/agent/tasks", () =>
        HttpResponse.json(
          { detail: "agent_attachment_limit", details: { limit: "file_bytes", name: "paste-too-big.bin", max: 10 * 1024 * 1024 } },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    render(
      <MemoryRouter initialEntries={["/agent"]}>
        <RynAgent />
      </MemoryRouter>,
    );
    const textbox = await screen.findByRole("textbox");
    await user.type(textbox, "发送检查");
    await user.click(screen.getByRole("button", { name: "发送任务" }));
    const banner = await screen.findByRole("alert");
    expect(banner.textContent).toContain("paste-too-big.bin");
    expect(banner.textContent).toContain("10 MB");
  });

  it("T4: desktop file picker selects a 30 MiB file as a path attachment without reading base64", async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    const fileReaderSpy = vi.fn();
    vi.stubGlobal("FileReader", class {
      readAsDataURL() {
        fileReaderSpy();
      }
    });

    const size30MB = 30 * 1024 * 1024;
    tauriMocks.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "choose_drop_paths") return ["/workspace/huge-trace-30mb.log"];
      if (cmd === "inspect_drop_paths") {
        return [{ path: "/workspace/huge-trace-30mb.log", size: size30MB, kind: "file" }];
      }
      return [];
    });

    const onAttachmentsChange = vi.fn();
    const user = userEvent.setup();
    render(<StatefulComposer onAttachmentsChange={onAttachmentsChange} />);

    await user.click(screen.getByLabelText("Options", { selector: "summary" }));
    await user.click(screen.getByRole("button", { name: "Attach images or files" }));

    await waitFor(() => expect(onAttachmentsChange).toHaveBeenCalledTimes(1));
    const attached = onAttachmentsChange.mock.calls[0][0] as AgentDraftAttachment[];
    expect(attached).toHaveLength(1);
    expect(attached[0]).toMatchObject({
      name: "huge-trace-30mb.log",
      path: "/workspace/huge-trace-30mb.log",
      size: size30MB,
    });
    expect(attached[0].data_base64).toBeUndefined();
    expect(fileReaderSpy).not.toHaveBeenCalled();
    expect(tauriMocks.invoke).toHaveBeenCalledWith("choose_drop_paths", { kind: "files" });
  });

  it("T5: reads attachment_limits from /api/local/agent/settings and falls back to defaults on failure", async () => {
    await act(async () => {
      await i18n.changeLanguage("zh-CN");
    });

    server.use(
      http.get("*/api/local/agent/projects", () => HttpResponse.json({ projects: [] })),
      http.get("*/api/local/agent/settings", () =>
        HttpResponse.json({
          enabled: true,
          model_id: "verified-model",
          permission_mode: "automatic",
          allowed_write_tools: [],
          max_steps: 16,
          max_seconds: 180,
          max_context_chars: 60000,
          max_output_chars: 20000,
          attachment_limits: {
            count: 2,
            images: 1,
            file_bytes: 5 * 1024 * 1024,
            total_bytes: 9 * 1024 * 1024,
          },
        }),
      ),
      http.get("*/api/local/agent/models", () =>
        HttpResponse.json({
          models: [{ id: "verified-model", name: "Verified", source: "local_api", available: true, agent_supported: true }],
        }),
      ),
      http.get("*/api/local/agent/resources", () => HttpResponse.json({ connections: [], supported_formats: [] })),
      http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [] })),
      http.get("*/api/local/agent/tasks", () => HttpResponse.json({ tasks: [] })),
      http.get("*/api/local/agent/automations", () => HttpResponse.json({ automations: [], runs: [] })),
      http.get("*/api/local/agent/extensions", () => HttpResponse.json({ extensions: [], mcp_servers: [], skills: [] })),
      http.get("*/api/local/agent/extensions/model-availability", () => HttpResponse.json({ restricted: false, sources: [] })),
    );

    const view = render(
      <MemoryRouter initialEntries={["/agent"]}>
        <RynAgent />
      </MemoryRouter>,
    );

    await waitFor(() => expect(AGENT_ATTACHMENT_LIMITS.count).toBe(2));
    expect(AGENT_ATTACHMENT_LIMITS.images).toBe(1);
    expect(AGENT_ATTACHMENT_LIMITS.fileBytes).toBe(5 * 1024 * 1024);
    expect(AGENT_ATTACHMENT_LIMITS.totalBytes).toBe(9 * 1024 * 1024);

    // Parameterized zh copy mentions large files can be dragged or picked via dialog as path references.
    expect(
      i18n.t("attachmentLimit", { ns: "rynAgent", count: 2, images: 1, fileMB: 5, totalMB: 9 }),
    ).toContain("大文件可直接拖入或用文件对话框选择，按路径引用");

    view.unmount();
    localStorage.clear();
    resetAgentWorkspaceMemory();

    // When settings endpoint fails and there is no cached copy, defaults (8 / 4 / 10 MiB / 20 MiB) apply.
    server.use(
      http.get("*/api/local/agent/settings", () => new HttpResponse(null, { status: 500 })),
    );
    const fallbackView = render(
      <MemoryRouter initialEntries={["/agent"]}>
        <RynAgent />
      </MemoryRouter>,
    );
    await waitFor(() => expect(AGENT_ATTACHMENT_LIMITS.count).toBe(8));
    expect(AGENT_ATTACHMENT_LIMITS.fileBytes).toBe(10 * 1024 * 1024);
    fallbackView.unmount();
  });

  it("T6: dragging a folder appends its path text to the composer input instead of adding an attachment", async () => {
    (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__ = {};
    await act(async () => {
      await i18n.changeLanguage("zh-CN");
    });

    tauriMocks.invoke.mockImplementation(async (cmd: string) => {
      if (cmd === "inspect_drop_paths") {
        return [{ path: "/workspace/quarterly-folder", size: 0, kind: "dir" }];
      }
      return [];
    });

    const onAttachmentsChange = vi.fn();
    const view = render(<StatefulComposer initial="请检查" onAttachmentsChange={onAttachmentsChange} />);
    const shell = view.container.querySelector(".ra-composer-shell") as HTMLElement;
    vi.spyOn(shell, "getBoundingClientRect").mockReturnValue({
      x: 0, y: 0, left: 0, right: 600, top: 0, bottom: 400, width: 600, height: 400, toJSON: () => ({}),
    });

    await waitFor(() => expect(tauriMocks.dropHandler).toBeDefined());
    await act(async () => {
      tauriMocks.dropHandler?.({
        payload: {
          type: "drop",
          position: { x: 120, y: 120 },
          paths: ["/workspace/quarterly-folder"],
        },
      });
    });

    expect(
      await screen.findByText("文件夹不能作为附件，已作为路径写入消息；需要读取其中文件请用自动处理档或额外工作目录"),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox")).toHaveValue("请检查 /workspace/quarterly-folder");
    expect(onAttachmentsChange).not.toHaveBeenCalled();
  });
});
