import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { offlineApi, type OfflineRecord } from "../domain/offlineReading";
import OfflineDownloadButton from "./OfflineDownloadButton";

afterEach(() => vi.restoreAllMocks());

function mount(itemId = "import:article") {
  render(<MemoryRouter><OfflineDownloadButton itemId={itemId} /></MemoryRouter>);
  return userEvent.setup();
}

it("requests a download and shows the node's reported state", async () => {
  const download = vi.spyOn(offlineApi, "download").mockResolvedValue({ state: "queued" } as OfflineRecord);
  const user = mount("import:article");
  expect(screen.getByRole("button", { name: "Download for offline" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Download for offline" }));
  expect(download).toHaveBeenCalledWith("import:article");
  expect(await screen.findByText("Queued.", { exact: false })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Offline downloads" })).toHaveAttribute("href", "/offline");
  expect(screen.getByRole("button", { name: "Download for offline" })).toBeEnabled();
});

it("shows an honest error and clears busy state when the download cannot be confirmed", async () => {
  vi.spyOn(offlineApi, "download").mockRejectedValue(new Error("The download storage limit was reached. Clear some downloads and retry."));
  const user = mount();
  await user.click(screen.getByRole("button", { name: "Download for offline" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("The download storage limit was reached. Clear some downloads and retry.");
  expect(screen.getByRole("button", { name: "Download for offline" })).toBeEnabled();
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

it("ignores a duplicate click dispatched before the disabled state is applied", async () => {
  let finish!: (value: OfflineRecord) => void;
  const download = vi.spyOn(offlineApi, "download").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  mount();
  const button = screen.getByRole("button", { name: "Download for offline" });
  fireEvent.click(button);
  fireEvent.click(button);
  finish({ state: "downloading" } as OfflineRecord);
  expect(await screen.findByText("Downloading.", { exact: false })).toBeInTheDocument();
  expect(download).toHaveBeenCalledTimes(1);
});
