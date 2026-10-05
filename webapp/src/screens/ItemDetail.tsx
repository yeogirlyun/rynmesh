import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { Copy, Download, Eye, FileText, Flag, Sparkles } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { nodeControlUrl } from "../domain/nodeUrl";
import {
  Button,
  FetchChip,
  Hash,
  KindIcon,
  KV,
  LoadingPanel,
  PageHeader,
  Panel,
  PeerPill,
  ProvBadge,
  ProvenanceTimeline,
  SafetyBadge,
  TierBadge,
  WeightBar,
} from "../components/ui";
import type { ContentBody, ContentItem, Peer, ProvenanceEvent } from "../domain/types";

type Tab = "overview" | "provenance" | "receipts" | "fetch";

export default function ItemDetail() {
  useUILanguage();
  const { contentId } = useParams();
  const { client, peers, confirm, notify } = useAppContext();
  const navigate = useNavigate();
  const [item, setItem] = useState<ContentItem | null>(null);
  const [contentBody, setContentBody] = useState<ContentBody | null>(null);
  const [events, setEvents] = useState<ProvenanceEvent[]>([]);
  const [tab, setTab] = useState<Tab>("overview");
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    if (!contentId) return;
    setLoading(true);
    void client
      .getContentItem(contentId)
      .then(async (content) => {
        const provenance = await client.getProvenance(content.provenance_head_hash);
        const body = await loadReadableBody(client, content);
        if (!active) return;
        setItem(content);
        setEvents(provenance);
        setContentBody(body);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [client, contentId]);

  const peersById = useMemo(() => new Map(peers.map((peer) => [peer.id, peer])), [peers]);
  const publisher = item ? peersById.get(item.publisher_peer_id) : undefined;
  const provider = item ? peersById.get(item.provider_peer_id) : undefined;

  if (loading) return <LoadingPanel />;
  if (!item) {
    return (
      <Panel>
        <p>{tr("Unknown item.")}</p>
        <Button onClick={() => navigate("/explore")}>{tr("Back to Explore")}</Button>
      </Panel>
    );
  }

  const fetchPreview = async () => {
    await client.fetchPreview(item.content_id, item.provider_peer_id);
    notify("ok", tr("Preview fetch requested through local node"));
  };

  const refreshReadableItem = async () => {
    const refreshed = await client.getContentItem(item.content_id);
    setItem(refreshed);
    setContentBody(await loadReadableBody(client, refreshed));
  };

  const fetchFull = () =>
    confirm({
      title: tr("Fetch full content?"),
      body: tr("The local Ryn node will fetch bytes from the provider, verify the manifest, safety receipts, provenance chain, and content hash, then store the item locally."),
      risk: "high",
      confirmLabel: tr("Fetch full"),
      details: [
        { label: tr("Item"), value: item.title },
        { label: tr("Provider"), value: provider?.name ?? item.provider_peer_id },
        { label: tr("Size"), value: item.size ?? "unknown" },
      ],
      onConfirm: async () => {
        await client.fetchFullContent(item.content_id, item.provider_peer_id);
        await refreshReadableItem();
        notify("ok", tr("Full content fetched and verified locally"));
      },
    });

  const isStoredLocally = item.fetch_status === "local" || item.fetch_status === "fetched_full";
  const isReadableDocument = canReadInline(item);
  const isInlineMedia = canRenderMediaInline(item);

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow={tr("Item detail")}
        title={item.title}
        context={tr("Inspect manifest, provenance, safety receipts, peer identity, and fetch state before acting.")}
        actions={
          <>
            <Button icon={Copy} onClick={() => notify("ok", tr("Citation copied"))}>
              {tr("Copy Citation")}
            </Button>
            {!isStoredLocally ? (
              <Button icon={Eye} onClick={() => void fetchPreview()}>
                {tr("Fetch Preview")}
              </Button>
            ) : null}
            {isStoredLocally ? (
              <Button
                variant="primary"
                icon={isReadableDocument ? FileText : Eye}
                onClick={() => {
                  if (isReadableDocument || isInlineMedia) setTab("overview");
                  else window.open(localContentBytesUrl(item.content_id), "_blank", "noopener,noreferrer");
                }}
              >
                {isReadableDocument ? tr("Read Document") : tr("View Content")}
              </Button>
            ) : (
              <Button variant="primary" icon={Download} onClick={fetchFull}>
                {tr("Fetch Full")}
              </Button>
            )}
          </>
        }
      />

      <div className="tabs">
        {(["overview", "provenance", "receipts", "fetch"] as const).map((candidate) => (
          <button key={candidate} className={tab === candidate ? "active" : ""} type="button" onClick={() => setTab(candidate)}>
            {tr(candidate)}
            {candidate === "provenance" ? <span>{events.length}</span> : null}
            {candidate === "fetch" ? <span>3</span> : null}
          </button>
        ))}
      </div>

      <div className="detail-grid">
        <div className="detail-left">
          <Panel className="preview-panel">
            {contentBody ? (
              <DocumentViewer item={item} body={contentBody} />
            ) : isStoredLocally && isInlineMedia ? (
              <MediaViewer item={item} />
            ) : (
              <PreviewSurface item={item} />
            )}
          </Panel>
          <Panel title={tr("Manifest")}>
            <KV
              rows={[
                { label: tr("Content ID"), value: <Hash value={item.content_id} /> },
                { label: tr("Manifest hash"), value: <Hash value={item.manifest_hash} /> },
                { label: tr("Kind"), value: item.content_kind },
                { label: tr("Type"), value: item.content_type },
                { label: tr("Size"), value: item.size ?? "unknown" },
                { label: tr("Published"), value: item.published ? new Date(item.published).toLocaleString(uiLocale()) : tr("not published") },
                { label: tr("Tags"), value: <span>{item.tags.join(", ")}</span> },
              ]}
            />
          </Panel>
        </div>

        <Panel className="detail-right">
          {tab === "overview" ? (
            <OverviewTab
              item={item}
              publisher={publisher}
              provider={provider}
              onReport={() =>
                confirm({
                  title: tr("Report or quarantine this peer?"),
                  body: tr("This is a high-risk action. Your local node will create a signed report for review."),
                  risk: "high",
                  confirmLabel: tr("Report"),
                  onConfirm: () => notify("warn", tr("Report prepared by local node")),
                })
              }
            />
          ) : null}
          {tab === "provenance" ? <ProvenanceTimeline events={events} peersById={peersById} /> : null}
          {tab === "receipts" ? <ReceiptsTab item={item} publisher={publisher} /> : null}
          {tab === "fetch" ? <FetchLog item={item} provider={provider} /> : null}
        </Panel>
      </div>
    </div>
  );
}

