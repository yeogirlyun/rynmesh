import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import AgentExtensions from "./AgentExtensions";
import { SkillSelection } from "./RynAgentGuidance";
import {
  buildNewSecretMap,
  buildSecretPatch,
  expectedSkillRevision,
  normalizeOverview,
} from "../domain/agentExtensions";

function makeOverviewFixture(overrides: Record<string, unknown> = {}) {
  return {
    revision: 5,
    updated_at: "2026-10-19T03:00:00Z",
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
        skill_ids: [],
        mcp_server_ids: ["computer-use"],
        skills_count: 0,
        mcp_servers_count: 1,
        skills: [],
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
            read_only_tools: ["computer_observe_windows", "computer_read_window"],
            tools: [
              {
                name: "computer_observe_windows",
                model_tool_name: "mcp__computer_use__computer_observe_windows",
                title: "Observe desktop windows",
                description: "List visible top-level Windows desktop windows.",
                enabled: true,
                component_enabled: true,
                owner_read_only: true,
                effective_effect: "read",
                effective_risk: "low",
                annotations: { readOnlyHint: true },
              },
            ],
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
          observable: ["Visible top-level Windows desktop windows and foreground window state"],
          controllable: ["Window focus, coordinate or element click, text entry, and key presses"],
          permission_note: "Requires both enabling Computer Use and enabling desktop_access.",
        },
        revision: 2,
      },
      {
        id: "team-toolkit",
        instance_id: "pkg-team-toolkit",
        name: "team-toolkit",
        title: "Team Toolkit",
        description: "Portable Codex plugin with a review skill and docs MCP server.",
        version: "1.2.0",
        kind: "imported",
        source_format: "codex_plugin",
        builtin: false,
        deletable: true,
        enabled: false,
        component_enabled: false,
        parent_enabled: true,
        status: "disabled",
        status_reason: "",
        computer_access: false,
        skill_ids: ["team-review"],
        mcp_server_ids: ["team-docs"],
        skills_count: 1,
        mcp_servers_count: 1,
        skills: [
          {
            id: "team-review",
            instance_id: "skill-team-review",
            name: "team-review",
            title: "Team Review Checklist",
            description: "Review pull requests with security checks.",
            summary: "Review pull requests with security checks.",
            version: "1.2.0",
            content: "# Team Review\nCheck all inputs and boundaries.",
            builtin: false,
            deletable: true,
            enabled: false,
            component_enabled: true,
            parent_enabled: false,
            source_format: "codex_plugin",
            plugin_id: "team-toolkit",
            files: [
              { path: "SKILL.md", size: 48 },
              { path: "references/checklist.md", size: 256 },
            ],
            warnings: ["Skill 'team-review' references missing file 'references/extra.md'."],
            revision: 3,
          },
        ],
        mcp_servers: [
          {
            id: "team-docs",
            instance_id: "mcp-team-docs",
            name: "Team Docs Server",
            title: "Team Docs Server",
            description: "Search internal docs over STDIO.",
            transport: "stdio",
            builtin: false,
            deletable: true,
            plugin_id: "team-toolkit",
            computer_access: false,
            enabled: false,
            component_enabled: true,
            parent_enabled: false,
            command: "npx",
            args: ["-y", "@example/docs-mcp"],
            cwd: "D:\\workspace",
            env_keys: ["DOCS_TOKEN"],
            masked_env: {
              DOCS_TOKEN: { has_value: true, masked: "••••••••" },
            },
            url: "",
            bearer_token_env: "",
            header_keys: [],
            masked_headers: {},
            has_secrets: true,
            startup_timeout_ms: 10000,
            tool_timeout_ms: 30000,
            enabled_tools: null,
            disabled_tools: [],
            read_only_tools: [],
            tools: [
              {
                name: "search_docs",
                model_tool_name: "mcp__team_docs__search_docs",
                title: "Search Docs",
                description: "Search internal markdown docs.",
                enabled: false,
                component_enabled: true,
                owner_read_only: false,
                effective_effect: "write",
                effective_risk: "high",
                annotations: { readOnlyHint: true },
              },
            ],
            tools_count: 1,
            enabled_tools_count: 0,
            status: "disabled",
            status_message: "Parent plugin is disabled.",
            last_tested_at: null,
            revision: 4,
          },
        ],
        unsupported_components: ["hooks", "commands"],
        warnings: ["Directory 'hooks/' is ignored; Ryn does not run external lifecycle hooks."],
        revision: 3,
      },
    ],
    skills: [
      {
        id: "system-diagnostics",
        instance_id: "skill-system-diagnostics",
        name: "system-diagnostics",
        title: "System diagnostics",
        description: "Inspect local Ryn node status and connectivity.",
        summary: "Inspect local Ryn node status and connectivity.",
        version: "1",
        content: "# System diagnostics\n1. Check node health.",
        builtin: true,
        deletable: false,
        enabled: true,
        component_enabled: true,
        parent_enabled: true,
        source_format: "builtin",
        plugin_id: null,
        files: [{ path: "SKILL.md", size: 42 }],
        warnings: [],
        revision: 1,
      },
      {
        id: "team-review",
        instance_id: "skill-team-review",
        name: "team-review",
        title: "Team Review Checklist",
        description: "Review pull requests with security checks.",
        summary: "Review pull requests with security checks.",
        version: "1.2.0",
        content: "# Team Review\nCheck all inputs and boundaries.",
        builtin: false,
        deletable: true,
        enabled: false,
        component_enabled: true,
        parent_enabled: false,
        source_format: "codex_plugin",
        plugin_id: "team-toolkit",
        files: [
          { path: "SKILL.md", size: 48 },
          { path: "references/checklist.md", size: 256 },
        ],
        warnings: ["Skill 'team-review' references missing file 'references/extra.md'."],
        revision: 3,
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
        read_only_tools: ["computer_observe_windows", "computer_read_window"],
        tools: [
          {
            name: "computer_observe_windows",
            model_tool_name: "mcp__computer_use__computer_observe_windows",
            title: "Observe desktop windows",
            description: "List visible top-level Windows desktop windows.",
            enabled: true,
            component_enabled: true,
            owner_read_only: true,
            effective_effect: "read",
            effective_risk: "low",
            annotations: { readOnlyHint: true },
          },
        ],
        tools_count: 1,
        enabled_tools_count: 1,
        status: "NeedsPermission",
        status_message: "Requires desktop_access in Agent settings.",
        last_tested_at: null,
        revision: 2,
      },
      {
        id: "team-docs",
        instance_id: "mcp-team-docs",
        name: "Team Docs Server",
        title: "Team Docs Server",
        description: "Search internal docs over STDIO.",
        transport: "stdio",
        builtin: false,
        deletable: true,
        plugin_id: "team-toolkit",
        computer_access: false,
        enabled: false,
        component_enabled: true,
        parent_enabled: false,
        command: "npx",
        args: ["-y", "@example/docs-mcp"],
        cwd: "D:\\workspace",
        env_keys: ["DOCS_TOKEN", "KEEP_SECRET"],
        masked_env: {
          DOCS_TOKEN: { has_value: true, masked: "••••••••" },
          KEEP_SECRET: { has_value: true, masked: "••••••••" },
        },
        url: "",
        bearer_token_env: "",
        header_keys: [],
        masked_headers: {},
        has_secrets: true,
        startup_timeout_ms: 10000,
        tool_timeout_ms: 30000,
        enabled_tools: ["search_docs"],
        disabled_tools: [],
        read_only_tools: [],
        tools: [
          {
            name: "search_docs",
            model_tool_name: "mcp__team_docs__search_docs",
            title: "Search Docs",
            description: "Search internal markdown docs.",
            enabled: false,
            component_enabled: true,
            owner_read_only: false,
            effective_effect: "write",
            effective_risk: "high",
            annotations: { readOnlyHint: true },
          },
        ],
        tools_count: 1,
        enabled_tools_count: 0,
        status: "disabled",
        status_message: "Parent plugin is disabled.",
        last_tested_at: null,
        revision: 4,
      },
    ],
    ...overrides,
  };
}

