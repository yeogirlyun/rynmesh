import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import Settings from "./Settings";

afterEach(cleanup);

it.each([true, false])("T2: absent usage with budget=%s never renders today usage or a percentage bar", async (hasBudget) => {
  const client = makeFixtureNodeClient();
  const settings = { ...await client.getSettings() };
  delete (settings as Partial<typeof settings>).fetch_used_mb;
  if (!hasBudget) delete settings.fetch_budget_mb;
  vi.spyOn(client, "getSettings").mockResolvedValue(settings);
  render(<MemoryRouter><Routes><Route element={<Outlet context={{ client, confirm: vi.fn(), notify: vi.fn(), refreshShell: vi.fn() }} />}>
    <Route path="/" element={<Settings />} />
  </Route></Routes></MemoryRouter>);
  await userEvent.click(await screen.findByRole("button", { name: "Fetch limits" }));
  expect(screen.queryByText("Used today")).not.toBeInTheDocument();
  expect(screen.getByText(/Not yet effective/)).toBeInTheDocument();
  expect(document.querySelector(".budget-bar")).toBeNull();
  expect(screen.queryByText(/undefined|NaN/)).not.toBeInTheDocument();
});

it("fixture omits unavailable usage and pending recommendation metrics", async () => {
  const client = makeFixtureNodeClient();
  expect(await client.getSettings()).not.toHaveProperty("fetch_used_mb");
  expect(await client.getNodeStatus()).not.toHaveProperty("pending_recs");
});
