import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { normalizePlugin } from "../domain/agentExtensions";
import { peerProviders } from "../domain/peerProviders";
import { PeerProviderSettings } from "./PeerProviderSettings";

it.each([
  ["agent_mcp_auth_required", "Sign in again"],
  ["agent_mcp_oauth_server_disabled", "Enable the company workspace plugin first"],
  ["peer_provider_companion_missing", "Enable the company workspace plugin first"],
  ["agent_mcp_token_unavailable", "Sign in again"],
])("S13-I30 displays %s from the status response", async (code, message) => {
  const raw = { plugin_id: "fixture", status: "signed_out", bound: false, selected_service_ids: [], sign_in_error: code };
  server.use(http.get("http://localhost/api/local/peer-providers/fixture/status", () => HttpResponse.json(raw)));
  expect((await peerProviders.status("fixture")).sign_in_error).toBe(code);
  render(<PeerProviderSettings plugin={normalizePlugin({ id: "fixture", enabled: true })} />);
  expect(await screen.findByText(message)).toBeInTheDocument();
});
