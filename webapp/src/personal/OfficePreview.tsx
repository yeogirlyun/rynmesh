import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { type FilePreview, type OfficePreviewLoader, type PreviewBlock } from "../domain/agentFilePreview";
import "./officePreview.css";

const labels = {
  en: { loading: "Loading document…", retry: "Retry preview", failed: "The document could not be previewed. Try again or download the original.",
    invalid: "This file is damaged, encrypted, or uses an unsupported document structure. Download it to open in its original app.",
    large: "This document exceeds the preview size limit. Download the original to view it.",
    converter: "This older document format needs LibreOffice installed on the node. Download it to open in its original app.",
    changed: "The file has changed. Refresh the file list to preview its latest version.",
    limited: "Showing a limited content preview. Download the original for all content and formatting.",
    formulas: "Some formulas have no saved result. The formula is shown instead; open in Excel to calculate it.",
    empty: "This document has no visible text content.", sheets: "Worksheets", previous: "Previous slide", next: "Next slide", slide: "Slide", content: "Content preview", layout: "Preview shows document content; original layout and embedded objects may differ.", rows: "rows", columns: "columns" },
  zh: { loading: "正在加载文档…", retry: "重新预览", failed: "文档预览失败，请重试或下载原文件查看。",
    invalid: "文件可能已损坏、已加密或使用了暂不支持的文档结构，请下载后用原应用打开。",
    large: "文件超出预览大小限制，请下载原文件查看。",
    converter: "此旧版文档格式需要节点安装 LibreOffice 才能预览，可下载后用原应用打开。",
    changed: "文件已更新，请刷新文件列表后预览最新版本。",
    limited: "当前展示部分内容，完整内容和格式请下载原文件查看。",
    formulas: "部分公式尚无已保存的计算结果，预览将显示公式，打开 Excel 后可计算。",
    empty: "此文档没有可预览的文本内容。", sheets: "工作表", previous: "上一页幻灯片", next: "下一页幻灯片", slide: "幻灯片", content: "内容预览", layout: "预览展示文档内容，原始排版和嵌入对象可能有所不同。", rows: "行", columns: "列" },
};

function columnLabel(value: number): string {
  let text = "";
  for (; value > 0; value = Math.floor((value - 1) / 26)) text = String.fromCharCode(65 + (value - 1) % 26) + text;
  return text;
}

function Block({ block }: { block: PreviewBlock }) {
  if (block.type === "image") return <img className="office-embedded-image" src={block.url} alt="" loading="lazy" />;
  if (block.type === "table") return <div className="office-document-table"><table><tbody>{block.rows.map((row, index) =>
    <tr key={index}>{row.map((cell, column) => <td key={column}>{cell}</td>)}</tr>)}</tbody></table></div>;
  const heading = /^(?:heading|title|标题)/i.test(block.style);
  return <p className={heading ? "office-document-heading" : undefined}>{block.runs.length ? block.runs.map((run, index) =>
    <span key={index} style={{ fontWeight: run.bold ? 600 : undefined, fontStyle: run.italic ? "italic" : undefined }}>{run.text}</span>) : <br />}</p>;
}

