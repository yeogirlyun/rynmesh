import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useTranslation } from "react-i18next";
import { canUseAgentAction, rynAgent, type AgentResourceCatalog, type AgentResourceSelection, type AgentScopeSelection, type AgentScopeSnapshot, type AgentTask } from "../domain/rynAgent";
import { nasRequest, type NasListing } from "../domain/nas";

/** Scope is a dedicated owner action; model replies and ordinary messages never grant it. */
export default function RynAgentScope({ task, modelName, modelID: selectedModelID, destinationRevision, sourceName, sourceID, providerID, initialSelection, busy, onSave, onSelect }: {
  task: AgentTask | null; modelName: string; modelID?: string; destinationRevision?: string | null; busy: boolean;
  onSave?: (snapshot: AgentScopeSnapshot, scope: AgentScopeSelection) => Promise<boolean>;
  onSelect?: (scope: AgentScopeSelection, modelID: string, destinationRevision: string | null) => void;
  initialSelection?: AgentScopeSelection;
  sourceName?: string; sourceID?: string; providerID?: string;
}) {
  const { t } = useTranslation("rynAgent");
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [catalog, setCatalog] = useState<AgentResourceCatalog | null>(null);
  const [snapshot, setSnapshot] = useState<AgentScopeSnapshot | null>(null);
  const [resources, setResources] = useState<AgentResourceSelection[]>(initialSelection?.resources || []);
  const [consent, setConsent] = useState(false);
  const [consentModel, setConsentModel] = useState("");
  const [consentRevision, setConsentRevision] = useState<string | null>(null);
  const [browser, setBrowser] = useState<number | null>(null);
  const loadRevision = useRef(0);
  const refresh = async () => {
    const revision = ++loadRevision.current;
    setLoading(true); setError("");
    if (!task) {
      try { const available = await rynAgent.resources(); if (revision === loadRevision.current) setCatalog(available); }
      catch { if (revision === loadRevision.current) setCatalog(null); }
      finally { if (revision === loadRevision.current) setLoading(false); }
      return;
    }
    const [scope, available] = await Promise.allSettled([rynAgent.scope(task.id), rynAgent.resources()]);
    if (revision !== loadRevision.current) return;
    if (scope.status === "fulfilled") {
      setSnapshot(scope.value); setResources(scope.value.scope.selection?.resources || []);
      const authorized = scope.value.model_destination;
      const current = scope.value.current_model_destination;
      setConsent(scope.value.scope.selection?.allow_model_data === true && Boolean(authorized?.destination_revision) && authorized?.destination_revision === current?.destination_revision && authorized?.catalog_model_id === current?.catalog_model_id);
      if (!scope.value.scope.selection && scope.value.scope.resource_grants.length > 0) setError("scopeMissingSelection");
    }
    setCatalog(available.status === "fulfilled" ? available.value : null);
    if (scope.status === "rejected" || available.status === "rejected") setError("scopeLoadError");
    setLoading(false);
  };
  useEffect(() => { if ((!task || open) && !snapshot) void refresh(); return () => { loadRevision.current++; }; }, [open, task?.id]);
  const update = (index: number, changes: Partial<AgentResourceSelection>) => {
    setResources(current => current.map((row, position) => position === index ? { ...row, ...changes } : row)); setConsent(false);
  };
  const allowed = task ? canUseAgentAction(task, "scope") : catalog?.create_scope_supported === true;
  const valid = resources.length <= 8 && resources.every(row => (row.root_kind !== "file" || (catalog?.file_scope_supported === true && Boolean(row.root_path) && !row.recursive && row.actions.length === 1 && row.actions[0] === "read_ai")) && catalog?.connections.some(connection => connection.connection_id === row.connection_id && connection.enabled && connection.allow_ai && connection.agent_supported && (!row.actions.some(action => action.startsWith("create_")) || (connection.create_supported === true && connection.writable === true))));
  const modelID = task ? snapshot?.current_model_destination?.catalog_model_id || task.model_id : selectedModelID || "";
  const currentDestinationRevision = task ? snapshot?.current_model_destination?.destination_revision : destinationRevision;
  const approvedConsent = task ? consent : consent && consentModel === modelID && consentRevision === currentDestinationRevision;
  const onSelectRef = useRef(onSelect); onSelectRef.current = onSelect;
  useEffect(() => { if (!task) onSelectRef.current?.({ resources, allow_model_data: resources.length > 0 && approvedConsent && valid }, modelID, currentDestinationRevision || null); }, [resources, approvedConsent, valid, modelID, currentDestinationRevision, task?.id]);
  useEffect(() => { if (!task) setConsent(false); }, [modelID, currentDestinationRevision]);
  const save = async () => {
    if (!snapshot || !onSave || busy || !allowed || !valid || (resources.length > 0 && (!approvedConsent || !currentDestinationRevision))) return;
    if (await onSave(snapshot, { resources, allow_model_data: resources.length > 0 && approvedConsent })) {
      setBrowser(null); await refresh();
    } else setError("scopeReviewError");
  };
  if (!task && !catalog?.create_scope_supported) return null;
  return <details className="ra-task-context ra-scope" onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>{t("scopeTitle")}</summary>
    {open && <div>
      <p>{t("scopeHelp")}</p>
      {loading && <p role="status">{t("loading")}</p>}
      {error && <p role="alert">{t(error)}</p>}
      {(snapshot || !task) && <>
        <p>{t("scopeDestination", { model: modelName || modelID })}<code>{modelID}</code>{(snapshot?.current_model_destination?.source_id || sourceID) && <small> · {t("scopeSource")}: {sourceName || snapshot?.current_model_destination?.source_id || sourceID}</small>}{(snapshot?.current_model_destination?.provider_peer_id || providerID) && <small> · {t("scopeProvider")}: {snapshot?.current_model_destination?.provider_peer_id || providerID}</small>}</p>
        {!currentDestinationRevision && <p>{t("scopeDestinationUnavailable")}</p>}
        {resources.map((row, index) => <fieldset key={index} className="ra-scope-root"><legend>{t(row.root_kind === "file" ? "scopeFile" : "scopeDirectory", { count: index + 1 })}</legend>
          <label>{t("scopeConnection")}<select aria-label={t("scopeConnectionNumber", { count: index + 1 })} value={row.connection_id} disabled={busy || !allowed || !catalog} onChange={event => { update(index, { connection_id: event.target.value, root_path: "", recursive: false, actions: row.root_kind === "file" ? ["read_ai"] : ["list", "read_ai"] }); setBrowser(null); }}><option value="">{t("scopeChooseConnection")}</option>{row.connection_id && !catalog?.connections.some(connection => connection.connection_id === row.connection_id) && <option disabled value={row.connection_id}>{row.connection_id}</option>}{catalog?.connections.map(connection => <option key={connection.connection_id} value={connection.connection_id} disabled={!connection.enabled || !connection.allow_ai || !connection.agent_supported}>{connection.name} · {connection.protocol}{!connection.enabled ? ` · ${t("scopeConnectionDisabled")}` : !connection.allow_ai ? ` · ${t("scopeAiDisabled")}` : !connection.agent_supported ? ` · ${connection.reason || t("notSupported")}` : ""}</option>)}</select></label>
          <p>{t(row.root_kind === "file" ? "scopeSelectedFile" : "scopeSelectedDirectory")}: <code>{row.root_path || t(row.root_kind === "file" ? "scopeNoFile" : "scopeRoot")}</code></p>
          <div className="ra-scope-actions"><button disabled={busy || !allowed || !catalog?.connections.some(connection => connection.connection_id === row.connection_id && connection.enabled && connection.allow_ai && connection.agent_supported)} onClick={() => setBrowser(browser === index ? null : index)}>{t(row.root_kind === "file" ? "scopeChooseFile" : "scopeChooseDirectory")}</button><button disabled={busy || !allowed} onClick={() => { setResources(current => current.filter((_, position) => position !== index)); setConsent(false); setBrowser(null); }}>{t(row.root_kind === "file" ? "scopeRemoveFile" : "scopeRemove")}</button></div>
          {browser === index && <DirectoryPicker connection={row.connection_id} path={row.root_path} file={row.root_kind === "file"} busy={busy} onSelect={path => { update(index, { root_path: path }); setBrowser(null); }} />}
          {row.root_kind === "file" ? <p>{t("scopeExactFileHelp")}</p> : <><label className="ra-scope-check"><input type="checkbox" checked={row.recursive} disabled={busy || !allowed} onChange={event => update(index, { recursive: event.target.checked })} />{t("scopeRecursive")}</label>
          <label>{t("scopeAccess")}<select aria-label={t("scopeAccessNumber", { count: index + 1 })} value={row.actions.some(action => action.startsWith("create_")) ? "create" : "read"} disabled={busy || !allowed} onChange={event => update(index, { actions: event.target.value === "create" ? ["list", "read_ai", "create_file", "create_directory"] : ["list", "read_ai"] })}><option value="read">{t("scopeReadOnly")}</option><option value="create" disabled={!catalog?.connections.some(connection => connection.connection_id === row.connection_id && connection.create_supported === true && connection.writable === true && connection.allow_ai && connection.enabled && connection.agent_supported)}>{t("scopeAllowCreate")}</option></select></label></>}
          {row.actions.some(action => action.startsWith("create_")) && <p>{t("scopeCreatePermissionHelp")}</p>}
        </fieldset>)}
        {resources.length === 0 && <p>{t("scopeEmpty")}</p>}
        <div className="ra-scope-actions"><button disabled={busy || !allowed || !catalog || resources.length >= 8} onClick={() => { setResources(current => [...current, { connection_id: "", root_path: "", recursive: false, actions: ["list", "read_ai"] }]); setConsent(false); }}>{t("scopeAdd")}</button>{catalog?.file_scope_supported === true && <button disabled={busy || !allowed || resources.length >= 8} onClick={() => { setResources(current => [...current, { connection_id: "", root_path: "", root_kind: "file", recursive: false, actions: ["read_ai"] }]); setConsent(false); }}>{t("scopeAddFile")}</button>}<button disabled={busy || loading} onClick={() => void refresh()}>{t("scopeRefresh")}</button></div>
        {catalog && catalog.connections.length === 0 && <p>{t("scopeNoConnections")}</p>}
        {catalog && <p>{t("scopeFormats", { formats: catalog.supported_formats.join(", ") })}</p>}
        <p><Link to="/drives">{t("scopeManageConnections")}</Link></p>
        {resources.length > 0 && <label className="ra-scope-check"><input type="checkbox" checked={approvedConsent} disabled={busy || !allowed || !currentDestinationRevision} onChange={event => { setConsent(event.target.checked); setConsentModel(modelID); setConsentRevision(currentDestinationRevision || null); }} />{t(resources.some(row => row.root_kind === "file") ? "scopeResourceConsent" : "scopeConsent", { model: modelName || modelID })}</label>}
        <p>{t(task ? "scopeChangeHelp" : "scopeCreateHelp")}</p>
        {task && allowed && <button className="ra-primary" disabled={busy || loading || Boolean(error) || !catalog || !valid || (resources.length > 0 && (!approvedConsent || !currentDestinationRevision))} onClick={() => void save()}>{t("scopeSave")}</button>}
        {snapshot && <details className="ra-exact-details"><summary>{t("scopeEvidence")}</summary><pre>{JSON.stringify(snapshot.scope, null, 2)}</pre></details>}
      </>}
      {!snapshot && error && <button disabled={busy || loading} onClick={() => void refresh()}>{t("retry")}</button>}
    </div>}
  </details>;
}

