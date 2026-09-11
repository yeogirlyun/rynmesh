import {
  Bookmark,
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
import { Button, Chip, EvidenceDetails } from "./ui";
import ShareContentButton from "./ShareContentButton";
import AskAboutButton from "./AskAboutButton";

export type ViewerAction = "up" | "down" | "hide" | "opened" | "more_like_this";

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
  bookmarked,
  onBookmark,
  onProgress,
  initialProgress,
}: {
  items: DigestItem[];
  index: number;
  onIndexChange: (next: number) => void;
  onClose: () => void;
  onFeedback: (item: DigestItem, action: ViewerAction) => Promise<void> | void;
  onSteer: (text: string) => Promise<void>;
  bookmarked: boolean;
  onBookmark: (item: DigestItem, bookmarked: boolean) => Promise<void>;
  onProgress: (item: DigestItem, progress: number) => Promise<void> | void;
  initialProgress: number;
}) {
  const item = items[index];
  const [article, setArticle] = useState<ReaderArticle | null>(null);
  const [readerState, setReaderState] = useState<"idle" | "loading" | "failed">("idle");
  const [rated, setRated] = useState<ViewerAction | null>(null);
  const [steerText, setSteerText] = useState("");
  const [steerSaved, setSteerSaved] = useState(false);
  const [saved, setSaved] = useState(bookmarked);
  const [pending, setPending] = useState(false);
  const [actionError, setActionError] = useState("");
  const [readerAttempt, setReaderAttempt] = useState(0);
  const feedbackCallback = useRef(onFeedback);
  feedbackCallback.current = onFeedback;
  const currentItem = useRef(item?.item_id);
  currentItem.current = item?.item_id;
  const bodyRef = useRef<HTMLDivElement>(null);
  const lastProgress = useRef(0);
  const restoredProgress = useRef(false);
  const progressWrites = useRef<Promise<void>>(Promise.resolve());
  const closeCallback = useRef<() => Promise<void>>(async () => undefined);

  const kind = item?.content_kind ?? "document";
  const isArticle = kind !== "video" && kind !== "audio" && kind !== "image";
  const embedId = kind === "video" ? youtubeId(item?.link ?? "") : "";

  // Reading position and per-item state must reset when the item changes,
  // otherwise the next article opens scrolled to the middle of the last one.
  useEffect(() => {
    setRated(null);
    setArticle(null);
    setReaderState("idle");
    setSaved(bookmarked);
    setActionError("");
    setPending(false);
    lastProgress.current = initialProgress;
    restoredProgress.current = false;
    bodyRef.current?.scrollTo({ top: 0 });
    // Initial progress belongs to this item snapshot; later progress writes
    // must not jump the reader while the owner is scrolling.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [item?.item_id]);

  useEffect(() => {
    setSaved(bookmarked);
  }, [bookmarked, item?.item_id]);

  useEffect(() => {
    if (!item || !isArticle) return;
    let cancelled = false;
    setReaderState("loading");
    digestApi
      .readArticle(item.link)
      .then(async (result) => {
        if (cancelled) return;
        setArticle(result);
        setReaderState(result.blocks.length ? "idle" : "failed");
        if (result.blocks.length) {
          try { await feedbackCallback.current(item, "opened"); }
          catch { if (!cancelled) setActionError("The article is open, but its reading record could not be saved. Retry reading to save it."); }
        }
      })
      .catch(() => {
        if (!cancelled) setReaderState("failed");
      });
    return () => {
      cancelled = true;
    };
  }, [item?.item_id, item?.link, isArticle, readerAttempt]);

  useEffect(() => {
    if (!isArticle || !article || restoredProgress.current || initialProgress <= 0) return;
    restoredProgress.current = true;
    window.requestAnimationFrame(() => {
      const element = bodyRef.current;
      if (!element) return;
      const available = element.scrollHeight - element.clientHeight;
      if (available > 0) element.scrollTo({ top: available * initialProgress });
    });
  }, [article, initialProgress, isArticle]);

  const go = useCallback(
    (delta: number) => {
      const next = index + delta;
      if (next >= 0 && next < items.length) onIndexChange(next);
    },
    [index, items.length, onIndexChange],
  );

  const rate = useCallback(
    async (action: ViewerAction) => {
      if (!item || pending) return;
      setPending(true);
      setActionError("");
      try {
        await onFeedback(item, action);
        if (currentItem.current === item.item_id) setRated(action);
      } catch {
        if (currentItem.current === item.item_id) setActionError("Feedback could not be confirmed. Please retry.");
      } finally {
        if (currentItem.current === item.item_id) setPending(false);
      }
    },
    [item, onFeedback, pending],
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && ["INPUT", "TEXTAREA"].includes(target.tagName)) return;
      if (event.key === "Escape") void closeCallback.current();
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
    setActionError("");
    try {
      await onSteer(text);
      setSteerText("");
      setSteerSaved(true);
      window.setTimeout(() => setSteerSaved(false), 2600);
    } catch { setActionError("Your preference could not be saved. Please retry."); }
  };

  const reportProgress = (progress: number, force = false) => {
    const normalized = Math.max(0, Math.min(1, progress));
    if (!force && Math.abs(normalized - lastProgress.current) < 0.05) return progressWrites.current;
    lastProgress.current = normalized;
    const write = progressWrites.current.catch(() => undefined).then(() => onProgress(item, normalized));
    progressWrites.current = write;
    return write.catch(() => {
      if (currentItem.current === item.item_id) {
        lastProgress.current = -1;
        setActionError("Your reading position could not be saved. Retry closing to save it.");
      }
      throw new Error("reading_progress_failed");
    });
  };

  const closeViewer = async () => {
    try {
      const element = bodyRef.current;
      if (isArticle && article && element) {
        const height = element.scrollHeight - element.clientHeight;
        if (height > 0) await reportProgress(element.scrollTop / height, true);
      }
      await progressWrites.current;
      onClose();
    } catch { /* Keep the reader open with its recoverable save error. */ }
  };
  closeCallback.current = closeViewer;

  return (
    <div
      className="viewer-backdrop"
      role="presentation"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) void closeViewer();
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
            <button type="button" className="viewer-close" onClick={() => void closeViewer()} aria-label="Close">
              <X size={18} />
            </button>
          </div>
        </header>

        <div
          className="viewer-body"
          ref={bodyRef}
          onScroll={(event) => {
            const element = event.currentTarget;
            const available = element.scrollHeight - element.clientHeight;
            if (available > 0 && article && readerState === "idle") void reportProgress(element.scrollTop / available).catch(() => undefined);
          }}
        >
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
                <audio
                  src={item.media_url}
                  controls
                  preload="none"
                  autoPlay={false}
                  onTimeUpdate={(event) => {
                    const media = event.currentTarget;
                    if (Number.isFinite(media.duration) && media.duration > 0) {
                      void reportProgress(media.currentTime / media.duration).catch(() => undefined);
                    }
                  }}
                  onLoadedMetadata={(event) => {
                    const media = event.currentTarget;
                    if (initialProgress > 0 && media.duration > 0) {
                      media.currentTime = media.duration * initialProgress;
                    }
                  }}
                />
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
            <div className="viewer-ai-wrap">
              <p className="viewer-ai">
                <Sparkles size={13} /> {item.ai_summary}
              </p>
              <span>AI summary from the title and public-feed description — not the full content.</span>
            </div>
          ) : null}

          {item.evidence_packet ? <EvidenceDetails packet={item.evidence_packet} /> : null}

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
                  . <Button onClick={() => setReaderAttempt((value) => value + 1)}>Retry reading</Button>
                </p>
              ) : null}
            </div>
          ) : null}
        </div>

        <footer className="viewer-foot">
          {article?.blocks?.length ? <ShareContentButton key={item.item_id} itemId={item.item_id} title={item.title} /> : null}
          {article?.blocks?.length ? <AskAboutButton key={`ask-${item.item_id}`} itemId={item.item_id} /> : null}
          {actionError ? <p role="alert">{actionError} <Button onClick={() => setReaderAttempt((value) => value + 1)}>Retry reading</Button></p> : null}
          <div className="viewer-rate">
            <Button
              icon={ThumbsUp}
              variant={rated === "up" ? "primary" : "standard"}
              disabled={pending}
              onClick={() => rate("up")}
            >
              More like this
            </Button>
            <Button
              icon={ThumbsDown}
              variant={rated === "down" ? "danger" : "standard"}
              disabled={pending}
              onClick={() => rate("down")}
            >
              Less
            </Button>
            <Button disabled={pending} onClick={() => void rate("hide")}>Hide</Button>
            <Button
              icon={Bookmark}
              variant={saved ? "primary" : "standard"}
              disabled={pending}
              onClick={async () => {
                const next = !saved;
                setPending(true);
                setActionError("");
                try {
                  await onBookmark(item, next);
                  if (currentItem.current === item.item_id) setSaved(next);
                } catch {
                  if (currentItem.current === item.item_id) setActionError("The bookmark could not be confirmed. Please retry.");
                } finally {
                  if (currentItem.current === item.item_id) setPending(false);
                }
              }}
            >
              {saved ? "Saved" : "Save"}
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
