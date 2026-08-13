import {
  Database,
  Download,
  Eye,
  FileText,
  Film,
  FilterX,
  Image as ImageIcon,
  Music,
  PlayCircle,
  Sparkles,
} from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { nodeControlUrl } from "../domain/nodeUrl";
import {
  Button,
  Chip,
  ContentRow,
  EmptyState,
  KindIcon,
  LoadingPanel,
  PageHeader,
  Panel,
} from "../components/ui";
import type {
  ContentFilters,
  ContentItem,
  ContentKind,
  IdentityTier,
  ProvenanceStatus,
  SafetyOutcome,
} from "../domain/types";

const contentKinds: Array<ContentKind | "all"> = [
  "all",
  "video",
  "image",
  "audio",
  "document",
  "slides",
  "dataset",
  "code",
  "report",
  "package",
  "model",
];
const safetyValues: Array<SafetyOutcome | "all"> = ["all", "passed", "pending", "flagged", "blocked", "unscanned"];
const tierValues: Array<IdentityTier | "all"> = ["all", "unverified", "attested", "staked", "proven"];
const provenanceValues: Array<ProvenanceStatus | "all"> = ["all", "signed", "partial", "unsigned", "broken"];
const categoryTiles = [
  { label: "All", kind: "all", icon: Sparkles, detail: "Everything your node can see" },
  { label: "Videos", kind: "video", icon: Film, detail: "Watch-style browse, then fetch or play" },
  { label: "Images", kind: "image", icon: ImageIcon, detail: "Visual material and generated stills" },
  { label: "Files", kind: "document", icon: FileText, detail: "Documents, profiles, notes, PDFs" },
  { label: "Audio", kind: "audio", icon: Music, detail: "Voice, music, and sound clips" },
  { label: "Data/code", kind: "dataset", icon: Database, detail: "Datasets first; code stays filterable" },
] as const;

