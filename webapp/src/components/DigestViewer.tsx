import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Loader2,
  Sparkles,
  ThumbsDown,
  ThumbsUp,
  X,
} from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { digestApi, type DigestItem, type ReaderArticle } from "../domain/digestClient";
import { Button, Chip } from "./ui";

export type ViewerAction = "up" | "down" | "opened" | "more_like_this";

function youtubeId(url: string): string {
  try {
    const parsed = new URL(url);
    if (parsed.hostname.includes("youtu.be")) return parsed.pathname.slice(1);
    if (parsed.pathname.startsWith("/shorts/")) return parsed.pathname.split("/")[2] ?? "";
    return parsed.searchParams.get("v") ?? "";
  } catch {
    return "";
  }
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

/**
 * The content experience: open an item, see or play the real thing, react, and
 * move on — the conventions of a video site or a feed reader, not a new idiom.
 * Arrow keys move, Esc closes, l/d rate.
 */
export default function DigestViewer({
  items,
  index,
  onIndexChange,
  onClose,
  onFeedback,
  onSteer,
}: {
  items: DigestItem[];
  index: number;
  onIndexChange: (next: number) => void;
  onClose: () => void;
  onFeedback: (item: DigestItem, action: ViewerAction) => void;
  onSteer: (text: string) => Promise<void>;
}) {
  const item = items[index];
  const [article, setArticle] = useState<ReaderArticle | null>(null);
  const [readerState, setReaderState] = useState<"idle" | "loading" | "failed">("idle");
  const [rated, setRated] = useState<ViewerAction | null>(null);
  const [steerText, setSteerText] = useState("");
  const [steerSaved, setSteerSaved] = useState(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  const kind = item?.content_kind ?? "document";
  const isArticle = kind !== "video" && kind !== "audio" && kind !== "image";
  const embedId = kind === "video" ? youtubeId(item?.link ?? "") : "";

  // Reading position and per-item state must reset when the item changes,
  // otherwise the next article opens scrolled to the middle of the last one.
  useEffect(() => {
    setRated(null);
    setArticle(null);
    setReaderState("idle");
    bodyRef.current?.scrollTo({ top: 0 });
  }, [item?.item_id]);

  // Opening an item is itself a signal, exactly as it is in a feed reader.
  useEffect(() => {
    if (item) onFeedback(item, "opened");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.item_id]);

  useEffect(() => {
    if (!item || !isArticle) return;
    let cancelled = false;
    setReaderState("loading");
    digestApi
      .readArticle(item.link)
      .then((result) => {
        if (cancelled) return;
        setArticle(result);
        setReaderState(result.blocks.length ? "idle" : "failed");
      })
      .catch(() => {
        if (!cancelled) setReaderState("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [item?.item_id, item?.link, isArticle]);

  const go = useCallback(
    (delta: number) => {
      const next = index + delta;
      if (next >= 0 && next < items.length) onIndexChange(next);
    },
    [index, items.length, onIndexChange],
  );

  const rate = useCallback(
    (action: ViewerAction) => {
      if (!item) return;
      setRated(action);
      onFeedback(item, action);
      // Rating is a "done with this one" gesture; advance like a feed does.
      window.setTimeout(() => go(1), 320);
    },
    [item, onFeedback, go],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      if (event.key === "Escape") onClose();
      else if (event.key === "ArrowRight" || event.key === "j") go(1);
      else if (event.key === "ArrowLeft" || event.key === "k") go(-1);
      else if (event.key === "l") rate("up");
      else if (event.key === "d") rate("down");
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [go, onClose, rate]);

  if (!item) return null;

  const submitSteer = async () => {
    const text = steerText.trim();
    if (!text) return;
    await onSteer(text);
    setSteerText("");
    setSteerSaved(true);
    window.setTimeout(() => setSteerSaved(false), 2600);
  };

  return (
    <div
      className="viewer-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <section className="viewer" role="dialog" aria-modal="true" aria-label={item.title}>
        <header className="viewer-head">
          <div className="viewer-kicker">
            <Chip tone="info">{item.source_kind}</Chip>
            <span className="viewer-source">{item.source_title}</span>
            {hostOf(item.link) ? <span className="viewer-host">{hostOf(item.link)}</span> : null}
          </div>
          <div className="viewer-head-right">
            <span className="viewer-count">
              {index + 1} of {items.length}
            </span>
            <button type="button" className="viewer-close" onClick={onClose} aria-label="Close">
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="viewer-body" ref={bodyRef}>
          <h1 className="viewer-title">{item.title}</h1>

          {kind === "video" && embedId ? (
            <div className="viewer-embed">
              <iframe
                src={`https://www.youtube-nocookie.com/embed/${encodeURIComponent(embedId)}`}
                title={item.title}
                allow="accelerometer; autoplay; clipboard-write; encrypted-media; picture-in-picture"
                allowFullScreen
              />
            </div>
          ) : null}

          {kind === "audio" ? (
            <div className="viewer-audio">
              {item.thumbnail ? <img src={item.thumbnail} alt="" className="viewer-audio-art" /> : null}
              {item.media_url ? (
                <audio src={item.media_url} controls preload="none" autoPlay={false} />
              ) : (
                <p className="viewer-note">
                  This episode didn't publish a direct audio link.{" "}
                  <a href={item.link} target="_blank" rel="noreferrer noopener">
                    Open it at the source
                  </a>
                  .
                </p>
              )}
            </div>
          ) : null}

          {kind === "image" ? (
            <div className="viewer-image">
              <img src={item.media_url || item.thumbnail} alt={item.title} />
            </div>
          ) : null}

          {item.ai_summary ? (
            <p className="viewer-ai">
              <Sparkles size={13} /> {item.ai_summary}
            </p>
          ) : null}

          {isArticle ? (
            <div className="viewer-article">
              {readerState === "loading" ? (
                <p className="viewer-note">
                  <Loader2 size={14} className="viewer-spin" /> Your node is fetching this article…
                </p>
              ) : null}
              {article?.byline ? <p className="viewer-byline">{article.byline}</p> : null}
              {article?.blocks.map((block, position) =>
                block.tag.startsWith("h") ? (
                  <h3 key={position}>{block.text}</h3>
                ) : block.tag === "li" ? (
                  <li key={position}>{block.text}</li>
                ) : block.tag === "blockquote" ? (
                  <blockquote key={position}>{block.text}</blockquote>
                ) : (
                  <p key={position}>{block.text}</p>
                ),
              )}
              {readerState === "failed" ? (
                <p className="viewer-note">
                  This page couldn't be read here — some sites render entirely in the browser.{" "}
                  <a href={item.link} target="_blank" rel="noreferrer noopener">
                    Open the original
                  </a>
                  .
                </p>
              ) : null}
            </div>
          ) : null}
        </div>

        <footer className="viewer-foot">
          <div className="viewer-rate">
            <Button
              icon={ThumbsUp}
              variant={rated === "up" ? "primary" : "standard"}
              onClick={() => rate("up")}
            >
              More like this
            </Button>
            <Button
              icon={ThumbsDown}
              variant={rated === "down" ? "danger" : "standard"}
              onClick={() => rate("down")}
            >
              Less
            </Button>
            <a className="viewer-original" href={item.link} target="_blank" rel="noreferrer noopener">
              <ExternalLink size={13} /> Original
            </a>
          </div>

          <div className="viewer-steer">
            <input
              value={steerText}
              placeholder="Tell Ryn what you want more of — “more math explainers, less politics”"
              onChange={(event) => setSteerText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") void submitSteer();
              }}
            />
            <Button onClick={() => void submitSteer()} disabled={!steerText.trim()}>
              {steerSaved ? "Saved" : "Send"}
            </Button>
          </div>

          <div className="viewer-nav">
            <button type="button" onClick={() => go(-1)} disabled={index === 0} aria-label="Previous">
              <ChevronLeft size={18} />
            </button>
            <button
              type="button"
              onClick={() => go(1)}
              disabled={index >= items.length - 1}
              aria-label="Next"
            >
              Next <ChevronRight size={18} />
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
