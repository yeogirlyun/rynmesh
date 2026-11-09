import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { server } from "../test/server";
import RynAgentReadOnlyHints from "./RynAgentReadOnlyHints";

function mcpServer(overrides: Record<string, unknown> = {}) {
  return { id: "company", name: "Company data", revision: 4, instance_id: "inst-1", discovery_revision: 2,
    transport: "streamable_http", builtin: false, deletable: true, enabled: true, component_enabled: true, parent_enabled: true,
    computer_access: false, tools: [], server_hint_candidates: [
      { name: "query_cash", title: "Cash", schema_sha256: "a".repeat(64) },
      { name: "query_bank", title: "Bank", schema_sha256: "b".repeat(64) }], ...overrides };
}

it("asks once to let declared read-only plugin tools run and submits the exact tools", async () => {
  localStorage.clear();
  let applied: unknown = null;
  server.use(
    http.get("*/api/local/agent/extensions", () => HttpResponse.json({ revision: 1, desktop_access: false, plugins: [], skills: [], mcp_servers: [mcpServer()] })),
    http.post("*/api/local/agent/extensions/mcp/servers/company/apply-server-hints", async ({ request }) => {
      applied = await request.json();
      return HttpResponse.json(mcpServer({ revision: 5, server_hint_candidates: [] }));
    }),
  );
  const user = userEvent.setup();
  render(<RynAgentReadOnlyHints />);
  await screen.findByText(/declares 2 tools read-only/);
  await user.click(screen.getByRole("button", { name: "Allow" }));
  expect(applied).toEqual({ expected_revision: 4, instance_id: "inst-1", discovery_revision: 2,
    tools: [{ name: "query_cash", schema_sha256: "a".repeat(64) }, { name: "query_bank", schema_sha256: "b".repeat(64) }] });
  expect(screen.queryByText(/read-only/)).not.toBeInTheDocument();
});

it("stays dismissed for the same plugin discovery", async () => {
  localStorage.clear();
  server.use(http.get("*/api/local/agent/extensions", () => HttpResponse.json({ revision: 1, desktop_access: false, plugins: [], skills: [], mcp_servers: [mcpServer()] })));
  const user = userEvent.setup();
  const first = render(<RynAgentReadOnlyHints />);
  await screen.findByText(/read-only/);
  await user.click(screen.getByRole("button", { name: "Not now" }));
  expect(screen.queryByText(/read-only/)).not.toBeInTheDocument();
  first.unmount();
  render(<RynAgentReadOnlyHints />);
  await new Promise(resolve => setTimeout(resolve, 50));
  expect(screen.queryByText(/read-only/)).not.toBeInTheDocument();
});
