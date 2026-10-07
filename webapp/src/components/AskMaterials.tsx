import { useEffect, useState } from "react";
import { askHistory, type AskSource } from "../domain/askHistory";
import { Button } from "./ui";
import { tr, useUILanguage } from "../uiI18n";

export default function AskMaterials({ ids, byteLimits, onRemove }: { ids: string[]; byteLimits?: number[]; onRemove?: (id: string) => Promise<void> | void }) {
  return <div className="ai-workspace-materials">{ids.map((id, index) => <Material key={id} id={id} byteLimit={byteLimits?.[index]} onRemove={onRemove} />)}</div>;
}
export function AskAnswerSources({ ids, byteLimits }: { ids: string[]; byteLimits?: number[] }) {
  useUILanguage();
  const [open, setOpen] = useState(false);
  return <details onToggle={(event) => setOpen(event.currentTarget.open)}><summary>{tr("Sources supplied for this answer")}</summary>{open ? <AskMaterials ids={ids} byteLimits={byteLimits} /> : null}</details>;
}
function Material({ id, byteLimit, onRemove }: { id: string; byteLimit?: number; onRemove?: (id: string) => Promise<void> | void }) {
  useUILanguage();
  const [source, setSource] = useState<AskSource | null>(null);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setError("");
    void askHistory.context(id).then((result) => { if (active) setSource(result); }).catch((cause: Error) => { if (active) { setSource(null); setError(cause.message); } });
    return () => { active = false; };
  }, [id, retry]);
  const metadata = source ? <>
      {source.shared_by_peer_id ? <p style={{ overflowWrap: "anywhere" }}>Saved from friend node: {source.shared_by_peer_id}. This is a private saved copy; current sharing permission may differ.</p> : null}
      {source.publisher_peer_id ? <p style={{ overflowWrap: "anywhere" }}>Publisher recorded in the shared copy: {source.publisher_peer_id}. Original authorship is not independently verified.</p> : null}
      {source.source_url && /^https?:\/\//.test(source.source_url) ? <p>{tr("Source")}: <a href={source.source_url} target="_blank" rel="noreferrer">{source.source_url}</a></p> : <p>{source.nas ? `${tr("NAS files")}: ${source.nas.source_name} / ${source.nas.path}` : tr("Source: private document on this node")}</p>}
      <p>{source.extraction_truncated ? tr("Document extraction was truncated. ") : ""}{byteLimit === undefined ? tr("The model receives only the material shown in the send review.") : tr("Supplied excerpt: {{bytes}} bytes of {{total}}.", { bytes: byteLimit, total: source.text_bytes })}{byteLimit !== undefined && byteLimit < source.text_bytes ? tr(" Truncated for the context budget.") : ""}</p>
      <details><summary>{tr(byteLimit === undefined ? "Read local source copy" : "Read supplied excerpt")}</summary><p style={{ whiteSpace: "pre-wrap", maxHeight: "35vh", overflow: "auto" }}>{byteLimit === undefined ? source.text : new TextDecoder().decode(new TextEncoder().encode(source.text ?? "").slice(0, byteLimit))}</p></details>
    </> : null;
  return <section>
    <div className="ai-material-header">{source ? <strong>{tr("Article: {{title}}", { title: source.title })}</strong> : !error ? <span>{tr("Reading local article copy…")}</span> : null}
      {onRemove ? <Button onClick={() => Promise.resolve(onRemove(id)).catch((cause: Error) => setError(cause.message))}>{tr("Remove article from context")}</Button> : null}</div>
    {onRemove && source ? <details><summary>{tr("Source details")}</summary>{metadata}</details> : metadata}
    {error ? <><p role="alert">{error}</p><Button onClick={() => setRetry((value) => value + 1)}>{tr("Retry source")}</Button></> : null}
  </section>;
}
