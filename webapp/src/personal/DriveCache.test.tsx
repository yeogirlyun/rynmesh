import { beforeEach, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { clearNasMemory, type NasStatus } from "../domain/nas";
import { NasBrowser } from "./Nas";

vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), confirm: vi.fn() }) }));
const state: NasStatus = { enabled: true, max_file_bytes: 67108864, systems: [], sources: ["a", "b"].map(id => ({
  id, name: `Drive ${id}`, system: "google", protocol: "google", root: "", username: "", writable: false, allow_ai: false,
})) };
const entry = (name: string) => ({ name, path: name, directory: true, size: 0, modified: 0 });
const view = () => render(<MemoryRouter><NasBrowser status={state} /></MemoryRouter>);
beforeEach(() => {
  clearNasMemory();
  server.use(http.put("*/api/local/plugins/nas/browser-location", () => HttpResponse.json({ ok: true })));
});

it("shows disk cache while refreshing and lets a slow drive be switched away from", async () => {
  let finish!: () => void;
  const waiting = new Promise<void>(resolve => { finish = resolve; });
  let started = false;
  server.use(http.get("*/api/local/plugins/nas/sources/:id/files", async ({ request, params }) => {
    if (new URL(request.url).searchParams.has("cached")) return HttpResponse.json({ entries: [entry(`${params.id}-cached`)], updated_at: 1 });
    if (params.id === "a") { started = true; await waiting; }
    return HttpResponse.json({ entries: [entry(`${params.id}-fresh`)], updated_at: Date.now() / 1000 });
  }));
  const mounted = view();
  expect(await screen.findByRole("button", { name: "a-cached" })).toBeEnabled();
  await waitFor(() => expect(started).toBe(true));
  expect(screen.getByText("Checking for updates…")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: /Drive b/ }));
  expect(await screen.findByRole("button", { name: "b-fresh" })).toBeInTheDocument();
  await act(async () => { finish(); await waiting; });
  expect(screen.queryByRole("button", { name: "a-fresh" })).not.toBeInTheDocument();
  mounted.unmount();
});

it("retains cached rows on a failed refresh and restores a fresh directory without another remote request", async () => {
  let freshRequests = 0;
  server.use(http.get("*/api/local/plugins/nas/sources/:id/files", ({ request }) => {
    if (new URL(request.url).searchParams.has("cached")) return HttpResponse.json({ entries: [entry("saved-folder")], updated_at: Date.now() / 1000 });
    freshRequests++;
    return HttpResponse.json({ detail: "drive_provider_unreachable" }, { status: 502 });
  }));
  const mounted = view();
  await screen.findByRole("button", { name: "saved-folder" });
  mounted.unmount();
  view();
  expect(screen.getByRole("button", { name: "saved-folder" })).toBeInTheDocument();
  expect(freshRequests).toBe(0);
  fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
  await screen.findByRole("alert");
  expect(screen.getByRole("button", { name: "saved-folder" })).toBeEnabled();
  expect(screen.getByText("Showing cached list")).toBeInTheDocument();
  expect(freshRequests).toBe(1);
});

it("renders the first page before subsequent pages finish", async () => {
  let finish!: () => void;
  const waiting = new Promise<void>(resolve => { finish = resolve; });
  server.use(http.get("*/api/local/plugins/nas/sources/:id/files", async ({ request }) => {
    const query = new URL(request.url).searchParams;
    if (query.has("cached")) return HttpResponse.json({ entries: null });
    if (query.has("cursor")) {
      await waiting;
      return HttpResponse.json({ entries: [entry("first-page"), entry("second-page")], next_cursor: "", scan: "scan" });
    }
    return HttpResponse.json({ entries: [entry("first-page")], next_cursor: "next", scan: "scan" });
  }));
  view();
  expect(await screen.findByRole("button", { name: "first-page" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "second-page" })).not.toBeInTheDocument();
  await act(async () => { finish(); await waiting; });
  expect(await screen.findByRole("button", { name: "second-page" })).toBeInTheDocument();
});