export default function Explore() {
  const { client, peers, notify, confirm } = useAppContext();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<ContentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (params.has("rank")) return;
    let active = true;
    void client.getSettings().then((settings) => {
      if (!active || settings.rank_default === "weight") return;
      const next = new URLSearchParams(params);
      next.set("rank", settings.rank_default);
      setParams(next, { replace: true });
    });
    return () => {
      active = false;
    };
  }, [client, params, setParams]);
  const filters = useMemo<ContentFilters>(
    () => ({
      source: params.get("source") ?? "all",
      kind: (params.get("kind") as ContentKind | null) ?? "all",
      safety: (params.get("safety") as SafetyOutcome | null) ?? "all",
      tier: (params.get("tier") as IdentityTier | null) ?? "all",
      provenance: (params.get("provenance") as ProvenanceStatus | null) ?? "all",
      search: params.get("search") ?? "",
      rank: (params.get("rank") as ContentFilters["rank"]) ?? "weight",
    }),
    [params],
  );

  useEffect(() => {
    let active = true;
    setLoading(true);
    void client
      .listContent(filters)
      .then((content) => {
        if (active) setItems(content);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [client, filters]);

  const setFilter = (key: keyof ContentFilters, value: string) => {
    const next = new URLSearchParams(params);
    if (!value || value === "all") next.delete(key);
    else next.set(key, value);
    setParams(next);
  };

  const clearFilters = () => setParams(new URLSearchParams());
  const publisherMap = new Map(peers.map((peer) => [peer.id, peer]));
  const local = items.filter((item) => item.fetch_status === "local").length;
  const fetched = items.filter((item) => ["fetched_full", "preview_only", "local"].includes(item.fetch_status)).length;
  const discovered = items.filter((item) => item.fetch_status === "discovered").length;
  const mediaMode = filters.kind === "video" || filters.kind === "image" || filters.kind === "audio";

  const refreshItems = async () => {
    setItems(await client.listContent(filters));
  };

  const fetchFullItem = (item: ContentItem) =>
    confirm({
      title: `Fetch ${item.title}?`,
      body: "The local Ryn node will download the full item, verify provenance and hashes, then store it locally for viewing.",
      risk: "high",
      confirmLabel: "Fetch full",
      details: [
        { label: "Kind", value: item.content_kind },
        { label: "Size", value: item.size ?? "unknown" },
      ],
      onConfirm: async () => {
        await client.fetchFullContent(item.content_id, item.provider_peer_id);
        await refreshItems();
        notify("ok", "Full content fetched and verified locally");
      },
    });

  if (loading) return <LoadingPanel />;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Explore"
        title="Available materials"
        context="Browse local, fetched, and discovered content. Fetches and peer queries stay mediated by your node."
        actions={
          <>
            <Chip tone="info">{local} local</Chip>
            <Chip tone="ok">{fetched} fetched</Chip>
            <Chip tone="muted">{discovered} discovered</Chip>
          </>
        }
      />

      <Panel className="filter-panel">
        <div className="category-grid">
          {categoryTiles.map((category) => {
            const Icon = category.icon;
            const active = (filters.kind ?? "all") === category.kind;
            return (
              <button
                key={category.kind}
                className={active ? "category-tile active" : "category-tile"}
                type="button"
                onClick={() => setFilter("kind", category.kind)}
              >
                <Icon size={18} />
                <span>{category.label}</span>
                <small>{category.detail}</small>
              </button>
            );
          })}
        </div>
        <div className="filter-grid">
          <label className="field wide">
            <span>Search</span>
            <input
              value={filters.search ?? ""}
              onChange={(event) => setFilter("search", event.target.value)}
              placeholder="Search title, tag, or description"
            />
          </label>
          <Select label="Rank" value={filters.rank ?? "weight"} values={["weight", "newest", "trusted", "ai", "novelty"]} onChange={(v) => setFilter("rank", v)} />
          <Select label="Kind" value={filters.kind ?? "all"} values={contentKinds} onChange={(v) => setFilter("kind", v)} />
          <Select label="Safety" value={filters.safety ?? "all"} values={safetyValues} onChange={(v) => setFilter("safety", v)} />
          <Select label="Tier" value={filters.tier ?? "all"} values={tierValues} onChange={(v) => setFilter("tier", v)} />
          <Select label="Provenance" value={filters.provenance ?? "all"} values={provenanceValues} onChange={(v) => setFilter("provenance", v)} />
        </div>
        <div className="source-chips">
          {["all", "local", "fetched", "discovered"].map((source) => (
            <button
              key={source}
              className={filters.source === source ? "filter-chip active" : "filter-chip"}
              type="button"
              onClick={() => setFilter("source", source)}
            >
              {source}
            </button>
          ))}
          <Button variant="ghost" icon={FilterX} onClick={clearFilters}>
            Clear filters
          </Button>
        </div>
      </Panel>

      {selected.size ? (
        <div className="selection-toolbar">
          <span className="mono">{selected.size} selected</span>
          <Button icon={Eye} onClick={() => notify("info", "Bulk preview fetch queued through local node")}>
            Fetch Preview
          </Button>
          <Button
            variant="primary"
            icon={Download}
            onClick={() =>
              confirm({
                title: "Fetch full content for selected items?",
                body: "Full fetches may use bandwidth and storage. The local Ryn node will verify manifests, safety receipts, provenance, and hashes before storing bytes.",
                risk: "high",
                confirmLabel: "Fetch full",
                onConfirm: () => notify("ok", "Full fetches requested through local node"),
              })
            }
          >
            Fetch Full
          </Button>
        </div>
      ) : null}

      <Panel className="table-panel">
        {items.length ? (
          mediaMode ? (
            <MediaGallery
              items={items}
              publisherMap={publisherMap}
              onInspect={(item) => navigate(`/items/${item.content_id}`)}
              onFetchFull={fetchFullItem}
            />
          ) : (
            <div className="table-wrap">
            <table className="content-table">
              <thead>
                <tr>
                  <th />
                  <th>Title</th>
                  <th>Kind</th>
                  <th>Source</th>
                  <th>Tier</th>
                  <th>Safety</th>
                  <th>Provenance</th>
                  <th>Fetch</th>
                  <th>Weight</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <ContentRow
                    key={item.content_id}
                    item={item}
                    publisher={publisherMap.get(item.publisher_peer_id)}
                    provider={publisherMap.get(item.provider_peer_id)}
                    selected={selected.has(item.content_id)}
                    onSelect={() =>
                      setSelected((current) => {
                        const next = new Set(current);
                        if (next.has(item.content_id)) next.delete(item.content_id);
                        else next.add(item.content_id);
                        return next;
                      })
                    }
                    onOpen={() => navigate(`/items/${item.content_id}`)}
                  />
                ))}
              </tbody>
            </table>
          </div>
          )
        ) : (
          <EmptyState
            icon={Sparkles}
            title="No items match your filters"
            body="Ask the AI curator to search from the node or loosen the filters."
            action={<Button onClick={() => navigate("/search-ask")}>Ask AI curator</Button>}
          />
        )}
      </Panel>
    </div>
  );
}

