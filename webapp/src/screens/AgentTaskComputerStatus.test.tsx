import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n";
import {
  AgentTaskComputerStatus,
  buildTaskComputerStatusRefreshKey,
  type AgentTaskComputerStatusSettingsLike,
  type AgentTaskComputerStatusTaskLike,
} from "./AgentTaskComputerStatus";

function makeTaskStatusDto(
  taskId: string,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    schema_version: 1,
    snapshot_id: `snap-${taskId}`,
    generated_at: "2026-10-19T09:00:00Z",
    context: {
      task_id: taskId,
      task_revision: 3,
      settings_revision: 4,
      selection_source: "task",
      model_id: "codex-default",
      selection_epoch: `epoch-${taskId}-3`,
    },
    plugin: {
      installed: true,
      enabled: true,
      revision: 2,
      evidence: { source: "extension_store", observed_at: "2026-10-19T09:00:00Z" },
      reason_code: null,
    },
    native: {
      platform: "windows",
      executable_presence: "present",
      status: "unverified",
      version: null,
      protocol_version: null,
      evidence: { source: "none", observed_at: null },
      reason_code: "native_not_verified",
    },
    permissions: {
      agent_enabled: true,
      global_desktop_access: true,
      task_scope: "present",
      task_local_execution: true,
      task_desktop_access: true,
      global_permission_mode: "workspace",
      task_permission_mode: "workspace",
      evidence: { source: "agent_store", observed_at: "2026-10-19T09:00:00Z" },
    },
    session: {
      state: "none",
      generation: 1,
      evidence: { source: "live_session", observed_at: "2026-10-19T09:00:00Z" },
      reason_code: null,
    },
    model: {
      id: "codex-default",
      display_name: "Codex CLI",
      adapter: "codex_cli",
      availability: "available",
      image_input: {
        status: "carrier_available",
        carrier: "codex_turn_input",
        evidence: { source: "model_route_contract", observed_at: "2026-10-19T09:00:00Z" },
        reason_code: "image_verification_pending",
      },
    },
    latest_observation: null,
    ...overrides,
  };
}