export function OfficePreview({ load }: { load: OfficePreviewLoader }) {
  const { i18n } = useTranslation();
  const text = labels[i18n.language.startsWith("zh") ? "zh" : "en"];
  const [preview, setPreview] = useState<FilePreview | null>(null);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [selected, setSelected] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setPreview(null); setError(""); setSelected(0);
    void load(controller.signal).then(value => {
      if (!controller.signal.aborted) {
        setPreview(value);
        if (value.kind === "spreadsheet") setSelected(Math.max(0, value.sheets.findIndex(sheet => !sheet.hidden)));
      }
    }).catch(reason => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message || "agent_preview_failed" : "agent_preview_failed"); });
    return () => controller.abort();
  }, [load, reload]);
  if (error) {
    const message = error === "agent_preview_converter_missing" ? text.converter : error === "agent_preview_limit" ? text.large
      : error === "agent_preview_invalid" ? text.invalid : error === "agent_artifact_content_changed" ? text.changed : text.failed;
    return <div className="office-preview-error"><p role="alert">{message}</p><button onClick={() => setReload(value => value + 1)}><RefreshCw size={14} />{text.retry}</button></div>;
  }
  if (!preview) return <p className="nas-preview-message" role="status">{text.loading}</p>;
  const sheet = preview.kind === "spreadsheet" ? preview.sheets[selected] : undefined;
  const slide = preview.kind === "presentation" ? preview.slides[selected] : undefined;
  const limited = preview.truncated || sheet?.truncated;
  return <div className="office-preview">
    <div className="office-preview-label">{text.content}</div>
    {limited && <p className="office-preview-notice" role="note">{text.limited}</p>}
    {preview.kind === "spreadsheet" ? <>
      {preview.formulas_uncalculated && <p className="office-preview-notice" role="note">{text.formulas}</p>}
      <div className="office-sheet-tabs" role="tablist" aria-label={text.sheets}>
        {preview.sheets.map((item, index) => <button key={index} role="tab" id={`office-sheet-${index}`} aria-selected={selected === index}
          aria-controls="office-sheet-panel" onClick={() => setSelected(index)}>{item.name}</button>)}
      </div>
      {sheet ? <>
        <div className="office-sheet-size">{sheet.total_rows} {text.rows} · {sheet.total_columns} {text.columns}</div>
        <div className="office-sheet-scroll" role="tabpanel" id="office-sheet-panel" aria-labelledby={`office-sheet-${selected}`} tabIndex={0}>
          <table className="office-sheet"><thead><tr><th aria-label="#" />{Array.from({ length: sheet.columns }, (_, index) => <th key={index} scope="col">{columnLabel(index + 1)}</th>)}</tr></thead>
            <tbody>{Array.from({ length: Math.min(200, sheet.total_rows) }, (_, index) => {
              const row = sheet.rows.find(item => item.index === index + 1);
              return <tr key={index}><th scope="row">{index + 1}</th>{Array.from({ length: sheet.columns }, (_, column) => {
                const cell = row?.cells.find(item => item.column === column + 1);
                return <td key={column} title={cell?.formula || cell?.value}>{cell?.value}</td>;
              })}</tr>;
            })}</tbody>
          </table>
          {!sheet.total_rows && <p className="nas-preview-message">{text.empty}</p>}
        </div>
      </> : <p className="nas-preview-message">{text.empty}</p>}
    </> : preview.kind === "document" ? <>
      <p className="office-layout-note">{text.layout}</p>
      <article className="office-document">{preview.blocks.length ? preview.blocks.map((block, index) => <Block key={index} block={block} />) : <p>{text.empty}</p>}</article>
    </> : <>
      <div className="office-slide-navigation"><button aria-label={text.previous} disabled={selected === 0} onClick={() => setSelected(index => index - 1)}><ChevronLeft size={16} /></button>
        <span>{text.slide} {slide ? selected + 1 : 0} / {preview.total_slides}</span>
        <button aria-label={text.next} disabled={selected >= preview.slides.length - 1} onClick={() => setSelected(index => index + 1)}><ChevronRight size={16} /></button></div>
      <p className="office-layout-note">{text.layout}</p>
      <article className="office-slide" aria-label={`${text.slide} ${selected + 1}`}>
        {slide?.paragraphs.map((paragraph, index) => index === 0 ? <h3 key={index}>{paragraph}</h3> : <p key={index}>{paragraph}</p>)}
        {slide?.images?.map((url, index) => <img className="office-embedded-image" key={index} src={url} alt="" loading="lazy" />)}
        {!slide?.paragraphs.length && !slide?.images?.length && <p>{text.empty}</p>}
      </article>
    </>}
  </div>;
}
