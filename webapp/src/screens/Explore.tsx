import { tr, useUILanguage } from "../uiI18n";
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
  { get label() { return tr("All"); }, kind: "all", icon: Sparkles, get detail() { return tr("Everything your node can see"); } },
  { get label() { return tr("Videos"); }, kind: "video", icon: Film, get detail() { return tr("Watch-style browse, then fetch or play"); } },
  { get label() { return tr("Images"); }, kind: "image", icon: ImageIcon, get detail() { return tr("Visual material and generated stills"); } },
  { get label() { return tr("Files"); }, kind: "document", icon: FileText, get detail() { return tr("Documents, profiles, notes, PDFs"); } },
  { get label() { return tr("Audio"); }, kind: "audio", icon: Music, get detail() { return tr("Voice, music, and sound clips"); } },
  { get label() { return tr("Data/code"); }, kind: "dataset", icon: Database, get detail() { return tr("Datasets first; code stays filterable"); } },
] as const;

export default function Explore() {
  useUILanguage();
  const { client, peers, notify, confirm } = useAppContext();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<ContentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [fetching, setFetching] = useState(false);

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

  const clearFilters = () => setParams(client.mode === "fixture" ? new URLSearchParams("client=fixture") : new URLSearchParams());
  const publisherMap = new Map(peers.map((peer) => [peer.id, peer]));
  const local = items.filter((item) => item.fetch_status === "local").length;
  const fetched = items.filter((item) => ["fetched_full", "preview_only", "local"].includes(item.fetch_status)).length;
  const discovered = items.filter((item) => item.fetch_status === "discovered").length;
  const mediaMode = filters.kind === "video" || filters.kind === "image" || filters.kind === "audio";

  const refreshItems = async () => {
    setItems(await client.listContent(filters));
  };
  const fetchSelected = async (full: boolean) => {
    if (fetching) return;
    setFetching(true);
    try {
      const results = await Promise.allSettled(items.filter((item) => selected.has(item.content_id)).map((item) =>
        full ? client.fetchFullContent(item.content_id, item.provider_peer_id) : client.fetchPreview(item.content_id, item.provider_peer_id)));
      await refreshItems();
      const failed = results.filter((result) => result.status === "rejected").length;
      notify(failed ? "danger" : "ok", failed ? tr("{{v0}} downloads failed. Please retry.", { v0: failed }) : tr("Selected content downloaded"));
      if (!failed) setSelected(new Set());
    } catch (error) {
      notify("danger", error instanceof Error ? error.message : tr("Download failed"));
    } finally { setFetching(false); }
  };

  const fetchFullItem = (item: ContentItem) =>
    confirm({
      title: tr("Fetch {{v0}}?", { v0: item.title }),
      body: tr("The local Ryn node will download the full item, verify provenance and hashes, then store it locally for viewing."),
      risk: "high",
      confirmLabel: tr("Fetch full"),
      details: [
        { label: tr("Kind"), value: tr(item.content_kind) },
        { label: tr("Size"), value: item.size ?? tr("unknown") },
      ],
      onConfirm: async () => {
        await client.fetchFullContent(item.content_id, item.provider_peer_id);
        await refreshItems();
        notify("ok", tr("Full content fetched and verified locally"));
      },
    });

  if (loading) return <LoadingPanel />;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow={tr("Explore")}
        title={tr("Explore")}
        context={tr("Browse content from your devices and your network.")}
        actions={
          <>
            <Chip tone="info">{local} {tr("local")}</Chip>
            <Chip tone="ok">{fetched} {tr("fetched")}</Chip>
            <Chip tone="muted">{discovered} {tr("discovered")}</Chip>
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
            <span>{tr("Search")}</span>
            <input
              value={filters.search ?? ""}
              onChange={(event) => setFilter("search", event.target.value)}
              placeholder={tr("Search title, tag, or description")}
            />
          </label>
          <Select label={tr("Rank")} value={filters.rank ?? "weight"} values={["weight", "newest", "trusted", "ai", "novelty"]} onChange={(v) => setFilter("rank", v)} />
          <Select label={tr("Kind")} value={filters.kind ?? "all"} values={contentKinds} onChange={(v) => setFilter("kind", v)} />
          <Select label={tr("Safety")} value={filters.safety ?? "all"} values={safetyValues} onChange={(v) => setFilter("safety", v)} />
          <Select label={tr("Tier")} value={filters.tier ?? "all"} values={tierValues} onChange={(v) => setFilter("tier", v)} />
          <Select label={tr("Provenance")} value={filters.provenance ?? "all"} values={provenanceValues} onChange={(v) => setFilter("provenance", v)} />
        </div>
        <div className="source-chips">
          {["all", "local", "fetched", "discovered"].map((source) => (
            <button
              key={source}
              className={filters.source === source ? "filter-chip active" : "filter-chip"}
              type="button"
              onClick={() => setFilter("source", source)}
            >
              {tr(source)}
            </button>
          ))}
          <Button variant="ghost" icon={FilterX} onClick={clearFilters}>
            {tr("Clear filters")}
          </Button>
        </div>
      </Panel>

      {selected.size ? (
        <div className="selection-toolbar">
          <span className="mono">{selected.size} {tr("selected")}</span>
          <Button icon={Eye} disabled={fetching} onClick={() => void fetchSelected(false)}>
            {tr("Fetch Preview")}
          </Button>
          <Button
            variant="primary"
            icon={Download}
            disabled={fetching}
            onClick={() =>
              confirm({
                title: tr("Fetch full content for selected items?"),
                body: tr("Full fetches may use bandwidth and storage. The local Ryn node will verify manifests, safety receipts, provenance, and hashes before storing bytes."),
                risk: "high",
                confirmLabel: tr("Fetch full"),
                onConfirm: () => fetchSelected(true),
              })
            }
          >
            {tr("Fetch Full")}
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
                  <th>{tr("Title")}</th>
                  <th>{tr("Kind")}</th>
                  <th>{tr("Source")}</th>
                  <th>{tr("Tier")}</th>
                  <th>{tr("Safety")}</th>
                  <th>{tr("Provenance")}</th>
                  <th>{tr("Fetch")}</th>
                  <th>{tr("Weight")}</th>
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
            title={tr("No items match your filters")}
            body={tr("Ask the AI curator to search from the node or loosen the filters.")}
            action={<Button onClick={() => navigate("/search-ask")}>{tr("Ask AI curator")}</Button>}
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
  useUILanguage();
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
              <p>{item.description || tr("{{kind}} from {{publisher}}", { kind: tr(item.content_kind), publisher: publisherMap.get(item.publisher_peer_id)?.name ?? tr("unknown node") })}</p>
            </div>
            <div className="media-meta">
              <span>{publisherMap.get(item.publisher_peer_id)?.name ?? tr("unknown")}</span>
              <span>{item.size ?? tr("unknown size")}</span>
              <span>{item.fetch_status === "discovered" ? tr("not downloaded") : tr("local copy")}</span>
            </div>
            <div className="button-row">
              <Button icon={Eye} onClick={() => onInspect(item)}>
                {tr("Inspect")}
              </Button>
              {item.fetch_status === "local" || item.fetch_status === "fetched_full" ? (
                <Button icon={PlayCircle} variant="primary" onClick={() => window.open(localContentBytesUrl(item.content_id), "_blank", "noopener,noreferrer")}>
                  {item.content_kind === "video" ? tr("Play") : item.content_kind === "audio" ? tr("Listen") : tr("View")}
                </Button>
              ) : (
                <Button icon={Download} variant="primary" onClick={() => onFetchFull(item)}>
                  {tr("Fetch")}
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
  useUILanguage();
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
      {tr(item.content_kind)}
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
  useUILanguage();
  return (
    <label className="field">
      <span>{label}</span>
      <select value={value} onChange={(event) => onChange(event.target.value as T)}>
        {values.map((candidate) => (
          <option key={candidate} value={candidate}>
            {tr(candidate)}
          </option>
        ))}
      </select>
    </label>
  );
}