describe("AgentTaskComputerStatus", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("en");
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders nothing and makes no GET request when task or task.id is absent (never falls back to settings status)", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const settings: AgentTaskComputerStatusSettingsLike = {
      enabled: true,
      desktop_access: true,
      permission_mode: "workspace",
    };

    const { rerender } = render(
      <AgentTaskComputerStatus task={null} settings={settings} />,
    );
    expect(screen.queryByTestId("agent-task-computer-status")).toBeNull();
    expect(screen.queryByTestId("agent-computer-status-panel")).toBeNull();

    rerender(
      <AgentTaskComputerStatus task={{ id: "   " }} settings={settings} />,
    );
    expect(screen.queryByTestId("agent-task-computer-status")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(buildTaskComputerStatusRefreshKey(null, settings)).toBe("");
    expect(buildTaskComputerStatusRefreshKey({ id: "  " }, settings)).toBe("");

    // JSON tuple encoding avoids delimiter collisions
    expect(
      buildTaskComputerStatusRefreshKey(
        { id: "task-1", model_id: "a|b" },
        settings,
      ),
    ).not.toBe(
      buildTaskComputerStatusRefreshKey(
        {
          id: "task-1",
          model_id: "a",
          model_destination: { catalog_model_id: "b" },
        },
        settings,
      ),
    );
  });

  it("renders collapsed by default with localized summary and makes zero GET requests until expanded", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    const task: AgentTaskComputerStatusTaskLike = {
      id: "task-lazy-1",
      model_id: "codex-default",
      control_revision: 1,
      scope_revision: 1,
      permission_mode: "workspace",
      local_execution: { workspace_root: "D:\\work", desktop_access: true },
    };
    const settings: AgentTaskComputerStatusSettingsLike = {
      enabled: true,
      desktop_access: true,
      permission_mode: "workspace",
    };

    const { rerender } = render(
      <AgentTaskComputerStatus task={task} settings={settings} />,
    );

    const details = screen.getByTestId(
      "agent-task-computer-status",
    ) as HTMLDetailsElement;
    expect(details.open).toBe(false);
    expect(screen.getByText("Computer control status")).toBeInTheDocument();
    expect(screen.queryByTestId("agent-computer-status-panel")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();

    // Re-rendering with updated revisions while still collapsed must not trigger GET
    rerender(
      <AgentTaskComputerStatus
        task={{ ...task, control_revision: 2, model_id: "gpt-4o" }}
        settings={{ ...settings, desktop_access: false }}
      />,
    );
    expect(fetchMock).not.toHaveBeenCalled();

    await i18n.changeLanguage("zh-CN");
    rerender(
      <AgentTaskComputerStatus
        task={{ ...task, control_revision: 2, model_id: "gpt-4o" }}
        settings={{ ...settings, desktop_access: false }}
      />,
    );
    expect(screen.getByText("电脑控制状态")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("mounts panel when expanded, issues GET with exact task_id, and keeps 404 as unknown without fixture fallback", async () => {
    const user = userEvent.setup();
    const calls: Array<{ url: string; method: string; credentials?: string }> = [];
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        url: String(input),
        method: init?.method ?? "GET",
        credentials: init?.credentials,
      });
      return new Response(JSON.stringify({ error: "not_found" }), {
        status: 404,
        headers: { "content-type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AgentTaskComputerStatus
        task={{ id: "task-404-check", model_id: "codex-default", control_revision: 4 }}
        settings={{ enabled: true, desktop_access: false, permission_mode: "workspace" }}
      />,
    );

    await user.click(screen.getByText("Computer control status"));

    await waitFor(() => {
      expect(calls).toHaveLength(1);
    });
    expect(calls[0].method).toBe("GET");
    expect(calls[0].credentials).toBe("include");
    expect(calls[0].url).toContain(
      "/api/local/agent/computer/status?task_id=task-404-check",
    );

    const notice = await screen.findByTestId("computer-status-unavailable-notice");
    expect(notice).toHaveTextContent(
      /Computer status is unavailable; readiness cannot be confirmed/i,
    );

    const pluginRow = screen.getByTestId("computer-status-row-plugin");
    const nativeRow = screen.getByTestId("computer-status-row-native");
    expect(within(pluginRow).getByText("Unknown")).toBeInTheDocument();
    expect(within(nativeRow).getAllByText("Unknown").length).toBeGreaterThan(0);
    expect(within(nativeRow).queryByText("Ready")).not.toBeInTheDocument();
  });

  it("aborts in-flight GET request and unmounts panel when collapsed", async () => {
    const user = userEvent.setup();
    const signals: AbortSignal[] = [];
    let resolvePending!: (response: Response) => void;

    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      if (init?.signal) {
        signals.push(init.signal);
      }
      return new Promise<Response>((resolve) => {
        resolvePending = resolve;
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AgentTaskComputerStatus
        task={{ id: "task-collapse-abort", model_id: "codex-default" }}
        settings={{ enabled: true, desktop_access: true }}
      />,
    );

    // Expand <details>
    await user.click(screen.getByText("Computer control status"));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId("agent-computer-status-panel")).toBeInTheDocument();
    expect(signals[0]?.aborted).toBe(false);

    // Collapse <details> while request is still in flight
    await user.click(screen.getByText("Computer control status"));

    await waitFor(() => {
      expect(screen.queryByTestId("agent-computer-status-panel")).toBeNull();
    });
    expect(signals[0]?.aborted).toBe(true);

    // Late resolution after collapse must not remount or throw
    resolvePending(
      new Response(JSON.stringify(makeTaskStatusDto("task-collapse-abort")), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    expect(screen.queryByTestId("agent-computer-status-panel")).toBeNull();
  });

  it("clears task A and prevents late task A response from ever appearing after switching to task B", async () => {
    const user = userEvent.setup();
    const signals: AbortSignal[] = [];
    const resolvers = new Map<string, (response: Response) => void>();

    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.signal) {
        signals.push(init.signal);
      }
      const match = /[?&]task_id=([^&]+)/.exec(url);
      const taskId = match ? decodeURIComponent(match[1]) : "none";
      return new Promise<Response>((resolve) => {
        resolvers.set(taskId, resolve);
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    const { rerender } = render(
      <AgentTaskComputerStatus
        task={{ id: "task-a", model_id: "model-a", control_revision: 1 }}
        settings={{ enabled: true, desktop_access: true }}
      />,
    );

    await user.click(screen.getByText("Computer control status"));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(String(fetchMock.mock.calls[0][0])).toContain("task_id=task-a");

    // Switch active task to task-b before task-a responds
    rerender(
      <AgentTaskComputerStatus
        task={{ id: "task-b", model_id: "model-b", control_revision: 1 }}
        settings={{ enabled: true, desktop_access: true }}
      />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    expect(signals[0]?.aborted).toBe(true);
    expect(String(fetchMock.mock.calls[1][0])).toContain("task_id=task-b");

    // Resolve task-b first with valid v1 schema and task-b observation
    resolvers.get("task-b")?.(
      new Response(
        JSON.stringify(
          makeTaskStatusDto("task-b", {
            context: {
              task_id: "task-b",
              task_revision: 3,
              settings_revision: 4,
              selection_source: "task",
              model_id: "model-b",
              selection_epoch: "epoch-task-b-3",
            },
            model: {
              id: "model-b",
              display_name: "Task B Model",
              adapter: "openai_responses",
              availability: "available",
              image_input: {
                status: "verified",
                carrier: "api_image_blocks",
                evidence: {
                  source: "model_image_probe",
                  observed_at: "2026-10-19T09:01:00Z",
                },
                reason_code: null,
              },
            },
            latest_observation: {
              id: "obs-from-task-b",
              task_revision: 3,
              selection_epoch: "epoch-task-b-3",
              model_id: "model-b",
              matches_current_selection: true,
              width: 1280,
              height: 720,
              stages: {
                saved: {
                  state: "recorded",
                  evidence: {
                    source: "runtime_receipt",
                    observed_at: "2026-10-19T09:01:00Z",
                  },
                },
                displayed: {
                  state: "recorded",
                  evidence: {
                    source: "client_render_receipt",
                    observed_at: "2026-10-19T09:01:01Z",
                  },
                },
                submitted: {
                  state: "not_recorded",
                  evidence: { source: "none", observed_at: null },
                },
                semantic_observed: {
                  state: "not_recorded",
                  evidence: { source: "none", observed_at: null },
                },
              },
            },
          }),
        ),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    const modelRow = screen.getByTestId("computer-status-row-model");
    expect(await within(modelRow).findByText("Task B Model")).toBeInTheDocument();
    expect(screen.getByText("obs-from-task-b")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-saved")).getByText("Recorded"),
    ).toBeInTheDocument();

    // Now resolve late task-a response; it must never overwrite task-b
    resolvers.get("task-a")?.(
      new Response(
        JSON.stringify(
          makeTaskStatusDto("task-a", {
            context: {
              task_id: "task-a",
              task_revision: 3,
              settings_revision: 4,
              selection_source: "task",
              model_id: "model-a",
              selection_epoch: "epoch-task-a-3",
            },
            model: {
              id: "model-a",
              display_name: "Late Task A Model",
              adapter: "codex_cli",
              availability: "available",
              image_input: {
                status: "carrier_available",
                carrier: "codex_turn_input",
                evidence: {
                  source: "model_route_contract",
                  observed_at: "2026-10-19T09:00:30Z",
                },
                reason_code: "image_verification_pending",
              },
            },
            latest_observation: {
              id: "obs-late-from-task-a",
              task_revision: 3,
              selection_epoch: "epoch-task-a-3",
              model_id: "model-a",
              matches_current_selection: true,
              width: 800,
              height: 600,
              stages: {
                saved: {
                  state: "recorded",
                  evidence: {
                    source: "runtime_receipt",
                    observed_at: "2026-10-19T09:00:30Z",
                  },
                },
                displayed: {
                  state: "not_recorded",
                  evidence: { source: "none", observed_at: null },
                },
                submitted: {
                  state: "not_recorded",
                  evidence: { source: "none", observed_at: null },
                },
                semantic_observed: {
                  state: "not_recorded",
                  evidence: { source: "none", observed_at: null },
                },
              },
            },
          }),
        ),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );

    expect(screen.queryByText("Late Task A Model")).toBeNull();
    expect(screen.queryByText("obs-late-from-task-a")).toBeNull();
    expect(within(modelRow).getByText("Task B Model")).toBeInTheDocument();
    expect(screen.getByText("obs-from-task-b")).toBeInTheDocument();
  });

  it("ignores unrelated stream/poll updates and refetches only when relevant task/permission revisions change", async () => {
    const user = userEvent.setup();
    let currentModelId = "codex-default";
    let currentDesktopAccess = false;
    let currentTaskRevision = 10;

    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify(
          makeTaskStatusDto("task-stream-1", {
            context: {
              task_id: "task-stream-1",
              task_revision: currentTaskRevision,
              settings_revision: 4,
              selection_source: "task",
              model_id: currentModelId,
              selection_epoch: `epoch-${currentModelId}`,
            },
            permissions: {
              agent_enabled: true,
              global_desktop_access: currentDesktopAccess,
              task_scope: "present",
              task_local_execution: true,
              task_desktop_access: false,
              global_permission_mode: "workspace",
              task_permission_mode: "workspace",
              evidence: { source: "agent_store", observed_at: "2026-10-19T09:00:00Z" },
            },
            model: {
              id: currentModelId,
              display_name: `Model ${currentModelId}`,
              adapter: "codex_cli",
              availability: "available",
              image_input: {
                status: "carrier_available",
                carrier: "codex_turn_input",
                evidence: {
                  source: "model_route_contract",
                  observed_at: "2026-10-19T09:00:00Z",
                },
                reason_code: "image_verification_pending",
              },
            },
          }),
        ),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    });
    vi.stubGlobal("fetch", fetchMock);

    const baseTask = {
      id: "task-stream-1",
      model_id: "codex-default",
      model_destination: {
        catalog_model_id: "codex-default",
        provider_peer_id: null,
        source_id: "local-cli",
        destination_revision: "dest-rev-1",
      },
      control_revision: 2,
      scope_revision: 1,
      goal: { revision: 1 },
      permission_mode: "workspace",
      local_execution: { workspace_root: "D:\\code\\rynmesh", desktop_access: false },
      file_artifact_revision: 0,
      // Unrelated volatile fields from RynAgent polling/streaming:
      revision: 10,
      steps: 1,
      updated_at: "2026-10-19T09:00:00Z",
      event_cursor: 5,
      streaming: { seq: 5, turn_started_seq: 1, text: "H", updated_at: "2026-10-19T09:00:00Z" },
    };

    const baseSettings: AgentTaskComputerStatusSettingsLike = {
      enabled: true,
      desktop_access: false,
      permission_mode: "workspace",
    };

    const { rerender } = render(
      <AgentTaskComputerStatus task={baseTask} settings={baseSettings} />,
    );

    await user.click(screen.getByText("Computer control status"));
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(1);
    });
    expect(await screen.findByText("Model codex-default")).toBeInTheDocument();

    // Simulate multiple token stream and task poll updates that do NOT affect computer status context
    const streamUpdate1 = {
      ...baseTask,
      revision: 11,
      steps: 2,
      updated_at: "2026-10-19T09:00:01Z",
      event_cursor: 12,
      streaming: {
        seq: 12,
        turn_started_seq: 1,
        text: "Hello streaming token...",
        updated_at: "2026-10-19T09:00:01Z",
      },
    };
    rerender(
      <AgentTaskComputerStatus
        task={streamUpdate1}
        settings={{ ...baseSettings }}
      />,
    );

    const streamUpdate2 = {
      ...baseTask,
      revision: 12,
      steps: 3,
      updated_at: "2026-10-19T09:00:02Z",
      event_cursor: 19,
      streaming: null,
    };
    rerender(
      <AgentTaskComputerStatus
        task={streamUpdate2}
        settings={{ ...baseSettings }}
      />,
    );

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.getByText("Model codex-default")).toBeInTheDocument();

    // Relevant change 1: task model_id / destination_revision changes
    currentModelId = "gpt-4.1";
    currentTaskRevision = 12;
    const modelUpdatedTask = {
      ...baseTask,
      revision: 12,
      model_id: "gpt-4.1",
      model_destination: {
        ...baseTask.model_destination,
        catalog_model_id: "gpt-4.1",
        destination_revision: "dest-rev-2",
      },
      control_revision: 3,
    };
    rerender(
      <AgentTaskComputerStatus
        task={modelUpdatedTask}
        settings={baseSettings}
      />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(2);
    });
    expect(await screen.findByText("Model gpt-4.1")).toBeInTheDocument();

    // Relevant change 2: desktop_access permission changes in settings
    currentDesktopAccess = true;
    rerender(
      <AgentTaskComputerStatus
        task={modelUpdatedTask}
        settings={{
          ...baseSettings,
          desktop_access: true,
        }}
      />,
    );

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledTimes(3);
    });
    const permRow = screen.getByTestId("computer-status-row-permissions");
    expect(
      await within(permRow).findByText("Global desktop: Granted"),
    ).toBeInTheDocument();
  });

  it("clears old verified/Granted state at render when same-task model or permission changes and rejects stale old-model or old-permission replies", async () => {
    const user = userEvent.setup();
    let calls = 0;
    let resolvePending!: (dto: Record<string, unknown>) => void;

    const makeSameTaskFixture = (modelId: string, enabled = true, taskDesktop = true) =>
      makeTaskStatusDto("same-task", {
        snapshot_id: `snap-${modelId}-${calls}`,
        context: {
          task_id: "same-task",
          task_revision: 3,
          settings_revision: 4,
          selection_source: "task",
          model_id: modelId,
          selection_epoch: `epoch-${modelId}`,
        },
        native: {
          platform: "windows",
          executable_presence: "present",
          status: "ready",
          version: "1.0.0",
          protocol_version: "1",
          evidence: { source: "native_probe_cache", observed_at: "2026-10-19T09:00:00Z" },
          reason_code: null,
        },
        permissions: {
          agent_enabled: enabled,
          global_desktop_access: enabled,
          task_scope: "present",
          task_local_execution: true,
          task_desktop_access: taskDesktop,
          global_permission_mode: "workspace",
          task_permission_mode: "workspace",
          evidence: { source: "agent_store", observed_at: "2026-10-19T09:00:00Z" },
        },
        model: {
          id: modelId,
          display_name: `Fixture ${modelId}`,
          adapter: "codex_cli",
          availability: "available",
          image_input: {
            status: "verified",
            carrier: "codex_turn_input",
            evidence: { source: "model_image_probe", observed_at: "2026-10-19T09:00:00Z" },
            reason_code: null,
          },
        },
      });

    const fetchStatus = vi.fn(() => {
      calls += 1;
      if (calls === 1) {
        return Promise.resolve(makeSameTaskFixture("model-a", true, true) as never);
      }
      return new Promise<never>((resolve) => {
        resolvePending = resolve as unknown as (dto: Record<string, unknown>) => void;
      });
    });

    const { rerender } = render(
      <AgentTaskComputerStatus
        task={{
          id: "same-task",
          model_id: "model-a",
          control_revision: 1,
          scope_revision: 1,
          local_execution: { workspace_root: "D:\\work", desktop_access: true },
        }}
        settings={{ enabled: true, desktop_access: true, permission_mode: "workspace" }}
        fetchStatus={fetchStatus}
      />,
    );

    await user.click(screen.getByText("Computer control status"));

    expect(await screen.findByText("Fixture model-a")).toBeInTheDocument();
    expect(screen.getByText("Image input verified")).toBeInTheDocument();
    const permRow = screen.getByTestId("computer-status-row-permissions");
    expect(within(permRow).getByText("Agent: Granted")).toBeInTheDocument();
    expect(within(permRow).getByText("Global desktop: Granted")).toBeInTheDocument();
    expect(within(permRow).getByText("Task desktop: Granted")).toBeInTheDocument();

    // 1. Same task changes model-a -> model-b and revokes global permissions while second GET is pending
    rerender(
      <AgentTaskComputerStatus
        task={{
          id: "same-task",
          model_id: "model-b",
          control_revision: 2,
          scope_revision: 1,
          local_execution: { workspace_root: "D:\\work", desktop_access: true },
        }}
        settings={{ enabled: false, desktop_access: false, permission_mode: "workspace" }}
        fetchStatus={fetchStatus}
      />,
    );

    // Immediately while pending: old model-a, Image input verified, and Granted claims are cleared
    expect(calls).toBe(2);
    expect(screen.queryByText("Fixture model-a")).toBeNull();
    expect(screen.queryByText("Image input verified")).toBeNull();
    expect(within(permRow).queryByText(/Granted/)).toBeNull();
    expect(screen.getByText("Refreshing status…")).toBeInTheDocument();

    // Resolve the second GET with a well-formed but stale model-a / Granted snapshot: must be rejected
    resolvePending(makeSameTaskFixture("model-a", true, true));

    const notice = await screen.findByTestId("computer-status-unavailable-notice");
    expect(notice).toHaveTextContent(
      /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
    );
    expect(screen.queryByText("Fixture model-a")).toBeNull();
    expect(screen.queryByText("Image input verified")).toBeNull();
    expect(within(permRow).queryByText(/Granted/)).toBeNull();

    // 2. Same-model scope/permission update (model-b stays model-b, scope_revision increments):
    // First resolve a valid model-b state on scope_revision: 2
    rerender(
      <AgentTaskComputerStatus
        task={{
          id: "same-task",
          model_id: "model-b",
          control_revision: 2,
          scope_revision: 2,
          local_execution: { workspace_root: "D:\\work", desktop_access: true },
        }}
        settings={{ enabled: true, desktop_access: true, permission_mode: "workspace" }}
        fetchStatus={fetchStatus}
      />,
    );
    expect(calls).toBe(3);
    resolvePending(makeSameTaskFixture("model-b", true, true));

    expect(await screen.findByText("Fixture model-b")).toBeInTheDocument();
    expect(screen.getByText("Image input verified")).toBeInTheDocument();
    expect(within(permRow).getByText("Task desktop: Granted")).toBeInTheDocument();

    // Now change scope_revision with the SAME model-b and revoke task desktop_access
    rerender(
      <AgentTaskComputerStatus
        task={{
          id: "same-task",
          model_id: "model-b",
          control_revision: 3,
          scope_revision: 3,
          local_execution: { workspace_root: "D:\\work", desktop_access: false },
        }}
        settings={{ enabled: true, desktop_access: true, permission_mode: "workspace" }}
        fetchStatus={fetchStatus}
      />,
    );

    // Immediately while pending on same model-b: old green verified/Granted state is cleared
    expect(calls).toBe(4);
    expect(screen.queryByText("Fixture model-b")).toBeNull();
    expect(screen.queryByText("Image input verified")).toBeNull();
    expect(within(permRow).queryByText(/Granted/)).toBeNull();
    expect(screen.getByText("Refreshing status…")).toBeInTheDocument();

    // Resolving a stale response where task_desktop_access is still true is rejected
    resolvePending(makeSameTaskFixture("model-b", true, true));
    expect(
      await screen.findByTestId("computer-status-unavailable-notice"),
    ).toHaveTextContent(
      /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
    );
    expect(within(permRow).queryByText(/Granted/)).toBeNull();
  });

  it("captures caller task.revision lower bound on relevant destination/control/scope changes without refetching on volatile revisions, and rejects old or null task_revision replies on same-model updates", async () => {
    const user = userEvent.setup();
    const calls: Array<{ resolve: (dto: Record<string, unknown>) => void }> = [];

    const fetchStatus = vi.fn(() => {
      return new Promise<never>((resolve) => {
        calls.push({
          resolve: resolve as unknown as (dto: Record<string, unknown>) => void,
        });
      });
    });

    const makeSnapshot = (
      taskRevision: number | null,
      selectionEpoch: string,
    ): Record<string, unknown> =>
      makeTaskStatusDto("same-task", {
        snapshot_id: `snap-model-a-${String(taskRevision)}`,
        context: {
          task_id: "same-task",
          task_revision: taskRevision,
          settings_revision: 4,
          selection_source: "task",
          model_id: "model-a",
          selection_epoch: selectionEpoch,
        },
        native: {
          platform: "windows",
          executable_presence: "present",
          status: "ready",
          version: "1.0.0",
          protocol_version: "1",
          evidence: {
            source: "native_probe_cache",
            observed_at: "2026-10-19T09:00:00Z",
          },
          reason_code: null,
        },
        permissions: {
          agent_enabled: true,
          global_desktop_access: true,
          task_scope: "present",
          task_local_execution: true,
          task_desktop_access: true,
          global_permission_mode: "workspace",
          task_permission_mode: "workspace",
          evidence: { source: "agent_store", observed_at: "2026-10-19T09:00:00Z" },
        },
        model: {
          id: "model-a",
          display_name: "Fixture model-a",
          adapter: "codex_cli",
          availability: "available",
          image_input: {
            status: "verified",
            carrier: "codex_turn_input",
            evidence: {
              source: "model_image_probe",
              observed_at: "2026-10-19T09:00:00Z",
            },
            reason_code: null,
          },
        },
      });

    const oldTask: AgentTaskComputerStatusTaskLike = {
      id: "same-task",
      revision: 10,
      model_id: "model-a",
      control_revision: 1,
      scope_revision: 1,
      goal: { revision: 1 },
      model_destination: {
        catalog_model_id: "model-a",
        source_id: "fixture-source",
        destination_revision: "old-destination",
      },
      permission_mode: "workspace",
      local_execution: { desktop_access: true, workspace_root: "D:/own-fixture" },
    };
    const settings: AgentTaskComputerStatusSettingsLike = {
      enabled: true,
      desktop_access: true,
      permission_mode: "workspace",
    };

    const { rerender } = render(
      <AgentTaskComputerStatus
        task={oldTask}
        settings={settings}
        fetchStatus={fetchStatus}
      />,
    );

    await user.click(screen.getByText("Computer control status"));
    expect(calls).toHaveLength(1);

    calls[0].resolve(makeSnapshot(10, "old-selection-epoch"));
    expect(await screen.findByText("Image input verified")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();

    // Volatile task.revision poll alone (10 -> 15) must not trigger a new GET or invalidate current state
    rerender(
      <AgentTaskComputerStatus
        task={{ ...oldTask, revision: 15 }}
        settings={settings}
        fetchStatus={fetchStatus}
      />,
    );
    expect(calls).toHaveLength(1);
    expect(screen.getByText("Image input verified")).toBeInTheDocument();

    // Relevant same-model update: revision=20, control_revision=2, scope_revision=2, destination_revision="new-destination"
    const newTask: AgentTaskComputerStatusTaskLike = {
      ...oldTask,
      revision: 20,
      control_revision: 2,
      scope_revision: 2,
      model_destination: {
        ...oldTask.model_destination,
        destination_revision: "new-destination",
      },
    };
    rerender(
      <AgentTaskComputerStatus
        task={newTask}
        settings={settings}
        fetchStatus={fetchStatus}
      />,
    );

    // While pending: exactly 1 new GET fired, and old verified/Ready are immediately absent
    expect(calls).toHaveLength(2);
    expect(screen.queryByText("Image input verified")).toBeNull();
    expect(screen.queryByText("Ready")).toBeNull();

    // Resolving the NEW fetch with old context.task_revision=10 / old-selection-epoch must be rejected
    calls[1].resolve(makeSnapshot(10, "old-selection-epoch"));

    expect(
      await screen.findByTestId("computer-status-unavailable-notice"),
    ).toHaveTextContent(
      /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
    );
    expect(screen.queryByText("Image input verified")).toBeNull();
    expect(screen.queryByText("Ready")).toBeNull();
    const permRow = screen.getByTestId("computer-status-row-permissions");
    expect(within(permRow).queryByText(/Granted/)).toBeNull();

    // Manual refresh returning null task_revision must also be rejected because minTaskRevision=20 is known
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(calls).toHaveLength(3);
    calls[2].resolve(makeSnapshot(null, "null-revision-epoch"));

    expect(
      await screen.findByTestId("computer-status-unavailable-notice"),
    ).toHaveTextContent(
      /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
    );
    expect(screen.queryByText("Image input verified")).toBeNull();

    // Manual refresh returning fresh task_revision=20 / new-selection-epoch is accepted
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(calls).toHaveLength(4);
    calls[3].resolve(makeSnapshot(20, "new-selection-epoch"));

    expect(await screen.findByText("Image input verified")).toBeInTheDocument();
    expect(screen.getByText("Ready")).toBeInTheDocument();
    expect(within(permRow).getByText("Global desktop: Granted")).toBeInTheDocument();
  });
});
