import { tr, useUILanguage } from "../uiI18n";
import { AlertTriangle, Bookmark, CheckCircle2, Clock3, Eye, ExternalLink, Plus, RefreshCcw, Save, SlidersHorizontal, Sparkles, ThumbsDown, ThumbsUp, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, EmptyState, EvidenceDetails, IconButton, LoadingPanel, NavIcons, PageHeader, Panel } from "../components/ui";
import DigestViewer, { type ViewerAction } from "../components/DigestViewer";
import {
  digestApi as liveDigestApi,
  type AiStatus,
  type ConsumptionRecord,
  type DiscoveryStatus,
  type Digest as DigestPayload,
  type DigestItem,
  type DigestSource,
  type Watcher,
} from "../domain/digestClient";
import type { RecommendationProfile } from "../domain/types";
import { fixtureDigestApi } from "../domain/fixtureDigestClient";

function timeAgo(unix: number): string {
  if (!unix) return "";
  const hours = (Date.now() / 1000 - unix) / 3600;
  if (hours < 1) return tr("just now");
  if (hours < 24) return tr("{{v0}}h ago", { v0: Math.floor(hours) });
  const days = Math.floor(hours / 24);
  return days === 1 ? tr("1 day ago") : tr("{{v0}} days ago", { v0: days });
}

function timeUntil(unix: number): string {
  if (!unix) return tr("not scheduled");
  const seconds = Math.max(0, unix - Date.now() / 1000);
  if (seconds < 90) return tr("within a minute");
  if (seconds < 3600) return tr("in {{count}} minutes", { count: Math.ceil(seconds / 60) });
  return tr("in {{count}} hours", { count: Math.ceil(seconds / 3600) });
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
  useUILanguage();
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
            <Chip tone="info">{tr(item.source_kind)}</Chip>
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
                label={tr("More like this")}
                onClick={() => {
                  setVoted("up");
                  onFeedback(item, "up");
                }}
                disabled={voted !== null}
              />
              <IconButton
                icon={ThumbsDown}
                label={tr("Less like this")}
                onClick={() => {
                  setVoted("down");
                  onFeedback(item, "down");
                }}
                disabled={voted !== null}
              />
              <IconButton
                icon={ExternalLink}
                label={tr("Open")}
                onClick={() => {
                  onOpen(item);
                }}
              />
            </div>
          </div>
          <EvidenceDetails packet={item.evidence_packet} />
        </div>
      </div>
    </Panel>
  );
}