function renderExtensions(initialEntry = "/settings/extensions") {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/settings/extensions" element={<AgentExtensions />} />
        <Route path="/settings/extensions/:section" element={<AgentExtensions />} />
        <Route path="/agent/extensions" element={<AgentExtensions />} />
        <Route path="/agent/extensions/:section" element={<AgentExtensions />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  document.documentElement.dataset.theme = "light";
  server.use(
    http.get("*/api/local/agent/extensions/skills/recommended", () =>
      HttpResponse.json({ skills: [], count: 0 }),
    ),
    http.get("*/api/local/agent/computer/status", () =>
      HttpResponse.json({
        enabled: true,
        browser_running: false,
        desktop_supported: true,
        active_session: false,
      }),
    ),
  );
});

describe("agentExtensions domain contract & helpers", () => {
  it("preserves exact store.py DTO fields including status, content, source_format, component_enabled, parent_enabled, and enabled_tools null", () => {
    const overview = normalizeOverview(makeOverviewFixture());
    const srv = overview.mcp_servers.find(m => m.id === "computer-use")!;
    expect(srv.status).toBe("NeedsPermission");
    expect(srv.status_message).toBe("Requires desktop_access in Agent settings.");
    expect(srv.enabled_tools).toBeNull();
    expect(srv.tools[0].owner_read_only).toBe(true);
    expect(srv.tools[0].effective_effect).toBe("read");

    const childSkill = overview.skills.find(s => s.id === "team-review")!;
    expect(childSkill.enabled).toBe(false);
    expect(childSkill.component_enabled).toBe(true);
    expect(childSkill.parent_enabled).toBe(false);
    expect(childSkill.content).toContain("# Team Review");
    expect(childSkill.source_format).toBe("codex_plugin");

    // Built-in skill revision always uses skill.revision, never overview.revision
    const builtinSkill = overview.skills.find(s => s.id === "system-diagnostics")!;
    expect(builtinSkill.revision).toBe(1);
    expect(overview.revision).toBe(5);
    expect(expectedSkillRevision(builtinSkill)).toBe(1);
  });

  it("handles special keys (__proto__, constructor) and preserve/replace/delete in secret helpers", () => {
    // Delete 'constructor' key
    const deleteConstructor = buildSecretPatch([], ["constructor"]);
    expect(deleteConstructor).toBeDefined();
    expect(Object.hasOwn(deleteConstructor!, "constructor")).toBe(true);
    expect(deleteConstructor!.constructor).toBeNull();

    // Create '__proto__' key in new secret map
    const protoMap = buildNewSecretMap([
      {
        id: "1",
        key: "__proto__",
        value: "custom-val",
        savedKey: null,
        isSavedMasked: false,
      },
    ]);
    expect(protoMap).toBeDefined();
    expect(Object.hasOwn(protoMap!, "__proto__")).toBe(true);
    expect(protoMap!["__proto__"]).toBe("custom-val");
  });
});

