import { Archive, CheckCircle2, Eye, FileUp, FolderOpen, Pause, Play, RotateCcw, Search, ShieldCheck, Sparkles, Trash2, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, Chip, PageHeader } from "../components/ui";
import type { LibraryItem, LibrarySource, LibraryView, LibraryWatcher } from "../domain/libraryTypes";
import styles from "./MyContent.module.css";
import { libraryCleanup, libraryCleanupScope, libraryReviewCounts } from "../domain/libraryCleanup";
import { nodeControlUrl } from "../domain/nodeUrl";

const tabs: Array<{ view: LibraryView | "sources"; label: string }> = [
  { view: "saved", label: "Saved" },
  { view: "continue", label: "Continue" },
  { view: "history", label: "History" },
  { view: "files", label: "Files" },
  { view: "sources", label: "Sources & monitors" },
  { view: "archived", label: "Archived" },
];

function bytesBase64(buffer: ArrayBuffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary);
}

export default function MyContent() {
  const { client, confirm, notify } = useAppContext();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const initialView = params.get("view") as LibraryView | null;
  const [view, setView] = useState<LibraryView | "sources">(tabs.some((tab) => tab.view === initialView) ? initialView! : "saved");
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [sources, setSources] = useState<LibrarySource[]>([]);
  const [watchers, setWatchers] = useState<LibraryWatcher[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<{ title: string; text: string; truncated: boolean } | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(async (cursor = "", append = false) => {
    setLoading(true);
    setError("");
    try {
      if (view === "sources") {
        const result = await client.listLibrarySources();
        setSources(result.sources);
        setWatchers(result.watchers);
      } else {
        const result = await client.listLibrary({ view, query, kind, cursor, limit: 50 });
        setItems((current) => append ? [...current, ...result.items] : result.items);
        setNextCursor(result.next_cursor);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "My Content is temporarily unavailable.");
    } finally {
      setLoading(false);
    }
  }, [client, kind, query, view]);

  useEffect(() => {
    const timer = window.setTimeout(() => void load(), query ? 180 : 0);
    return () => window.clearTimeout(timer);
  }, [load, query]);

  const switchView = (next: LibraryView | "sources") => {
    setView(next);
    setItems([]);
    setSelected([]);
    setQuery("");
    setKind("");
    setParams(next === "saved" ? {} : { view: next });
  };

  const choose = (id: string) => {
    setSelected((current) => current.includes(id) ? current.filter((value) => value !== id)
      : current.length < 20 ? [...current, id] : current);
  };

  const action = async (name: "archive" | "unarchive" | "mark_completed" | "restart" | "remove_saved", ids = selected) => {
    if (!ids.length) return;
    await client.runLibraryAction({ action: name, library_ids: ids });
    setSelected([]);
    await load();
    notify("ok", "My Content was updated on this node");
  };

  const askSelected = async () => {
    try {
      if (selected.length > 8) throw new Error("You can attach up to eight materials.");
      let materials = selected;
      if (client.mode === "live") {
        const response = await fetch(nodeControlUrl("/library/contexts"), { method: "POST", credentials: "include", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ library_ids: selected }) });
        if (!response.ok) throw new Error("Could not prepare these materials. Reopen them and try again.");
        materials = (await response.json()).materials;
      }
      const next = new URLSearchParams();
      materials.forEach((id) => next.append("material", id));
      navigate(`/ask?${next.toString()}`);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not prepare materials"); }
  };

  const importFile = async (file: File | undefined) => {
    if (!file) return;
    try {
      const lower = file.name.toLowerCase();
      const mime = lower.endsWith(".pdf") ? "application/pdf" : lower.endsWith(".md") || lower.endsWith(".markdown") ? "text/markdown" : file.type || "text/plain";
      await client.importLibraryFile({ filename: file.name, mime, data_base64: bytesBase64(await file.arrayBuffer()) });
      switchView("files");
      notify("ok", `${file.name} copied into private managed storage`);
    } catch {
      notify("danger", "That file could not be imported safely");
    } finally {
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const openItem = async (item: LibraryItem) => {
    if (item.import_id) {
      const result = await client.getLibraryImportPreview(item.import_id);
      setPreview({ title: result.title, text: result.text, truncated: result.truncated });
    }
  };

  const deleteImport = async (item: LibraryItem) => {
    if (!item.import_id) return;
    try {
    const review = client.mode === "live" ? await libraryCleanup.preview(item.import_id) : null;
    confirm({
      title: `Delete Ryn's copy of ${item.title}?`,
      body: "Only the managed copy inside Ryn is deleted. Your original file is never modified or removed." + (review ? ` ${libraryReviewCounts(review)} ${libraryCleanupScope}` : ""),
      risk: "high", confirmLabel: "Delete managed copy",
      onConfirm: async () => {
        if (review) {
          const job = await libraryCleanup.begin(review);
          if (!job.local_copies_complete) throw new Error("Cleanup is unfinished. Continue it in Privacy & data.");
        } else await client.deleteLibraryImport(item.import_id!);
        await load();
        notify("ok", "Managed copy deleted; original file was not touched");
      },
    });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not review document cleanup"); }
  };

  const removeSource = (source: LibrarySource) => confirm({
    title: `Remove ${source.title}?`,
    body: "This stops future checks and removes this source's cached feed items. Reading history and Library metadata remain.",
    risk: "high", confirmLabel: "Remove source",
    onConfirm: async () => { await client.removeLibrarySource(source.id); await load(); },
  });

  const removeWatcher = (watcher: LibraryWatcher) => confirm({
    title: `Stop monitoring ${watcher.title}?`,
    body: "Future checks stop. Existing reading history remains on this device.",
    risk: "high", confirmLabel: "Stop monitoring",
    onConfirm: async () => { await client.removeLibraryWatcher(watcher.id); await load(); },
  });

  const kinds = useMemo(() => [...new Set(items.map((item) => item.kind))].sort(), [items]);

  return (
    <div className={styles.workspace}>
      <PageHeader eyebrow="Private library" title="My Content"
        context="Saved items, reading progress, local files, and sources—owned and managed on this device."
        actions={<><Chip tone="ok" icon={ShieldCheck}>private by default</Chip><Button icon={FileUp} onClick={() => fileInput.current?.click()}>Import file</Button></>} />
      <input ref={fileInput} hidden type="file" accept=".txt,.md,.markdown,.pdf,text/plain,text/markdown,application/pdf"
        onChange={(event) => void importFile(event.target.files?.[0])} />
      <nav className={styles.tabs} aria-label="My Content views">
        {tabs.map((tab) => <button type="button" key={tab.view} className={view === tab.view ? styles.active : ""}
          onClick={() => switchView(tab.view)}>{tab.label}</button>)}
      </nav>

      {view !== "sources" ? <>
        <div className={styles.toolbar}>
          <label><span className="sr-only">Search My Content</span><input type="search" aria-label="Search My Content" value={query}
            onChange={(event) => setQuery(event.target.value)} placeholder="Search title, source, tags, or private file text" /></label>
          <select aria-label="Filter content kind" value={kind} onChange={(event) => setKind(event.target.value)}>
            <option value="">All formats</option>{kinds.map((value) => <option key={value}>{value}</option>)}
          </select>
          <span className={styles.privacy}><Search size={14} /> Search runs only on this node</span>
        </div>
        {selected.length ? <div className={styles.selection} role="status">
          <span><b>{selected.length}</b> selected · maximum 8 for Ask Ryn</span>
          <Button icon={Sparkles} variant="primary" onClick={askSelected}>Ask Ryn</Button>
          <Button icon={Archive} onClick={() => void action(view === "archived" ? "unarchive" : "archive")}>{view === "archived" ? "Restore" : "Archive"}</Button>
          <Button icon={CheckCircle2} onClick={() => void action("mark_completed")}>Complete</Button>
        </div> : null}
        {error ? <div className={styles.empty} role="alert"><h3>Could not load My Content</h3><p>{error}</p><Button onClick={() => void load()}>Retry</Button></div> : null}
        {!error && !loading && !items.length ? <div className={styles.empty}><FolderOpen size={30} /><h3>Nothing in this view yet</h3><p>Save an item or import a private TXT, Markdown, or PDF file.</p></div> : null}
        <div className={styles.grid} aria-busy={loading}>
          {items.map((item) => <article key={item.library_id} className={`${styles.card} ${selected.includes(item.library_id) ? styles.selected : ""}`}>
            <input type="checkbox" aria-label={`Select ${item.title}`} checked={selected.includes(item.library_id)} onChange={() => choose(item.library_id)} />
            <div className={styles.cardBody}>
              <div className={styles.meta}><Chip tone={item.import_id ? "info" : "neutral"}>{item.kind}</Chip>
                {item.import_id ? <Chip tone="ok">managed copy</Chip> : null}{item.state.completed ? <Chip tone="ok">completed</Chip> : null}</div>
              <h3>{item.title}</h3><p>{item.summary}</p>
              {item.state.progress > 0 && item.state.progress < 1 ? <><div className={styles.progress}><span style={{ width: `${item.state.progress * 100}%` }} /></div><small>{Math.round(item.state.progress * 100)}% complete</small></> : null}
              <div className={styles.tags}>{item.tags.map((tag) => <Chip key={tag}>#{tag}</Chip>)}</div>
              <div className={styles.actions}>
                {item.import_id ? <button type="button" onClick={() => void openItem(item)}><Eye size={12} /> Preview</button>
                  : item.source.url ? <a href={item.source.url} target="_blank" rel="noreferrer noopener">Open source</a> : null}
                {item.state.completed ? <button type="button" onClick={() => void action("restart", [item.library_id])}><RotateCcw size={12} /> Restart</button> : null}
                {item.state.saved && !item.import_id ? <button type="button" onClick={() => void action("remove_saved", [item.library_id])}>Remove saved</button> : null}
                {item.import_id ? <button type="button" onClick={() => deleteImport(item)}><Trash2 size={12} /> Delete copy</button> : null}
              </div>
            </div>
          </article>)}
        </div>
        {nextCursor ? <div className={styles.load}><Button onClick={() => void load(nextCursor, true)}>Load 50 more</Button></div> : null}
      </> : <SourceManager sources={sources} watchers={watchers} loading={loading}
        onSource={async (id, paused) => { await client.setLibrarySourcePaused(id, paused); await load(); }}
        onWatcher={async (id, paused) => { await client.setLibraryWatcherPaused(id, paused); await load(); }}
        onAddSource={async (url) => { await client.addLibrarySource(url); await load(); }}
        onAddWatcher={async (url, note) => { await client.addLibraryWatcher(url, note); await load(); }}
        onRemoveSource={removeSource} onRemoveWatcher={removeWatcher} />}

      {preview ? <aside className={styles.preview} role="dialog" aria-modal="true" aria-label={`Preview ${preview.title}`}>
        <header><div><b>{preview.title}</b><small> · private managed copy{preview.truncated ? " · preview truncated" : ""}</small></div>
          <button type="button" aria-label="Close preview" onClick={() => setPreview(null)}><X /></button></header><pre>{preview.text}</pre>
      </aside> : null}
    </div>
  );
}

function SourceManager({ sources, watchers, loading, onSource, onWatcher, onAddSource, onAddWatcher, onRemoveSource, onRemoveWatcher }: {
  sources: LibrarySource[]; watchers: LibraryWatcher[]; loading: boolean;
  onSource: (id: string, paused: boolean) => Promise<void>;
  onWatcher: (id: string, paused: boolean) => Promise<void>;
  onAddSource: (url: string) => Promise<void>;
  onAddWatcher: (url: string, note: string) => Promise<void>;
  onRemoveSource: (source: LibrarySource) => void;
  onRemoveWatcher: (watcher: LibraryWatcher) => void;
}) {
  const [sourceUrl, setSourceUrl] = useState("");
  const [watchUrl, setWatchUrl] = useState("");
  const [watchNote, setWatchNote] = useState("");
  return <div className={styles.sources} aria-busy={loading}>
    <div><h2>Sources</h2><p className="muted">Pause keeps history and cached items. Resume allows future bounded refreshes.</p></div>
    <form className={styles.add} onSubmit={(event) => { event.preventDefault(); if (sourceUrl.trim()) void onAddSource(sourceUrl.trim()).then(() => setSourceUrl("")); }}>
      <input aria-label="New source URL" value={sourceUrl} onChange={(event) => setSourceUrl(event.target.value)} placeholder="RSS, YouTube channel, or subreddit" />
      <Button type="submit">Add source</Button>
    </form>
    {sources.map((source) => <article className={styles.source} key={source.id}><div><b>{source.title}</b>
      <p>{source.kind} · {source.health?.item_count ?? 0} cached items · {source.paused ? "paused" : source.health?.ok === false ? "needs attention" : "healthy"}</p></div>
      <div className={styles.actions}><Button icon={source.paused ? Play : Pause} onClick={() => void onSource(source.id, !source.paused)}>{source.paused ? "Resume" : "Pause"}</Button>
        <Button icon={Trash2} variant="danger" onClick={() => onRemoveSource(source)}>Remove</Button></div></article>)}
    <div><h2>Page monitors</h2><p className="muted">Monitors check only the pages you explicitly added.</p></div>
    <form className={styles.add} onSubmit={(event) => { event.preventDefault(); if (watchUrl.trim()) void onAddWatcher(watchUrl.trim(), watchNote.trim()).then(() => { setWatchUrl(""); setWatchNote(""); }); }}>
      <input aria-label="Page monitor URL" value={watchUrl} onChange={(event) => setWatchUrl(event.target.value)} placeholder="https://…" />
      <input aria-label="Page monitor note" value={watchNote} onChange={(event) => setWatchNote(event.target.value)} placeholder="Optional note" />
      <Button type="submit">Watch page</Button>
    </form>
    {watchers.map((watcher) => <article className={styles.source} key={watcher.id}><div><b>{watcher.title}</b><p>{watcher.note || watcher.url}</p></div>
      <div className={styles.actions}><Button icon={watcher.paused ? Play : Pause} onClick={() => void onWatcher(watcher.id, !watcher.paused)}>{watcher.paused ? "Resume" : "Pause"}</Button>
        <Button icon={Trash2} variant="danger" onClick={() => onRemoveWatcher(watcher)}>Remove</Button></div></article>)}
  </div>;
}
