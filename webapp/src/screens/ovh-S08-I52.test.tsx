import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import AgentExtensions from "./AgentExtensions";
import { McpAuthSettings } from "./McpAuthSettings";
import { normalizeMcpServer } from "../domain/agentExtensions";

describe("S08-I52 OAuth credentials reset notice", () => {
  it("shows reset notice in McpAuthSettings when oauth.error is agent_mcp_oauth_credentials_reset", async () => {
    await i18n.changeLanguage("zh-CN");
    const srv = normalizeMcpServer({
      id: "remote-oauth",
      instance_id: "inst-oauth-1",
      revision: 2,
      config_generation: 1,
      name: "Remote OAuth",
      transport: "streamable_http",
      url: "https://mcp.example/http",
      enabled: true,
      auth: { mode: "oauth", client_id: null, scopes: [], callback_port: null },
      oauth: {
        state: "logged_out",
        error: "agent_mcp_oauth_credentials_reset",
      },
      status: "NeedsAuth",
      status_message: "agent_mcp_oauth_credentials_reset",
    });

    render(<McpAuthSettings server={srv} onRefresh={vi.fn()} />);
    expect(screen.getByText("登录信息已损坏并重置，请重新登录")).toBeInTheDocument();
  });

  it("shows reset notice on AgentExtensions MCP server list when credentials were reset", async () => {
    await i18n.changeLanguage("zh-CN");
    server.use(
      http.get("*/api/local/agent/extensions", () =>
        HttpResponse.json({
          revision: 4,
          desktop_access: false,
          windows_supported: true,
          plugins: [],
          skills: [],
          mcp_servers: [
            {
              id: "remote-oauth",
              instance_id: "inst-oauth-1",
              name: "Remote OAuth",
              title: "Remote OAuth",
              description: "OAuth MCP server",
              transport: "streamable_http",
              builtin: false,
              deletable: true,
              plugin_id: null,
              computer_access: false,
              enabled: true,
              component_enabled: true,
              parent_enabled: true,
              command: "",
              args: [],
              cwd: "",
              env_keys: [],
              masked_env: {},
              url: "https://mcp.example/http",
              bearer_token_env: "",
              header_keys: [],
              masked_headers: {},
              auth: { mode: "oauth", client_id: null, scopes: [], callback_port: null },
              oauth: {
                state: "logged_out",
                error: "agent_mcp_oauth_credentials_reset",
              },
              has_secrets: false,
              startup_timeout_ms: 10000,
              tool_timeout_ms: 60000,
              enabled_tools: null,
              disabled_tools: [],
              read_only_tools: [],
              tools: [],
              tools_count: 0,
              enabled_tools_count: 0,
              status: "NeedsAuth",
              status_message: "agent_mcp_oauth_credentials_reset",
              revision: 4,
            },
          ],
        }),
      ),
    );

    render(
      <MemoryRouter initialEntries={["/extensions/mcp"]}>
        <Routes>
          <Route path="/extensions/:section" element={<AgentExtensions />} />
        </Routes>
      </MemoryRouter>,
    );

    expect(
      await screen.findByText("登录信息已损坏并重置，请重新登录"),
    ).toBeInTheDocument();
  });
});
