import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import i18n from "../i18n";
import { AgentComputerStatusPanel } from "./AgentComputerStatusPanel";
import AgentExtensions from "./AgentExtensions";

function makeStatusFixture(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    snapshot_id: "snap-100",
    generated_at: "2026-10-19T08:00:00Z",
    context: {
      task_id: null,
      task_revision: null,
      settings_revision: 4,
      selection_source: "settings",
      model_id: "codex-default",
      selection_epoch: "epoch-settings-4",
    },
    plugin: {
      installed: true,
      enabled: true,
      revision: 2,
      evidence: { source: "extension_store", observed_at: "2026-10-19T08:00:00Z" },
      reason_code: null,
    },
    native: {
      platform: "windows",
      executable_presence: "present",
      status: "ready",
      version: "1.0.0",
      protocol_version: "1",
      evidence: { source: "native_probe_cache", observed_at: "2026-10-19T08:00:00Z" },
      reason_code: null,
    },
    permissions: {
      agent_enabled: true,
      global_desktop_access: true,
      task_scope: "not_requested",
      task_local_execution: null,
      task_desktop_access: null,
      global_permission_mode: "workspace",
      task_permission_mode: null,
      evidence: { source: "agent_store", observed_at: "2026-10-19T08:00:00Z" },
    },
    session: {
      state: "not_applicable",
      generation: null,
      evidence: { source: "none", observed_at: null },
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
        evidence: { source: "model_route_contract", observed_at: "2026-10-19T08:00:00Z" },
        reason_code: "image_verification_pending",
      },
    },
    latest_observation: null,
    ...overrides,
  };
}

