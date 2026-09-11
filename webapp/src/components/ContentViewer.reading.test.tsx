import { MemoryRouter } from "react-router-dom";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import ContentViewer from "./ContentViewer";

afterEach(() => vi.restoreAllMocks());

it("restores position, reports truncated content and retries a failed position save before closing", async () => {
  const client = { ...makeFixtureNodeClient(), mode: "live" as const };
  const item = (await client.listContent()).find((row) => row.content_kind === "document")!;
  vi.spyOn(client, "getContentBody").mockResolvedValue({ ok: true, content_id: item.content_id,
    content_type: "text/plain", size: "20", truncated: true, text: "A real saved article." });
  vi.spyOn(digestApi, "listConsumption").mockResolvedValue([
    { item_id: item.content_id, progress: 0.4 } as ConsumptionRecord,
  ]);
  const write = vi.spyOn(client, "recordContentConsumption").mockResolvedValue(undefined);
  const close = vi.fn();
  const { container } = render(<MemoryRouter><ContentViewer item={item} client={client} onClose={close} /></MemoryRouter>);
  const stage = container.querySelector(".content-viewer-stage") as HTMLElement;
  Object.defineProperties(stage, { scrollHeight: { configurable: true, value: 1500 }, clientHeight: { configurable: true, value: 500 } });
  await screen.findByText("A real saved article.");
  await waitFor(() => expect(stage.scrollTop).toBe(400));
  expect(screen.getByText(/shortened preview/)).toBeInTheDocument();
  stage.scrollTop = 700;
  fireEvent.scroll(stage);
  await waitFor(() => expect(write).toHaveBeenCalledWith(item, "progress", 0.7));
  write.mockRejectedValueOnce(new Error("disk full"));
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("could not be saved");
  expect(close).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Close content viewer" }));
  expect(close).toHaveBeenCalledTimes(1);
});
