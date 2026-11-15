import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import AgentExtensions from "./AgentExtensions";

describe("S08-I12 AgentExtensions invalid tool schema notice", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh-CN");
  });

  it("displays 'schema 不合规，未提供给模型' on MCP tools marked schema_invalid", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("*/api/local/agent/extensions", () =>
        HttpResponse.json({
          revision: 3,
          updated_at: "2026-11-15T12:00:00Z",
          desktop_access: false,
          windows_supported: true,
          plugins: [],
          skills: [],
          mcp_servers: [
            {
              id: "demo-mcp",
              instance_id: "inst-demo-mcp",
              name: "Demo MCP Server",
              title: "Demo MCP Server",
              description: "Server with valid and invalid schema tools",
              transport: "stdio",
              builtin: false,
              deletable: true,
              plugin_id: null,
              computer_access: false,
              enabled: true,
              component_enabled: true,
              parent_enabled: true,
              command: "python",
              args: ["-V"],
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
              read_only_tools: [],
              tools: [
                {
                  name: "bad_schema_tool",
                  model_tool_name: "mcp__demo_mcp__bad_schema_tool__11111111",
                  title: "Bad Schema Tool",
                  description: "Top-level schema is not object",
                  enabled: false,
                  component_enabled: true,
                  schema_invalid: true,
                  owner_read_only: false,
                  effective_effect: "write",
                  effective_risk: "high",
                  annotations: { readOnlyHint: false },
                },
                {
                  name: "good_schema_tool",
                  model_tool_name: "mcp__demo_mcp__good_schema_tool__22222222",
                  title: "Good Schema Tool",
                  description: "Valid object schema",
                  enabled: true,
                  component_enabled: true,
                  schema_invalid: false,
                  owner_read_only: false,
                  effective_effect: "write",
                  effective_risk: "high",
                  annotations: { readOnlyHint: false },
                },
              ],
              tools_count: 2,
              enabled_tools_count: 1,
              status: "ready",
              status_message: "",
              last_tested_at: "2026-11-15T12:00:00Z",
              revision: 3,
            },
          ],
        }),
      ),
    );

    render(
      <MemoryRouter initialEntries={["/settings/extensions/mcp"]}>
        <Routes>
          <Route path="/settings/extensions/:section" element={<AgentExtensions />} />
        </Routes>
      </MemoryRouter>,
    );

    const card = (await screen.findByRole("heading", { name: "Demo MCP Server" })).closest("article")!;
    await user.click(within(card).getByRole("button", { name: "查看详情" }));
    const dialog = await screen.findByRole("dialog", { name: "Demo MCP Server" });

    const badRow = within(dialog).getByText("bad_schema_tool").closest(".ae-tool-row") as HTMLElement;
    const goodRow = within(dialog).getByText("good_schema_tool").closest(".ae-tool-row") as HTMLElement;

    expect(within(badRow).getByText("schema 不合规，未提供给模型")).toBeInTheDocument();
    expect(within(goodRow).queryByText("schema 不合规，未提供给模型")).not.toBeInTheDocument();
  });
});
