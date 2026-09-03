import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import LocalAISetup from "./LocalAISetup";

function renderSetup(client = makeFixtureNodeClient()) {
  const context: AppOutletContext = {
    client,
    node: { node_name: "Test", peer_id: "self", daemon_running: true, registry: "connected",
      peer_count: 0, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 1 },
    registry: { status: "connected", url: "local" },
    peers: [],
    refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(),
    notify: vi.fn(),
  };
  const user = userEvent.setup();
  render(
    <MemoryRouter initialEntries={["/local-ai"]}>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route path="local-ai" element={<LocalAISetup />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { client, user };
}

describe("LocalAISetup", () => {
  it("requires license confirmation, tracks real stages, and shows a first answer", async () => {
    const { user } = renderSetup();
    expect(await screen.findByRole("heading", { name: "Enable AI on this device" })).toBeInTheDocument();
    const install = screen.getByRole("button", { name: "Install and verify local AI" });
    expect(install).toBeDisabled();
    await user.click(screen.getByRole("checkbox"));
    expect(install).toBeEnabled();
    await user.click(install);

    expect(await screen.findByRole("progressbar", { name: "Local AI setup progress" })).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Local AI is ready" }, { timeout: 6000 })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Get my first answer" }));
    expect(await screen.findByText("Ryn is running privately on this device.")).toBeInTheDocument();
  }, 10_000);

  it("does not claim one-click support when no reviewed runtime is published", async () => {
    const client = makeFixtureNodeClient();
    const original = await client.getLocalAIRecommendations();
    vi.spyOn(client, "getLocalAIRecommendations").mockResolvedValue({
      ...original,
      runtime_support: { ...original.runtime_support, managed: false },
      recommended_profile: null,
      profiles: original.profiles.map((profile) => ({
        ...profile,
        supported: false,
        unsupported_reason: "A reviewed native runtime is not published for this platform.",
      })),
    });
    renderSetup(client);

    expect(await screen.findByRole("heading", { name: "No reviewed profile available" })).toBeInTheDocument();
    expect(screen.getByText(/No reviewed native runtime fits this platform/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Install and verify local AI" })).not.toBeInTheDocument();
  });
});