function makeExtensionsOverviewFixture(overrides: Record<string, unknown> = {}) {
  return {
    revision: 5,
    updated_at: "2026-10-19T08:00:00Z",
    desktop_access: false,
    windows_supported: true,
    plugins: [
      {
        id: "computer-use",
        instance_id: "builtin-computer-use",
        name: "computer-use",
        title: "Computer Use",
        description: "Inspect and control Windows desktop applications and the isolated Ryn browser.",
        version: "1.0.0",
        kind: "builtin",
        source_format: "builtin",
        builtin: true,
        deletable: false,
        enabled: true,
        component_enabled: true,
        parent_enabled: true,
        status: "needs_permission",
        status_reason: "desktop_access_disabled",
        computer_access: true,
        skill_ids: ["desktop-basics"],
        mcp_server_ids: ["computer-use"],
        skills_count: 1,
        mcp_servers_count: 1,
        skills: [
          {
            id: "desktop-basics",
            instance_id: "skill-desktop-basics",
            name: "desktop-basics",
            title: "Desktop Basics",
            description: "Safe desktop inspection workflow.",
            summary: "Safe desktop inspection workflow.",
            version: "1.0.0",
            content: "# Desktop Basics",
            builtin: true,
            deletable: false,
            enabled: true,
            component_enabled: true,
            parent_enabled: true,
            source_format: "builtin",
            plugin_id: "computer-use",
            files: [{ path: "SKILL.md", size: 32 }],
            warnings: [],
            revision: 1,
          },
        ],
        mcp_servers: [
          {
            id: "computer-use",
            instance_id: "mcp-builtin-computer-use",
            name: "Computer Use",
            title: "Computer Use",
            description: "First-party Windows desktop control MCP server.",
            transport: "stdio",
            builtin: true,
            deletable: false,
            plugin_id: "computer-use",
            computer_access: true,
            enabled: true,
            component_enabled: true,
            parent_enabled: true,
            command: "python",
            args: ["-m", "rynmesh.agent_runtime.extensions.computer_use_server"],
            cwd: "",
            env_keys: [],
            masked_env: {},
            url: "",
            bearer_token_env: "",
            header_keys: [],
            masked_headers: {},
            has_secrets: false,
            startup_timeout_ms: 10000,
            tool_timeout_ms: 30000,
            enabled_tools: null,
            disabled_tools: [],
            read_only_tools: ["computer_observe_windows"],
            tools: [],
            tools_count: 1,
            enabled_tools_count: 1,
            status: "NeedsPermission",
            status_message: "Requires desktop_access in Agent settings.",
            last_tested_at: null,
            revision: 2,
          },
        ],
        unsupported_components: [],
        warnings: [],
        capabilities: {
          platform: "win32",
          windows_supported: true,
          desktop_access_enabled: false,
          observable: ["Visible top-level Windows desktop windows"],
          controllable: ["Window focus and click"],
          permission_note: "Requires both enabling Computer Use and enabling desktop_access.",
        },
        revision: 2,
      },
    ],
    skills: [],
    mcp_servers: [],
    ...overrides,
  };
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  document.documentElement.dataset.theme = "light";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("AgentComputerStatusPanel acceptance matrix", () => {
  it("1. clears current-ready on 404/503/invalid schema, shows unknown/unavailable, and never falls back to fake success", async () => {
    const user = userEvent.setup();
    let callNum = 0;

    const fetchMock = vi.fn(async () => {
      callNum += 1;
      if (callNum === 1) {
        return new Response(JSON.stringify(makeStatusFixture()), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (callNum === 2) {
        return new Response(JSON.stringify({ detail: "Not Found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (callNum === 3) {
        return new Response(JSON.stringify({ detail: "agent_computer_status_unavailable" }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response(JSON.stringify({ schema_version: 999 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AgentComputerStatusPanel />);

    const nativeRow = screen.getByTestId("computer-status-row-native");
    expect(await within(nativeRow).findByText("Ready")).toBeInTheDocument();

    // Refresh -> 404 (endpoint not implemented / absent) clears Ready and shows Unknown + unavailable notice
    const refreshBtn = screen.getByRole("button", { name: "Refresh status" });
    refreshBtn.focus();
    await user.click(refreshBtn);

    expect(
      await screen.findByText(/Computer status is unavailable; readiness cannot be confirmed/i),
    ).toBeInTheDocument();
    expect(within(nativeRow).queryByText("Ready")).not.toBeInTheDocument();
    expect(within(nativeRow).getAllByText("Unknown").length).toBeGreaterThan(0);
    expect(screen.getByText(/Last successful response \(stale\)/i)).toBeInTheDocument();
    // Focus remains on refresh button
    expect(refreshBtn).toHaveFocus();

    // Refresh -> 503 keeps current ready cleared
    await user.click(refreshBtn);
    expect(
      await screen.findByText(/Computer status is unavailable; readiness cannot be confirmed/i),
    ).toBeInTheDocument();
    expect(within(nativeRow).queryByText("Ready")).not.toBeInTheDocument();

    // Refresh -> invalid schema_version=999 rejects response and keeps current ready cleared
    await user.click(refreshBtn);
    expect(
      await screen.findByText(
        /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
      ),
    ).toBeInTheDocument();
    expect(within(nativeRow).queryByText("Ready")).not.toBeInTheDocument();
  });

  it("2 & 3. keeps plugin, native engine, and permissions separate and treats present executable without verified handshake as unverified", async () => {
    const user = userEvent.setup();
    let scenario = 1;

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        if (scenario === 1) {
          // Plugin enabled + native missing
          return new Response(
            JSON.stringify(
              makeStatusFixture({
                plugin: {
                  installed: true,
                  enabled: true,
                  revision: 1,
                  evidence: { source: "extension_store", observed_at: "2026-10-19T08:00:00Z" },
                  reason_code: null,
                },
                native: {
                  platform: "windows",
                  executable_presence: "missing",
                  status: "missing",
                  version: null,
                  protocol_version: null,
                  evidence: { source: "none", observed_at: null },
                  reason_code: "native_missing",
                },
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (scenario === 2) {
          // Native ready + plugin disabled + global granted + task denied
          return new Response(
            JSON.stringify(
              makeStatusFixture({
                context: {
                  task_id: "task-1",
                  task_revision: 3,
                  settings_revision: 4,
                  selection_source: "task",
                  model_id: "codex-default",
                  selection_epoch: "epoch-t1",
                },
                plugin: {
                  installed: true,
                  enabled: false,
                  revision: 2,
                  evidence: { source: "extension_store", observed_at: "2026-10-19T08:00:00Z" },
                  reason_code: "plugin_disabled",
                },
                native: {
                  platform: "windows",
                  executable_presence: "present",
                  status: "ready",
                  version: "1.2.0",
                  protocol_version: "1",
                  evidence: { source: "native_probe_cache", observed_at: "2026-10-19T08:00:00Z" },
                  reason_code: null,
                },
                permissions: {
                  agent_enabled: true,
                  global_desktop_access: true,
                  task_scope: "present",
                  task_local_execution: true,
                  task_desktop_access: false,
                  global_permission_mode: "workspace",
                  task_permission_mode: "read_only",
                  evidence: { source: "agent_store", observed_at: "2026-10-19T08:00:00Z" },
                  reason_code: "task_desktop_access_disabled",
                },
                session: {
                  state: "connected",
                  generation: 2,
                  evidence: { source: "live_session", observed_at: "2026-10-19T08:00:00Z" },
                  reason_code: null,
                },
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        // Scenario 3: present executable without verified version/handshake
        return new Response(
          JSON.stringify(
            makeStatusFixture({
              native: {
                platform: "windows",
                executable_presence: "present",
                status: "ready",
                version: null,
                protocol_version: null,
                evidence: { source: "none", observed_at: null },
                reason_code: "native_not_verified",
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    const { rerender } = render(<AgentComputerStatusPanel />);

    const pluginRow = screen.getByTestId("computer-status-row-plugin");
    const nativeRow = screen.getByTestId("computer-status-row-native");
    const permRow = screen.getByTestId("computer-status-row-permissions");
    const sessionRow = screen.getByTestId("computer-status-row-session");

    expect(await within(pluginRow).findByText("Enabled")).toBeInTheDocument();
    expect(within(nativeRow).getByText("Missing")).toBeInTheDocument();
    expect(within(sessionRow).getByText("No task selected")).toBeInTheDocument();

    // Switch to task-1 for Scenario 2
    scenario = 2;
    rerender(<AgentComputerStatusPanel taskId="task-1" />);

    expect(await within(pluginRow).findByText("Disabled")).toBeInTheDocument();
    expect(within(nativeRow).getByText("Ready")).toBeInTheDocument();
    expect(within(permRow).getByText("Global desktop: Granted")).toBeInTheDocument();
    expect(within(permRow).getByText("Task desktop: Denied")).toBeInTheDocument();
    expect(within(sessionRow).getByText("Connected")).toBeInTheDocument();
    expect(
      within(sessionRow).getByText(
        /Session connection does not imply native engine or model vision readiness/i,
      ),
    ).toBeInTheDocument();

    // Scenario 3: executable present without verified handshake -> Unverified, never Ready
    scenario = 3;
    rerender(<AgentComputerStatusPanel />);
    await user.click(screen.getByRole("button", { name: "Refresh status" }));

    expect(await within(nativeRow).findByText("Unverified")).toBeInTheDocument();
    expect(within(nativeRow).getByText("Executable: Present")).toBeInTheDocument();
    expect(within(nativeRow).queryByText("Ready")).not.toBeInTheDocument();
  });

  it("4 & 5. renders Antigravity unsupported, Codex verification pending, API verified, and distinct 4-stage image observation receipts", async () => {
    const user = userEvent.setup();
    let step = 1;

    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        if (step === 1) {
          // Antigravity selected + saved recorded only
          return new Response(
            JSON.stringify(
              makeStatusFixture({
                context: {
                  task_id: "task-obs",
                  task_revision: 1,
                  settings_revision: 1,
                  selection_source: "task",
                  model_id: "ag-1",
                  selection_epoch: "epoch-ag-1",
                },
                model: {
                  id: "ag-1",
                  display_name: "Antigravity Headless",
                  adapter: "antigravity_cli",
                  availability: "available",
                  image_input: {
                    status: "unsupported",
                    carrier: "none",
                    evidence: { source: "model_route_contract", observed_at: "2026-10-19T08:00:00Z" },
                    reason_code: "image_route_unsupported",
                  },
                },
                latest_observation: {
                  id: "obs-saved-only",
                  task_revision: 1,
                  selection_epoch: "epoch-ag-1",
                  model_id: "ag-1",
                  matches_current_selection: true,
                  width: 1280,
                  height: 720,
                  stages: {
                    saved: {
                      state: "recorded",
                      evidence: { source: "runtime_receipt", observed_at: "2026-10-19T08:01:00Z" },
                    },
                    displayed: { state: "not_recorded", evidence: { source: "none", observed_at: null } },
                    submitted: { state: "not_recorded", evidence: { source: "none", observed_at: null } },
                    semantic_observed: {
                      state: "not_recorded",
                      evidence: { source: "none", observed_at: null },
                    },
                  },
                },
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (step === 2) {
          // Codex carrier_available + displayed recorded alone + earlier model observation
          return new Response(
            JSON.stringify(
              makeStatusFixture({
                context: {
                  task_id: "task-obs",
                  task_revision: 2,
                  settings_revision: 1,
                  selection_source: "task",
                  model_id: "codex-1",
                  selection_epoch: "epoch-codex-2",
                },
                model: {
                  id: "codex-1",
                  display_name: "Codex CLI",
                  adapter: "codex_cli",
                  availability: "available",
                  image_input: {
                    status: "carrier_available",
                    carrier: "codex_turn_input",
                    evidence: { source: "model_route_contract", observed_at: "2026-10-19T08:02:00Z" },
                    reason_code: "image_verification_pending",
                  },
                },
                latest_observation: {
                  id: "obs-displayed-only",
                  task_revision: 1,
                  selection_epoch: "epoch-earlier-1",
                  model_id: "gpt-4.1",
                  matches_current_selection: false,
                  width: 800,
                  height: 600,
                  stages: {
                    saved: { state: "unknown", evidence: { source: "none", observed_at: null } },
                    displayed: {
                      state: "recorded",
                      evidence: {
                        source: "client_render_receipt",
                        observed_at: "2026-10-19T08:02:00Z",
                      },
                    },
                    submitted: { state: "not_recorded", evidence: { source: "none", observed_at: null } },
                    semantic_observed: {
                      state: "recorded",
                      evidence: { source: "runtime_receipt", observed_at: "2026-10-19T08:02:01Z" },
                    },
                  },
                },
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        // Step 3: API verified + headless submitted & semantic_observed while displayed stays unknown
        return new Response(
          JSON.stringify(
            makeStatusFixture({
              context: {
                task_id: "task-obs",
                task_revision: 3,
                settings_revision: 1,
                selection_source: "task",
                model_id: "gpt-4.1",
                selection_epoch: "epoch-gpt-3",
              },
              model: {
                id: "gpt-4.1",
                display_name: "GPT-4.1",
                adapter: "openai_responses",
                availability: "available",
                image_input: {
                  status: "verified",
                  carrier: "api_image_blocks",
                  evidence: { source: "model_image_probe", observed_at: "2026-10-19T08:03:00Z" },
                  reason_code: null,
                },
              },
              latest_observation: {
                id: "obs-headless",
                task_revision: 3,
                selection_epoch: "epoch-gpt-3",
                model_id: "gpt-4.1",
                matches_current_selection: true,
                width: 1440,
                height: 900,
                stages: {
                  saved: {
                    state: "recorded",
                    evidence: { source: "runtime_receipt", observed_at: "2026-10-19T08:03:00Z" },
                  },
                  displayed: { state: "unknown", evidence: { source: "none", observed_at: null } },
                  submitted: {
                    state: "recorded",
                    evidence: { source: "runtime_receipt", observed_at: "2026-10-19T08:03:01Z" },
                  },
                  semantic_observed: {
                    state: "recorded",
                    evidence: { source: "runtime_receipt", observed_at: "2026-10-19T08:03:02Z" },
                  },
                },
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    render(<AgentComputerStatusPanel taskId="task-obs" />);

    const modelRow = screen.getByTestId("computer-status-row-model");
    expect(await within(modelRow).findByText("Image input unsupported")).toBeInTheDocument();
    expect(within(screen.getByTestId("computer-status-stage-saved")).getByText("Recorded")).toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-submitted")).getByText("Not recorded"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-semantic_observed")).getByText("Not recorded"),
    ).toBeInTheDocument();

    // Step 2: Codex verification pending + historical observation from earlier model
    step = 2;
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(
      await within(modelRow).findByText("Image carrier available; verification pending"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("computer-status-observation-earlier-model")).toHaveTextContent(
      "Earlier model",
    );
    expect(
      within(screen.getByTestId("computer-status-stage-displayed")).getByText("Recorded"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-saved")).getByText("Unknown"),
    ).toBeInTheDocument();

    // Step 3: API verified + headless submitted & semantic_observed while displayed is Unknown
    step = 3;
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(await within(modelRow).findByText("Image input verified")).toBeInTheDocument();
    expect(
      screen.queryByTestId("computer-status-observation-earlier-model"),
    ).not.toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-displayed")).getByText("Unknown"),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-submitted")).getByText(
        "Submitted (not proof of observation)",
      ),
    ).toBeInTheDocument();
    expect(
      within(screen.getByTestId("computer-status-stage-semantic_observed")).getByText(
        "Model observation returned",
      ),
    ).toBeInTheDocument();
  });

  it("6. discards late responses from previous task A->B switch and out-of-order rapid refreshes, and cleans up on unmount", async () => {
    const user = userEvent.setup();
    const pendingResolvers: Array<{
      url: string;
      signal?: AbortSignal;
      resolve: (res: Response) => void;
    }> = [];

    vi.stubGlobal(
      "fetch",
      vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        return new Promise<Response>(resolve => {
          pendingResolvers.push({
            url: String(input),
            signal: init?.signal ?? undefined,
            resolve,
          });
        });
      }),
    );

    const { rerender, unmount } = render(<AgentComputerStatusPanel taskId="task-a" />);
    expect(pendingResolvers).toHaveLength(1);
    expect(pendingResolvers[0].url).toContain("task_id=task-a");

    // Switch to task-b before task-a responds
    rerender(<AgentComputerStatusPanel taskId="task-b" />);
    expect(pendingResolvers[0].signal?.aborted).toBe(true);
    expect(pendingResolvers).toHaveLength(2);
    expect(pendingResolvers[1].url).toContain("task_id=task-b");

    // Resolve task-b first
    pendingResolvers[1].resolve(
      new Response(
        JSON.stringify(
          makeStatusFixture({
            context: {
              task_id: "task-b",
              task_revision: 2,
              settings_revision: 1,
              selection_source: "task",
              model_id: "model-b",
              selection_epoch: "epoch-b",
            },
            model: {
              id: "model-b",
              display_name: "Model B Active",
              adapter: "openai_responses",
              availability: "available",
              image_input: {
                status: "verified",
                carrier: "api_image_blocks",
                evidence: { source: "model_image_probe", observed_at: "2026-10-19T08:05:00Z" },
                reason_code: null,
              },
            },
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    const modelRow = screen.getByTestId("computer-status-row-model");
    expect(await within(modelRow).findByText("Model B Active")).toBeInTheDocument();

    // Now resolve late task-a response (even if network ignored abort): must NOT overwrite task-b
    pendingResolvers[0].resolve(
      new Response(
        JSON.stringify(
          makeStatusFixture({
            context: {
              task_id: "task-a",
              task_revision: 1,
              settings_revision: 1,
              selection_source: "task",
              model_id: "model-a",
              selection_epoch: "epoch-a",
            },
            model: {
              id: "model-a",
              display_name: "Stale Model A",
              adapter: "codex_cli",
              availability: "available",
              image_input: {
                status: "carrier_available",
                carrier: "codex_turn_input",
                evidence: { source: "model_route_contract", observed_at: "2026-10-19T08:00:00Z" },
                reason_code: null,
              },
            },
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );

    expect(within(modelRow).getByText("Model B Active")).toBeInTheDocument();
    expect(within(modelRow).queryByText("Stale Model A")).not.toBeInTheDocument();

    // Rapid refresh older/newer reversal on task-b
    const refreshBtn = screen.getByRole("button", { name: "Refresh status" });
    await user.click(refreshBtn); // request #3 (older refresh)
    await user.click(refreshBtn); // request #4 (newer refresh)
    expect(pendingResolvers).toHaveLength(4);
    expect(pendingResolvers[2].signal?.aborted).toBe(true);

    // Resolve newer refresh (#4) first
    pendingResolvers[3].resolve(
      new Response(
        JSON.stringify(
          makeStatusFixture({
            context: {
              task_id: "task-b",
              task_revision: 4,
              settings_revision: 1,
              selection_source: "task",
              model_id: "model-b-newest",
              selection_epoch: "epoch-b-4",
            },
            model: {
              id: "model-b-newest",
              display_name: "Newest Model B",
              adapter: "openai_responses",
              availability: "available",
              image_input: {
                status: "verified",
                carrier: "api_image_blocks",
                evidence: { source: "model_image_probe", observed_at: "2026-10-19T08:06:00Z" },
                reason_code: null,
              },
            },
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(await within(modelRow).findByText("Newest Model B")).toBeInTheDocument();

    // Resolve older refresh (#3) late: must be ignored
    pendingResolvers[2].resolve(
      new Response(
        JSON.stringify(
          makeStatusFixture({
            context: {
              task_id: "task-b",
              task_revision: 3,
              settings_revision: 1,
              selection_source: "task",
              model_id: "model-b-older",
              selection_epoch: "epoch-b-3",
            },
            model: {
              id: "model-b-older",
              display_name: "Older Model B",
              adapter: "codex_cli",
              availability: "available",
              image_input: {
                status: "unknown",
                carrier: "unknown",
                evidence: { source: "none", observed_at: null },
                reason_code: null,
              },
            },
          }),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    expect(within(modelRow).getByText("Newest Model B")).toBeInTheDocument();
    expect(within(modelRow).queryByText("Older Model B")).not.toBeInTheDocument();

    // Unmount aborts any active request
    await user.click(refreshBtn);
    expect(pendingResolvers).toHaveLength(5);
    unmount();
    expect(pendingResolvers[4].signal?.aborted).toBe(true);
  });

  it("7 & 8. ignores unexpected raw secrets, developer paths, and exception traces and never renders them", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        return new Response(
          JSON.stringify(
            makeStatusFixture({
              detail: "Traceback: C:\\Users\\admin\\.codex\\auth.json api_key=sk-secret9999999999",
              stderr: "Fatal worker crash at D:\\secret\\ryn-native.exe",
              native: {
                platform: "windows",
                executable_presence: "present",
                status: "error",
                version: "C:\\Program Files\\Ryn\\ryn-computer.exe --version",
                protocol_version: "Bearer sk-secret9999999999",
                evidence: { source: "none", observed_at: null },
                reason_code: "Exception: spawn C:\\Users\\secret\\worker.exe ENOENT",
              },
              model: {
                id: "codex-default",
                display_name: "Traceback (most recent call last): /home/admin/.env",
                adapter: "codex_cli",
                availability: "available",
                image_input: {
                  status: "unknown",
                  carrier: "unknown",
                  evidence: { source: "none", observed_at: null },
                  reason_code: "secret=ghp_1234567890abcdef",
                },
              },
            }),
          ),
          { status: 200, headers: { "Content-Type": "application/json" } },
        );
      }),
    );

    const { container } = render(<AgentComputerStatusPanel />);

    const nativeRow = screen.getByTestId("computer-status-row-native");
    expect(await within(nativeRow).findByText("Error")).toBeInTheDocument();
    expect(within(nativeRow).getByText("Status unavailable")).toBeInTheDocument();

    const text = container.textContent ?? "";
    expect(text).not.toContain("C:\\Users");
    expect(text).not.toContain("C:\\Program Files");
    expect(text).not.toContain(".codex");
    expect(text).not.toContain("sk-secret");
    expect(text).not.toContain("ghp_");
    expect(text).not.toContain("Traceback");
    expect(text).not.toContain("ENOENT");
    expect(text).not.toContain("/home/admin");
  });

  it("9 & 10. mounts in AgentExtensions Computer Use details after desktop_access switch and before bundled skills, enforcing GET-only status refresh and preserving existing switch handlers", async () => {
    const user = userEvent.setup();
    const recordedRequests: Array<{ method: string; url: string }> = [];
    let currentDesktopAccess = false;
    let currentOverviewRevision = 5;

    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = (init?.method ?? "GET").toUpperCase();
        recordedRequests.push({ method, url });

        if (url.includes("/api/local/agent/extensions") && method === "GET") {
          return new Response(
            JSON.stringify(
              makeExtensionsOverviewFixture({
                revision: currentOverviewRevision,
                desktop_access: currentDesktopAccess,
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/local/agent/computer/status") && method === "GET") {
          return new Response(
            JSON.stringify(
              makeStatusFixture({
                permissions: {
                  agent_enabled: true,
                  global_desktop_access: currentDesktopAccess,
                  task_scope: "not_requested",
                  task_local_execution: null,
                  task_desktop_access: null,
                  global_permission_mode: "workspace",
                  task_permission_mode: null,
                  evidence: { source: "agent_store", observed_at: "2026-10-19T08:00:00Z" },
                },
              }),
            ),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        if (url.includes("/api/local/agent/settings") && method === "PUT") {
          currentDesktopAccess = true;
          currentOverviewRevision += 1;
          return new Response(
            JSON.stringify({
              enabled: true,
              model_id: "codex-default",
              permission_mode: "workspace",
              allowed_write_tools: [],
              desktop_access: true,
              max_steps: 20,
              max_seconds: 120,
              max_context_chars: null,
              max_output_chars: 8000,
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          );
        }
        return new Response(JSON.stringify({ detail: "Unexpected request" }), { status: 404 });
      }),
    );

    render(
      <MemoryRouter initialEntries={["/settings/extensions/plugins"]}>
        <Routes>
          <Route path="/settings/extensions/:section" element={<AgentExtensions />} />
        </Routes>
      </MemoryRouter>,
    );

    const card = (await screen.findByRole("heading", { name: "Computer Use" })).closest("article")!;
    await user.click(within(card).getByRole("button", { name: "View details" }));

    const dialog = await screen.findByRole("dialog", { name: "Computer Use" });
    const statusPanel = await within(dialog).findByTestId("agent-computer-status-panel");
    const desktopSwitch = within(dialog).getByRole("switch", {
      name: "Allow computer and browser operations",
    });
    const bundledSkillsHeading = within(dialog).getByRole("heading", {
      name: /Bundled skills/i,
    });

    // Verify DOM order: desktop_access switch -> AgentComputerStatusPanel -> Bundled skills
    expect(
      desktopSwitch.compareDocumentPosition(statusPanel) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      statusPanel.compareDocumentPosition(bundledSkillsHeading) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();

    // No duplicate switches inside AgentComputerStatusPanel
    expect(within(statusPanel).queryAllByRole("switch")).toHaveLength(0);

    // Only GET requests were made on mount + opening dialog
    expect(recordedRequests.every(r => r.method === "GET")).toBe(true);

    const permRow = within(statusPanel).getByTestId("computer-status-row-permissions");
    expect(await within(permRow).findByText("Global desktop: Denied")).toBeInTheDocument();

    // Toggling existing desktop_access switch calls existing PUT /settings and triggers status panel GET refresh via refreshKey
    await user.click(desktopSwitch);
    expect(await within(permRow).findByText("Global desktop: Granted")).toBeInTheDocument();

    const statusCalls = recordedRequests.filter(r =>
      r.url.includes("/api/local/agent/computer/status"),
    );
    expect(statusCalls.length).toBeGreaterThanOrEqual(2);
    expect(statusCalls.every(r => r.method === "GET")).toBe(true);
  });

  it("11. immediately clears old verified/Granted state at render when same-task model, scope, or permission changes and rejects stale old-model responses", async () => {
    let calls = 0;
    let resolveSecond!: (dto: ReturnType<typeof makeStatusFixture>) => void;

    const makeTaskModelFixture = (modelId: string, enabled = true) =>
      makeStatusFixture({
        context: {
          task_id: "same-task",
          task_revision: 3,
          settings_revision: 4,
          selection_source: "task",
          model_id: modelId,
          selection_epoch: `epoch-${modelId}`,
        },
        permissions: {
          agent_enabled: enabled,
          global_desktop_access: enabled,
          task_scope: "present",
          task_local_execution: true,
          task_desktop_access: enabled,
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
        return Promise.resolve(makeTaskModelFixture("model-a", true) as never);
      }
      return new Promise<never>(resolve => {
        resolveSecond = resolve as unknown as (dto: ReturnType<typeof makeStatusFixture>) => void;
      });
    });

    const { rerender } = render(
      <AgentComputerStatusPanel
        taskId="same-task"
        refreshKey="rev-1"
        expectedModelId="model-a"
        expectedPermissions={{ agentEnabled: true, globalDesktopAccess: true }}
        fetchStatus={fetchStatus}
      />,
    );

    expect(await screen.findByText("Fixture model-a")).toBeInTheDocument();
    expect(screen.getByText("Image input verified")).toBeInTheDocument();
    const permRow = screen.getByTestId("computer-status-row-permissions");
    expect(within(permRow).getByText("Global desktop: Granted")).toBeInTheDocument();

    // Same task switches to model-b and revokes global permissions while second GET is pending
    rerender(
      <AgentComputerStatusPanel
        taskId="same-task"
        refreshKey="rev-2"
        expectedModelId="model-b"
        expectedPermissions={{ agentEnabled: false, globalDesktopAccess: false }}
        fetchStatus={fetchStatus}
      />,
    );

    // Immediately while pending: old model, verified label, and Granted badges must be cleared
    expect(calls).toBe(2);
    expect(screen.queryByText("Fixture model-a")).not.toBeInTheDocument();
    expect(screen.queryByText("Image input verified")).not.toBeInTheDocument();
    expect(within(permRow).queryByText(/Granted/)).not.toBeInTheDocument();
    expect(screen.getByText("Refreshing status…")).toBeInTheDocument();

    // Resolve the second request with an internally consistent but stale model-a payload: must be rejected
    resolveSecond(makeTaskModelFixture("model-a", true));

    expect(
      await screen.findByText(
        /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Fixture model-a")).not.toBeInTheDocument();
    expect(screen.queryByText("Image input verified")).not.toBeInTheDocument();
    expect(within(permRow).queryByText(/Granted/)).not.toBeInTheDocument();
  });

  it("12. rejects old numeric or null task_revision when minTaskRevision advances on same-task same-model refresh", async () => {
    const user = userEvent.setup();
    const resolvers: Array<(dto: ReturnType<typeof makeStatusFixture>) => void> = [];

    const makeRevisionFixture = (taskRevision: number | null, epoch: string) =>
      makeStatusFixture({
        context: {
          task_id: "same-task",
          task_revision: taskRevision,
          settings_revision: 4,
          selection_source: "task",
          model_id: "model-a",
          selection_epoch: epoch,
        },
        model: {
          id: "model-a",
          display_name: "Fixture model-a",
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
      return new Promise<never>(resolve => {
        resolvers.push(resolve as unknown as (dto: ReturnType<typeof makeStatusFixture>) => void);
      });
    });

    const { rerender } = render(
      <AgentComputerStatusPanel
        taskId="same-task"
        refreshKey="dest-1"
        expectedModelId="model-a"
        minTaskRevision={10}
        fetchStatus={fetchStatus}
      />,
    );

    expect(resolvers).toHaveLength(1);
    resolvers[0](makeRevisionFixture(10, "old-selection-epoch"));
    expect(await screen.findByText("Image input verified")).toBeInTheDocument();

    // Advance minTaskRevision to 20 with same model-a
    rerender(
      <AgentComputerStatusPanel
        taskId="same-task"
        refreshKey="dest-2"
        expectedModelId="model-a"
        minTaskRevision={20}
        fetchStatus={fetchStatus}
      />,
    );

    expect(resolvers).toHaveLength(2);
    expect(screen.queryByText("Image input verified")).not.toBeInTheDocument();

    // Returning old task_revision=10 is rejected
    resolvers[1](makeRevisionFixture(10, "old-selection-epoch"));
    expect(
      await screen.findByText(
        /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Image input verified")).not.toBeInTheDocument();

    // Manual refresh returning null task_revision is also rejected when minTaskRevision=20
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(resolvers).toHaveLength(3);
    resolvers[2](makeRevisionFixture(null, "null-rev-epoch"));
    expect(
      await screen.findByText(
        /Status response is invalid or context mismatched; readiness cannot be confirmed/i,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Image input verified")).not.toBeInTheDocument();

    // Manual refresh returning task_revision=20 succeeds
    await user.click(screen.getByRole("button", { name: "Refresh status" }));
    expect(resolvers).toHaveLength(4);
    resolvers[3](makeRevisionFixture(20, "new-selection-epoch"));
    expect(await screen.findByText("Image input verified")).toBeInTheDocument();
  });
});
