import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { MemoryRouter } from "react-router-dom";
import { server } from "../test/server";
import { rynAgent } from "../domain/rynAgent";
import RynAgent from "./RynAgent";
import RynAgentStorageHealth from "./RynAgentStorageHealth";

vi.mock("../personal/model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => ({ demo: false, appearance: "light", setAppearance: vi.fn(), devices: [], services: [], space: null }),
}));

const damaged = { state: "damaged" as const, damaged_tasks: 2, quarantine_dir: "/agent/quarantine", recovered_from_prev: false };

it("shows damaged task count and opens backups through the real owner API", async () => {
  const opened = vi.fn();
  server.use(http.post("*/api/local/agent/storage/backups/open", () => { opened(); return HttpResponse.json({ opened: true }); }));
  render(<RynAgentStorageHealth health={damaged} zh={false} onRefresh={vi.fn()} />);
  expect(screen.getByRole("alert")).toHaveTextContent("2");
  fireEvent.click(screen.getByRole("button", { name: "Open backup directory" }));
  await waitFor(() => expect(opened).toHaveBeenCalledTimes(1));
});

it("renders storage health in the real Agent page", async () => {
  server.use(http.get(/\/api\/local\/agent\//, ({ request }) => {
    const path = new URL(request.url).pathname;
    if (path.endsWith("/settings")) return HttpResponse.json({ enabled: true, model_id: "", permission_mode: "automatic", allowed_write_tools: [], max_steps: 64, max_seconds: 900, max_context_chars: null, max_output_chars: 20000, storage_health: damaged });
    if (path.endsWith("/models")) return HttpResponse.json({ models: [] });
    if (path.endsWith("/tasks")) return HttpResponse.json({ tasks: [] });
    if (path.endsWith("/projects")) return HttpResponse.json({ projects: [] });
    if (path.endsWith("/resources")) return HttpResponse.json({ connections: [], supported_formats: [] });
    if (path.endsWith("/tools")) return HttpResponse.json({ tools: [] });
    if (path.endsWith("/automations")) return HttpResponse.json({ automations: [] });
    return HttpResponse.json({});
  }));
  render(<MemoryRouter><RynAgent /></MemoryRouter>);
  expect(await screen.findByRole("button", { name: "Open backup directory" })).toBeVisible();
});

it("carries unavailable health on settings errors without inventing settings", async () => {
  server.use(http.get("*/api/local/agent/settings", () => HttpResponse.json({ detail: "agent_state_unavailable", storage_health: { ...damaged, state: "unavailable", damaged_tasks: 0 } }, { status: 503 })));
  await expect(rynAgent.settings()).rejects.toMatchObject({ details: { storage_health: { state: "unavailable" } } });
});

it("rechecks an unavailable store after thirty seconds and clears the timer on unmount", () => {
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
  try {
    const refresh = vi.fn();
    const view = render(<RynAgentStorageHealth health={{ ...damaged, state: "unavailable", damaged_tasks: 0 }} zh={false} onRefresh={refresh} />);
    act(() => vi.advanceTimersByTime(29999));
    expect(refresh).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(1));
    expect(refresh).toHaveBeenCalledTimes(1);
    view.unmount();
    act(() => vi.advanceTimersByTime(30000));
    expect(refresh).toHaveBeenCalledTimes(1);
  } finally { vi.useRealTimers(); }
});

it("does not display a warning for a healthy store", () => {
  render(<RynAgentStorageHealth health={{ ...damaged, state: "ok", damaged_tasks: 0 }} zh={false} onRefresh={vi.fn()} />);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});