export default function Digest() {
  useUILanguage();
  const { client, notify } = useAppContext();
  const digestApi = client.mode === "fixture" ? fixtureDigestApi : liveDigestApi;
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
  const [viewer, setViewer] = useState<{ items: DigestItem[]; index: number } | null>(null);
  const [consumption, setConsumption] = useState<ConsumptionRecord[]>([]);
  const [profile, setProfile] = useState<RecommendationProfile | null>(null);
  const [direction, setDirection] = useState("");

  const load = async () => {
    setLoading(true);
    try {
      const [digestPayload, sourceList, watcherList, ai, status, recommendationProfile, consumptionRecords] = await Promise.all([
        digestApi.getDigest(),
        digestApi.listSources(),
        digestApi.listWatchers(),
        digestApi.aiStatus().catch(() => null),
        digestApi.markDiscoverySeen().catch(() => null),
        client.getRecommendationProfile(),
        digestApi.listConsumption(),
      ]);
      setDigest(digestPayload);
      setSources(sourceList);
      setWatchers(watcherList);
      setAiStatus(ai);
      setDiscovery(status);
      setProfile(recommendationProfile);
      setDirection(recommendationProfile.direction);
      setConsumption(consumptionRecords);
      window.dispatchEvent(new Event("ryn-discovery-seen"));
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : tr("Could not reach the local node."));
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
      setError(err instanceof Error ? err.message : tr("Refresh failed."));
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
      setError(err instanceof Error ? err.message : tr("Could not add source."));
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
      setError(err instanceof Error ? err.message : tr("Could not remove source."));
    }
  };

  const onFeedback = (item: DigestItem, action: ViewerAction) => {
    if (action === "opened") {
      void digestApi.recordConsumption(item, "opened").then(async () => {
        setConsumption(await digestApi.listConsumption());
      }).catch(() => undefined);
    }
    void digestApi.sendFeedback(item.item_id, action).then(async () => {
      const [nextProfile, nextDigest] = await Promise.all([
        client.getRecommendationProfile(),
        digestApi.getDigest(),
      ]);
      setProfile(nextProfile);
      setDigest(nextDigest);
    }).catch(() => undefined);
    if (action === "down") {
      setHidden((current) => new Set(current).add(item.item_id));
    }
  };

  const updateConsumption = async (
    item: DigestItem,
    action: "bookmark" | "unbookmark" | "progress" | "completed",
    progress?: number,
  ) => {
    await digestApi.recordConsumption(item, action, progress);
    setConsumption(await digestApi.listConsumption());
  };

  const saveProfile = async (
    patch: Partial<Pick<RecommendationProfile, "direction" | "topics" | "platforms">>,
  ) => {
    const next = await client.updateRecommendationProfile(patch);
    setProfile(next);
    setDirection(next.direction);
    setDigest(await digestApi.getDigest());
    notify("ok", tr("Your local For You profile has been updated"));
  };

  const toggleChoice = (field: "topics" | "platforms", id: string) => {
    if (!profile) return;
    const current = profile[field];
    const next = current.includes(id) ? current.filter((value) => value !== id) : [...current, id];
    void saveProfile({ [field]: next });
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
      setNotice(tr("Saved \"{{v0}}\" — it'll lead your next digest.", { v0: result.title }));
      setDigest(await digestApi.getDigest().catch(() => digest));
    } catch (err) {
      setError(err instanceof Error ? err.message : tr("Could not save the link."));
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
      setNotice(tr("Watching \"{{v0}}\" — changes will appear in your digest.", { v0: watcher.title }));
    } catch (err) {
      setError(err instanceof Error ? err.message : tr("Could not add the watcher."));
    } finally {
      setSaving(false);
    }
  };

  const removeWatcher = async (watcherId: string) => {
    try {
      await digestApi.removeWatcher(watcherId);
      setWatchers((current) => current.filter((watcher) => watcher.id !== watcherId));
    } catch (err) {
      setError(err instanceof Error ? err.message : tr("Could not remove the watcher."));
    }
  };

  if (loading) return <LoadingPanel />;

  const items = (digest?.items ?? []).filter((item) => !hidden.has(item.item_id));
  const generated = digest?.generated_at_unix ? timeAgo(digest.generated_at_unix) : null;

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow={tr("Personal assistant")}
        title={tr("For You")}
        context={tr("Discover content selected around your interests.")}
        actions={
          <>
            {aiStatus?.provider ? (
              <Chip tone="ok">{aiStatus.provider}: {aiStatus.model}</Chip>
            ) : (
              <Chip tone="muted">{tr("no AI — run Ollama or add a key")}</Chip>
            )}
            {discovery?.phase === "refreshing" ? <Chip tone="info">{tr("agent discovering")}</Chip> : null}
            {generated ? <Chip tone="muted">{tr("built")} {generated}</Chip> : null}
            <Button icon={RefreshCcw} onClick={() => void refresh()} disabled={refreshing}>
              {refreshing ? tr("Refreshing…") : tr("Refresh")}
            </Button>
          </>
        }
      />

      {digest?.brief ? (
        <Panel title={tr("Briefing")} className="digest-brief">
          <p className="digest-brief-basis">
            <Sparkles size={12} /> {tr("AI synthesis from titles and public-feed summaries only. Numbered references point to items below.")}
          </p>
          <p className="digest-brief-text">{digest.brief}</p>
        </Panel>
      ) : null}

      <Panel className="recommendation-status-panel">
        <div className="recommendation-status-heading">
          <div>
            <span className="eyebrow">{tr("Discovery health")}</span>
            <h2>{client.mode === "fixture" ? tr("Your personal feed") : discovery?.item_count ? tr("{{v0}} items are ready", { v0: discovery.item_count }) : tr("Ryn is collecting your first items")}</h2>
            <p>{discovery?.message || tr("The background agent is preparing its first zero-setup review.")}</p>
          </div>
          <Chip tone={discovery?.phase === "error" ? "danger" : discovery?.degraded ? "warn" : discovery?.item_count ? "ok" : "info"}>
            {client.mode === "fixture" ? tr("Preview") : discovery?.phase === "refreshing" ? tr("reviewing now") : discovery?.degraded ? tr("using healthy sources") : discovery?.item_count ? tr("ready") : tr("starting")}
          </Chip>
        </div>
        <div className="recommendation-readiness-grid">
          <div>
            <CheckCircle2 size={18} />
            <span>{tr("Public sources")}</span>
            <strong>{discovery ? tr("{{healthy}}/{{total}} healthy", { healthy: discovery.healthy_sources, total: discovery.source_count }) : tr("Checking")}</strong>
            <p>{discovery?.cached_sources ? tr("{{count}} unavailable sources are serving cached items.", { count: discovery.cached_sources }) : tr("Each source is checked independently, so one failure cannot blank your feed.")}</p>
          </div>
          <div>
            <Clock3 size={18} />
            <span>{tr("Background schedule")}</span>
            <strong>{timeUntil(discovery?.next_refresh_unix ?? 0)}</strong>
            <p>{discovery?.last_completed_unix ? tr("Last completed {{v0}}.", { v0: timeAgo(discovery.last_completed_unix) }) : tr("The first review starts automatically after the daemon is ready.")}</p>
          </div>
          <div>
            <Sparkles size={18} />
            <span>{tr("Formats available")}</span>
            <strong>{discovery?.formats.length ? discovery.formats.map(format => tr(format)).join(", ") : tr("Collecting")}</strong>
            <p>{tr("No account, API key, YouTube handle, subreddit, or preference setup is required.")}</p>
          </div>
        </div>
        {discovery?.failed_sources ? (
          <div className="recommendation-runtime-note">
            <AlertTriangle size={15} />
            {tr("{{count}} sources are temporarily unavailable. Ryn kept the remaining feed usable and scheduled an earlier retry.", { count: discovery.failed_sources })}
          </div>
        ) : null}
      </Panel>

      {profile ? (
        <Panel className="recommendation-profile-panel">
          <div className="recommendation-profile-heading">
            <div>
              <span className="eyebrow">{tr("Your private recommendation profile")}</span>
              <h2>{tr("Tell Ryn what deserves your attention")}</h2>
              <p>{tr("With no choices, Ryn explores broadly. These preferences and every feedback action now drive this same feed.")}</p>
            </div>
            <Chip tone={profile.feedback_count ? "ok" : "muted"} icon={SlidersHorizontal}>
              {tr("{{count}} feedback signals", { count: profile.feedback_count })}
            </Chip>
          </div>
          <label className="recommendation-direction">
            <span>{tr("Direction")}</span>
            <textarea
              value={direction}
              onChange={(event) => setDirection(event.target.value)}
              placeholder={tr("For example: More local AI and open-source research, less repetitive trend coverage.")}
              rows={3}
            />
          </label>
          <div className="recommendation-choice-group">
            <span>{tr("Topics")}</span>
            <div className="choice-chip-row">
              {profile.topic_choices.map((choice) => (
                <button
                  key={choice.id}
                  type="button"
                  className={profile.topics.includes(choice.id) ? "choice-chip active" : "choice-chip"}
                  onClick={() => toggleChoice("topics", choice.id)}
                >
                  {tr(choice.label)}
                </button>
              ))}
            </div>
          </div>
          <div className="recommendation-choice-group">
            <span>{tr("Platforms and sources")}</span>
            <div className="choice-chip-row">
              {profile.platform_choices.map((choice) => (
                <button
                  key={choice.id}
                  type="button"
                  className={profile.platforms.includes(choice.id) ? "choice-chip active" : "choice-chip"}
                  onClick={() => toggleChoice("platforms", choice.id)}
                >
                  {tr(choice.label)}
                </button>
              ))}
            </div>
          </div>
          <div className="button-row">
            <Button variant="primary" icon={Save} onClick={() => void saveProfile({ direction })}>
              {tr("Save direction")}
            </Button>
            <Button variant="ghost" onClick={() => void saveProfile({ direction: "", topics: [], platforms: [] })}>
              {tr("Explore broadly")}
            </Button>
          </div>
        </Panel>
      ) : null}

      <Panel title={tr("Sources Ryn watches")}>
        <p className="digest-hint digest-default-note">
          {tr("Ryn starts with a broad public catalog automatically. Add a source only when you want more from a particular channel, community, or publication.")}
        </p>
        <div className="digest-add-row">
          <input
            className="digest-add-input"
            placeholder={tr("Paste a YouTube channel, @handle, r/subreddit, or RSS URL")}
            value={newSource}
            onChange={(event) => setNewSource(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void addSource();
            }}
          />
          <Button icon={Plus} variant="primary" onClick={() => void addSource()} disabled={adding}>
            {adding ? tr("Checking…") : tr("Add")}
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
                    {source.builtin ? tr(" · default") : ""}
                    {health && !health.ok ? tr(" — unreachable") : ""}
                  </Chip>
                  <IconButton icon={Trash2} label={tr("Remove {{v0}}", { v0: source.title })} onClick={() => void removeSource(source.id)} />
                </span>
              );
            })}
          </div>
        ) : (
          <p className="digest-hint">
            {tr("The agent is restoring its default public catalog. You do not need to add anything.")}
          </p>
        )}
        <div className="digest-add-row digest-save-row">
          <input
            className="digest-add-input"
            placeholder={tr("Save a link for later, or watch a page for changes")}
            value={saveUrl}
            onChange={(event) => setSaveUrl(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void saveForLater();
            }}
          />
          <Button icon={Bookmark} onClick={() => void saveForLater()} disabled={saving}>
            {tr("Read later")}
          </Button>
          <Button icon={Eye} onClick={() => void watchPage()} disabled={saving}>
            {tr("Watch")}
          </Button>
        </div>
        {watchers.length ? (
          <div className="chip-row digest-source-chips">
            {watchers.map((watcher) => (
              <span key={watcher.id} className="digest-source-chip">
                <Chip tone="info">{tr("watching:")} {watcher.note || watcher.title}</Chip>
                <IconButton
                  icon={Trash2}
                  label={tr("Stop watching {{v0}}", { v0: watcher.title })}
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
              onOpen={() => setViewer({ items, index: position })}
            />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={NavIcons.digest}
          title={sources.length ? tr("The agent is reviewing fresh content") : tr("Starting proactive discovery")}
          body={
            sources.length
              ? tr("This page updates after the current review. You can request an immediate refresh above.")
              : tr("Default sources are installed automatically; no setup is required.")
          }
        />
      )}
      {consumption.length ? (
        <Panel title={tr("Saved and recently opened")}>
          <div className="digest-stack">
            {consumption.slice(0, 8).map((record) => (
              <button
                key={record.item_id}
                type="button"
                className="digest-title"
                onClick={() => setViewer({ items: [record.item], index: 0 })}
              >
                {record.item.title}
                {record.bookmarked ? tr(" · saved") : ""}
                {record.completed ? tr(" · completed") : record.progress ? ` · ${Math.round(record.progress * 100)}%` : ""}
              </button>
            ))}
          </div>
        </Panel>
      ) : null}
      {viewer && viewer.items[viewer.index] ? (
        <DigestViewer
          items={viewer.items}
          index={viewer.index}
          onIndexChange={(index) => setViewer((current) => current ? { ...current, index } : null)}
          onClose={() => setViewer(null)}
          onFeedback={onFeedback}
          bookmarked={Boolean(consumption.find((record) => record.item_id === viewer.items[viewer.index].item_id)?.bookmarked)}
          initialProgress={consumption.find((record) => record.item_id === viewer.items[viewer.index].item_id)?.progress ?? 0}
          onBookmark={(item, bookmarked) => updateConsumption(item, bookmarked ? "bookmark" : "unbookmark")}
          onProgress={(item, progress) => {
            void updateConsumption(item, progress >= 0.95 ? "completed" : "progress", progress);
          }}
          onSteer={async (text) => {
            await digestApi.steer(text);
            setNotice(tr("Got it — Ryn will use that from the next refresh."));
          }}
        />
      ) : null}
    </div>
  );
}
