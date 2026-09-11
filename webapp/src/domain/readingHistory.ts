import type { DigestItem } from "./digestClient";
import type { ContentItem } from "./types";

/** Restore metadata without inventing verification evidence absent from history. */
export function contentFromHistory(record: { item: Partial<DigestItem> & Pick<DigestItem, "item_id" | "title" | "link"> }): ContentItem {
  const item = record.item;
  const external = /^https?:\/\//.test(item.link);
  return {
    content_id: external ? `digest:${item.item_id}` : item.item_id,
    digest_item_id: external ? item.item_id : undefined,
    title: item.title, description: item.summary ?? "", tags: item.tags ?? [],
    content_kind: (item.content_kind ?? "document") as ContentItem["content_kind"], content_type: item.content_type ?? "text/plain",
    publisher_peer_id: item.source_id ?? "", provider_peer_id: item.source_id ?? "",
    source_peer_name: item.source_title ?? "", source_platform: item.source_kind,
    manifest_hash: "", provenance_head_hash: null, distribution_weight: 0,
    safety_outcome: "unscanned", provenance_status: "unsigned", review_basis: "metadata",
    fetch_status: "discovered", published: null,
    external, external_url: external ? item.link : undefined,
    thumbnail_url: item.thumbnail, media_url: item.media_url,
  };
}
