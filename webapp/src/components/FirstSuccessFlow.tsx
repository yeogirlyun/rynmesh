import { Bookmark, Check, Circle, ExternalLink, RefreshCw, Sparkles, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { NodeClient } from "../domain/nodeClient";
import { digestApi } from "../domain/digestClient";
import type { ContentItem, FirstSuccessStatus, Recommendation } from "../domain/types";
import ContentViewer from "./ContentViewer";
import { Button, Chip, IconButton } from "./ui";

interface FirstSuccessFlowProps {
  client: NodeClient;
  status: FirstSuccessStatus;
  onStatusChange: (status: FirstSuccessStatus) => void;
  onClose: () => void;
}

function mergeItems(recommendations: Recommendation[], items: ContentItem[]): ContentItem[] {
  return recommendations
    .map((recommendation) =>
      recommendation.item ?? items.find((item) => item.content_id === recommendation.contentId),
    )
    .filter((item): item is ContentItem => Boolean(item))
    .filter((item, index, all) => all.findIndex((candidate) => candidate.content_id === item.content_id) === index)
    .slice(0, 3);
}

function Milestone({ done, children }: { done: boolean; children: string }) {
  return <span className={done ? "done" : ""}>{done ? <Check size={14} /> : <Circle size={14} />} {children}</span>;
}

export default function FirstSuccessFlow({
  client,
  status,
  onStatusChange,
  onClose,
}: FirstSuccessFlowProps) {
  const navigate = useNavigate();
  const [recommendations, setRecommendations] = useState<Recommendation[]>([]);
  const [content, setContent] = useState<ContentItem[]>([]);
  const [viewing, setViewing] = useState<ContentItem | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const statusGeneration = useRef(0);
  const candidates = useMemo(() => mergeItems(recommendations, content), [recommendations, content]);

  const refreshStatus = async () => {
    const generation = ++statusGeneration.current;
    const next = await client.getFirstSuccess();
    if (generation === statusGeneration.current) onStatusChange(next);
    return next;
  };

  const loadCandidates = async () => {
    const [nextRecommendations, nextContent] = await Promise.all([
      client.requestRecommendations({ limit: 6 }),
      client.listContent(),
    ]);
    setRecommendations(nextRecommendations);
    setContent(nextContent);
  };

  useEffect(() => {
    if (status.phase === "ready" || status.phase === "awaiting_signal") {
      void loadCandidates().catch(() => setError("Ryn could not load the current picks. Try again."));
    }
  }, [status.phase]);

  useEffect(() => {
    if (status.completed) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refreshStatus().catch(() => undefined);
    }, 3000);
    return () => window.clearInterval(timer);
  }, [client, status.completed]);

  const retry = async () => {
    setBusy(true);
    setError("");
    try {
      if (client.mode === "live") await digestApi.refreshDigest();
      await Promise.all([refreshStatus(), loadCandidates()]);
    } catch {
      setError("The sources are still unavailable. Your node is safe; you can retry when the connection returns.");
    } finally {
      setBusy(false);
    }
  };

  const openItem = async (item: ContentItem) => {
    setViewing(item);
    setBusy(true);
    setError("");
    try {
      await client.recordContentConsumption(item, "opened");
      await refreshStatus();
    } catch {
      setError("The item opened, but Ryn could not save this step. You can retry without losing the item.");
    } finally {
      setBusy(false);
    }
  };

  const saveSignal = async (item: ContentItem) => {
    setBusy(true);
    setError("");
    try {
      await client.recordContentConsumption(item, "bookmark");
      await client.submitRecommendationFeedback(item.content_id, "more");
      await refreshStatus();
    } catch {
      setError("Ryn could not save that choice yet. Try again; the item remains open.");
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async () => {
    setBusy(true);
    try {
      const next = await client.dismissFirstSuccess();
      onStatusChange(next);
      onClose();
    } finally {
      setBusy(false);
    }
  };

  const openedItem = viewing ?? candidates[0] ?? null;

  return (
    <div className="first-success-backdrop" role="presentation">
      <section className="first-success-dialog" role="dialog" aria-modal="true" aria-labelledby="first-success-title">
        <header className="first-success-header">
          <div>
            <span className="eyebrow">First useful result</span>
            <strong>{status.completed ? "Ready to explore" : "About two minutes"}</strong>
          </div>
          <IconButton icon={X} label="Continue later" onClick={() => void dismiss()} disabled={busy} />
        </header>

        <div className="first-success-progress" aria-label="First result progress" aria-live="polite">
          <Milestone done={status.node_ready}>Node ready</Milestone>
          <Milestone done={status.content_ready}>Picks ready</Milestone>
          <Milestone done={status.first_item_opened}>Open one</Milestone>
          <Milestone done={status.first_signal_recorded}>Save a choice</Milestone>
        </div>

        <div className="first-success-body">
          {status.phase === "checking_sources" ? (
            <div className="first-success-state">
              <Sparkles size={34} />
              <h1 id="first-success-title">Finding a few useful things for you</h1>
              <p>Your local node is reviewing its built-in public sources. No account, AI model, or peer connection is required.</p>
              <Button icon={RefreshCw} onClick={() => void retry()} disabled={busy}>{busy ? "Checking…" : "Check now"}</Button>
            </div>
          ) : null}

          {status.phase === "needs_action" ? (
            <div className="first-success-state">
              <RefreshCw size={34} />
              <h1 id="first-success-title">The sources need another try</h1>
              <p>Your node is running and no private data was sent. Check the connection, then retry discovery.</p>
              <Button variant="primary" icon={RefreshCw} onClick={() => void retry()} disabled={busy}>{busy ? "Retrying…" : "Retry discovery"}</Button>
            </div>
          ) : null}

          {status.phase === "ready" ? (
            <>
              <h1 id="first-success-title">Open one real recommendation</h1>
              <p className="first-success-lead">These picks came through your local node. Choose anything that looks useful.</p>
              {status.degraded ? (
                <p className="first-success-cache-note">Some sources are unavailable; {status.item_count} usable picks remain{status.using_cache ? " from the local cache" : ""}.</p>
              ) : null}
              <div className="first-success-picks">
                {candidates.map((item) => (
                  <button key={item.content_id} type="button" onClick={() => void openItem(item)} disabled={busy}>
                    <span><Chip tone="info">{item.source_platform || item.content_kind}</Chip></span>
                    <strong>{item.title}</strong>
                    <small>{item.description || "Open this recommendation"}</small>
                    <span className="first-success-open"><ExternalLink size={15} /> Open</span>
                  </button>
                ))}
              </div>
              {!candidates.length ? (
                <div className="first-success-empty">
                  <p>The picks are ready but could not be displayed yet.</p>
                  <Button icon={RefreshCw} onClick={() => void retry()} disabled={busy}>Reload picks</Button>
                </div>
              ) : null}
            </>
          ) : null}

          {status.phase === "awaiting_signal" ? (
            <div className="first-success-state">
              <Bookmark size={34} />
              <h1 id="first-success-title">Save one useful signal</h1>
              <p>This stays on your device and immediately shapes what Ryn ranks next.</p>
              {openedItem ? (
                <div className="first-success-signal-card">
                  <strong>{openedItem.title}</strong>
                  <Button variant="primary" icon={Bookmark} onClick={() => void saveSignal(openedItem)} disabled={busy}>
                    {busy ? "Saving…" : "Save and show me more like this"}
                  </Button>
                </div>
              ) : (
                <Button onClick={() => void loadCandidates()}>Load the item again</Button>
              )}
            </div>
          ) : null}

          {status.phase === "completed" ? (
            <div className="first-success-state first-success-complete">
              <span className="first-success-check"><Check size={34} /></span>
              <h1 id="first-success-title">Your Ryn is already learning</h1>
              <p>You opened a real item and saved a preference locally. Your next recommendations can now adapt.</p>
              <div className="first-success-actions">
                <Button variant="primary" icon={Sparkles} onClick={() => { onClose(); navigate("/digest"); }}>See more For You</Button>
                <Button onClick={() => { onClose(); navigate("/settings"); }}>Optional: set up private AI</Button>
              </div>
            </div>
          ) : null}

          {error ? <p className="first-success-error" role="alert">{error}</p> : null}
        </div>

        {!status.completed ? (
          <footer className="first-success-footer">
            <span>{status.using_cache ? "Using a safe cached copy while sources refresh." : "Milestones only—titles and choices are not duplicated in onboarding data."}</span>
            <button type="button" onClick={() => void dismiss()} disabled={busy}>Continue later</button>
          </footer>
        ) : null}
      </section>
      {viewing ? <ContentViewer item={viewing} onClose={() => setViewing(null)} /> : null}
    </div>
  );
}
