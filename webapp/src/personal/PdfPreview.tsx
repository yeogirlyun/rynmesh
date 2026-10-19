import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight, Minus, Plus, RefreshCw } from "lucide-react";
// The official polyfilled build also supports older Chromium WebView2 runtimes.
import { getDocument, GlobalWorkerOptions, TextLayer, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist/legacy/build/pdf.mjs";
import workerUrl from "pdfjs-dist/legacy/build/pdf.worker.min.mjs?url";
import "./pdfPreview.css";

export type PdfPreviewLoader = (signal: AbortSignal) => Promise<Blob>;

// Vite emits these as local, versioned assets, including CJK CMaps and font licences.
// A lookup keeps resource requests inside this bundle instead of accepting PDF URLs.
const assetUrls = import.meta.glob<string>([
  "/node_modules/pdfjs-dist/cmaps/*.bcmap",
  "/node_modules/pdfjs-dist/cmaps/LICENSE",
  "/node_modules/pdfjs-dist/standard_fonts/*",
  "/node_modules/pdfjs-dist/wasm/*.wasm",
  "/node_modules/pdfjs-dist/wasm/LICENSE*",
  "/node_modules/pdfjs-dist/LICENSE",
], { eager: true, query: "?url", import: "default" });
class LocalPdfResources {
  async fetch({ kind, filename }: { kind: string; filename: string }): Promise<Uint8Array> {
    const directory = kind === "cMapUrl" ? "cmaps" : kind === "standardFontDataUrl" ? "standard_fonts" : kind === "wasmUrl" ? "wasm" : "";
    const url = assetUrls[`/node_modules/pdfjs-dist/${directory}/${filename}`];
    if (!directory || !url) throw new Error("pdf_resource_unavailable");
    const response = await fetch(url);
    if (!response.ok) throw new Error("pdf_resource_unavailable");
    return new Uint8Array(await response.arrayBuffer());
  }
}
GlobalWorkerOptions.workerSrc = workerUrl;

const labels = {
  en: { loading: "Loading PDF…", failed: "The PDF could not be displayed. Retry or download the original.", changed: "The file has changed. Refresh the file list to view its latest version.", retry: "Retry preview", previous: "Previous PDF page", next: "Next PDF page", out: "Zoom out", in: "Zoom in", fit: "Fit to width", page: "PDF page" },
  zh: { loading: "正在加载 PDF…", failed: "PDF 预览失败，请重试或下载原文件查看。", changed: "文件已更新，请刷新文件列表后预览最新版本。", retry: "重新预览", previous: "上一页 PDF", next: "下一页 PDF", out: "缩小", in: "放大", fit: "适合宽度", page: "PDF 页面" },
};

export function PdfPreview({ load, name }: { load: PdfPreviewLoader; name: string }) {
  const { i18n } = useTranslation();
  const text = labels[i18n.language.startsWith("zh") ? "zh" : "en"];
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [pageNumber, setPageNumber] = useState(1);
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  const [rendering, setRendering] = useState(true);
  const container = useRef<HTMLDivElement>(null);
  const pageElement = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    let task: ReturnType<typeof getDocument> | undefined;
    setPdf(null); setError(""); setPageNumber(1); setZoom(1); setRendering(true);
    void load(controller.signal).then(async blob => {
      const data = new Uint8Array(await blob.arrayBuffer());
      controller.signal.throwIfAborted();
      task = getDocument({ data, BinaryDataFactory: LocalPdfResources, useWorkerFetch: false, enableXfa: false });
      const document = await task.promise;
      if (!controller.signal.aborted) setPdf(document);
    }).catch(reason => {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "pdf_preview_failed");
    });
    return () => { controller.abort(); void task?.destroy().catch(() => {}); };
  }, [load, reload]);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const measure = () => setWidth(Math.max(1, element.clientWidth - 24));
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!pdf || !width || !pageElement.current) return;
    let cancelled = false;
    let render: RenderTask | undefined;
    let textLayer: TextLayer | undefined;
    const holder = pageElement.current;
    // Each render owns its canvas: a cancelled page cannot paint over a newer one.
    const canvas = document.createElement("canvas");
    const layer = document.createElement("div");
    layer.className = "pdf-text-layer";
    holder.replaceChildren(canvas, layer);
    holder.dataset.rendered = "false";
    setRendering(true);
    void pdf.getPage(pageNumber).then(async page => {
      if (cancelled) return;
      const base = page.getViewport({ scale: 1 });
      const viewport = page.getViewport({ scale: width / base.width * zoom });
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.ceil(viewport.width * ratio);
      canvas.height = Math.ceil(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      canvas.setAttribute("role", "img");
      canvas.setAttribute("aria-label", `${name} · ${pageNumber} / ${pdf.numPages}`);
      holder.style.width = `${viewport.width}px`;
      holder.style.height = `${viewport.height}px`;
      layer.style.setProperty("--total-scale-factor", String(viewport.scale));
      render = page.render({ canvas, viewport, transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0] });
      await render.promise;
      if (cancelled) return;
      const content = await page.getTextContent();
      if (cancelled) return;
      textLayer = new TextLayer({ textContentSource: content, container: layer, viewport });
      await textLayer.render();
      if (!cancelled) { holder.dataset.rendered = "true"; setRendering(false); }
    }).catch(reason => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : "pdf_preview_failed");
    });
    return () => { cancelled = true; render?.cancel(); textLayer?.cancel(); canvas.remove(); layer.remove(); };
  }, [pdf, pageNumber, width, zoom, name]);

  return <div className="pdf-preview" ref={container} aria-label={text.page}>
    {error ? <div className="pdf-preview-error"><p role="alert">{error === "agent_artifact_content_changed" ? text.changed : text.failed}</p>
      <button onClick={() => setReload(value => value + 1)}><RefreshCw size={14} />{text.retry}</button></div> : <>
      <div className="pdf-preview-controls">
        <button aria-label={text.previous} disabled={!pdf || pageNumber <= 1} onClick={() => setPageNumber(value => value - 1)}><ChevronLeft size={16} /></button>
        <span aria-label={text.page}>{pdf ? `${pageNumber} / ${pdf.numPages}` : "—"}</span>
        <button aria-label={text.next} disabled={!pdf || pageNumber >= pdf.numPages} onClick={() => setPageNumber(value => value + 1)}><ChevronRight size={16} /></button>
        <button aria-label={text.out} disabled={!pdf || zoom <= 0.5} onClick={() => setZoom(value => Math.max(0.5, value - 0.25))}><Minus size={15} /></button>
        <button className="pdf-fit" disabled={!pdf} onClick={() => setZoom(1)}>{zoom === 1 ? text.fit : `${Math.round(zoom * 100)}%`}</button>
        <button aria-label={text.in} disabled={!pdf || zoom >= 3} onClick={() => setZoom(value => Math.min(3, value + 0.25))}><Plus size={15} /></button>
      </div>
      {rendering && <p className="nas-preview-message" role="status">{text.loading}</p>}
      <div className="pdf-preview-scroll"><div className="pdf-preview-page" ref={pageElement} /></div>
    </>}
  </div>;
}
