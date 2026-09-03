import { screen } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import type { DigestItem } from "../domain/digestClient";
import { renderDigest } from "../test/digestScenario";
import { ARTICLE_FIXTURE, makeDigest, makeDigestItem, TEST_API_BASE } from "../test/fixtures";
import { consumeGroundedContextHandoff } from "../domain/groundedContextHandoff";

async function openViewer(item: DigestItem) {
  const result = renderDigest({ digest: makeDigest([item]) });
  await result.user.click(await screen.findByRole("button", { name: item.title }));
  expect(await screen.findByRole("dialog", { name: item.title })).toBeInTheDocument();
  return result;
}

describe("DigestViewer formats", () => {
  it("opens an article using content returned by the mocked local node", async () => {
    const item = makeDigestItem();
    const { scenario } = await openViewer(item);

    expect(
      await screen.findByText("This article body came from the mocked local node."),
    ).toBeInTheDocument();
    expect(screen.getByText("Rynmesh Test Author")).toBeInTheDocument();
    expect(scenario.requests.feedback).toContainEqual({ item_id: item.item_id, action: "opened" });
  });

  it("reopens consumption-history items whose compact record omitted evidence details", async () => {
    const item = { ...makeDigestItem(), evidence_packet: undefined } as unknown as DigestItem;
    await openViewer(item);
    expect(await screen.findByText("This article body came from the mocked local node.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask about this item" })).toBeInTheDocument();
  });

  it("opens Private AI with an opaque one-time handoff after readable extraction", async () => {
    const item = makeDigestItem({ title: "UNIQUE_TITLE_25", link: "https://example.test/UNIQUE_URL_25" });
    const { user } = await openViewer(item);
    await screen.findByText("This article body came from the mocked local node.");
    await user.click(screen.getByRole("button", { name: "Ask about this item" }));

    const search = screen.getByLabelText("Private AI handoff location").textContent ?? "";
    expect(search).toMatch(/^\?grounding=[0-9a-f]{48}$/);
    expect(search).not.toContain("UNIQUE_TITLE_25");
    expect(search).not.toContain("UNIQUE_URL_25");
    expect(search).not.toContain("article body");
    const historyState = screen.getByLabelText("Private AI handoff history state").textContent ?? "";
    expect(historyState).not.toContain("UNIQUE_TITLE_25");
    expect(historyState).not.toContain("UNIQUE_URL_25");
    expect(historyState).not.toContain("article body");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    const id = new URLSearchParams(search).get("grounding")!;
    const context = consumeGroundedContextHandoff(id);
    expect(context?.itemId).toBe(item.item_id);
    expect(context?.blocks[0].text).toContain("article body");
    expect(consumeGroundedContextHandoff(id)).toBeNull();
  });

  it("keeps grounded asking disabled and explains failed extraction", async () => {
    const failed = makeDigestItem({ item_id: "item-reader-failed", title: "Unreadable article" });
    const { user } = renderDigest({
      digest: makeDigest([failed]),
      handlers: [http.get(`${TEST_API_BASE}/reader`, () => HttpResponse.json({ error: "unreadable" }, { status: 502 }))],
    });
    await user.click(await screen.findByRole("button", { name: failed.title }));
    expect(await screen.findByText(/Grounded asking is unavailable because no readable article text was extracted/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask about this item" })).toBeDisabled();
  });

  it("keeps grounded asking disabled and explains empty extraction", async () => {
    const empty = makeDigestItem({ item_id: "item-reader-empty", title: "Empty article" });
    const { user } = renderDigest({
      digest: makeDigest([empty]),
      article: { ...ARTICLE_FIXTURE, blocks: [], word_count: 0 },
    });
    await user.click(await screen.findByRole("button", { name: empty.title }));
    expect(await screen.findByText(/Grounded asking is unavailable because no readable article text was extracted/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ask about this item" })).toBeDisabled();
  });

  it("opens an image item without fetching live media", async () => {
    const mediaUrl = "data:image/png;base64,iVBORw0KGgo=";
    const item = makeDigestItem({
      item_id: "item-image",
      title: "A generated local image",
      link: "https://example.test/image",
      content_kind: "image",
      content_type: "image/png",
      media_url: mediaUrl,
    });

    await openViewer(item);

    expect(screen.getByRole("img", { name: item.title })).toHaveAttribute("src", mediaUrl);
  });

  it("opens an audio item with a non-autoplay local fixture source", async () => {
    const mediaUrl = "data:audio/mpeg;base64,SUQz";
    const item = makeDigestItem({
      item_id: "item-audio",
      title: "A local audio briefing",
      link: "https://example.test/audio",
      content_kind: "audio",
      content_type: "audio/mpeg",
      media_url: mediaUrl,
    });

    const { container } = await openViewer(item);
    const audio = container.querySelector("audio");

    expect(audio).toHaveAttribute("src", mediaUrl);
    expect(audio).toHaveAttribute("preload", "none");
    expect(audio).not.toHaveAttribute("autoplay");
  });

  it("opens YouTube through the privacy-enhanced embed URL without live playback", async () => {
    const item = makeDigestItem({
      item_id: "item-video",
      title: "A local-first YouTube explainer",
      link: "https://www.youtube.com/watch?v=abc123fixture",
      content_kind: "video",
      content_type: "text/html",
    });

    await openViewer(item);

    expect(screen.getByTitle(item.title)).toHaveAttribute(
      "src",
      "https://www.youtube-nocookie.com/embed/abc123fixture",
    );
    expect(
      screen.getAllByRole("link", { name: /Original/ }).some((link) => link.getAttribute("href") === item.link),
    ).toBe(true);
  });
});
