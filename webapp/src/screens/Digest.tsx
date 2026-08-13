import { Bookmark, Eye, ExternalLink, Plus, RefreshCcw, Sparkles, ThumbsDown, ThumbsUp, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { Button, Chip, EmptyState, IconButton, LoadingPanel, NavIcons, PageHeader, Panel } from "../components/ui";
import DigestViewer, { type ViewerAction } from "../components/DigestViewer";
import {
  digestApi,
  type AiStatus,
  type DiscoveryStatus,
  type Digest as DigestPayload,
  type DigestItem,
  type DigestSource,
  type Watcher,
} from "../domain/digestClient";

function timeAgo(unix: number): string {
  if (!unix) return "";
  const hours = (Date.now() / 1000 - unix) / 3600;
  if (hours < 1) return "just now";
  if (hours < 24) return `${Math.floor(hours)}h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "1 day ago" : `${days} days ago`;
}

function DigestCard({
  item,
  onFeedback,
  onOpen,
}: {
  item: DigestItem;
  onFeedback: (item: DigestItem, action: ViewerAction) => void;
  onOpen: (item: DigestItem) => void;
}) {
  const [voted, setVoted] = useState<"up" | "down" | null>(null);
  return (
    <Panel className="digest-card">
      <div className="digest-card-main">
        {item.thumbnail ? (
          <button className="digest-thumb-button" type="button" onClick={() => onOpen(item)}>
            <img className="digest-thumb" src={item.thumbnail} alt="" loading="lazy" />
          </button>
        ) : null}
        <div className="digest-card-body">
          <div className="digest-source-line">
            <Chip tone="info">{item.source_kind}</Chip>
            <span className="digest-source-title">{item.source_title}</span>
            <span className="digest-when">{timeAgo(item.published_unix)}</span>
          </div>
          <button
            type="button"
            className="digest-title"
            onClick={() => onOpen(item)}
          >
            {item.title || item.link}
          </button>
          {item.ai_summary ? (
            <p className="digest-summary digest-ai-summary">
              <Sparkles size={12} /> {item.ai_summary}
            </p>
          ) : item.summary ? (
            <p className="digest-summary">{item.summary}</p>
          ) : null}
          <div className="digest-foot">
            <div className="chip-row">
              {item.reasons.map((reason) => (
                <Chip key={reason} tone="muted">
                  {reason}
                </Chip>
              ))}
            </div>
            <div className="digest-actions">
              <IconButton
                icon={ThumbsUp}
                label="More like this"
                onClick={() => {
                  setVoted("up");
                  onFeedback(item, "up");
                }}
                disabled={voted !== null}
              />
              <IconButton
                icon={ThumbsDown}
                label="Less like this"
                onClick={() => {
                  setVoted("down");
                  onFeedback(item, "down");
                }}
                disabled={voted !== null}
              />
              <IconButton
                icon={ExternalLink}
                label="Open"
                onClick={() => {
                  onOpen(item);
                }}
              />
            </div>
          </div>
        </div>
      </div>
    </Panel>
  );
}

export default function Digest() {
  const [digest, setDigest] = useState<DigestPayload | null>(null);
  const [sources, setSources] = useState<DigestSource[]>([]);
  const [watchers, setWatchers] = useState<Watcher[]>([]);
  const [aiStatus, setAiStatus] = useState<AiStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [adding, setAdding] = useState(false);
  const [saving, setSaving] = useState(false);
  const [newSource, setNewSource] = useState("");
  const [saveUrl, setSaveUrl] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [discovery, setDiscovery] = useState<DiscoveryStatus | null>(null);
  const [viewingIndex, setViewingIndex] = useState<number | null>(null);

  const load = async () => {
    setLoading(true);
    try {
      const [digestPayload, sourceList, watcherList, ai, status] = await Promise.all([
        digestApi.getDigest(),
        digestApi.listSources(),
        digestApi.listWatchers(),
        digestApi.aiStatus().catch(() => null),
        digestApi.markDiscoverySeen().catch(() => null),
      ]);
      setDigest(digestPayload);
      setSources(sourceList);
      setWatchers(watcherList);
      setAiStatus(ai);
      setDiscovery(status);
      window.dispatchEvent(new Event("ryn-discovery-seen"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not reach the local node.");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const refresh = async () => {
    setRefreshing(true);
    setError("");
    try {
      const result = await digestApi.refreshDigest();
      setDigest(result.digest);
      setDiscovery(result.status);
      setHidden(new Set());
    } catch (err) {
      setError(err instanceof Error ? err.message : "Refresh failed.");
    } finally {
      setRefreshing(false);
    }
  };

  const addSource = async () => {
    const value = newSource.trim();
    if (!value) return;
    setAdding(true);
    setError("");
    try {
      await digestApi.addSource(value);
      setNewSource("");
      setSources(await digestApi.listSources());
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add source.");
    } finally {
      setAdding(false);
    }
  };

  const removeSource = async (sourceId: string) => {
    try {
      await digestApi.removeSource(sourceId);
      setSources((current) => current.filter((source) => source.id !== sourceId));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove source.");
    }
  };

  const onFeedback = (item: DigestItem, action: ViewerAction) => {
    void digestApi.sendFeedback(item.item_id, action).catch(() => undefined);
    if (action === "down") {
      setHidden((current) => new Set(current).add(item.item_id));
    }
  };

  const saveForLater = async () => {
    const value = saveUrl.trim();
    if (!value) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const result = await digestApi.saveReadLater(value);
      setSaveUrl("");
      setNotice(`Saved "${result.title}" — it'll lead your next digest.`);
      setDigest(await digestApi.getDigest().catch(() => digest));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the link.");
    } finally {
      setSaving(false);
    }
  };

  const watchPage = async () => {
    const value = saveUrl.trim();
    if (!value) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const watcher = await digestApi.addWatcher(value, "");
      setSaveUrl("");
      setWatchers((current) => [...current, watcher]);
      setNotice(`Watching "${watcher.title}" — changes will appear in your digest.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not add the watcher.");
    } finally {
      setSaving(false);
    }
  };

  const removeWatcher = async (watcherId: string) => {
    try {
      await digestApi.removeWatcher(watcherId);
      setWatchers((current) => current.filter((watcher) => watcher.id !== watcherId));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not remove the watcher.");
    }
  };

  if (loading) return <LoadingPanel />;

  const items = (digest?.items ?? []).filter((item) => !hidden.has(item.item_id));
  const generated = digest?.generated_at_unix ? timeAgo(digest.generated_at_unix) : null;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Your agent"
        title="Daily Digest"
        context="Ryn automatically reviews a broad public catalog across video, articles, research, podcasts, audiobooks, and comics. Add personal sources only when you want to."
        actions={
          <>
            {aiStatus?.provider ? (
              <Chip tone="ok">{aiStatus.provider}: {aiStatus.model}</Chip>
            ) : (
              <Chip tone="muted">no AI — run Ollama or add a key</Chip>
            )}
            {discovery?.phase === "refreshing" ? <Chip tone="info">agent discovering</Chip> : null}
            {generated ? <Chip tone="muted">built {generated}</Chip> : null}
            <Button icon={RefreshCcw} onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? "Refreshing…" : "Refresh"}
            </Button>
          </>
        }
      />

      {digest?.brief ? (
        <Panel title="Briefing" className="digest-brief">
          <p className="digest-brief-text">{digest.brief}</p>
        </Panel>
      ) : null}

      <Panel title="Sources Ryn watches">
        <p className="digest-hint digest-default-note">
          Ryn starts with a broad public catalog automatically. Add a source only when you want more from a particular channel, community, or publication.
        </p>
        <div className="digest-add-row">
          <input
            className="digest-add-input"
            placeholder="Paste a YouTube channel, @handle, r/subreddit, or RSS URL"
            value={newSource}
            onChange={(event) => setNewSource(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void addSource();
            }}
          />
          <Button icon={Plus} variant="primary" onClick={() => void addSource()} disabled={adding}>
            {adding ? "Checking…" : "Add"}
          </Button>
        </div>
        {sources.length ? (
          <div className="chip-row digest-source-chips">
            {sources.map((source) => {
              const health = digest?.sources.find((entry) => entry.id === source.id);
              return (
                <span key={source.id} className="digest-source-chip">
                  <Chip tone={health && !health.ok ? "danger" : "ok"}>
                    {source.title}
                    {source.builtin ? " · default" : ""}
                    {health && !health.ok ? " — unreachable" : ""}
                  </Chip>
                  <IconButton icon={Trash2} label={`Remove ${source.title}`} onClick={() => void removeSource(source.id)} />
                </span>
              );
            })}
          </div>
        ) : (
          <p className="digest-hint">
            The agent is restoring its default public catalog. You do not need to add anything.
          </p>
        )}
        <div className="digest-add-row digest-save-row">
          <input
            className="digest-add-input"
            placeholder="Save a link for later, or watch a page for changes"
            value={saveUrl}
            onChange={(event) => setSaveUrl(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void saveForLater();
            }}
          />
          <Button icon={Bookmark} onClick={() => void saveForLater()} disabled={saving}>
            Read later
          </Button>
          <Button icon={Eye} onClick={() => void watchPage()} disabled={saving}>
            Watch
          </Button>
        </div>
        {watchers.length ? (
          <div className="chip-row digest-source-chips">
            {watchers.map((watcher) => (
              <span key={watcher.id} className="digest-source-chip">
                <Chip tone="info">watching: {watcher.note || watcher.title}</Chip>
                <IconButton
                  icon={Trash2}
                  label={`Stop watching ${watcher.title}`}
                  onClick={() => void removeWatcher(watcher.id)}
                />
              </span>
            ))}
          </div>
        ) : null}
        {notice ? <p className="digest-hint">{notice}</p> : null}
        {error ? <p className="digest-error">{error}</p> : null}
      </Panel>

      {items.length ? (
        <div className="digest-stack">
          {items.map((item, position) => (
            <DigestCard
              key={item.item_id}
              item={item}
              onFeedback={onFeedback}
              onOpen={() => setViewingIndex(position)}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={NavIcons.digest}
          title={sources.length ? "The agent is reviewing fresh content" : "Starting proactive discovery"}
          body={
            sources.length
              ? "This page updates after the current review. You can request an immediate refresh above."
              : "Default sources are installed automatically; no setup is required."
          }
        />
      )}
      {viewingIndex !== null && items[viewingIndex] ? (
        <DigestViewer
          items={items}
          index={viewingIndex}
          onIndexChange={setViewingIndex}
          onClose={() => setViewingIndex(null)}
          onFeedback={onFeedback}
          onSteer={async (text) => {
            await digestApi.steer(text);
            setNotice("Got it — Ryn will use that from the next refresh.");
          }}
        />
      ) : null}
    </div>
  );
}