function DirectoryPicker({ connection, path, file = false, busy, onSelect }: { connection: string; path: string; file?: boolean; busy: boolean; onSelect: (path: string) => void }) {
  const { t } = useTranslation("rynAgent");
  const [location, setLocation] = useState(file ? "" : path);
  const [crumbs, setCrumbs] = useState<Array<{ path: string; name: string }>>([{ path: "", name: t("scopeRoot") }, ...(!file && path ? [{ path, name: path }] : [])]);
  const [listing, setListing] = useState<NasListing | null>(null);
  const [error, setError] = useState(false);
  const [loading, setLoading] = useState(false);
  const sequence = useRef(0);
  const load = async (cursor = "", scan = "") => {
    const revision = ++sequence.current; setLoading(true); setError(false);
    try {
      const response = await nasRequest(`/sources/${encodeURIComponent(connection)}/files?path=${encodeURIComponent(location)}${cursor ? `&cursor=${encodeURIComponent(cursor)}&scan=${encodeURIComponent(scan)}` : ""}`);
      const next = await response.json() as NasListing;
      if (revision === sequence.current) setListing(current => ({ ...next, entries: cursor ? [...(current?.entries || []), ...(next.entries || [])] : next.entries }));
    } catch { if (revision === sequence.current) setError(true); }
    finally { if (revision === sequence.current) setLoading(false); }
  };
  useEffect(() => { setListing(null); void load(); return () => { sequence.current++; }; }, [connection, location]);
  return <div className="ra-directory-picker" role="group" aria-label={t(file ? "scopeChooseFile" : "scopeChooseDirectory")}>
    <div className="ra-scope-actions">{crumbs.map((crumb, index) => <button key={`${crumb.path}/${index}`} disabled={busy || loading} onClick={() => { setLocation(crumb.path); setCrumbs(current => current.slice(0, index + 1)); }}>{crumb.name}</button>)}</div>
    {loading && <p role="status">{t("loading")}</p>}
    {error && <p role="alert">{t("scopeDirectoryError")} <button disabled={busy || loading} onClick={() => void load()}>{t("retry")}</button></p>}
    {listing?.entries?.filter(entry => entry.directory).map(entry => <button className="ra-directory-row" key={entry.path} disabled={busy || loading} onClick={() => { setLocation(entry.path); setCrumbs(current => [...current, { path: entry.path, name: entry.name }]); }}>{entry.name}</button>)}
    {file && listing?.entries?.filter(entry => !entry.directory).map(entry => <button className="ra-directory-row" key={entry.path} disabled={busy || loading} onClick={() => onSelect(entry.path)}>{t("scopeSelectFile", { name: entry.name })}</button>)}
    {listing?.next_cursor && <button disabled={busy || loading} onClick={() => void load(listing.next_cursor, listing.scan)}>{t("scopeMoreDirectories")}</button>}
    {!file && !loading && !error && listing?.entries && <button className="ra-primary" disabled={busy} onClick={() => onSelect(location)}>{t("scopeSelectDirectory")}</button>}
  </div>;
}
