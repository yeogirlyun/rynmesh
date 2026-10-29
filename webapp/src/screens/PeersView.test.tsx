import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import Peers from "./Peers";

// Redesign checks for /peers: summary strip, tier segments with a local name search, and the right-hand detail panel.
function show() {
  const client = makeFixtureNodeClient();
  const confirm = vi.fn();
  const notify = vi.fn();
  const context: AppOutletContext = {
    client, node: { node_name: "Laptop", peer_id: "peer:test", daemon_running: true, registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 1 },
    registry: { status: "connected", url: "" }, peers: [], refreshShell: vi.fn(async () => undefined), confirm, notify,
  };
  const view = render(<MemoryRouter><Routes><Route element={<Outlet context={context} />}><Route index element={<Peers />} /></Route></Routes></MemoryRouter>);
  return { ...view, client, confirm, notify };
}

it("renders the page root, summary counts, tier segments and a selectable list with a detail panel", async () => {
  const { container } = show();
  await screen.findByRole("heading", { level: 1, name: "Connection details" });
  expect(container.querySelector(".rd-peers")).toHaveClass("rf");
  const summary = screen.getByRole("region", { name: "Peers" });
  expect(within(summary).getByText("Online")).toBeInTheDocument();
  const tiers = screen.getByRole("group", { name: "Tier" });
  expect(within(tiers).getAllByRole("button")).toHaveLength(5);
  expect(within(tiers).getByRole("button", { name: /^all/i })).toHaveAttribute("aria-pressed", "true");
  const rows = container.querySelectorAll("button.rd-row");
  expect(rows.length).toBeGreaterThan(2);
  // The first peer is selected by default and the panel is a complementary landmark named after it.
  const first = rows[0].querySelector("b")!.textContent!;
  expect(screen.getByRole("complementary", { name: first })).toBeInTheDocument();
  fireEvent.click(rows[1]);
  const second = rows[1].querySelector("b")!.textContent!;
  expect(await screen.findByRole("complementary", { name: second })).toBeInTheDocument();
  expect(rows[1]).toHaveAttribute("aria-pressed", "true");
});

it("filters by name locally and keeps the trust actions behind a confirmation", async () => {
  const { container, confirm, notify } = show();
  await screen.findByRole("heading", { level: 1, name: "Connection details" });
  const search = screen.getByRole("textbox", { name: "Search" });
  expect(search).toHaveAttribute("placeholder", "Search name, ID or address");
  fireEvent.change(search, { target: { value: "zzz-no-such-peer" } });
  expect(await screen.findByText("No peers to show.")).toBeInTheDocument();
  expect(container.querySelector("aside.rd-detail")).toBeNull();
  fireEvent.change(search, { target: { value: "" } });
  const panel = await screen.findByRole("complementary");
  const quarantine = within(panel).getByRole("button", { name: "Quarantine" });
  fireEvent.click(quarantine);
  expect(confirm).toHaveBeenCalledTimes(1);
  expect(confirm.mock.calls[0][0]).toMatchObject({ risk: "high", confirmLabel: "Quarantine" });
  expect(notify).not.toHaveBeenCalled();
  confirm.mock.calls[0][0].onConfirm();
  await waitFor(() => expect(notify).toHaveBeenCalledWith("warn", "Quarantine request sent to local node"));
  expect(within(panel).getByText("These change local identity policy and ask for confirmation first.")).toBeInTheDocument();
});

it("disables Trust root for a peer that is already a trusted root", async () => {
  const { container } = show();
  await screen.findByRole("heading", { level: 1, name: "Connection details" });
  const rows = Array.from(container.querySelectorAll("button.rd-row"));
  const client = makeFixtureNodeClient();
  const peers = await client.listPeers({ tier: "all", search: "" });
  const root = peers.find((peer) => peer.trustedRoot && !peer.quarantined);
  expect(root).toBeTruthy();
  const index = peers.findIndex((peer) => peer.id === root!.id);
  fireEvent.click(rows[index]);
  const panel = await screen.findByRole("complementary", { name: root!.name });
  expect(within(panel).getByRole("button", { name: "Trust root" })).toBeDisabled();
});
