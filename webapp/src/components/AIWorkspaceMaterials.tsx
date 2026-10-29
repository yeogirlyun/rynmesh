import { useEffect, useState } from "react";
import { ArrowLeft, FileText, Folder, HardDrive, Bookmark } from "lucide-react";
import { askHistory, type AskSource } from "../domain/askHistory";
import { nasJson, type NasStatus, type NasSource, type NasEntry } from "../domain/nas";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { Modal } from "../personal/components";
import { isNasText } from "../personal/NasPreview";
import { tr, useUILanguage } from "../uiI18n";

export default function AIWorkspaceMaterials({ onSelect, onClose }: { onSelect: (source: AskSource) => Promise<void>; onClose: () => void }) {
  useUILanguage();
  const [tab, setTab] = useState<"nas" | "saved">("nas");
  const [sources, setSources] = useState<NasSource[]>([]);
  const [sourceId, setSourceId] = useState("");
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<NasEntry[]>([]);
  const [saved, setSaved] = useState<ConsumptionRecord[]>([]);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let active = true; setLoading(true); setError("");
    void (async () => {
      try {
        if (tab === "saved") { const rows = await digestApi.listConsumption(); if (active) setSaved(rows.filter(row => row.bookmarked)); }
        else if (!sourceId) { const status = await nasJson<NasStatus>(); if (active) setSources(status.enabled ? status.sources.filter(source => source.allow_ai) : []); }
        else { const result = await nasJson<{ entries: NasEntry[] }>(`/sources/${encodeURIComponent(sourceId)}/files?path=${encodeURIComponent(path)}`); if (active) setEntries(result.entries); }
      } catch (cause) { if (active) setError(cause instanceof Error ? cause.message : tr("Could not load materials.")); }
      finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, [tab, sourceId, path, retry]);
  const attach = async (prepare: () => Promise<AskSource>) => {
    setBusy(true); setError("");
    try { await onSelect(await prepare()); onClose(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : tr("Could not attach this material.")); }
    finally { setBusy(false); }
  };
  return <Modal title={tr("Add materials")} onClose={onClose}>
    <div className="ai-material-tabs"><button type="button" className={tab === "nas" ? "active" : ""} disabled={busy} onClick={() => setTab("nas")}><HardDrive size={16} />{tr("NAS files")}</button><button type="button" className={tab === "saved" ? "active" : ""} disabled={busy} onClick={() => setTab("saved")}><Bookmark size={16} />{tr("Saved articles")}</button></div>
    <p>{tr("Adding saves a source copy on this node for history and citations. Nothing is sent to AI until you review and confirm.")}</p>
    {loading ? <p role="status">{tr("Loading materials…")}</p> : tab === "saved" ? <>
      {saved.map(row => <button className="ai-material-option" type="button" key={row.item_id} disabled={busy} onClick={() => void attach(() => askHistory.prepareContext(row.item_id))}><FileText size={18} /><span>{row.item.title}</span></button>)}
      {!saved.length ? <p>{tr("No saved articles yet. Bookmark an article in My reading first.")}</p> : null}
    </> : !sourceId ? <>
      {sources.map(source => <button className="ai-material-option" type="button" key={source.id} disabled={busy} onClick={() => { setSourceId(source.id); setPath(""); }}><HardDrive size={18} /><span>{source.name}<small>{source.protocol}</small></span></button>)}
      {!sources.length ? <p>{tr("No NAS connection allows AI access. Enable it in NAS connection settings first.")}</p> : null}
    </> : <>
      <button className="pf-button" type="button" disabled={busy} onClick={() => path ? setPath(path.split("/").slice(0, -1).join("/")) : setSourceId("")}><ArrowLeft size={15} />{tr("Back")}</button>
      <p className="ai-material-path">{sources.find(source => source.id === sourceId)?.name} / {path || "/"}</p>
      {entries.map(entry => <button className="ai-material-option" type="button" key={entry.path} disabled={busy || (!entry.directory && !isNasText(entry))} onClick={() => entry.directory ? setPath(entry.path) : void attach(() => askHistory.prepareNas(sourceId, entry.path))}>{entry.directory ? <Folder size={18} /> : <FileText size={18} />}<span>{entry.name}<small>{entry.directory ? tr("Folder") : isNasText(entry) ? tr("UTF-8 text document") : tr("This format is not supported for NAS AI yet.")}</small></span></button>)}
      {!entries.length ? <p>{tr("This folder is empty.")}</p> : null}
    </>}
    {error ? <><p role="alert">{error}</p><button className="pf-button" type="button" disabled={busy} onClick={() => setRetry(value => value + 1)}>{tr("Retry")}</button></> : null}{busy ? <p role="status">{tr("Preparing source copy…")}</p> : null}
  </Modal>;
}
