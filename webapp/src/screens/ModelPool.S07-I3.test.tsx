import { beforeEach, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { AppOutletContext } from "../appContext";
import ModelPool from "./ModelPool";

beforeEach(async () => { await i18n.changeLanguage("zh-CN"); });

it("saves and clears the subagent model through the real settings API without changing the main default", async () => {
  const writes: unknown[] = [];
  const saved = { enabled: false, model_id: "parent", default_subagent_model_id: "", permission_mode: "automatic" };
  server.use(
    http.get("*/api/local/agent/models", () => HttpResponse.json({ models: [
      { id: "parent", name: "Parent", source: "local_api", available: true, agent_supported: true },
      { id: "worker", name: "Worker", source: "local_api", available: true, agent_supported: true },
      { id: "offline", name: "Offline", source: "local_api", available: false, agent_supported: true },
      { id: "no-tools", name: "No tools", source: "local_api", available: true, agent_supported: false },
    ] })),
    http.get("*/api/local/agent/settings", () => HttpResponse.json(saved)),
    http.put("*/api/local/agent/settings", async ({ request }) => {
      const body = await request.json() as { default_subagent_model_id: string };
      writes.push(body); Object.assign(saved, body); return HttpResponse.json(saved);
    }),
  );
  render(<MemoryRouter initialEntries={["/models"]}><Routes><Route element={<Outlet context={{ client: makeFixtureNodeClient() } as AppOutletContext} />}>
    <Route path="/models" element={<ModelPool sources={[]} cli={null} native={null} peers={[]} loading={false} error="" onRefresh={vi.fn()} onEditSource={vi.fn()} onCLI={vi.fn()} onNative={vi.fn()} />} />
  </Route></Routes></MemoryRouter>);
  const user = userEvent.setup();
  const select = await screen.findByRole("combobox", { name: "子 agent 默认模型" });
  expect(within(select).queryByRole("option", { name: "Offline" })).not.toBeInTheDocument();
  expect(within(select).queryByRole("option", { name: "No tools" })).not.toBeInTheDocument();
  await user.selectOptions(select, "worker");
  await waitFor(() => expect(writes).toEqual([{ default_subagent_model_id: "worker" }]));
  expect(select).toHaveValue("worker");
  expect(screen.getByText("可选功能")).toBeVisible();
  await user.selectOptions(select, "");
  await waitFor(() => expect(writes).toEqual([{ default_subagent_model_id: "worker" }, { default_subagent_model_id: "" }]));
  expect(select).toHaveValue("");
});
