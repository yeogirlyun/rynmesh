import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import ImageStudioOwner from "./ImageStudioOwner";
import { imageStudio, type StudioJob, type StudioStatus } from "../domain/imageStudio";
import { nodeSites } from "../domain/nodeSites";
import { peerProviders } from "../domain/peerProviders";

vi.mock("../domain/imageStudio", async (original) => {
  const actual = await original<typeof import("../domain/imageStudio")>();
  return { ...actual, imageStudio: { status: vi.fn(), queue: vi.fn(), usage: vi.fn(), cancel: vi.fn(), prioritize: vi.fn() } };
});
vi.mock("../domain/nodeSites", async (original) => {
  const actual = await original<typeof import("../domain/nodeSites")>();
  return { ...actual, nodeSites: { ...actual.nodeSites, mine: vi.fn(), update: vi.fn() } };
});
vi.mock("../domain/peerProviders", async (original) => {
  const actual = await original<typeof import("../domain/peerProviders")>();
  return { ...actual, peerProviders: { ...actual.peerProviders, workspace: vi.fn() } };
});
const studio = vi.mocked(imageStudio);
const sites = vi.mocked(nodeSites);
const providers = vi.mocked(peerProviders);

const now = Date.now() / 1000;
function job(over: Partial<StudioJob>): StudioJob {
  return { id: "a".repeat(24), purpose: "poster", width: 1024, height: 1365, brief: "圣诞袜海报", words: "", state: "queued",
    failure: null, created_at: now, started_at: null, caller_name: "Ajiu", caller: "r|m-2", priority: 0, ...over };
}
function status(over: Partial<StudioStatus> = {}): StudioStatus {
  return { model: { ready: true, code: "" }, enabled: true, audience: { mode: "all", members: [] }, queue_length: 3,
    running: "r".repeat(24), today: 12, average_seconds: 50, ...over };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ providers: [{ plugin_id: "company-workspace", status: "connected" }] }))));
  providers.workspace.mockResolvedValue({ can_publish_models: true } as never);
  sites.mine.mockResolvedValue({ sites: [], members: [{ provider_resource: "r", member_id: "m-2", member_name: "Ajiu" }] });
  studio.status.mockResolvedValue(status());
  studio.queue.mockResolvedValue([
    job({ id: "r".repeat(24), state: "running", started_at: now - 80, caller_name: "Xiaomei", brief: "保温杯主图" }),
    job({ id: "b".repeat(24) }),
    job({ id: "c".repeat(24), caller_name: "Bai", brief: "冬季新品横幅" }),
  ]);
  studio.usage.mockResolvedValue([{ caller: "r|m-1", name: "Xiaomei", today: 6, last_30_days: 84, failed: 2 }]);
  studio.cancel.mockResolvedValue(job({}));
  studio.prioritize.mockResolvedValue(job({}));
  sites.update.mockResolvedValue({} as never);
});

const show = () => render(<MemoryRouter><ImageStudioOwner /></MemoryRouter>);

it("shows status, queue and usage, and manages the queue", async () => {
  show();
  expect(await screen.findByText("12 images")).toBeInTheDocument();
  expect(screen.getByText("Signed in, ready")).toBeInTheDocument();
  const rows = screen.getAllByRole("row");
  const running = rows.find(row => within(row).queryByText("保温杯主图"))!;
  expect(within(running).getByText(/Generating 1:2\d/)).toBeInTheDocument();
  expect(within(running).queryByRole("button", { name: "Move up" })).toBeNull();
  await userEvent.click(within(running).getByRole("button", { name: "Stop" }));
  expect(studio.cancel).toHaveBeenCalledWith("r".repeat(24));
  const next = rows.find(row => within(row).queryByText("圣诞袜海报"))!;
  expect(within(next).queryByRole("button", { name: "Move up" })).toBeNull(); // already next
  const last = rows.find(row => within(row).queryByText("冬季新品横幅"))!;
  await userEvent.click(within(last).getByRole("button", { name: "Move up" }));
  expect(studio.prioritize).toHaveBeenCalledWith("c".repeat(24));
  expect(screen.getByText("84 images")).toBeInTheDocument();
});

it("opens, closes and limits the studio through the node site switch", async () => {
  studio.status.mockResolvedValue(status({ enabled: false }));
  show();
  const open = await screen.findByRole("checkbox", { name: "Not open" });
  await userEvent.click(open);
  expect(sites.update).toHaveBeenCalledWith("image-studio", { enabled: true });
  await userEvent.click(screen.getByRole("radio", { name: "Only selected people" }));
  expect(sites.update).toHaveBeenCalledWith("image-studio", { audience: { mode: "members", members: [] } });
});

it("warns when colleagues cannot reach it or Codex cannot make images", async () => {
  providers.workspace.mockResolvedValue({ can_publish_models: false } as never);
  studio.status.mockResolvedValue(status({ model: { ready: false, code: "codex_signed_out" } }));
  show();
  expect(await screen.findByText(/hasn't made you a publisher/)).toBeInTheDocument();
  await waitFor(() => expect(screen.getAllByText("Codex isn't signed in").length).toBeGreaterThan(0));
});
