import { tr, useUILanguage } from "../uiI18n";
import { Compass, Settings2, Sparkles, UploadCloud } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import ContentViewer from "../components/ContentViewer";
import {
  ActivityIcon,
  Button,
  Chip,
  EmptyState,
  LoadingPanel,
  PageHeader,
  Panel,
  RecommendationCard,
} from "../components/ui";
import type { ActivityEvent, ContentItem, Recommendation } from "../domain/types";
import { digestApi } from "../domain/digestClient";
import RecommendedServices from "./components/RecommendedServices";

export default function Home() {
  useUILanguage();
  const { client, node, peers, notify } = useAppContext();
  const navigate = useNavigate();
  const [activity, setActivity] = useState<ActivityEvent[]>([]);
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [items, setItems] = useState<ContentItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [viewing, setViewing] = useState<ContentItem | null>(null);
  const [availableRecommendations, setAvailableRecommendations] = useState(0);
  const knownDiscoveryItems = useRef(0);

  useEffect(() => {
    let active = true;
    void Promise.all([client.getActivity(), client.requestRecommendations({ limit: 2 }), client.listContent()])
      .then(([events, recs, content]) => {
        if (!active) return;
        setActivity(events);
        setRecommendations(recs);
        setItems(content);
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [client]);

  useEffect(() => {
    const updateDiscovery = () => {
      void digestApi.getDiscoveryStatus().then(async (status) => {
        setAvailableRecommendations(status.item_count);
        if (status.item_count > 0 && status.item_count !== knownDiscoveryItems.current) {
          knownDiscoveryItems.current = status.item_count;
          setRecommendations(await client.requestRecommendations({ limit: 2 }));
        }
      }).catch(() => undefined);
    };
    updateDiscovery();
    const timer = window.setInterval(updateDiscovery, 4000);
    return () => window.clearInterval(timer);
  }, [client]);

  if (loading) return <LoadingPanel />;

  const flagged = items.filter((item) => ["flagged", "blocked"].includes(item.safety_outcome)).length;
  const showingStarters = recommendations.length > 0 && recommendations.every((rec) => rec.item?.starter);

  return (
    <div className="screen-grid home-grid">
      <RecommendedServices client={client} />
      <PageHeader
        eyebrow={tr("Ryn node")}
        title={tr("Local node console")}
        context={tr("Your private assistant, recommendation profile, digest, and peer network—all mediated by the node on this machine.")}
        actions={
          <>
            <Chip tone="ok">{tr("daemon online")}</Chip>
            <Chip tone="info">{node.peer_count} {tr("peers")}</Chip>
          </>
        }
      />

      <Panel className="home-hero">
        <div>
          <span className="eyebrow">{tr("This node")}</span>
          <h2>{node.node_name}</h2>
          <p className="mono">{node.peer_id}</p>
        </div>
        <div className="hero-actions">
          <Button icon={Compass} onClick={() => navigate("/explore")}>
            {tr("Explore")}
          </Button>
          <Button icon={Sparkles} onClick={() => navigate("/search-ask")}>
            {tr("Ask AI")}
          </Button>
          <Button icon={UploadCloud} variant="primary" onClick={() => navigate("/publish")}>
            {tr("Publish")}
          </Button>
          <Button icon={Settings2} onClick={() => navigate("/settings")}>
            {tr("Settings")}
          </Button>
        </div>
      </Panel>

      <div className="stat-grid">
        <StatTile label={tr("Local items")} value={node.local_items} to="/explore?source=local" />
        <StatTile label={tr("Fetched")} value={node.fetched_items} to="/explore?source=fetched" />
        <StatTile
          label={tr("Available recs")}
          value={Math.max(availableRecommendations, recommendations.length)}
          to="/digest"
        />
        <StatTile label={tr("Flagged")} value={flagged} to="/explore?safety=flagged" tone={flagged ? "warn" : "neutral"} />
      </div>

      <Panel title={showingStarters ? tr("Start here: teach your assistant") : tr("Curator Highlights")} className="home-recs">
        {recommendations.length ? (
          <>
            {showingStarters ? (
              <div className="home-starter-note">
                <div>
                  <strong>{tr("Ryn is collecting the first live recommendations.")}</strong>
                  <p>{tr("The background agent is reviewing its built-in public catalog now; this section updates automatically when real content is ready.")}</p>
                </div>
                <Button icon={Sparkles} onClick={() => navigate("/digest")}>{tr("Open For You")}</Button>
              </div>
            ) : null}
            <div className="rec-stack compact">
            {recommendations.map((rec) => {
              const item = rec.item ?? items.find((candidate) => candidate.content_id === rec.contentId);
              if (!item) return null;
              const publisher = peers.find((peer) => peer.id === item.publisher_peer_id);
              return (
                <RecommendationCard
                  key={rec.id}
                  rec={rec}
                  item={item}
                  publisher={publisher}
                  onInspect={() => navigate(`/items/${item.content_id}`)}
                  onOpen={() => {
                    setViewing(item);
                    if (item.digest_item_id) void digestApi.sendFeedback(item.digest_item_id, "opened").catch(() => undefined);
                  }}
                  onFetchPreview={() => notify("info", tr("Preview fetch requested through local node"))}
                  onFetchFull={() => notify("warn", tr("Full fetch requires confirmation from item detail"))}
                  onFeedback={async (action) => {
                    await client.submitRecommendationFeedback(item.content_id, action);
                    if (item.digest_item_id) {
                      void digestApi.sendFeedback(item.digest_item_id, action === "more" ? "up" : "down").catch(() => undefined);
                    }
                    setRecommendations(await client.requestRecommendations({ limit: 2 }));
                    notify("ok", tr("Your local recommendation profile learned from that feedback"));
                  }}
                />
              );
            })}
            </div>
          </>
        ) : (
          <EmptyState title={tr("No recommendations yet")} body={tr("Ask the AI curator to review visible node evidence.")} />
        )}
      </Panel>
      {viewing ? <ContentViewer item={viewing} onClose={() => setViewing(null)} /> : null}

      <Panel title={tr("Recent Activity")} className="activity-panel">
        <div className="activity-list">
          {activity.map((event) => (
            <div key={`${event.t}-${event.text}`} className="activity-row">
              <span className="activity-icon">
                <ActivityIcon kind={event.kind} />
              </span>
              <span>{event.text}</span>
              <b>{event.t}</b>
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}

function StatTile({
  label,
  value,
  to,
  tone = "neutral",
}: {
  label: string;
  value: number;
  to: string;
  tone?: "neutral" | "warn";
}) {
  useUILanguage();
  return (
    <Link className={`stat-tile stat-${tone}`} to={to}>
      <span>{label}</span>
      <strong className="mono">{value}</strong>
    </Link>
  );
}