function MediaGallery({
  items,
  publisherMap,
  onInspect,
  onFetchFull,
}: {
  items: ContentItem[];
  publisherMap: Map<string, { name: string }>;
  onInspect: (item: ContentItem) => void;
  onFetchFull: (item: ContentItem) => void;
}) {
  return (
    <div className="media-grid">
      {items.map((item) => (
        <article key={`${item.content_id}-${item.provider_peer_id}`} className="media-card">
          <button className="media-thumb" type="button" onClick={() => onInspect(item)}>
            <MediaThumb item={item} />
          </button>
          <div className="media-card-body">
            <div>
              <h3>{item.title}</h3>
              <p>{item.description || `${item.content_kind} from ${publisherMap.get(item.publisher_peer_id)?.name ?? "unknown node"}`}</p>
            </div>
            <div className="media-meta">
              <span>{publisherMap.get(item.publisher_peer_id)?.name ?? "unknown"}</span>
              <span>{item.size ?? "unknown size"}</span>
              <span>{item.fetch_status === "discovered" ? "not downloaded" : "local copy"}</span>
            </div>
            <div className="button-row">
              <Button icon={Eye} onClick={() => onInspect(item)}>
                Inspect
              </Button>
              {item.fetch_status === "local" || item.fetch_status === "fetched_full" ? (
                <Button icon={PlayCircle} variant="primary" onClick={() => window.open(localContentBytesUrl(item.content_id), "_blank", "noopener,noreferrer")}>
                  {item.content_kind === "video" ? "Play" : item.content_kind === "audio" ? "Listen" : "View"}
                </Button>
              ) : (
                <Button icon={Download} variant="primary" onClick={() => onFetchFull(item)}>
                  Fetch
                </Button>
              )}
            </div>
          </div>
        </article>
      ))}
    </div>
  );
}

function MediaThumb({ item }: { item: ContentItem }) {
  const stored = item.fetch_status === "local" || item.fetch_status === "fetched_full";
  const src = localContentBytesUrl(item.content_id);
  if (stored && item.content_type.startsWith("image/")) {
    return <img src={src} alt={item.title} />;
  }
  if (stored && item.content_type.startsWith("video/")) {
    return <video src={src} preload="metadata" muted playsInline />;
  }
  return (
    <span className="media-placeholder">
      <KindIcon kind={item.content_kind} size={34} />
      {item.content_kind}
    </span>
  );
}

function localContentBytesUrl(contentId: string) {
  return nodeControlUrl(`/content/${encodeURIComponent(contentId)}/bytes`);
}

function Select<T extends string>({
  label,
  value,
  values,
  onChange,
}: {
  label: string;
  value: T;
  values: readonly T[];
  onChange: (value: T) => void;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value as T)}>
        {values.map((candidate) => (
          <option key={candidate} value={candidate}>
            {candidate}
          </option>
        ))}
      </select>
    </label>
  );
}