describe("AgentExtensions UI", () => {
  it("renders Plugins, controls Computer Use plugin switch and desktop_access separately, and handles package plugin child switches", async () => {
    const user = userEvent.setup();
    const pluginPatchBodies: unknown[] = [];
    const settingsPutBodies: unknown[] = [];
    const skillPatchBodies: unknown[] = [];

    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
      http.patch("*/api/local/agent/extensions/plugins/:id", async ({ request, params }) => {
        const body = (await request.json()) as { enabled: boolean; expected_revision: number };
        pluginPatchBodies.push({ id: params.id, ...body });
        return HttpResponse.json({
          ...makeOverviewFixture().plugins[0],
          enabled: body.enabled,
          component_enabled: body.enabled,
          revision: body.expected_revision + 1,
        });
      }),
      http.put("*/api/local/agent/settings", async ({ request }) => {
        const body = await request.json();
        settingsPutBodies.push(body);
        return HttpResponse.json({
          enabled: true,
          model_id: "m1",
          permission_mode: "workspace",
          allowed_write_tools: [],
          desktop_access: true,
          max_steps: 20,
          max_seconds: 120,
          max_context_chars: null,
          max_output_chars: 8000,
        });
      }),
      http.patch("*/api/local/agent/extensions/skills/:id", async ({ request, params }) => {
        const body = (await request.json()) as { enabled: boolean; expected_revision: number };
        skillPatchBodies.push({ id: params.id, ...body });
        return HttpResponse.json({
          ...makeOverviewFixture().skills[1],
          component_enabled: body.enabled,
          revision: body.expected_revision + 1,
        });
      }),
    );

    renderExtensions("/settings/extensions/plugins");

    expect(await screen.findByRole("heading", { name: "Computer Use" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Plugins/i })).toHaveAttribute("aria-current", "page");

    // Open Computer Use details
    const computerCard = screen.getByRole("heading", { name: "Computer Use" }).closest("article")!;
    await user.click(within(computerCard).getByRole("button", { name: "View details" }));

    const dialog = await screen.findByRole("dialog", { name: "Computer Use" });
    expect(
      within(dialog).getByText(/Visible top-level Windows desktop windows/i),
    ).toBeInTheDocument();

    // Toggle desktop_access permission switch inside Computer Use details
    const desktopSwitch = within(dialog).getByRole("switch", {
      name: "Allow computer and browser operations",
    });
    expect(desktopSwitch).toHaveAttribute("aria-checked", "false");
    await user.click(desktopSwitch);
    await waitFor(() =>
      expect(settingsPutBodies).toEqual([{ desktop_access: true }]),
    );

    // Toggle Computer Use package switch
    const packageSwitch = within(dialog).getByRole("switch", {
      name: "Enable plugin package Computer Use",
    });
    await user.click(packageSwitch);
    await waitFor(() =>
      expect(pluginPatchBodies).toEqual([
        { id: "computer-use", enabled: false, expected_revision: 2 },
      ]),
    );

    await user.click(within(dialog).getByRole("button", { name: "Close dialog" }));

    // Open Team Toolkit package plugin details: parent is off, child skill preference is still on
    const toolkitCard = screen.getByRole("heading", { name: "Team Toolkit" }).closest("article")!;
    await user.click(within(toolkitCard).getByRole("button", { name: "View details" }));

    const toolkitDialog = await screen.findByRole("dialog", { name: "Team Toolkit" });
    expect(
      within(toolkitDialog).getByText(
        /Currently inactive: parent plugin is disabled \(skill preference remains on\)/i,
      ),
    ).toBeInTheDocument();
    expect(within(toolkitDialog).getByText("hooks")).toBeInTheDocument();

    const bundledSkillSwitch = within(toolkitDialog).getByRole("switch", {
      name: "Enable bundled skill Team Review Checklist",
    });
    expect(bundledSkillSwitch).toHaveAttribute("aria-checked", "true");
    await user.click(bundledSkillSwitch);
    await waitFor(() =>
      expect(skillPatchBodies).toEqual([
        { id: "team-review", enabled: false, expected_revision: 3 },
      ]),
    );
  });

  it("renders Skills, submits built-in skill's own revision, displays SKILL.md and files, and links from SkillSelection", async () => {
    const user = userEvent.setup();
    const skillPatchCalls: unknown[] = [];
    const skillDeleteCalls: string[] = [];

    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
      http.patch("*/api/local/agent/extensions/skills/:id", async ({ request, params }) => {
        const body = (await request.json()) as { enabled: boolean; expected_revision: number };
        skillPatchCalls.push({ id: params.id, ...body });
        return HttpResponse.json({
          ...makeOverviewFixture().skills[0],
          enabled: body.enabled,
          component_enabled: body.enabled,
          revision: 6,
        });
      }),
      http.delete("*/api/local/agent/extensions/skills/:id", ({ request, params }) => {
        const url = new URL(request.url);
        skillDeleteCalls.push(`${params.id}?expected_revision=${url.searchParams.get("expected_revision")}`);
        return HttpResponse.json({ deleted: true, id: params.id, revision: 7 });
      }),
    );

    renderExtensions("/settings/extensions/skills");

    expect(await screen.findByRole("heading", { name: "System diagnostics" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Skills/i })).toHaveAttribute("aria-current", "page");

    // Toggle built-in skill (revision=1 while overview.revision=5)
    const builtinSwitch = screen.getByRole("switch", {
      name: "Enable skill System diagnostics",
    });
    await user.click(builtinSwitch);
    await waitFor(() =>
      expect(skillPatchCalls).toEqual([
        { id: "system-diagnostics", enabled: false, expected_revision: 1 },
      ]),
    );

    // Open imported skill details
    const importedRow = screen
      .getByRole("heading", { name: "Team Review Checklist" })
      .closest("article")!;
    await user.click(within(importedRow).getByRole("button", { name: "View details" }));

    const skillDialog = await screen.findByRole("dialog", { name: "Team Review Checklist" });
    expect(within(skillDialog).getByText("references/checklist.md")).toBeInTheDocument();
    expect(within(skillDialog).getByText(/# Team Review/)).toBeInTheDocument();
    expect(
      within(skillDialog).getByText(/references missing file 'references\/extra\.md'/),
    ).toBeInTheDocument();

    // Remove imported skill with confirmation
    await user.click(within(skillDialog).getByRole("button", { name: "Remove skill" }));
    const confirmModal = await screen.findByRole("dialog", {
      name: "Remove skill Team Review Checklist",
    });
    await user.click(within(confirmModal).getByRole("button", { name: "Confirm remove" }));
    await waitFor(() => expect(skillDeleteCalls).toEqual(["team-review?expected_revision=3"]));
  });

  it("shows Manage skills link and enabled imported skills in SkillSelection", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/local/agent/skills", () =>
        HttpResponse.json({
          skills: [
            {
              id: "system-diagnostics",
              title: "System diagnostics",
              version: "1",
              description: "Built-in",
            },
            {
              id: "team-review",
              title: "Team Review Checklist",
              version: "1.2.0",
              description: "Imported",
            },
          ],
        }),
      ),
    );
    render(
      <MemoryRouter>
        <SkillSelection value={[]} onChange={() => undefined} disabled={false} />
      </MemoryRouter>,
    );
    await user.click(screen.getByRole("button", { name: "Skills · optional" }));
    expect(await screen.findByText("Team Review Checklist")).toBeInTheDocument();
    const manageLink = screen.getByRole("link", { name: "Manage skills" });
    expect(manageLink).toHaveAttribute("href", "/settings/extensions/skills");
  });

  it("manages MCP servers: honest status, test connection, per-tool switches, masked secret preservation/replacement/removal, and 409 conflict recovery", async () => {
    const user = userEvent.setup();
    const patchCalls: Array<{ id: string; body: Record<string, unknown> }> = [];
    let shouldConflictOnce = true;

    server.use(
      http.get("*/api/local/agent/extensions", () =>
        HttpResponse.json(
          makeOverviewFixture({
            revision: shouldConflictOnce ? 5 : 9,
          }),
        ),
      ),
      http.post("*/api/local/agent/extensions/mcp/servers/:id/test", () =>
        HttpResponse.json({
          server: {
            ...makeOverviewFixture().mcp_servers[1],
            enabled: true,
            parent_enabled: true,
            status: "ready",
            status_message: "",
            last_tested_at: "2026-10-19T04:00:00Z",
            tools_count: 1,
            revision: 8,
          },
          test_result: { status: "connected", tools_count: 1 },
        }),
      ),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", async ({ request, params }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchCalls.push({ id: String(params.id), body });
        if (shouldConflictOnce && body.name === "Team Docs Updated") {
          shouldConflictOnce = false;
          return HttpResponse.json(
            { detail: "agent_extensions_revision_conflict" },
            { status: 409 },
          );
        }
        return HttpResponse.json({
          ...makeOverviewFixture().mcp_servers[1],
          name: typeof body.name === "string" ? body.name : "Team Docs Server",
          disabled_tools: Array.isArray(body.disabled_tools) ? body.disabled_tools : [],
          read_only_tools: Array.isArray(body.read_only_tools) ? body.read_only_tools : [],
          enabled_tools: body.enabled_tools !== undefined ? body.enabled_tools : ["search_docs"],
          revision: 10,
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");

    expect(await screen.findByRole("heading", { name: "Team Docs Server" })).toBeInTheDocument();

    // Test connection transitions server to Connected
    const docsRow = screen.getByRole("heading", { name: "Team Docs Server" }).closest("article")!;
    await user.click(within(docsRow).getByRole("button", { name: "Test connection" }));
    expect(await within(docsRow).findByText(/Connected · 1 tools/i)).toBeInTheDocument();

    // Open details & toggle tool read-only and allowlist
    await user.click(within(docsRow).getByRole("button", { name: "View details" }));
    const detailsDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });

    // Clear allowlist (sends enabled_tools: null)
    await user.click(
      within(detailsDialog).getByRole("button", { name: /Allowlist active .* Allow all tools/i }),
    );
    await waitFor(() =>
      expect(patchCalls.some(c => c.body.enabled_tools === null)).toBe(true),
    );

    // Toggle owner read-only checkbox on search_docs
    await user.click(
      within(detailsDialog).getByRole("checkbox", { name: "Mark search_docs as read-only" }),
    );
    await waitFor(() =>
      expect(
        patchCalls.some(
          c =>
            Array.isArray(c.body.read_only_tools) &&
            c.body.read_only_tools.includes("search_docs"),
        ),
      ).toBe(true),
    );

    // Open Edit modal from details
    await user.click(within(detailsDialog).getByRole("button", { name: "Edit" }));
    const editDialog = await screen.findByRole("dialog", { name: /Edit server Team Docs Server/i });

    // Verify individually labelled environment variable inputs
    expect(
      within(editDialog).getByLabelText("Environment variable 1 name"),
    ).toHaveValue("DOCS_TOKEN");
    expect(
      within(editDialog).getByLabelText("Environment variable 2 name"),
    ).toHaveValue("KEEP_SECRET");

    // Replace DOCS_TOKEN value, keep KEEP_SECRET untouched, and add a new variable NEW_VAR
    const replaceButtons = within(editDialog).getAllByRole("button", { name: "Replace value" });
    await user.click(replaceButtons[0]);
    const val1 = within(editDialog).getByLabelText("Environment variable 1 value");
    await user.type(val1, "fresh-secret-123");

    // Change name
    const nameInput = within(editDialog).getByLabelText("Server name");
    await user.clear(nameInput);
    await user.type(nameInput, "Team Docs Updated");

    // First save hits 409 conflict: draft and typed secret are preserved
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));
    expect(await within(editDialog).findByRole("alert")).toHaveTextContent(
      /Configuration changed in another view/i,
    );
    expect(within(editDialog).getByLabelText("Environment variable 1 value")).toHaveValue(
      "fresh-secret-123",
    );

    // Second save succeeds; verify KEEP_SECRET was omitted and DOCS_TOKEN sent new value
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());

    const lastPatch = patchCalls[patchCalls.length - 1];
    expect(lastPatch.body.env).toEqual({ DOCS_TOKEN: "fresh-secret-123" });
  });

  it("previews and commits MCP configuration import with single source field and displays warnings/unsupported components", async () => {
    const user = userEvent.setup();
    const previewRequests: unknown[] = [];
    const commitRequests: unknown[] = [];

    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
      http.post("*/api/local/agent/extensions/import/preview", async ({ request }) => {
        const body = await request.json();
        previewRequests.push(body);
        return HttpResponse.json({
          preview_id: "prev-123",
          name: "Imported FS Server",
          version: "1.0.0",
          source_format: "mcp_json",
          plugins: [],
          skills: [],
          mcp_servers: [
            {
              id: "fs-server",
              name: "fs-server",
              transport: "stdio",
              command: "npx",
              args: ["-y", "@modelcontextprotocol/server-filesystem"],
            },
          ],
          warnings: ["OAuth configuration ignored; use bearer_token_env or headers."],
          unsupported_components: ["oauth"],
          can_import: true,
          expected_revision: 5,
        });
      }),
      http.post("*/api/local/agent/extensions/import/commit", async ({ request }) => {
        const body = await request.json();
        commitRequests.push(body);
        return HttpResponse.json({
          committed: { plugins: [], skills: [], mcp_servers: ["fs-server"] },
          overview: makeOverviewFixture({ revision: 6 }),
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");
    await screen.findByRole("heading", { name: "Team Docs Server" });

    await user.click(screen.getByRole("button", { name: "Import configuration" }));
    const importDialog = await screen.findByRole("dialog", {
      name: "Import MCP server configuration",
    });

    const textarea = within(importDialog).getByLabelText("MCP configuration text");
    await user.type(
      textarea,
      '{{"mcpServers": {{"fs-server": {{"command": "npx"}}}',
    );

    await user.click(within(importDialog).getByRole("button", { name: "Preview import" }));

    expect(await within(importDialog).findByText(/Imported FS Server/)).toBeInTheDocument();
    expect(within(importDialog).getByText("oauth")).toBeInTheDocument();
    expect(
      within(importDialog).getByText(/OAuth configuration ignored/i),
    ).toBeInTheDocument();

    // Ensure only config_text (+ kind + filename) was sent
    expect(previewRequests).toHaveLength(1);
    expect(previewRequests[0]).toEqual({
      kind: "mcp",
      config_text: '{"mcpServers": {"fs-server": {"command": "npx"}}}',
      filename: ".mcp.json",
    });

    await user.click(within(importDialog).getByRole("button", { name: "Commit import" }));
    await waitFor(() =>
      expect(commitRequests).toEqual([{ preview_id: "prev-123", expected_revision: 5 }]),
    );
  });

  it("renders Chinese localization and supports dark theme attribute", async () => {
    await i18n.changeLanguage("zh-CN");
    document.documentElement.dataset.theme = "dark";
    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
    );

    renderExtensions("/settings/extensions/plugins");
    expect(await screen.findByRole("heading", { name: "Agent 扩展" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "导入插件" })).toBeInTheDocument();
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("rebases untouched fields on 409 edit conflict without overwriting concurrent changes and aborts if instance_id changed", async () => {
    const user = userEvent.setup();
    const patchBodies: Array<Record<string, unknown>> = [];
    let getCount = 0;

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        getCount += 1;
        const base = makeOverviewFixture({ revision: getCount === 1 ? 5 : 8 });
        base.mcp_servers[1] = {
          ...base.mcp_servers[1],
          instance_id: "inst-original",
          command: getCount === 1 ? "old-command" : "external-new-command",
          description: "Initial",
          revision: getCount === 1 ? 4 : 8,
        };
        return HttpResponse.json(base);
      }),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchBodies.push(body);
        if (patchBodies.length === 1) {
          return HttpResponse.json(
            { detail: "agent_extensions_revision_conflict" },
            { status: 409 },
          );
        }
        return HttpResponse.json({
          ...makeOverviewFixture().mcp_servers[1],
          instance_id: "inst-original",
          command: String(body.command),
          description: String(body.description),
          revision: 9,
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");
    const row = (await screen.findByRole("heading", { name: "Team Docs Server" })).closest(
      "article",
    )!;
    await user.click(within(row).getByRole("button", { name: "View details" }));
    const detailsDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });
    await user.click(within(detailsDialog).getByRole("button", { name: "Edit" }));

    const editDialog = await screen.findByRole("dialog", { name: /Edit server Team Docs Server/i });
    const descInput = within(editDialog).getByLabelText(/Description/i);
    await user.clear(descInput);
    await user.type(descInput, "Edited description only");

    // First save hits 409 conflict; untouched command must rebase to external-new-command
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));
    expect(await within(editDialog).findByRole("alert")).toHaveTextContent(
      /Configuration changed in another view/i,
    );
    expect(within(editDialog).getByLabelText("Command")).toHaveValue("external-new-command");
    expect(within(editDialog).getByLabelText(/Description/i)).toHaveValue(
      "Edited description only",
    );

    // Second save sends rebased command + user's dirty description with expected_revision=8
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));
    await waitFor(() => expect(patchBodies).toHaveLength(2));
    expect(patchBodies[1]).toMatchObject({
      command: "external-new-command",
      description: "Edited description only",
      expected_revision: 8,
    });
  });

  it("aborts stale edit form when server was deleted and recreated with a different instance_id", async () => {
    const user = userEvent.setup();
    let getCount = 0;

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        getCount += 1;
        const base = makeOverviewFixture({ revision: getCount === 1 ? 5 : 9 });
        base.mcp_servers[1] = {
          ...base.mcp_servers[1],
          instance_id: getCount === 1 ? "inst-v1" : "inst-v2-recreated",
          revision: getCount === 1 ? 4 : 9,
        };
        return HttpResponse.json(base);
      }),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", () =>
        HttpResponse.json({ detail: "agent_extensions_revision_conflict" }, { status: 409 }),
      ),
    );

    renderExtensions("/settings/extensions/mcp");
    const row = (await screen.findByRole("heading", { name: "Team Docs Server" })).closest(
      "article",
    )!;
    await user.click(within(row).getByRole("button", { name: "View details" }));
    const detailsDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });
    await user.click(within(detailsDialog).getByRole("button", { name: "Edit" }));

    const editDialog = await screen.findByRole("dialog", { name: /Edit server Team Docs Server/i });
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));

    expect(await within(editDialog).findByRole("alert")).toHaveTextContent(/instance_id changed/i);
    expect(within(editDialog).getByRole("button", { name: "Save server" })).toBeDisabled();
  });

  it("ignores delayed overview GET responses that started before a mutation", async () => {
    const user = userEvent.setup();
    let resolveDelayedRefresh: ((value: Response) => void) | null = null;
    let getCallCount = 0;

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        getCallCount += 1;
        if (getCallCount === 1) {
          const initialFixture = makeOverviewFixture({ revision: 5 });
          initialFixture.mcp_servers[1] = {
            ...initialFixture.mcp_servers[1],
            enabled: false,
            component_enabled: false,
            revision: 2,
          };
          return HttpResponse.json(initialFixture);
        }
        if (getCallCount === 2) {
          return new Promise<Response>(resolve => {
            resolveDelayedRefresh = resolve;
          });
        }
        const updatedFixture = makeOverviewFixture({ revision: 6 });
        updatedFixture.mcp_servers[1] = {
          ...updatedFixture.mcp_servers[1],
          enabled: true,
          component_enabled: true,
          parent_enabled: true,
          revision: 6,
        };
        return HttpResponse.json(updatedFixture);
      }),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", () =>
        HttpResponse.json({
          ...makeOverviewFixture().mcp_servers[1],
          enabled: true,
          component_enabled: true,
          parent_enabled: true,
          revision: 6,
        }),
      ),
    );

    renderExtensions("/settings/extensions/mcp");
    const serverSwitch = await screen.findByRole("switch", {
      name: "Enable server Team Docs Server",
    });
    expect(serverSwitch).toHaveAttribute("aria-checked", "false");

    // Start a manual Refresh that hangs in flight with a stale disabled payload
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(resolveDelayedRefresh).not.toBeNull();

    // Perform a mutation while the Refresh GET is still pending
    await user.click(serverSwitch);
    await waitFor(() => expect(getCallCount).toBeGreaterThanOrEqual(3));

    // Resolve the stale Refresh GET with old revision=1 and component_enabled=false
    const staleFixture = makeOverviewFixture({ revision: 1 });
    staleFixture.mcp_servers[1] = {
      ...staleFixture.mcp_servers[1],
      enabled: false,
      component_enabled: false,
      revision: 1,
    };
    resolveDelayedRefresh!(HttpResponse.json(staleFixture));

    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Enable server Team Docs Server" }),
      ).toHaveAttribute("aria-checked", "true"),
    );
  });

  it("refreshes global overview.revision after item mutations so subsequent create uses the updated expected_revision", async () => {
    const user = userEvent.setup();
    const createBodies: Array<Record<string, unknown>> = [];
    let currentGlobalRevision = 5;

    server.use(
      http.get("*/api/local/agent/extensions", () =>
        HttpResponse.json(makeOverviewFixture({ revision: currentGlobalRevision })),
      ),
      http.patch("*/api/local/agent/extensions/skills/:id", () => {
        currentGlobalRevision = 6;
        return HttpResponse.json({
          ...makeOverviewFixture().skills[0],
          enabled: false,
          component_enabled: false,
          revision: 2,
        });
      }),
      http.post("*/api/local/agent/extensions/mcp/servers", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        createBodies.push(body);
        return HttpResponse.json({
          ...makeOverviewFixture().mcp_servers[1],
          id: "created-server",
          name: String(body.name),
          revision: 7,
        });
      }),
    );

    renderExtensions("/settings/extensions/skills");
    const skillSwitch = await screen.findByRole("switch", {
      name: "Enable skill System diagnostics",
    });
    await user.click(skillSwitch);

    await user.click(screen.getByRole("link", { name: /MCP servers/i }));
    const addBtn = await screen.findByRole("button", { name: "Add server" });
    await waitFor(() => expect(addBtn).not.toBeDisabled());
    await user.click(addBtn);

    const createDialog = await screen.findByRole("dialog", { name: "Add MCP server" });
    await user.type(within(createDialog).getByLabelText("Server name"), "Fresh MCP");
    await user.type(within(createDialog).getByLabelText("Command"), "npx");
    await user.click(within(createDialog).getByRole("button", { name: "Save server" }));

    await waitFor(() => expect(createBodies).toHaveLength(1));
    expect(createBodies[0].expected_revision).toBe(6);
  });

  it("preserves exact MCP argument strings including leading/trailing spaces and empty arguments", async () => {
    const user = userEvent.setup();
    const patchBodies: Array<Record<string, unknown>> = [];

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        const fixture = makeOverviewFixture();
        fixture.mcp_servers[1] = {
          ...fixture.mcp_servers[1],
          args: ["--label", "  spaced  ", ""],
        };
        return HttpResponse.json(fixture);
      }),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchBodies.push(body);
        return HttpResponse.json({
          ...makeOverviewFixture().mcp_servers[1],
          args: body.args as string[],
          revision: 6,
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");
    const row = (await screen.findByRole("heading", { name: "Team Docs Server" })).closest(
      "article",
    )!;
    await user.click(within(row).getByRole("button", { name: "View details" }));
    const detailsDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });
    await user.click(within(detailsDialog).getByRole("button", { name: "Edit" }));

    const editDialog = await screen.findByRole("dialog", { name: /Edit server Team Docs Server/i });
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));

    await waitFor(() => expect(patchBodies).toHaveLength(1));
    expect(patchBodies[0].args).toEqual(["--label", "  spaced  ", ""]);
  });

  it("clears previous ZIP bytes immediately and disables Preview while a replacement ZIP file is still reading", async () => {
    const user = userEvent.setup();
    const previewBodies: Array<Record<string, unknown>> = [];
    const OriginalFileReader = globalThis.FileReader;
    const pendingReaders: Array<{ file: File; instance: FileReader }> = [];

    class ControlledFileReader {
      result: string | ArrayBuffer | null = null;
      onload: (() => void) | null = null;
      onerror: (() => void) | null = null;
      onabort: (() => void) | null = null;
      abort() {
        this.onabort?.();
      }
      readAsDataURL(file: File) {
        if (file.name === "A.zip") {
          this.result = "data:application/zip;base64,QQ==";
          this.onload?.();
        } else {
          pendingReaders.push({ file, instance: this as unknown as FileReader });
        }
      }
      readAsText() {}
    }

    globalThis.FileReader = ControlledFileReader as unknown as typeof FileReader;
    try {
      server.use(
        http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
        http.post("*/api/local/agent/extensions/import/preview", async ({ request }) => {
          const body = (await request.json()) as Record<string, unknown>;
          previewBodies.push(body);
          return HttpResponse.json({
            preview_id: "prev-zip",
            name: "Zip Plugin",
            version: "1.0.0",
            source_format: "ryn_plugin",
            plugins: [],
            skills: [],
            mcp_servers: [],
            warnings: [],
            unsupported_components: [],
            can_import: true,
            expected_revision: 5,
          });
        }),
      );

      renderExtensions("/settings/extensions/plugins");
      await screen.findByRole("heading", { name: "Computer Use" });
      await user.click(screen.getByRole("button", { name: "Import plugin" }));

      const dialog = await screen.findByRole("dialog", { name: "Import plugin package" });
      await user.selectOptions(within(dialog).getByLabelText("Source method"), "zip");

      const fileInput = within(dialog).getByLabelText("Choose ZIP file");
      await user.upload(fileInput, new File(["A"], "A.zip", { type: "application/zip" }));
      expect(within(dialog).getByRole("button", { name: "Preview import" })).not.toBeDisabled();

      // Now select B.zip which reads asynchronously: A.zip bytes must be cleared and button disabled
      await user.upload(fileInput, new File(["B"], "B.zip", { type: "application/zip" }));
      const readingBtn = within(dialog).getByRole("button", { name: /Reading file/i });
      expect(readingBtn).toBeDisabled();
      expect(previewBodies).toHaveLength(0);

      // Complete B.zip read and preview
      expect(pendingReaders).toHaveLength(1);
      (pendingReaders[0].instance as unknown as ControlledFileReader).result =
        "data:application/zip;base64,Qg==";
      (pendingReaders[0].instance as unknown as ControlledFileReader).onload?.();

      const previewBtn = await within(dialog).findByRole("button", { name: "Preview import" });
      expect(previewBtn).not.toBeDisabled();
      await user.click(previewBtn);

      await waitFor(() => expect(previewBodies).toHaveLength(1));
      expect(previewBodies[0]).toEqual({
        kind: "plugin",
        filename: "B.zip",
        zip_base64: "Qg==",
      });
    } finally {
      globalThis.FileReader = OriginalFileReader;
    }
  });

  it("calls native showModal on mount, traps Tab and Shift+Tab focus, supports bounding-rect backdrop click dismissal, and isolates delete confirmation dialog", async () => {
    const user = userEvent.setup();
    const originalShowModal = HTMLDialogElement.prototype.showModal;
    const originalClose = HTMLDialogElement.prototype.close;
    const showModalSpy = vi.fn(function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
    const closeSpy = vi.fn(function (this: HTMLDialogElement) {
      this.removeAttribute("open");
    });
    HTMLDialogElement.prototype.showModal = showModalSpy;
    HTMLDialogElement.prototype.close = closeSpy;

    try {
      server.use(
        http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
      );

      renderExtensions("/settings/extensions/plugins");
      const card = (await screen.findByRole("heading", { name: "Team Toolkit" })).closest(
        "article",
      )!;
      await user.click(within(card).getByRole("button", { name: "View details" }));

      const dialog = (await screen.findByRole("dialog", {
        name: "Team Toolkit",
      })) as HTMLDialogElement;
      expect(showModalSpy).toHaveBeenCalledTimes(1);
      expect(dialog.contains(document.activeElement)).toBe(true);

      // Shift+Tab from the first focusable element (Close button) wraps to the last element inside dialog
      await user.tab({ shift: true });
      expect(dialog.contains(document.activeElement)).toBe(true);

      // Tab from the last element wraps back to the first element inside dialog
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);

      // Opening Uninstall plugin hides the detail dialog and opens the isolated confirmation dialog via showModal
      await user.click(within(dialog).getByRole("button", { name: "Uninstall plugin" }));
      const confirmDialog = (await screen.findByRole("dialog", {
        name: "Uninstall plugin Team Toolkit",
      })) as HTMLDialogElement;
      expect(showModalSpy).toHaveBeenCalledTimes(2);
      expect(screen.queryByRole("dialog", { name: "Team Toolkit" })).not.toBeInTheDocument();

      // Mock bounding rect on confirmDialog to verify native ::backdrop click handling
      vi.spyOn(confirmDialog, "getBoundingClientRect").mockReturnValue({
        left: 100,
        right: 500,
        top: 100,
        bottom: 400,
        width: 400,
        height: 300,
        x: 100,
        y: 100,
        toJSON: () => ({}),
      });

      // Click inside dialog bounding rect must NOT close the dialog
      fireEvent.click(confirmDialog, { clientX: 250, clientY: 200 });
      expect(
        screen.getByRole("dialog", { name: "Uninstall plugin Team Toolkit" }),
      ).toBeInTheDocument();

      // Click outside dialog bounding rect (native ::backdrop retargeted click) closes the dialog
      fireEvent.click(confirmDialog, { clientX: 20, clientY: 20 });
      await waitFor(() =>
        expect(
          screen.queryByRole("dialog", { name: "Uninstall plugin Team Toolkit" }),
        ).not.toBeInTheDocument(),
      );
    } finally {
      HTMLDialogElement.prototype.showModal = originalShowModal;
      HTMLDialogElement.prototype.close = originalClose;
    }
  });

  it("renders derived 'Not tested' status for enabled untested child server with legacy raw status=disabled and separates neutral parent-off notice from connection error retry", async () => {
    const user = userEvent.setup();
    await i18n.changeLanguage("zh-CN");

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        const fixture = makeOverviewFixture();
        return HttpResponse.json({
          ...fixture,
          plugins: fixture.plugins.map((plugin, index) =>
            index === 1
              ? {
                  ...plugin,
                  enabled: true,
                  component_enabled: true,
                  parent_enabled: true,
                  status: "enabled",
                  mcp_servers: plugin.mcp_servers.map((srv) => ({
                    ...srv,
                    enabled: true,
                    component_enabled: true,
                    parent_enabled: true,
                    status: "disabled",
                    status_message: "",
                    tools: [],
                    tools_count: 0,
                    enabled_tools_count: 0,
                    last_tested_at: null,
                  })),
                }
              : plugin,
          ),
          mcp_servers: [
            fixture.mcp_servers[0],
            fixture.mcp_servers[1],
            {
              ...fixture.mcp_servers[1],
              id: "broken-mcp",
              instance_id: "mcp-broken",
              name: "Broken External MCP",
              title: "Broken External MCP",
              enabled: true,
              component_enabled: true,
              parent_enabled: true,
              status: "error",
              status_message: "ECONNREFUSED 127.0.0.1:9988",
            },
          ],
        });
      }),
    );

    renderExtensions("/settings/extensions/plugins");
    const toolkitCard = (await screen.findByRole("heading", { name: "Team Toolkit" })).closest(
      "article",
    )!;
    await user.click(within(toolkitCard).getByRole("button", { name: "查看详情" }));

    const pluginDialog = await screen.findByRole("dialog", { name: "Team Toolkit" });
    expect(within(pluginDialog).getByText("状态：未测试 · 0 个工具")).toBeInTheDocument();
    expect(within(pluginDialog).queryByText(/状态：disabled/i)).not.toBeInTheDocument();
    await user.click(within(pluginDialog).getByRole("button", { name: "关闭对话框" }));

    // Navigate to MCP servers section
    await user.click(screen.getByRole("link", { name: /MCP 服务器/i }));

    // Parent-disabled row shows localized neutral notice without redundant '重试连接', while '测试连接' remains available
    const parentOffRow = (await screen.findByRole("heading", { name: "Team Docs Server" })).closest(
      "article",
    )!;
    expect(
      within(parentOffRow).getByText("父插件当前已停用；启用所属插件后此服务器才会生效。"),
    ).toBeInTheDocument();
    expect(within(parentOffRow).queryByRole("button", { name: "重试连接" })).not.toBeInTheDocument();
    expect(within(parentOffRow).getByRole("button", { name: "测试连接" })).toBeInTheDocument();

    // Actual connection error row preserves verbatim provider diagnostic and shows '重试连接'
    const errorRow = screen.getByRole("heading", { name: "Broken External MCP" }).closest("article")!;
    expect(within(errorRow).getByRole("alert")).toHaveTextContent("ECONNREFUSED 127.0.0.1:9988");
    expect(within(errorRow).getByRole("button", { name: "重试连接" })).toBeInTheDocument();
  });

  it("displays explicit test failure and success inside details dialog for a parent-disabled server while public status remains disabled and switches stay unchanged", async () => {
    const user = userEvent.setup();
    let probeAttempt = 0;
    let currentServerRevision = 4;
    let currentComponentEnabled = true;

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        const fixture = makeOverviewFixture();
        return HttpResponse.json({
          ...fixture,
          mcp_servers: [
            fixture.mcp_servers[0],
            {
              ...fixture.mcp_servers[1],
              enabled: false,
              component_enabled: currentComponentEnabled,
              parent_enabled: false,
              status: "disabled",
              status_message: "Parent plugin is disabled.",
              revision: currentServerRevision,
            },
          ],
        });
      }),
      http.post("*/api/local/agent/extensions/mcp/servers/:id/test", () => {
        probeAttempt += 1;
        const baseServer = makeOverviewFixture().mcp_servers[1];
        if (probeAttempt === 1) {
          return HttpResponse.json({
            server: {
              ...baseServer,
              enabled: false,
              component_enabled: currentComponentEnabled,
              parent_enabled: false,
              status: "disabled",
              status_message: "Parent plugin is disabled.",
              revision: currentServerRevision,
            },
            test_result: {
              status: "error",
              tools_count: 0,
              error: "ECONNREFUSED 127.0.0.1:9988",
            },
          });
        }
        return HttpResponse.json({
          server: {
            ...baseServer,
            enabled: false,
            component_enabled: currentComponentEnabled,
            parent_enabled: false,
            status: "disabled",
            status_message: "Parent plugin is disabled.",
            tools_count: 2,
            last_tested_at: "2026-10-19T05:15:00Z",
            revision: currentServerRevision,
          },
          test_result: {
            status: "connected",
            tools_count: 2,
            error: null,
          },
        });
      }),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", async ({ request }) => {
        const body = (await request.json()) as { enabled: boolean; expected_revision: number };
        currentComponentEnabled = body.enabled;
        currentServerRevision = body.expected_revision + 1;
        return HttpResponse.json({
          ...makeOverviewFixture().mcp_servers[1],
          enabled: false,
          component_enabled: currentComponentEnabled,
          parent_enabled: false,
          status: "disabled",
          status_message: currentComponentEnabled ? "Parent plugin is disabled." : "",
          revision: currentServerRevision,
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");

    const parentOffRow = (await screen.findByRole("heading", { name: "Team Docs Server" })).closest(
      "article",
    )!;
    await user.click(within(parentOffRow).getByRole("button", { name: "View details" }));

    const detailsDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });
    expect(within(detailsDialog).getByText("Parent plugin is disabled.")).toBeInTheDocument();
    expect(within(detailsDialog).queryByRole("button", { name: /Retry/i })).not.toBeInTheDocument();

    const serverSwitch = within(detailsDialog).getByRole("switch", {
      name: "Enable server Team Docs Server",
    });
    expect(serverSwitch).toHaveAttribute("aria-checked", "true");

    // 1. Explicit test fails while parent plugin is disabled (public server.status remains 'disabled')
    await user.click(within(detailsDialog).getByRole("button", { name: "Test connection" }));

    const errorAlert = await within(detailsDialog).findByRole("alert");
    expect(errorAlert).toHaveTextContent("ECONNREFUSED 127.0.0.1:9988");
    expect(within(detailsDialog).getByText("Parent plugin is disabled.")).toBeInTheDocument();
    expect(within(detailsDialog).queryByRole("button", { name: /Retry/i })).not.toBeInTheDocument();
    expect(within(detailsDialog).getByRole("button", { name: "Test connection" })).toBeEnabled();
    expect(serverSwitch).toHaveAttribute("aria-checked", "true");

    // 2. Explicit test succeeds while parent plugin is still disabled
    await user.click(within(detailsDialog).getByRole("button", { name: "Test connection" }));

    await waitFor(() =>
      expect(within(detailsDialog).queryByRole("alert")).not.toBeInTheDocument(),
    );
    const probeBanner = await within(detailsDialog).findByTestId("mcp-explicit-probe-result");
    expect(probeBanner).toHaveTextContent(
      /Connection probe passed \(2 tools discovered\); server remains disabled\./i,
    );
    expect(within(detailsDialog).getByText("Parent plugin is disabled.")).toBeInTheDocument();
    expect(serverSwitch).toHaveAttribute("aria-checked", "true");

    // 3. Changing server configuration/switch clears the prior explicit probe result
    await user.click(serverSwitch);
    await waitFor(() =>
      expect(
        within(detailsDialog).queryByTestId("mcp-explicit-probe-result"),
      ).not.toBeInTheDocument(),
    );

    // Close dialog and verify parent plugin switch remains off (both on card and in plugin details)
    await user.click(within(detailsDialog).getByRole("button", { name: "Close dialog" }));
    await user.click(screen.getByRole("link", { name: /Plugins/i }));
    const parentPluginCardSwitch = await screen.findByRole("switch", {
      name: "Enable Team Toolkit",
    });
    expect(parentPluginCardSwitch).toHaveAttribute("aria-checked", "false");

    const toolkitCard = screen.getByRole("heading", { name: "Team Toolkit" }).closest("article")!;
    await user.click(within(toolkitCard).getByRole("button", { name: "View details" }));
    const pluginDialog = await screen.findByRole("dialog", { name: "Team Toolkit" });
    expect(
      within(pluginDialog).getByRole("switch", {
        name: "Enable plugin package Team Toolkit",
      }),
    ).toHaveAttribute("aria-checked", "false");
  });

  it("clears row loading state when closing details dialog during a pending probe without clearing a newer probe's busy state or leaking stale results", async () => {
    const user = userEvent.setup();
    const pendingProbes: Array<(res: Response) => void> = [];

    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(makeOverviewFixture())),
      http.post("*/api/local/agent/extensions/mcp/servers/:id/test", () => {
        return new Promise<Response>(resolve => {
          pendingProbes.push(resolve);
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");

    const docsRow = (await screen.findByRole("heading", { name: "Team Docs Server" })).closest(
      "article",
    )!;
    await user.click(within(docsRow).getByRole("button", { name: "View details" }));

    const detailsDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });
    await user.click(within(detailsDialog).getByRole("button", { name: "Test connection" }));
    expect(pendingProbes).toHaveLength(1);

    // Close dialog normally via visible Close button while probe #1 is still in flight
    await user.click(within(detailsDialog).getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("dialog", { name: "Team Docs Server" })).not.toBeInTheDocument();

    // Resolve probe #1: row must NOT remain stuck in Testing… and button must be enabled
    pendingProbes[0](
      HttpResponse.json({
        server: {
          ...makeOverviewFixture().mcp_servers[1],
          revision: 4,
        },
        test_result: {
          status: "error",
          tools_count: 0,
          error: "Stale retired probe error",
        },
      }),
    );

    await waitFor(() =>
      expect(within(docsRow).getByRole("button", { name: "Test connection" })).toBeEnabled(),
    );
    expect(within(docsRow).queryByRole("button", { name: "Testing…" })).not.toBeInTheDocument();

    // Reopening details must not display the retired probe result
    await user.click(within(docsRow).getByRole("button", { name: "View details" }));
    const reopenedDialog = await screen.findByRole("dialog", { name: "Team Docs Server" });
    expect(
      within(reopenedDialog).queryByTestId("mcp-explicit-probe-result"),
    ).not.toBeInTheDocument();
    expect(within(reopenedDialog).queryByText(/Stale retired probe error/i)).not.toBeInTheDocument();

    // Start probe #2 in dialog, close dialog, immediately start probe #3 on row:
    // resolving retired probe #2 must NOT clear probe #3's active Testing… state
    await user.click(within(reopenedDialog).getByRole("button", { name: "Test connection" }));
    expect(pendingProbes).toHaveLength(2);
    await user.click(within(reopenedDialog).getByRole("button", { name: "Close dialog" }));

    await user.click(within(docsRow).getByRole("button", { name: "Test connection" }));
    expect(pendingProbes).toHaveLength(3);
    expect(within(docsRow).getByRole("button", { name: "Testing…" })).toBeDisabled();

    // Resolve retired probe #2 while probe #3 is still pending
    pendingProbes[1](
      HttpResponse.json({
        server: makeOverviewFixture().mcp_servers[1],
        test_result: { status: "connected", tools_count: 1 },
      }),
    );
    expect(within(docsRow).getByRole("button", { name: "Testing…" })).toBeDisabled();

    // Resolve active probe #3: row transitions back to enabled Test connection
    pendingProbes[2](
      HttpResponse.json({
        server: makeOverviewFixture().mcp_servers[1],
        test_result: { status: "connected", tools_count: 1 },
      }),
    );
    await waitFor(() =>
      expect(within(docsRow).getByRole("button", { name: "Test connection" })).toBeEnabled(),
    );
  });

  it("prevents a delayed probe response from an older server instance from overwriting a replacement instance even before post-probe overview GET completes", async () => {
    const user = userEvent.setup();
    let getCount = 0;
    let resolveProbeA: ((res: Response) => void) | null = null;
    let resolvePostProbeGet: ((res: Response) => void) | null = null;

    const baseFixture = makeOverviewFixture();
    const serverInstanceA = {
      ...baseFixture.mcp_servers[1],
      id: "team-docs",
      instance_id: "instance-a",
      config_generation: 1,
      name: "Old instance A",
      title: "Old instance A",
      revision: 1,
    };
    const serverInstanceB = {
      ...baseFixture.mcp_servers[1],
      id: "team-docs",
      instance_id: "instance-b",
      config_generation: 3,
      name: "Replacement instance B",
      title: "Replacement instance B",
      revision: 3,
    };

    server.use(
      http.get("*/api/local/agent/extensions", () => {
        getCount += 1;
        if (getCount === 1) {
          return HttpResponse.json({
            ...baseFixture,
            revision: 1,
            mcp_servers: [baseFixture.mcp_servers[0], serverInstanceA],
          });
        }
        if (getCount === 2) {
          return HttpResponse.json({
            ...baseFixture,
            revision: 3,
            mcp_servers: [baseFixture.mcp_servers[0], serverInstanceB],
          });
        }
        return new Promise<Response>(resolve => {
          resolvePostProbeGet = resolve;
        });
      }),
      http.post("*/api/local/agent/extensions/mcp/servers/:id/test", () => {
        return new Promise<Response>(resolve => {
          resolveProbeA = resolve;
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");

    const rowA = (await screen.findByRole("heading", { name: "Old instance A" })).closest(
      "article",
    )!;
    await user.click(within(rowA).getByRole("button", { name: "View details" }));

    const dialogA = await screen.findByRole("dialog", { name: "Old instance A" });
    await user.click(within(dialogA).getByRole("button", { name: "Test connection" }));
    expect(resolveProbeA).not.toBeNull();

    // Close details and click Refresh to load Replacement instance B
    await user.click(within(dialogA).getByRole("button", { name: "Close dialog" }));
    await user.click(screen.getByRole("button", { name: "Refresh" }));

    expect(await screen.findByRole("heading", { name: "Replacement instance B" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Old instance A" })).not.toBeInTheDocument();

    // Resolve Old instance A's delayed probe response (rev 2) while post-probe GET #3 is held pending
    resolveProbeA!(
      HttpResponse.json({
        server: {
          ...serverInstanceA,
          revision: 2,
        },
        test_result: {
          status: "connected",
          tools_count: 1,
        },
      }),
    );

    await waitFor(() => expect(getCount).toBe(3));
    expect(resolvePostProbeGet).not.toBeNull();

    // Observe intermediate DOM state BEFORE post-probe GET resolves: Replacement instance B must remain
    expect(screen.getByRole("heading", { name: "Replacement instance B" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Old instance A" })).not.toBeInTheDocument();

    // Complete post-probe GET
    resolvePostProbeGet!(
      HttpResponse.json({
        ...baseFixture,
        revision: 3,
        mcp_servers: [baseFixture.mcp_servers[0], serverInstanceB],
      }),
    );
    expect(await screen.findByRole("heading", { name: "Replacement instance B" })).toBeInTheDocument();
  });

  it("renders Codex MCP cards (Servers, From plugins, MCP Apps), supports env_vars passthrough, env_http_headers, per-row arguments, and Clear all app data", async () => {
    const user = userEvent.setup();
    let currentOverview = makeOverviewFixture();
    let lastCreatePayload: Record<string, unknown> | null = null;
    let clearDataCalled = 0;

    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(currentOverview)),
      http.post("*/api/local/agent/extensions/mcp/servers", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        lastCreatePayload = body;
        const created = {
          id: "codex-custom-stdio",
          instance_id: "inst-codex-stdio",
          revision: 9,
          config_generation: 9,
          name: String(body.name || "Codex Custom"),
          title: String(body.name || "Codex Custom"),
          description: String(body.description || ""),
          transport: (body.transport as "stdio" | "streamable_http") || "stdio",
          builtin: false,
          deletable: true,
          plugin_id: null,
          computer_access: false,
          enabled: Boolean(body.enabled),
          component_enabled: Boolean(body.enabled),
          parent_enabled: true,
          command: String(body.command || ""),
          args: Array.isArray(body.args) ? body.args.map(String) : [],
          cwd: String(body.cwd || ""),
          env_keys: [],
          masked_env: {},
          env_vars: Array.isArray(body.env_vars) ? body.env_vars.map(String) : [],
          url: String(body.url || ""),
          bearer_token_env: String(body.bearer_token_env || ""),
          header_keys: [],
          masked_headers: {},
          env_http_headers:
            body.env_http_headers && typeof body.env_http_headers === "object"
              ? (body.env_http_headers as Record<string, string>)
              : {},
          startup_timeout_ms: 10000,
          tool_timeout_ms: 30000,
          enabled_tools: null,
          disabled_tools: [],
          read_only_tools: [],
          tools: [],
          tools_count: 0,
          enabled_tools_count: 0,
          has_secrets: false,
          status: "NeedsTest",
          status_message: "",
          last_tested_at: null,
        };
        currentOverview = {
          ...currentOverview,
          revision: 9,
          mcp_servers: [
            ...currentOverview.mcp_servers,
            created as unknown as (typeof currentOverview.mcp_servers)[number],
          ],
        };
        return HttpResponse.json(created);
      }),
      http.post("*/api/local/agent/extensions/mcp/apps/clear-data", () => {
        clearDataCalled += 1;
        return HttpResponse.json({
          cleared: true,
          overview: currentOverview,
        });
      }),
    );

    renderExtensions("/settings/extensions/mcp");

    expect(await screen.findByRole("heading", { name: "Servers" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "From plugins" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "MCP Apps" })).toBeInTheDocument();

    // Clear all app data button in MCP Apps card
    await user.click(screen.getByRole("button", { name: "Clear all app data" }));
    await waitFor(() => expect(clearDataCalled).toBe(1));
    expect(await screen.findByText("Cleared MCP app data")).toBeInTheDocument();

    // Open Add MCP server via Codex "Connect to a custom MCP" button
    await user.click(screen.getByRole("button", { name: "Connect to a custom MCP" }));
    const addDialog = await screen.findByRole("dialog", { name: "Add MCP server" });

    await user.type(within(addDialog).getByLabelText("Server name"), "Codex Custom");
    await user.type(within(addDialog).getByLabelText("Command"), "openai-dev-mcp");

    // Add argument via Codex "+ Add argument" button
    await user.click(within(addDialog).getByRole("button", { name: "Add argument" }));
    await user.type(within(addDialog).getByLabelText("Argument 1"), "serve-sqlite");

    // Add passthrough env var via Codex "+ Add passthrough variable" button
    await user.click(within(addDialog).getByRole("button", { name: "Add passthrough variable" }));
    await user.type(
      within(addDialog).getByLabelText("Passthrough environment variable 1"),
      "OPENAI_API_KEY",
    );

    await user.click(within(addDialog).getByRole("button", { name: "Save server" }));

    await waitFor(() => expect(lastCreatePayload).not.toBeNull());
    expect(lastCreatePayload).toMatchObject({
      name: "Codex Custom",
      transport: "stdio",
      command: "openai-dev-mcp",
      args: ["serve-sqlite"],
      env_vars: ["OPENAI_API_KEY"],
    });
    expect(await screen.findByRole("heading", { name: "Codex Custom" })).toBeInTheDocument();
  });

  it("renders Codex Skills page with Scope filter tabs, Recommended skills one-click install, Refresh discovery, and reference file preview", async () => {
    const user = userEvent.setup();
    let currentOverview = makeOverviewFixture();
    let refreshCalled = 0;
    let installedRecommendedPayload: Record<string, unknown> | null = null;
    let recommendedInstalled = false;

    const makeRecommendedResponse = () => ({
      count: 1,
      skills: [
        {
          id: "playwright",
          name: "playwright",
          title: "Playwright",
          description: "Automate real browsers from the terminal with Playwright CLI.",
          short_description: "Automate real browsers with Playwright",
          version: "1.0.0",
          repo_path: "skills/.curated/playwright",
          source_dir: "C:\\Users\\test\\.codex\\vendor_imports\\skills\\skills\\.curated\\playwright",
          content: "# Playwright Skill\nRun automated browser workflows.",
          installed: recommendedInstalled,
          interface: {
            display_name: "Playwright",
            short_description: "Automate real browsers with Playwright",
            icon_small: null,
            icon_large: null,
            brand_color: "#2EAD33",
            default_prompt: "Use $playwright to automate this browser check.",
          },
          policy: {
            allow_implicit_invocation: true,
          },
          files: [{ path: "SKILL.md", size: 49 }],
        },
      ],
    });

    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(currentOverview)),
      http.get("*/api/local/agent/extensions/skills/recommended", () =>
        HttpResponse.json(makeRecommendedResponse()),
      ),
      http.post("*/api/local/agent/extensions/skills/refresh", () => {
        refreshCalled += 1;
        return HttpResponse.json({
          synced_skill_ids: ["system-diagnostics"],
          overview: currentOverview,
        });
      }),
      http.post("*/api/local/agent/extensions/skills/recommended/install", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        installedRecommendedPayload = body;
        recommendedInstalled = true;
        const newSkill = {
          id: "playwright",
          instance_id: "skill-playwright",
          name: "playwright",
          title: "Playwright",
          description: "Automate real browsers from the terminal with Playwright CLI.",
          short_description: "Automate real browsers with Playwright",
          summary: "Automate real browsers with Playwright",
          version: "1.0.0",
          content: "# Playwright Skill\nRun automated browser workflows.",
          builtin: false,
          deletable: true,
          enabled: true,
          component_enabled: true,
          parent_enabled: true,
          source_format: "codex_skill",
          scope: "user",
          path: "C:\\Users\\test\\.codex\\skills\\playwright\\SKILL.md",
          plugin_id: null,
          interface: {
            display_name: "Playwright",
            short_description: "Automate real browsers with Playwright",
            icon_small: null,
            icon_large: null,
            brand_color: "#2EAD33",
            default_prompt: "Use $playwright to automate this browser check.",
          },
          policy: {
            allow_implicit_invocation: true,
          },
          files: [{ path: "SKILL.md", size: 49 }],
          warnings: [],
          revision: 6,
        };
        currentOverview = {
          ...currentOverview,
          revision: 6,
          skills: [
            ...currentOverview.skills,
            newSkill as unknown as (typeof currentOverview.skills)[number],
          ],
        };
        return HttpResponse.json({
          skill: newSkill,
          overview: currentOverview,
        });
      }),
      http.get("*/api/local/agent/extensions/skills/:id/files", ({ params, request }) => {
        const url = new URL(request.url);
        const relPath = url.searchParams.get("path") || "SKILL.md";
        return HttpResponse.json({
          skill_id: params.id,
          path: relPath,
          size: 32,
          content: "# Reference Checklist\n1. Verify boundaries.",
        });
      }),
    );

    renderExtensions("/settings/extensions/skills");

    expect(await screen.findByRole("heading", { name: /^Installed/i })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: /^Recommended/i })).toBeInTheDocument();

    // Filter by Scope tab ("System" shows system-diagnostics and hides plugin-scoped team-review)
    const scopeGroup = screen.getByRole("group", { name: "Skill scope filter" });
    await user.click(within(scopeGroup).getByRole("button", { name: /^System/i }));
    expect(screen.getByRole("heading", { name: "System diagnostics" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Team Review Checklist" })).not.toBeInTheDocument();

    await user.click(within(scopeGroup).getByRole("button", { name: /^All/i }));
    expect(screen.getByRole("heading", { name: "Team Review Checklist" })).toBeInTheDocument();

    // Click Refresh in the CodexSkillsView toolbar to trigger /skills/refresh
    const refreshButtons = screen.getAllByRole("button", { name: "Refresh" });
    await user.click(refreshButtons[refreshButtons.length - 1]);
    await waitFor(() => expect(refreshCalled).toBe(1));

    // One-click install a Recommended skill (Playwright)
    await user.click(screen.getByRole("button", { name: "Install skill Playwright" }));
    await waitFor(() => expect(installedRecommendedPayload).not.toBeNull());
    expect(installedRecommendedPayload).toMatchObject({
      skill_id: "playwright",
      repo_path: "skills/.curated/playwright",
    });
    expect(await screen.findByRole("heading", { name: "Playwright" })).toBeInTheDocument();

    // Open Team Review Checklist details and click reference file chip to preview its contents
    const reviewCard = screen
      .getByRole("heading", { name: "Team Review Checklist" })
      .closest("article")!;
    await user.click(within(reviewCard).getByRole("button", { name: "View details" }));
    const detailDialog = await screen.findByRole("dialog", { name: "Team Review Checklist" });
    await user.click(within(detailDialog).getByRole("button", { name: /references\/checklist\.md/i }));
    expect(await within(detailDialog).findByText(/# Reference Checklist/i)).toBeInTheDocument();
  });
});
