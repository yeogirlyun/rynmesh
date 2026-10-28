import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import { askHistory } from "../domain/askHistory";
import * as nas from "../domain/nas";
import { digestApi } from "../domain/digestClient";
import AIWorkspaceMaterials from "./AIWorkspaceMaterials";
import { makeDigestItem } from "../test/fixtures";

beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
afterEach(() => vi.restoreAllMocks());
it("excludes NAS sources without AI permission, attaches a server copy, and never sends inference", async () => {
  vi.spyOn(nas, "nasJson").mockResolvedValueOnce({ enabled: true, sources: [{ id: "denied", name: "Private", allow_ai: false }, { id: "allowed", name: "Allowed NAS", allow_ai: true, protocol: "webdav" }] })
    .mockResolvedValueOnce({ entries: [{ name: "report.txt", path: "report.txt", directory: false }] });
  const source = { library_id: "import:source", title: "report.txt", source_url: "", sha256: "hash", extraction_truncated: false, text_bytes: 20 };
  const prepare = vi.spyOn(askHistory, "prepareNas").mockResolvedValue(source);
  const begin = vi.spyOn(askHistory, "beginRun");
  const select = vi.fn(async () => undefined); const close = vi.fn();
  render(<AIWorkspaceMaterials onSelect={select} onClose={close} />); const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /Allowed NAS/ }));
  expect(screen.queryByRole("button", { name: "Private" })).not.toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: /report.txt/ }));
  expect(prepare).toHaveBeenCalledWith("allowed", "report.txt");
  expect(select).toHaveBeenCalledWith(source); expect(close).toHaveBeenCalled(); expect(begin).not.toHaveBeenCalled();
});
it("shows empty state and recovers a materials load failure through retry", async () => {
  vi.spyOn(nas, "nasJson").mockRejectedValueOnce(new Error("NAS offline")).mockResolvedValueOnce({ enabled: true, sources: [] });
  render(<AIWorkspaceMaterials onSelect={vi.fn()} onClose={vi.fn()} />); const user = userEvent.setup();
  expect(screen.getByRole("status")).toHaveTextContent("Loading materials");
  expect(await screen.findByRole("alert")).toHaveTextContent("NAS offline");
  await user.click(screen.getByRole("button", { name: "Retry" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(screen.getByText(/No NAS connection allows AI access/)).toBeInTheDocument();
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([]);
  await user.click(screen.getByRole("button", { name: "Saved articles" }));
  expect(await screen.findByText(/No saved articles yet/)).toBeInTheDocument();
});
it("attaches only a bookmarked article's verified copy and keeps preparation errors visible", async () => {
  vi.spyOn(nas, "nasJson").mockResolvedValue({ enabled: true, sources: [] });
  const saved = { item_id: "saved-item", item: makeDigestItem({ item_id: "saved-item", title: "Saved article" }), first_opened_unix: 1, last_opened_unix: 1, last_activity_unix: 1, open_count: 1, bookmarked: true, progress: 0, completed: false };
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([saved, { ...saved, item_id: "unsaved", item: makeDigestItem({ title: "Not bookmarked" }), bookmarked: false }]);
  const source = { library_id: "import:saved", title: "Saved article", source_url: "", sha256: "hash", extraction_truncated: false, text_bytes: 20 };
  const prepare = vi.spyOn(askHistory, "prepareContext").mockRejectedValueOnce(new Error("Source copy unavailable")).mockResolvedValueOnce(source);
  const select = vi.fn(async () => undefined); const close = vi.fn();
  render(<AIWorkspaceMaterials onSelect={select} onClose={close} />); const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Saved articles" }));
  await user.click(await screen.findByRole("button", { name: "Saved article" }));
  expect(screen.queryByRole("button", { name: "Not bookmarked" })).not.toBeInTheDocument();
  expect(await screen.findByRole("alert")).toHaveTextContent("Source copy unavailable");
  expect(close).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Saved article" }));
  expect(prepare).toHaveBeenCalledWith("saved-item");
  expect(select).toHaveBeenCalledWith(source); expect(close).toHaveBeenCalled();
});
