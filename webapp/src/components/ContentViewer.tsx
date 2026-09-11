import { ExternalLink, FileText, Headphones, Image as ImageIcon, Play, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { digestApi } from "../domain/digestClient";
import type { NodeClient } from "../domain/nodeClient";
import type { ContentItem } from "../domain/types";
import { Button, Chip } from "./ui";
import ShareContentButton from "./ShareContentButton";
import AskAboutButton from "./AskAboutButton";
import { friendsApi } from "../domain/friendsClient";
import { offlineApi, type OfflineBody } from "../domain/offlineReading";
import OfflineDownloadButton from "./OfflineDownloadButton";
import OfflineImages from "./OfflineImages";

function youtubeEmbed(url: string | undefined): string {
  if (!url) return "";
  try {
    const parsed = new URL(url);
    const id = parsed.hostname.includes("youtu.be")
      ? parsed.pathname.slice(1)
      : parsed.searchParams.get("v") ?? "";
    return id ? `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}` : "";
  } catch {
    return "";
  }
}

function actionLabel(item: ContentItem) {
  if (item.content_kind === "video") return "Watch original";
  if (item.content_kind === "audio") return "Open audio source";
  if (item.content_kind === "image") return "View original";
  return "Read original";
}

export default function ContentViewer({ item, onClose, client, onRead, loadBody, offlineKey }: {
  item: ContentItem;
  onClose: () => void;
  client?: NodeClient;
  onRead?: () => Promise<void>;
  loadBody?: () => Promise<{ text: string; truncated: boolean }>;
  offlineKey?: string;
}) {
  const [body, setBody] = useState<string[]>([]);
  const [bodyState, setBodyState] = useState<"loading" | "ready" | "failed">("loading");
  const [retry, setRetry] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [progressError, setProgressError] = useState("");
  const [offlineBody, setOfflineBody] = useState<OfflineBody | null>(null);
  const [bodyError, setBodyError] = useState("");
  const [resolvedOfflineKey, setResolvedOfflineKey] = useState("");
  const [settledImageJob, setSettledImageJob] = useState("");
  const readingStarted = useRef(false);
  const imagesReady = !offlineBody?.images.some((image) => image.state === "verified") || settledImageJob === offlineBody?.job_id;
  const [sourceItem, setSourceItem] = useState<string | null>(null);
  const readingId = item.digest_item_id ?? item.content_id;
  const forceSource = sourceItem === readingId;
  const stageRef = useRef<HTMLDivElement>(null);
  const dialogRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const dialog = dialogRef.current;
    const closeButton = dialog?.querySelector<HTMLButtonElement>(".content-viewer-close");
    closeButton?.focus();
    const keyboard = (event: KeyboardEvent) => {
      if (!dialog || Array.from(document.querySelectorAll('[role="dialog"]')).at(-1) !== dialog) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeButton?.click(); }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [tabindex="0"]'))
        .filter((element) => !element.closest('[hidden], [aria-hidden="true"]'));
      const first = focusable[0], last = focusable.at(-1);
      if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) { event.preventDefault(); first?.focus(); }
    };
    document.addEventListener("keydown", keyboard);
    return () => { document.removeEventListener("keydown", keyboard); if (previous?.isConnected) previous.focus(); };
  }, []);
  const savedProgress = useRef(0);
  const progressWrites = useRef<Promise<void>>(Promise.resolve());
  const restore = useRef(0);
  const positionReady = useRef(false);
  const positionLoaded = useRef(false);
  const readCallback = useRef(onRead);
  readCallback.current = onRead;
  const textContent = !["video", "audio", "image"].includes(item.content_kind);
  useEffect(() => {
    if (!client || !textContent) return;
    let active = true;
    let notDownloaded = false;
    setBodyState("loading");
    setBody([]);
    setTruncated(false);
    setOfflineBody(null); setBodyError(""); setResolvedOfflineKey("");
    setSettledImageJob(""); readingStarted.current = false;
    setProgressError("");
    savedProgress.current = 0;
    positionReady.current = false;
    positionLoaded.current = client.mode !== "live";
    const read = async () => {
      let blocks: string[];
      const offline = offlineKey ? { key: offlineKey, body: await offlineApi.body(readingId) }
        : !loadBody && client.mode === "live" && !forceSource ? await offlineApi.resolve(readingId) : null;
      notDownloaded = !offline && !offlineKey && !loadBody && !forceSource && client.mode === "live";
      if (!active) return;
      if (offline) {
        const result = offline.body;
        blocks = result.text.split(/\n\n+/);
        if (active) { setOfflineBody(result); setTruncated(result.truncated); setResolvedOfflineKey(offline.key); }
      } else if (loadBody) {
        const result = await loadBody();
        blocks = [result.text];
        if (active) setTruncated(result.truncated);
      } else if (item.content_id.startsWith("import:")) {
        const result = await friendsApi.document(item.content_id.slice(7));
        blocks = [result.text];
        if (active) setTruncated(result.truncated);
      } else if (item.external_url) {
        const result = await digestApi.readArticle(item.external_url);
        blocks = result.blocks.map((block) => block.text);
        if (active) setTruncated(Boolean(result.truncated));
      } else {
        const result = await client.getContentBody(item.content_id);
        if (!result.ok) throw new Error("reader_unavailable");
        blocks = [result.text];
        if (active) setTruncated(result.truncated);
      }
      if (!blocks.some((block) => block?.trim())) throw new Error("reader_empty");
      if (!active) return;
      if (client.mode === "live") {
        try {
          const history = await digestApi.listConsumption();
          if (!active) return;
          restore.current = history.find((row) => row.item_id === (item.digest_item_id ?? item.content_id))?.progress ?? 0;
          savedProgress.current = restore.current;
          positionLoaded.current = true;
        } catch { if (active) setProgressError("Your saved reading position could not be loaded. Retry reading to restore it."); }
      }
      if (!active) return;
      setBody(blocks);
      setBodyState("ready");
      try { await readCallback.current?.(); }
      catch { if (active) setProgressError("The article is open, but its reading record could not be saved. Retry reading to save it."); }
    };
    void read().catch((cause) => { if (active) { setBodyState("failed"); setBodyError((notDownloaded ? "This body has not been downloaded for offline reading. " : "") + (cause instanceof Error ? cause.message : "The article could not be opened.")); } });
    return () => { active = false; };
  }, [client, item.content_id, item.external_url, textContent, retry, loadBody, offlineKey, readingId, forceSource]);
  useEffect(() => {
    if (bodyState !== "ready" || !imagesReady) return;
    const frame = window.requestAnimationFrame(() => {
      const element = stageRef.current;
      if (element && !readingStarted.current) element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight) * restore.current;
      positionReady.current = positionLoaded.current;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [bodyState, imagesReady]);

  const saveProgress = (force = false) => {
    const element = stageRef.current;
    if (!client || client.mode !== "live" || !element || !textContent || bodyState !== "ready" || !positionReady.current) return Promise.resolve();
    const height = element.scrollHeight - element.clientHeight;
    const progress = height > 0 ? Math.max(0, Math.min(1, element.scrollTop / height)) : 0;
    if (!force && Math.abs(progress - savedProgress.current) < 0.05) return progressWrites.current;
    savedProgress.current = progress;
    const write = progressWrites.current.catch(() => undefined).then(() => client.recordContentConsumption(item, "progress", progress));
    progressWrites.current = write;
    return write.then(() => setProgressError(""), () => {
      savedProgress.current = -1;
      setProgressError("Your reading position could not be saved. Please retry before closing.");
      throw new Error("reading_progress_failed");
    });
  };
  const close = async () => {
    try { await saveProgress(true); onClose(); } catch { /* Keep the retry visible. */ }
  };
  const embed = item.source_platform === "youtube" ? youtubeEmbed(item.external_url) : "";
  const image = item.media_url || item.thumbnail_url || "";
  const directAudio = item.content_kind === "audio" && item.media_url;
  const directVideo = item.content_kind === "video" && item.media_url?.startsWith("http");

  return (
    <div className="content-viewer-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) void close();
    }}>
      <section ref={dialogRef} className="content-viewer" role="dialog" aria-modal="true" aria-label={item.title}>
        <header className="content-viewer-header">
          <div>
            <div className="content-viewer-kicker">
              <Chip tone="info">{item.content_id.startsWith("import:") ? "private saved copy" : item.source_platform || (item.external_url ? "public web" : "Ryn content")}</Chip>
              <Chip tone="muted">{item.content_kind}</Chip>
              <span>{item.source_peer_name}</span>
            </div>
            <h1>{item.title}</h1>
          </div>
          <button type="button" className="content-viewer-close" onClick={() => void close()} aria-label="Close content viewer">
            <X size={20} />
          </button>
        </header>

        <div className="content-viewer-stage" ref={stageRef} onScroll={() => void saveProgress().catch(() => undefined)}
          onWheel={() => { readingStarted.current = true; }} onTouchStart={() => { readingStarted.current = true; }}
          onPointerDown={() => { readingStarted.current = true; }} onKeyDown={(event) => {
            if (["ArrowDown", "ArrowUp", "PageDown", "PageUp", "Home", "End", " "].includes(event.key)) readingStarted.current = true;
          }}>
          {client && textContent ? (
            <article className="content-document-stage" aria-live="polite">
              {bodyState === "loading" ? <p role="status">{offlineKey ? "Opening the saved offline copy…" : "Loading the article through your Ryn…"}</p> : null}
              {offlineBody ? <p>Offline copy · {offlineBody.source} · Saved {new Date(offlineBody.downloaded_at * 1000).toLocaleString()}{offlineBody.partial ? " · Some resources are missing or shortened" : ""}</p> : null}
              {body.map((paragraph, index) => <p key={index}>{paragraph}</p>)}
              {truncated ? <p>This is a shortened preview. The full content has not been loaded.</p> : null}
              {offlineBody && resolvedOfflineKey ? <OfflineImages body={offlineBody} itemKey={resolvedOfflineKey} onReady={setSettledImageJob} /> : null}
              {!imagesReady ? <p role="status">Loading saved images before restoring your reading position. You can start scrolling now.</p> : null}
              {bodyState === "failed" ? <div role="alert"><p>{bodyError || "The article could not be loaded. Try again, or open the original."}</p><Button onClick={() => setRetry((value) => value + 1)}>Retry reading</Button></div> : null}
              {bodyState === "failed" && !offlineKey && !loadBody && !forceSource ? <Button onClick={() => setSourceItem(readingId)}>Try the source instead</Button> : null}
            </article>
          ) : embed ? (
            <iframe
              src={embed}
              title={item.title}
              allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share"
              allowFullScreen
            />
          ) : directAudio ? (
            <div className="content-audio-stage">
              {item.thumbnail_url ? <img src={item.thumbnail_url} alt="" /> : <Headphones size={64} />}
              <audio src={item.media_url} controls preload="metadata" />
            </div>
          ) : directVideo ? (
            <video src={item.media_url} controls playsInline preload="metadata" />
          ) : item.content_kind === "image" && image ? (
            <img className="content-image-stage" src={image} alt={item.title} />
          ) : (
            <div className="content-document-stage">
              {item.content_kind === "video" ? <Play size={48} /> : item.content_kind === "audio" ? <Headphones size={48} /> : item.content_kind === "image" ? <ImageIcon size={48} /> : <FileText size={48} />}
              <h2>{item.title}</h2>
              <p>{item.description || "Open the original source to view the full item."}</p>
            </div>
          )}
        </div>

        <footer className="content-viewer-footer">
          {progressError ? <p role="alert">{progressError} <Button onClick={() => void saveProgress(true).catch(() => undefined)}>Retry saving position</Button></p> : null}
          <p>{item.description}</p>
          {client?.mode === "live" && textContent && !offlineKey ? <OfflineDownloadButton key={item.digest_item_id ?? item.content_id} itemId={item.digest_item_id ?? item.content_id} /> : null}
          {offlineBody ? <p>Opening the original requires a connection. This view does not fetch external media automatically. Sharing or asking saves a separate text copy; clearing downloads keeps that copy.</p> : null}
          {client?.mode === "live" && textContent && bodyState === "ready" ? <ShareContentButton key={`${readingId}:${offlineBody?.job_id ?? "source"}`} itemId={readingId} title={item.title} offlineJobId={offlineBody?.job_id} /> : null}
          {client?.mode === "live" && textContent && bodyState === "ready" ? <AskAboutButton key={`ask:${readingId}:${offlineBody?.job_id ?? "source"}`} itemId={readingId} offlineJobId={offlineBody?.job_id} /> : null}
          {item.external_url ? (
            <Button
              variant="primary"
              icon={ExternalLink}
              onClick={() => window.open(item.external_url, "_blank", "noopener,noreferrer")}
            >
              {actionLabel(item)}
            </Button>
          ) : null}
        </footer>
      </section>
    </div>
  );
}
