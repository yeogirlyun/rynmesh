import { useCallback, useEffect, useState } from "react";
import { useAppContext } from "../../appContext";
import { Button } from "../../components/ui";
import { friendsApi } from "../../domain/friendsClient";

type Document = Awaited<ReturnType<typeof friendsApi.documents>>["documents"][number];

export default function PrivateCopiesPanel() {
  const { confirm } = useAppContext();
  const [documents, setDocuments] = useState<Document[] | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    setBusy(true); setError("");
    try { setDocuments((await friendsApi.documents()).documents); }
    catch { setError("Could not inspect document copies. Refresh to try again."); }
    finally { setBusy(false); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  const remove = (document?: Document) => confirm({
    title: document ? `Remove ${document.filename}?` : "Clear all managed document copies?",
    body: "This removes private document files and extracted text from this node, including copies prepared for sharing. Messages, cards and reading bookmarks remain. Downloads already in progress will not restore these files; a new download requires access to your friend's copy.",
    risk: "high", confirmLabel: document ? "Remove local copy" : "Clear document copies",
    onConfirm: async () => {
      const result = document ? await friendsApi.removeDocument(document.import_id) : await friendsApi.clearDocuments();
      setNotice(`${result.removed} local document ${result.removed === 1 ? "copy removed" : "copies removed"}.`);
      await refresh();
    },
  });
  return <section>
    <h3>Private document copies</h3>
    <p>Documents received from friends and local snapshots prepared for sharing. Clearing them also stops this node serving those document bytes.</p>
    {error ? <p role="alert">{error}</p> : null}
    {notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => void refresh()}>Refresh document copies</Button>
    <Button disabled={busy || !documents?.length} onClick={() => remove()}>Clear document copies</Button>
    {documents === null ? <p>Inspecting document copies…</p> : <>
      <p>{documents.length} copies · {(documents.reduce((sum, row) => sum + (row.size_bytes ?? 0), 0) / (1024 * 1024)).toFixed(1)} MiB of source documents</p>
      <ul>{documents.map((document) => <li key={document.import_id}>
        {document.filename} · {document.state === "ready" ? `${document.size_bytes ?? 0} bytes` : "Unavailable"}
        <Button disabled={busy} onClick={() => remove(document)}>Remove {document.filename}</Button>
      </li>)}</ul>
    </>}
  </section>;
}
