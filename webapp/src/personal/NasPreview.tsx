import { useTranslation } from "react-i18next";
import { useEffect, useRef, useState } from "react";
import {
  Download,
  File,
  FileText,
  Image as ImageIcon,
  Minus,
  Plus,
  Maximize2,
  Sparkles,
  X,
  Eye,
  Clock3,
  Folder,
} from "lucide-react";
import { nasRequest, type NasEntry, type NasSource } from "../domain/nas";
import { Modal } from "./components";

export const isNasImage = (entry: NasEntry) =>
  !entry.directory && /\.(jpe?g|png|webp)$/i.test(entry.name);
export const isNasText = (entry: NasEntry) =>
  !entry.directory &&
  /\.(txt|md|csv|json|log|ya?ml|xml|ini|toml|css|js|ts|py|html)$/i.test(
    entry.name,
  );

function useImage(
  source: string,
  entry: NasEntry,
  thumbnail: boolean,
  enabled = true,
) {
  const [state, setState] = useState({ url: "", error: "", dimensions: "" });
  useEffect(() => {
    setState({ url: "", error: "", dimensions: "" });
    if (!enabled) return;
    const controller = new AbortController();
    let url = "";
    void nasRequest(
      `/sources/${source}/content?path=${encodeURIComponent(entry.path)}&mode=${thumbnail ? "thumbnail" : "image"}`,
      { signal: controller.signal },
    )
      .then(async (response) => {
        const blob = await response.blob();
        if (controller.signal.aborted) return;
        url = URL.createObjectURL(blob);
        const width = response.headers.get("X-Image-Width");
        const height = response.headers.get("X-Image-Height");
        setState({
          url,
          error: "",
          dimensions: width && height ? `${width} × ${height}` : "",
        });
      })
      .catch((error) => {
        if (!controller.signal.aborted)
          setState({ url: "", error: error.message, dimensions: "" });
      });
    return () => {
      controller.abort();
      if (url) URL.revokeObjectURL(url);
    };
  }, [source, entry.path, thumbnail, enabled]);
  return state;
}

