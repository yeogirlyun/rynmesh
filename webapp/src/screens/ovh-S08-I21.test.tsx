import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import AgentExtensions from "./AgentExtensions";
import { normalizeMcpServer } from "../domain/agentExtensions";

describe("S08-I21 inherit_user_home option and risk notice", () => {
  it("normalizes inherit_user_home to false by default and preserves true", () => {
    const defSrv = normalizeMcpServer({ id: "demo", name: "Demo", transport: "stdio" });
    expect((defSrv as unknown as Record<string, unknown>).inherit_user_home).toBe(false);

    const optInSrv = normalizeMcpServer({
      id: "demo",
      name: "Demo",
      transport: "stdio",
      inherit_user_home: true,
    });
    expect((optInSrv as unknown as Record<string, unknown>).inherit_user_home).toBe(true);
  });

  it("renders inherit_user_home checkbox with explicit risk warning in STDIO server modal and sends it on save", async () => {
    await i18n.changeLanguage("en");
    const user = userEvent.setup();
    const patchBodies: Array<Record<string, unknown>> = [];

    server.use(
      http.get("*/api/local/agent/extensions", () =>
        HttpResponse.json({
          revision: 3,
          desktop_access: false,
          windows_supported: true,
          plugins: [],
          skills: [],
          mcp_servers: [
            {
              id: "local-npx",
              instance_id: "inst-1",
              name: "Local NPX",
              title: "Local NPX",
              description: "STDIO server",
              transport: "stdio",
              builtin: false,
              deletable: true,
              plugin_id: null,
              computer_access: false,
              inherit_user_home: false,
              enabled: true,
              component_enabled: true,
              parent_enabled: true,
              command: "npx",
              args: ["-y", "@demo/mcp"],
              cwd: "",
              env_keys: [],
              masked_env: {},
              url: "",
              bearer_token_env: "",
              header_keys: [],
              masked_headers: {},
              has_secrets: false,
              startup_timeout_ms: 10000,
              tool_timeout_ms: 60000,
              enabled_tools: null,
              disabled_tools: [],
              read_only_tools: [],
              tools: [],
              tools_count: 0,
              enabled_tools_count: 0,
              status: "ready",
              status_message: "",
              revision: 3,
            },
          ],
        }),
      ),
      http.patch("*/api/local/agent/extensions/mcp/servers/:id", async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>;
        patchBodies.push(body);
        return HttpResponse.json({
          id: "local-npx",
          instance_id: "inst-1",
          name: "Local NPX",
          transport: "stdio",
          command: "npx",
          args: ["-y", "@demo/mcp"],
          inherit_user_home: Boolean(body.inherit_user_home),
          enabled: true,
          component_enabled: true,
          parent_enabled: true,
          revision: 4,
        });
      }),
    );

    render(
      <MemoryRouter initialEntries={["/settings/extensions/mcp"]}>
        <Routes>
          <Route path="/settings/extensions/:section" element={<AgentExtensions />} />
        </Routes>
      </MemoryRouter>,
    );

    const row = (await screen.findByRole("heading", { name: "Local NPX" })).closest("article")!;
    await user.click(within(row).getByRole("button", { name: "View details" }));
    const detailsDialog = await screen.findByRole("dialog", { name: "Local NPX" });
    await user.click(within(detailsDialog).getByRole("button", { name: "Edit" }));

    const editDialog = await screen.findByRole("dialog", { name: /Edit server Local NPX/i });
    const advSummary = within(editDialog).getByText(/Advanced settings/i);
    await user.click(advSummary);
    const homeCheckbox = within(editDialog).getByRole("checkbox", {
      name: /inherit_user_home/i,
    });
    expect(homeCheckbox).not.toBeChecked();
    expect(within(editDialog).getByText(/risk:/i)).toBeInTheDocument();

    await user.click(homeCheckbox);
    await user.click(within(editDialog).getByRole("button", { name: "Save server" }));

    expect(patchBodies.length).toBe(1);
    expect(patchBodies[0].inherit_user_home).toBe(true);
  });
});
