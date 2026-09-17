import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { beforeEach, expect, it, vi } from "vitest";
import { askHistory } from "../domain/askHistory";
import { feedApi } from "../domain/friendFeed";
import { offlineApi } from "../domain/offlineReading";
import FriendPublicationActions, { DigestFriendActions } from "./FriendPublicationActions";

const publication = { relationship_id: "friend", publication_id: "story", revision: 4 };
beforeEach(() => {
  vi.spyOn(feedApi, "fetch").mockResolvedValue({ library_id: "import:verified" });
  vi.spyOn(offlineApi, "download").mockResolvedValue({ state: "queued" } as never);
  vi.spyOn(askHistory, "prepareContext").mockResolvedValue({ library_id: "import:verified" } as never);
  vi.spyOn(askHistory, "beginRun").mockRejectedValue(new Error("No automatic inference"));
});
function Destination() { return <p>Material review {useLocation().search}</p>; }
function show() {
  return render(<MemoryRouter><Routes><Route path="/" element={<FriendPublicationActions publication={publication} />} />
    <Route path="/ask" element={<Destination />} /></Routes></MemoryRouter>);
}

it("queues only after explicit verified download and reuses that private copy for material review", async () => {
  const user = userEvent.setup(); show();
  expect(feedApi.fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Save offline" }));
  expect(feedApi.fetch).toHaveBeenCalledWith("friend", "story", 4);
  expect(offlineApi.download).toHaveBeenCalledWith("import:verified");
  expect(await screen.findByRole("status")).toHaveTextContent("Queued");
  expect(screen.queryByText(/Body available offline/)).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Ask about this" }));
  expect(feedApi.fetch).toHaveBeenCalledTimes(1);
  expect(askHistory.prepareContext).toHaveBeenCalledWith("import:verified");
  expect(await screen.findByText("Material review ?material=import%3Averified")).toBeInTheDocument();
  expect(askHistory.beginRun).not.toHaveBeenCalled();
});

it("does not enqueue or prepare after revoked access and lets the owner retry", async () => {
  vi.mocked(feedApi.fetch).mockRejectedValueOnce(new Error("Sharing stopped"));
  const user = userEvent.setup(); show();
  await user.click(screen.getByRole("button", { name: "Ask about this" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Sharing stopped");
  expect(askHistory.prepareContext).not.toHaveBeenCalled();
  expect(offlineApi.download).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Ask about this" }));
  expect(await screen.findByText(/Material review/)).toBeInTheDocument();
});

it("retains a verified private copy after offline storage or extraction failure", async () => {
  vi.mocked(offlineApi.download).mockRejectedValueOnce(new Error("Storage full"));
  vi.mocked(askHistory.prepareContext).mockRejectedValueOnce(new Error("No readable text"));
  const user = userEvent.setup(); show();
  await user.click(screen.getByRole("button", { name: "Save offline" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Your source copy is saved in My content. Storage full");
  await user.click(screen.getByRole("button", { name: "Ask about this" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("No readable text");
  expect(feedApi.fetch).toHaveBeenCalledTimes(1);
  expect(askHistory.beginRun).not.toHaveBeenCalled();
});

it("stops subsequent actions if the reviewed publication disappears during fetch", async () => {
  let resolve!: (value: { library_id: string }) => void;
  vi.mocked(feedApi.fetch).mockImplementation(() => new Promise((done) => { resolve = done; }));
  const user = userEvent.setup(); const view = show();
  await user.click(screen.getByRole("button", { name: "Ask about this" }));
  expect(screen.getByRole("button", { name: "Save offline" })).toBeDisabled();
  view.unmount();
  await act(async () => resolve({ library_id: "import:verified" }));
  expect(askHistory.prepareContext).not.toHaveBeenCalled();
});

it("only adds the flow to digest entries with private friend provenance", () => {
  const view = render(<MemoryRouter><DigestFriendActions item={{ title: "Public" }} /></MemoryRouter>);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  view.rerender(<MemoryRouter><DigestFriendActions item={{ friend_provenance: publication }} /></MemoryRouter>);
  expect(screen.getByRole("button", { name: "Save offline" })).toBeInTheDocument();
});
