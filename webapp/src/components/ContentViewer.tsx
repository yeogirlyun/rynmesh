import { ExternalLink, FileText, Headphones, Image as ImageIcon, Play, X } from "lucide-react";
import type { ContentItem } from "../domain/types";
import { Button, Chip } from "./ui";

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

export default function ContentViewer({ item, onClose }: { item: ContentItem; onClose: () => void }) {
  const embed = item.source_platform === "youtube" ? youtubeEmbed(item.external_url) : "";
  const image = item.media_url || item.thumbnail_url || "";
  const directAudio = item.content_kind === "audio" && item.media_url;
  const directVideo = item.content_kind === "video" && item.media_url?.startsWith("http");

  return (
    <div className="content-viewer-backdrop" role="presentation" onMouseDown={(event) => {
      if (event.target === event.currentTarget) onClose();
    }}>
      <section className="content-viewer" role="dialog" aria-modal="true" aria-label={item.title}>
        <header className="content-viewer-header">
          <div>
            <div className="content-viewer-kicker">
              <Chip tone="info">{item.source_platform || "public web"}</Chip>
              <Chip tone="muted">{item.content_kind}</Chip>
              <span>{item.source_peer_name}</span>
            </div>
            <h1>{item.title}</h1>
          </div>
          <button type="button" className="content-viewer-close" onClick={onClose} aria-label="Close content viewer">
            <X size={20} />
          </button>
        </header>

        <div className="content-viewer-stage">
          {embed ? (
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
          <p>{item.description}</p>
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