export function NasThumbnail({
  source,
  entry,
}: {
  source: string;
  entry: NasEntry;
}) {
  const element = useRef<HTMLSpanElement>(null);
  const [visible, setVisible] = useState(false);
  useEffect(() => {
    if (!element.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(([item]) => {
      if (item.isIntersecting) {
        setVisible(true);
        observer.disconnect();
      }
    });
    observer.observe(element.current);
    return () => observer.disconnect();
  }, []);
  const { url } = useImage(source, entry, true, visible);
  return (
    <span ref={element} className="nas-file-icon image">
      {url ? (
        <img src={url} alt="" />
      ) : (
        <ImageIcon size={23} strokeWidth={1.5} />
      )}
    </span>
  );
}

export function NasPreview({
  source,
  entry,
  onClose,
  download,
  askAi,
  busy,
}: {
  source: NasSource;
  entry: NasEntry;
  onClose: () => void;
  download: () => void;
  askAi: () => void;
  busy: boolean;
}) {
  const { t, i18n } = useTranslation();
  const picture = isNasImage(entry);
  const textFile = isNasText(entry);
  const {
    url,
    error: imageError,
    dimensions,
  } = useImage(source.id, entry, false, picture);
  const [text, setText] = useState("");
  const [textError, setTextError] = useState("");
  const [loading, setLoading] = useState(textFile);
  const [zoom, setZoom] = useState(1);
  const [expanded, setExpanded] = useState(false);
  const [decodeError, setDecodeError] = useState(false);
  useEffect(() => {
    if (!textFile) return;
    const controller = new AbortController();
    void nasRequest(
      `/sources/${source.id}/content?path=${encodeURIComponent(entry.path)}&mode=preview`,
      { signal: controller.signal },
    )
      .then((response) => response.json())
      .then((value) => {
        if (!controller.signal.aborted) setText(value.text);
      })
      .catch((error) => {
        if (!controller.signal.aborted) setTextError(error.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [source.id, entry.path, textFile]);
  const error =
    imageError ||
    textError ||
    (decodeError ? t("personal.thisImageCouldNotBeDisplayed") : "");
  const size =
    entry.size < 1048576
      ? `${(entry.size / 1024).toFixed(1)} KiB`
      : `${(entry.size / 1048576).toFixed(1)} MiB`;
  const imageView = (
    <div className="nas-image-canvas" aria-label={t("personal.imagePreview")}>
      {url && !error ? (
        <img
          src={url}
          alt={entry.name}
          style={{
            width: `${zoom * 100}%`,
            height: zoom === 1 ? "100%" : "auto",
            maxWidth: zoom === 1 ? "100%" : "none",
          }}
          onError={() => setDecodeError(true)}
        />
      ) : (
        <p role={error ? "alert" : "status"}>{error || t("personal.loadingImage")}</p>
      )}
    </div>
  );
  const controls = (
    <div className="nas-image-controls">
      <button
        className="nas-icon-button"
        aria-label={t("personal.zoomOut")}
        disabled={!url || zoom <= 0.5}
        onClick={() => setZoom((z) => Math.max(0.5, z - 0.25))}
      >
        <Minus size={16} />
      </button>
      <button className="nas-fit" onClick={() => setZoom(1)}>
        {zoom === 1 ? t("personal.fitToWindow") : `${Math.round(zoom * 100)}%`}
      </button>
      <button
        className="nas-icon-button"
        aria-label={t("personal.zoomIn")}
        disabled={!url || zoom >= 3}
        onClick={() => setZoom((z) => Math.min(3, z + 0.25))}
      >
        <Plus size={16} />
      </button>
      {!expanded && (
        <button
          className="nas-expand"
          disabled={!url || !!error}
          onClick={() => setExpanded(true)}
        >
          <Maximize2 size={15} />
          {t("personal.viewLarge")}
        </button>
      )}
    </div>
  );
  return (
    <aside className="nas-preview-pane" aria-label={t("personal.filePreview")}>
      <header className="nas-preview-heading">
        <span
          className={`nas-file-icon ${picture ? "image" : textFile ? "text" : ""}`}
        >
          {picture ? (
            <ImageIcon size={25} />
          ) : textFile ? (
            <FileText size={24} />
          ) : (
            <File size={24} />
          )}
        </span>
        <div>
          <h3 title={entry.name}>{entry.name}</h3>
          <p>
            {entry.name.split(".").pop()?.toUpperCase()} ·{" "}
            {dimensions && `${dimensions} · `}
            {size}
          </p>
        </div>
        <button
          className="nas-icon-button"
          aria-label={t("personal.downloadPreview")}
          title={t("personal.downloadOriginal")}
          disabled={busy}
          onClick={download}
        >
          <Download size={17} />
        </button>
        <button
          className="nas-icon-button"
          aria-label={t("personal.closePreview")}
          onClick={onClose}
        >
          <X size={17} />
        </button>
      </header>
      {picture ? (
        <div className="nas-image-frame">
          {imageView}
          {controls}
        </div>
      ) : textFile ? (
        <div className="nas-text-frame">
          <div className="nas-text-label">
            <FileText size={14} />
            {t("personal.textPreview")}
            {source.allow_ai && (
              <button
                className="nas-ai-button"
                disabled={busy || loading || !!error}
                onClick={askAi}
              >
                <Sparkles size={14} />
                {t("personal.askAI")}
              </button>
            )}
          </div>
          {error ? (
            <p role="alert" className="nas-error">
              {error}
            </p>
          ) : loading ? (
            <p className="nas-preview-message" role="status">
              {t("personal.loadingDocument")}
            </p>
          ) : (
            <pre className="nas-preview-text">{text}</pre>
          )}
        </div>
      ) : (
        <div className="nas-preview-unavailable">
          <Eye size={30} />
          <h3>{t("personal.noPreviewForThisFile")}</h3>
          <p>{t("personal.downloadTheOriginalToOpenItOnYourDevice")}</p>
          <button className="pf-button" disabled={busy} onClick={download}>
            <Download size={16} />
            {t("personal.downloadFile")}
          </button>
        </div>
      )}
      <footer className="nas-preview-meta">
        <span>
          <Clock3 size={14} />
          {entry.modified
            ? new Date(
                typeof entry.modified === "number"
                  ? entry.modified * 1000
                  : entry.modified,
              ).toLocaleDateString(i18n.language)
            : t("personal.dateUnavailable")}
        </span>
        <span title={`${source.name} / ${entry.path}`}>
          <Folder size={14} />
          {source.name} /{" "}
          {entry.path.split("/").slice(0, -1).join("/") || t("personal.allFiles")}
        </span>
      </footer>
      {expanded && (
        <Modal title={entry.name} onClose={() => setExpanded(false)}>
          <div className="nas-lightbox">
            {imageView}
            {controls}
            <p>{t("personal.previewDownloadForTheOriginalResolution")}</p>
          </div>
        </Modal>
      )}
    </aside>
  );
}
