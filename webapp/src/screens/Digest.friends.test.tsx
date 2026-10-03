import { screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { renderDigest } from "../test/digestScenario";
import { makeDigest, makeConsumptionRecord, TEST_API_BASE } from "../test/fixtures";
import type { ContentItem } from "../domain/types";

vi.mock("../components/ContentViewer", () => ({ default: ({ item }: { item: ContentItem }) =>
  <div role="dialog" aria-label="Saved friend copy">{item.title}</div> }));

const friend = {
  relationship_id: "a".repeat(32), publication_id: "b".repeat(32), revision: 2,
  publisher_peer_id: "authenticated-friend", serving_peer_id: "authenticated-friend", node_name: "Alice",
  checked_at: 1789600000, unreachable: false, source: "Source claimed by Alice", source_url: "https://example.com/source",
  sha256: "c".repeat(64), content_truncated: false,
};
const mixed = () => {
  const digest = makeDigest();
  digest.items.unshift({ ...digest.items[0], item_id: "friend:checked-publication", title: "Friend article",
    source_id: "friend:hashed-source", source_kind: "friend", source_title: "Alice", link: "", thumbnail: "",
    friend_provenance: { ...friend } });
  return digest;
};

describe("For You friend publications", () => {
  it("mixes public and friend cards and makes authenticated sharing provenance inspectable", async () => {
    const { user } = renderDigest({ digest: mixed() });
    expect(await screen.findByRole("button", { name: "Friend article" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "A local-first assistant worth reading" })).toBeInTheDocument();
    expect(screen.getByText(/Shared by Alice/)).toBeInTheDocument();
    await user.click(screen.getByText("Sharing provenance"));
    expect(screen.getAllByText("authenticated-friend")).toHaveLength(2);
    expect(screen.getByText(/Original authorship and content safety have not/)).toBeInTheDocument();
  });

  it("saves through the node's checked friend endpoint before opening an independent copy", async () => {
    let fetched = false;
    const copy = makeConsumptionRecord({ ...makeDigest().items[0], item_id: "import:local-copy" });
    const { user, scenario } = renderDigest({ digest: mixed(), handlers: [
      http.post(`${TEST_API_BASE}/friend-feed/subscriptions/${friend.relationship_id}/${friend.publication_id}/fetch`, async ({ request }) => {
        expect(await request.json()).toEqual({ expected_revision: 2 });
        fetched = true;
        return HttpResponse.json({ library_id: copy.item_id });
      }),
      http.post(`${TEST_API_BASE}/friend-feed/subscriptions/${friend.relationship_id}/${friend.publication_id}/read`, () => HttpResponse.json({ read: true })),
      http.get(`${TEST_API_BASE}/consumption`, () => HttpResponse.json(fetched ? [copy] : [])),
    ] });
    await user.click(await screen.findByRole("button", { name: "Save copy and read" }));
    expect(await screen.findByRole("dialog", { name: "Saved friend copy" })).toBeInTheDocument();
    expect(fetched).toBe(true);
    expect(scenario.requests.consumption).toEqual([]); // The private card itself is never saved as public reading metadata.
  });

  it("keeps a failed or withdrawn publication out of the reader and explains offline checks", async () => {
    const digest = mixed();
    digest.items[0].friend_provenance!.unreachable = true;
    const { user } = renderDigest({ digest, handlers: [
      http.post(`${TEST_API_BASE}/friend-feed/subscriptions/${friend.relationship_id}/${friend.publication_id}/fetch`, () =>
        HttpResponse.json({ detail: "feed_publication_unavailable" }, { status: 409 })),
    ] });
    await screen.findByText(/Latest access could not be checked/);
    await user.click(screen.getByRole("button", { name: "Save copy and read" }));
    await screen.findByText(/This publication is no longer available to you/);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("uses the existing hide feedback and keeps public recommendations visible", async () => {
    const { user, scenario } = renderDigest({ digest: mixed() });
    await screen.findByRole("button", { name: "Friend article" });
    await user.click(screen.getAllByRole("button", { name: "Hide" })[0]);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Friend article" })).not.toBeInTheDocument());
    expect(scenario.requests.feedback).toContainEqual({ item_id: "friend:checked-publication", action: "hide" });
    expect(screen.getByRole("button", { name: "A local-first assistant worth reading" })).toBeInTheDocument();
  });
});
