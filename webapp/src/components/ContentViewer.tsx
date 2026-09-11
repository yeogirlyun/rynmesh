import { ExternalLink, FileText, Headphones, Image as ImageIcon, Play, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { digestApi } from "../domain/digestClient";
import type { NodeClient } from "../domain/nodeClient";
import type { ContentItem } from "../domain/types";
import { Button, Chip } from "./ui";
import ShareContentButton from "./ShareContentButton";
import AskAboutButton from "./AskAboutButton";
import { friendsApi } from "../domain/friendsClient";

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

export default function ContentViewer({ item, onClose, client, onRead }: {
  item: ContentItem;
  onClose: () => void;
  client?: NodeClient;
  onRead?: () => Promise<void>;
}) {
  const [body, setBody] = useState<string[]>([]);
  const [bodyState, setBodyState] = useState<"loading" | "ready" | "failed">("loading");
  const [retry, setRetry] = useState(0);
  const [truncated, setTruncated] = useState(false);
  const [progressError, setProgressError] = useState("");
  const stageRef = useRef<HTMLDivElement>(null);
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
    setBodyState("loading");
    setBody([]);
    setTruncated(false);
    setProgressError("");
    savedProgress.current = 0;
    positionReady.current = false;
    positionLoaded.current = client.mode !== "live";
    const read = async () => {
      let blocks: string[];
      if (item.content_id.startsWith("import:")) {
        const result = await friendsApi.document(item.content_id.slice(7));
        blocks = [result.text];
        if (active) setTruncated(result.truncated);
      } else if (item.external_url) {
        blocks = (await digestApi.readArticle(item.external_url)).blocks.map((block) => block.text);
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
          restore.current = history.find((row) => row.item_id === (item.digest_item_id ?? item.content_id))?.progress ?? 0;
          savedProgress.current = restore.current;
          positionLoaded.current = true;
        } catch { if (active) setProgressError("Your saved reading position could not be loaded. Retry reading to restore it."); }
      }
      if (!active) return;
      setBody(blocks);
      setBodyState("ready");
      await readCallback.current?.();
    };
    void read().catch(() => { if (active) setBodyState("failed"); });
    return () => { active = false; };
  }, [client, item.content_id, item.external_url, textContent, retry]);
  useEffect(() => {
    if (bodyState !== "ready") return;
    const frame = window.requestAnimationFrame(() => {
      const element = stageRef.current;
      if (element) element.scrollTop = Math.max(0, element.scrollHeight - element.clientHeight) * restore.current;
      positionReady.current = positionLoaded.current;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [bodyState]);

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
      <section className="content-viewer" role="dialog" aria-modal="true" aria-label={item.title}>
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

        <div className="content-viewer-stage" ref={stageRef} onScroll={() => void saveProgress().catch(() => undefined)}>
          {client && textContent ? (
            <article className="content-document-stage" aria-live="polite">
              {bodyState === "loading" ? <p role="status">Loading the article through your Ryn…</p> : null}
              {body.map((paragraph, index) => <p key={index}>{paragraph}</p>)}
              {truncated ? <p>This is a shortened preview. The full content has not been loaded.</p> : null}
              {bodyState === "failed" ? <div role="alert"><p>The article or its reading record could not be loaded. Try again, or open the original.</p><Button onClick={() => setRetry((value) => value + 1)}>Retry reading</Button></div> : null}
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
          {client?.mode === "live" && textContent && bodyState === "ready" ? <ShareContentButton itemId={item.digest_item_id ?? item.content_id} title={item.title} /> : null}
          {client?.mode === "live" && textContent && bodyState === "ready" ? <AskAboutButton itemId={item.digest_item_id ?? item.content_id} /> : null}
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