function canReadInline(item: ContentItem) {
  return (
    item.content_type.startsWith("text/") ||
    item.content_type === "application/json" ||
    item.content_kind === "document" ||
    item.content_kind === "code" ||
    item.content_kind === "report"
  );
}

function canRenderMediaInline(item: ContentItem) {
  return (
    item.content_type.startsWith("image/") ||
    item.content_type.startsWith("video/") ||
    item.content_type.startsWith("audio/")
  );
}

function localContentBytesUrl(contentId: string) {
  return nodeControlUrl(`/content/${encodeURIComponent(contentId)}/bytes`);
}

async function loadReadableBody(
  client: ReturnType<typeof useAppContext>["client"],
  item: ContentItem,
): Promise<ContentBody | null> {
  if ((item.fetch_status !== "local" && item.fetch_status !== "fetched_full") || !canReadInline(item)) {
    return null;
  }
  try {
    return await client.getContentBody(item.content_id);
  } catch {
    return null;
  }
}

function MediaViewer({ item }: { item: ContentItem }) {
  useUILanguage();
  const src = localContentBytesUrl(item.content_id);
  return (
    <div className="media-viewer">
      {item.content_type.startsWith("image/") ? <img src={src} alt={item.title} /> : null}
      {item.content_type.startsWith("video/") ? <video src={src} controls playsInline preload="metadata" /> : null}
      {item.content_type.startsWith("audio/") ? <audio src={src} controls /> : null}
      <div className="media-toolbar">
        <span>{item.title}</span>
        <a href={src} target="_blank" rel="noreferrer">
          {tr("Open bytes")}
        </a>
      </div>
    </div>
  );
}

function PreviewSurface({ item }: { item: ContentItem }) {
  useUILanguage();
  return (
    <div className="preview-surface">
      <KindIcon kind={item.content_kind} size={42} />
      <div>
        <span className="eyebrow">{tr(item.content_kind)}</span>
        <h2>{item.title}</h2>
        <p>{item.description}</p>
      </div>
      {item.fetch_status === "discovered" ? <span className="preview-overlay">{tr("Not fetched - metadata only")}</span> : null}
      {item.fetch_status === "preview_only" ? <span className="preview-overlay">{tr("Preview only - fetch full to inspect")}</span> : null}
    </div>
  );
}

