import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { digestApi, type DigestItem } from "../domain/digestClient";
import DigestViewer from "./DigestViewer";

const item = { item_id: "first", title: "Real article", link: "https://example.test/first", content_kind: "document", source_title: "Feed", source_kind: "rss",
  evidence_packet: { review_basis: "metadata", observations: [], limitations: [], citations: [] } } as unknown as DigestItem;
const article = { url: item.link, title: item.title, byline: "", lead_image: "", blocks: [{ tag: "p", text: "Actual text." }], word_count: 2, cached: false };

function props() {
  return { items: [item], index: 0, onIndexChange: vi.fn(), onClose: vi.fn(),
    onFeedback: vi.fn().mockResolvedValue(undefined), onSteer: vi.fn().mockResolvedValue(undefined),
    bookmarked: false, onBookmark: vi.fn().mockResolvedValue(undefined), onProgress: vi.fn(), initialProgress: 0 };
}

afterEach(() => vi.restoreAllMocks());

it("records reading only after the article loads and allows a failed read to recover", async () => {
  vi.spyOn(digestApi, "readArticle").mockRejectedValueOnce(new Error("offline")).mockResolvedValue(article);
  const callbacks = props();
  render(<DigestViewer {...callbacks} />);
  const user = userEvent.setup();
  const retry = await screen.findByRole("button", { name: "Retry reading" });
  expect(callbacks.onFeedback).not.toHaveBeenCalled();
  await user.click(retry);
  expect(await screen.findByText("Actual text.")).toBeInTheDocument();
  await waitFor(() => expect(callbacks.onFeedback).toHaveBeenCalledExactlyOnceWith(item, "opened"));
});

it("does not pretend a failed bookmark or feedback was saved", async () => {
  vi.spyOn(digestApi, "readArticle").mockResolvedValue(article);
  const callbacks = props();
  callbacks.onBookmark.mockRejectedValueOnce(new Error("full"));
  render(<DigestViewer {...callbacks} />);
  const user = userEvent.setup();
  await screen.findByText("Actual text.");
  await user.click(screen.getByRole("button", { name: /^Save$/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("bookmark could not be confirmed");
  expect(screen.queryByRole("button", { name: "Saved" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: /^Save$/ }));
  expect(await screen.findByRole("button", { name: "Saved" })).toBeInTheDocument();
  callbacks.onFeedback.mockRejectedValueOnce(new Error("full"));
  await user.click(screen.getByRole("button", { name: "More like this" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Feedback could not be confirmed");
  expect(callbacks.onIndexChange).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "More like this" })).toBeEnabled();
});
