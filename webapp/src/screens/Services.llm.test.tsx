import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import Services from "./Services";

function renderServices(options: { discoveryFailure?: boolean } = {}) {
  const client = makeFixtureNodeClient();
  if (options.discoveryFailure) {
    client.listLLMServices = vi.fn(async () => {
      throw new Error("LLM discovery unavailable");
    });
  }
  const submit = vi.spyOn(client, "submitLLMOrder");
  const context: AppOutletContext = {
    client,
    node: {
      node_name: "Test Ryn", peer_id: "peer:test", daemon_running: true,
      registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0,
      pending_recs: 0, version: "test", uptime_seconds: 60,
    },
    registry: { status: "connected", url: "https://registry.test" },
    peers: [],
    refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(),
    notify: vi.fn(),
  };
  const result = render(
    <MemoryRouter>
      <Routes>
        <Route element={<Outlet context={context} />}>
          <Route index element={<Services />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
  return { ...result, client, submit, user: userEvent.setup() };
}

describe("Services local LLM flow", () => {
  it("uses the production network default and submits the selected transport policy", async () => {
    const { user, submit } = renderServices();

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByLabelText("Discovery network")).toHaveValue("rynmesh-main");
    await user.selectOptions(screen.getByLabelText("Transport policy"), "p2p");
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));

    expect(await screen.findByText(/Strict P2P connection is in progress/)).toBeInTheDocument();
    await waitFor(() => expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      network_id: "rynmesh-main",
      transport: "p2p",
    })));
    expect(await screen.findByText("ice_udp_direct")).toBeInTheDocument();
  });

  it("keeps the Services screen usable when LLM discovery fails", async () => {
    renderServices({ discoveryFailure: true });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    expect(screen.getByText(/Service discovery failed: LLM discovery unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled();
  });

  it("configures a local API without publishing it automatically", async () => {
    const { client, user } = renderServices();
    const setup = vi.spyOn(client, "setupLLMService");
    const publish = vi.spyOn(client, "publishLLMService");

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Package ID"));
    await user.type(screen.getByLabelText("Package ID"), "my-local-api");
    await user.clear(screen.getByLabelText("Local API URL"));
    await user.type(screen.getByLabelText("Local API URL"), "http://127.0.0.1:9999");
    await user.click(screen.getByRole("button", { name: "Configure and run self-test" }));

    await waitFor(() => expect(setup).toHaveBeenCalledWith(expect.objectContaining({
      mode: "openai-compatible",
      package_id: "my-local-api",
      base_url: "http://127.0.0.1:9999",
      accept_risk: false,
    })));
    expect(publish).not.toHaveBeenCalled();
  });

  it("polls an asynchronous order and sends cancellation", async () => {
    const { client, user } = renderServices();
    let cancelled = false;
    client.submitLLMOrder = vi.fn(async () => ({ task_id: "task_async", state: "queued" }));
    client.getLLMOrder = vi.fn(async () => ({
      task_id: "task_async", state: cancelled ? "cancelled" : "running",
    }));
    client.cancelLLMOrder = vi.fn(async () => {
      cancelled = true;
      return { task_id: "task_async", state: "cancelled" };
    });

    expect(await screen.findByRole("heading", { name: "Ryn job capacity" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Place encrypted order" }));
    await user.click(await screen.findByRole("button", { name: "Cancel task" }));

    expect(client.cancelLLMOrder).toHaveBeenCalledWith("task_async");
    expect(await screen.findByText(/cancellation requested/)).toBeInTheDocument();
    await waitFor(() => expect(client.getLLMOrder).toHaveBeenCalledWith("task_async"), { timeout: 2000 });
  });
});