function DocumentViewer({ item, body }: { item: ContentItem; body: ContentBody }) {
  useUILanguage();
  return (
    <div className="document-viewer">
      <div className="document-toolbar">
        <div>
          <span className="eyebrow">{tr(item.content_kind)}</span>
          <h2>{item.title}</h2>
        </div>
        <span>{body.truncated ? tr("{{size}}, truncated", { size: body.size }) : body.size}</span>
      </div>
      <pre className="document-body">{body.text}</pre>
    </div>
  );
}

function OverviewTab({
  item,
  publisher,
  provider,
  onReport,
}: {
  item: ContentItem;
  publisher: Peer | undefined;
  provider: Peer | undefined;
  onReport: () => void;
}) {
  useUILanguage();
  return (
    <div className="tab-stack">
      {item.safety_outcome === "flagged" || item.safety_outcome === "blocked" ? (
        <div className="alert-callout">
          <Flag size={16} />
          {item.safety_notes ?? tr("Safety scanner recommends review before propagation.")}
        </div>
      ) : null}
      <section>
        <h3>{tr("Source")}</h3>
        <div className="source-cards">
          <div>
            <span className="eyebrow">{tr("Publisher")}</span>
            <PeerPill peer={publisher} />
          </div>
          <div>
            <span className="eyebrow">{tr("Provider")}</span>
            <PeerPill peer={provider} />
          </div>
        </div>
      </section>
      <section>
        <h3>{tr("Trust signals")}</h3>
        <KV
          rows={[
            { label: tr("Safety"), value: <SafetyBadge outcome={item.safety_outcome} /> },
            { label: tr("Provenance"), value: <ProvBadge status={item.provenance_status} /> },
            { label: tr("Distribution weight"), value: <WeightBar value={item.distribution_weight} /> },
            { label: tr("Identity tier"), value: <TierBadge tier={publisher?.tier ?? "unverified"} /> },
            { label: tr("Fetch state"), value: <FetchChip status={item.fetch_status} /> },
          ]}
        />
      </section>
      <div className="button-row">
        <Button icon={Sparkles}>{tr("More Like This")}</Button>
        <Button variant="ghost">{tr("Hide")}</Button>
        <Button variant="ghost">{tr("Downrank")}</Button>
        <Button variant="danger" icon={Flag} onClick={onReport}>
          {tr("Report")}
        </Button>
      </div>
    </div>
  );
}

function ReceiptsTab({
  item,
  publisher,
}: {
  item: ContentItem;
  publisher: Peer | undefined;
}) {
  useUILanguage();
  return (
    <div className="tab-stack">
      <section>
        <h3>{tr("Safety receipt")}</h3>
        <KV
          rows={[
            { label: tr("Outcome"), value: <SafetyBadge outcome={item.safety_outcome} /> },
            { label: tr("Scanner"), value: item.safety_scanner_id ?? tr("unscanned") },
            { label: tr("Notes"), value: item.safety_notes ?? tr("No scanner notes.") },
          ]}
        />
      </section>
      <section>
        <h3>{tr("Credit signals")}</h3>
        <KV
          rows={[
            { label: tr("Credits"), value: <span className="mono">{publisher?.credits ?? 0}</span> },
            { label: tr("Identity tier"), value: <TierBadge tier={publisher?.tier ?? "unverified"} /> },
            { label: tr("Distribution weight"), value: <WeightBar value={item.distribution_weight} /> },
          ]}
        />
      </section>
      <section className="citation-box">
        <span className="eyebrow">{tr("Citation")}</span>
        <pre>{`rynmesh:${item.content_id}
manifest: ${item.manifest_hash}
publisher: ${publisher?.name ?? item.publisher_peer_id}`}</pre>
      </section>
    </div>
  );
}

function FetchLog({
  item,
  provider,
}: {
  item: ContentItem;
  provider: Peer | undefined;
}) {
  useUILanguage();
  const rows = [
    ["verify", provider?.name ?? item.provider_peer_id, "manifest", item.manifest_hash],
    ["preview", provider?.name ?? item.provider_peer_id, item.fetch_status === "discovered" ? tr("not fetched") : "ok", item.content_id],
    ["full", provider?.name ?? item.provider_peer_id, item.fetch_status === "fetched_full" ? item.size ?? "ok" : tr("not fetched"), item.provenance_head_hash ?? "none"],
  ];
  return (
    <div className="fetch-log">
      {rows.map(([kind, peer, size, hash]) => (
        <div key={`${kind}-${hash}`} className="fetch-row">
          <span>{tr(kind)}</span>
          <b>{peer}</b>
          <span>{size}</span>
          <Hash value={hash} />
        </div>
      ))}
    </div>
  );
}
