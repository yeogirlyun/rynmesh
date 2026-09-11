import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import ContentViewer from "../components/ContentViewer";
import { Button, PageHeader, Panel } from "../components/ui";
import { digestApi, type ConsumptionRecord } from "../domain/digestClient";
import { bytesLabel, offlineActive, offlineApi, offlineError, offlineLabels, type OfflineRecord, type OfflineStatus } from "../domain/offlineReading";
import { contentFromHistory } from "../domain/readingHistory";

export default function OfflineReading() {
  const { client, confirm } = useAppContext();
  const [status, setStatus] = useState<OfflineStatus | null>(null);
  const [saved, setSaved] = useState<ConsumptionRecord[]>([]);
  const [itemId, setItemId] = useState("");
  const [filter, setFilter] = useState("all");
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState<OfflineRecord | null>(null);
  const mounted = useRef(true), generation = useRef(0), acting = useRef(false);
  const refresh = useCallback(async () => {
    const ticket = ++generation.current;
    const next = await offlineApi.status();
    if (mounted.current && ticket === generation.current) { setStatus(next); setLoadError(""); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    let pending = false;
    const poll = async () => {
      if (pending) return; pending = true;
      try { await refresh(); } catch { if (mounted.current) setLoadError("Downloads could not be refreshed. Check your local node and retry."); }
      finally { pending = false; }
    };
    void poll();
    void digestApi.listConsumption().then((rows) => { if (mounted.current) setSaved(rows); })
      .catch(() => { if (mounted.current) setError("Saved items could not be loaded. Existing downloads remain available."); });
    const timer = window.setInterval(() => void poll(), 2000);
    return () => { mounted.current = false; generation.current++; window.clearInterval(timer); };
  }, [refresh]);
  const act = async (operation: () => Promise<unknown>) => {
    if (acting.current) return;
    acting.current = true; setBusy(true); setError(""); setNotice("");
    try { await operation(); await refresh(); }
    catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : "Could not confirm this operation. Retry."); }
    finally { acting.current = false; if (mounted.current) setBusy(false); }
  };
  const clear = async (row?: OfflineRecord) => {
    if (acting.current) return;
    acting.current = true; setBusy(true); setError("");
    let review;
    try { review = await offlineApi.review(row?.item_id); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not review this cleanup."); }
    finally { acting.current = false; if (mounted.current) setBusy(false); }
    if (!review || !mounted.current) return;
    const reviewed = review;
    confirm({ title: row ? `Clear download: ${row.reference.title}?` : "Clear all offline downloads?", risk: "medium", confirmLabel: "Clear downloads",
      body: `${review.copies} saved copies, ${review.pending} active tasks, ${bytesLabel(review.bytes)}. Only offline download copies are removed. Bookmarks, reading positions, conversations and independent saved documents remain.`,
      onConfirm: () => act(async () => {
        const result = await offlineApi.clear(reviewed.review_token, row?.item_id);
        setNotice(`Cleared downloads; ${bytesLabel(result.freed_bytes)} freed.`);
      }) });
  };
  const rows = (status?.records ?? []).filter((row) => filter === "all" || (filter === "downloaded" ? !!row.current : filter === "active" ? offlineActive(row.state) : row.state === "failed"));
  const readingRecord = reading ? saved.find((row) => row.item_id === reading.item_id) ?? {
    item_id: reading.item_id, progress: 0, bookmarked: false, open_count: 0,
    item: { item_id: reading.item_id, title: reading.reference.title, source_title: reading.reference.source,
      link: reading.reference.url, content_kind: "document", summary: "", tags: [] },
  } : null;
  return <div className="screen-stack"><PageHeader eyebrow="Your device" title="Offline reading" context="Download articles before disconnecting. Bookmarks alone do not download the body. No model is required." />
    <Panel><h2>Download a saved or recently opened article</h2>
      <label>Content<select aria-label="Content to download" value={itemId} onChange={(event) => setItemId(event.target.value)}>
        <option value="">Choose content</option>{saved.filter((row) => !["video", "audio", "image"].includes(row.item.content_kind)).map((row) => <option key={row.item_id} value={row.item_id}>{row.item.title}</option>)}</select></label>
      <Button disabled={!itemId || busy} onClick={() => void act(() => offlineApi.download(itemId))}>Download for offline</Button>
      <p>Downloaded friend documents are independent copies. Revoking a share cannot recall an already saved copy.</p>
    </Panel>
    {loadError ? <p role="alert">{loadError}</p> : null}{error ? <p role="alert">{error}</p> : null}{notice ? <p role="status">{notice}</p> : null}
    <Button disabled={busy} onClick={() => void act(async () => { await refresh(); setSaved(await digestApi.listConsumption()); })}>Refresh downloads</Button>
    {!status ? <p role="status">{loadError ? "Download status unavailable." : "Loading downloads…"}</p> : <Panel>
      <h2>Downloads and storage</h2><p>{bytesLabel(status.used_bytes)} used of {bytesLabel(status.limits.total_bytes)}. Up to {bytesLabel(status.limits.item_bytes)} per saved copy, {status.limits.image_count} images, {bytesLabel(status.limits.image_bytes)} per image.</p>
      <p>Updates keep the old copy until the new one is verified. Interrupted downloads reuse verified resources; unfinished resources start again.</p>
      <label>Show<select aria-label="Download filter" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">All</option><option value="downloaded">Available offline</option><option value="active">In progress</option><option value="failed">Failed</option></select></label>
      <Button disabled={busy || !status.records.some((row) => row.state !== "cleared")} onClick={() => void clear()}>Clear all downloads</Button>
      {!rows.length ? <p>No downloads in this view. Choose an article above to start.</p> : rows.map((row) => <section key={row.key} aria-label={row.reference.title}>
        <h3>{row.reference.title}</h3><p>{row.reference.source}</p><p>{offlineLabels[row.state] ?? "Unknown download state"} · {bytesLabel(row.verified_bytes)} verified</p>
        {row.current ? <p>Saved {new Date(row.current.downloaded_at * 1000).toLocaleString()} · {bytesLabel(row.current.size_bytes)}{offlineActive(row.state) || row.state === "failed" ? " · Previous copy is still available" : ""}</p> : null}
        {row.error_code ? <p>{offlineError(row.error_code)}</p> : null}
        {row.current ? <><Button onClick={() => setReading(row)}>Read offline copy</Button><Button disabled={busy || offlineActive(row.state)} onClick={() => void act(() => offlineApi.download(row.item_id, true))}>{row.item_id.startsWith("import:") ? "Refresh from saved document" : "Download latest from source"}</Button></> : null}
        {row.item_id.startsWith("import:") ? <p>Refreshing uses your existing saved document. For newer friend content, check <Link to="/friend-updates">Friend updates</Link> or <Link to="/friends">Friends</Link>. Getting a new copy requires the publisher's current permission.</p> : null}
        {["failed", "cancelled"].includes(row.state) ? <Button disabled={busy} onClick={() => void act(() => offlineApi.retry(row.item_id))}>Retry download</Button> : null}
        {offlineActive(row.state) ? <Button disabled={busy || row.state === "cancel_requested"} onClick={() => void act(() => offlineApi.cancel(row.item_id))}>Cancel download</Button> : null}
        {row.state !== "cleared" ? <Button disabled={busy} onClick={() => void clear(row)}>Clear this download</Button> : null}
      </section>)}
    </Panel>}
    {reading && readingRecord ? <ContentViewer item={contentFromHistory(readingRecord)} client={client} offlineKey={reading.key}
      onRead={() => client.recordContentConsumption(contentFromHistory(readingRecord), "opened")} onClose={() => setReading(null)} /> : null}
  </div>;
}
