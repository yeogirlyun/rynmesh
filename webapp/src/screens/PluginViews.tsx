import { useEffect, useRef, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { ArrowLeft, ArrowRight, CheckCircle2, LoaderCircle, Search, Table2, X } from "lucide-react";
import { AgentExtensionError, applyMcpServerHints, queryPluginView, type ExtensionMcpServer, type ExtensionPlugin, type ExtensionPluginView, type ExtensionViewFormat } from "../domain/agentExtensions";
import { McpAuthSettings } from "./McpAuthSettings";
import "./PluginViews.css";

export function readViewPath(value: unknown, path: string): unknown {
  const parts = path.split(".");
  if (parts.length > 8 || parts.some(p => !p || ["__proto__", "prototype", "constructor"].includes(p))) return undefined;
  let current = value;
  for (const part of parts) {
    if (!current || typeof current !== "object" || Array.isArray(current) || !Object.prototype.hasOwnProperty.call(current, part)) return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

export function formatViewValue(value: unknown, format?: ExtensionViewFormat): string {
  if (value === undefined || value === null || value === "") return "—";
  if (!["string", "number", "boolean"].includes(typeof value)) return "—";
  const text = String(value);
  if (text.length > 4096 || (typeof value === "number" && !Number.isFinite(value))) return "—";
  if (format === "money" || format === "number") {
    // Group the original decimal text without rounding or converting money to binary float.
    const match = /^(-?)(\d+)(\.\d+)?$/.exec(text);
    if (match) return match[1] + match[2].replace(/\B(?=(\d{3})+(?!\d))/g, ",") + (match[3] || "");
  }
  return text;
}

function defaultInputs(view: ExtensionPluginView): Record<string, string> {
  const now = new Date();
  const date = (day: number) => `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
  return Object.fromEntries(view.inputs.map(input => [input.name, input.type === "date" && input.default ? date(input.default === "month_start" ? 1 : now.getDate()) : ""]));
}

function useText() {
  const { i18n } = useTranslation();
  const zh = (i18n.resolvedLanguage || i18n.language).startsWith("zh");
  return (chinese: string, english: string) => zh ? chinese : english;
}

export function PluginViews({ plugin, servers, onOpenServer, onRefresh, initialViewId }: {
  plugin: ExtensionPlugin; servers: ExtensionMcpServer[]; onOpenServer: (id: string) => void; onRefresh?: () => void; initialViewId?: string;
}) {
  const views = plugin.views || [];
  const tx = useText();
  const [selected, setSelected] = useState(initialViewId || views[0]?.id || "");
  useEffect(() => {
    if (initialViewId && views.some(v => v.id === initialViewId)) {
      setSelected(initialViewId);
    }
  }, [initialViewId, views]);
  const view = views.find(v => v.id === selected) || views[0];
  if (!view) return null;
  const server = servers.find(s => s.id === view.mcpServer);
  const binding = JSON.stringify([plugin.id, plugin.instance_id, plugin.revision, plugin.enabled, view,
    server?.instance_id, server?.revision, server?.config_generation, server?.discovery_revision, server?.enabled, server?.computer_access, server?.oauth?.state,
    server?.tools.find(t => t.name === view.tool)?.owner_read_only]);
  const isLoggedIn = server?.oauth?.state === "logged_in";
  return <section className="plugin-views" aria-label={tx("插件页面", "Plugin pages")}>
    <div className="plugin-views-heading">
      <Table2 size={18} />
      <h3>{tx("插件页面", "Plugin pages")}</h3>
      {isLoggedIn && <em className="plugin-views-auth-pill">{tx("已登录", "Signed in")}</em>}
      <span>{tx("只读", "Read only")}</span>
    </div>
    <nav className="plugin-views-tabs" aria-label={tx("选择页面", "Choose page")}>
      {views.map(item => <button key={item.id} type="button" aria-current={item.id === view.id ? "page" : undefined} onClick={() => setSelected(item.id)}>{item.title}</button>)}
    </nav>
    <PluginViewPanel key={binding} plugin={plugin} view={view} server={server} onOpenServer={onOpenServer} onRefresh={onRefresh} />
  </section>;
}

function PluginViewPanel({ plugin, view, server, onOpenServer, onRefresh }: {
  plugin: ExtensionPlugin; view: ExtensionPluginView; server?: ExtensionMcpServer; onOpenServer: (id: string) => void; onRefresh?: () => void;
}) {
  const tx = useText();
  const [inputs, setInputs] = useState(() => defaultInputs(view));
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [submitted, setSubmitted] = useState<Record<string, string> | null>(null);
  const [page, setPage] = useState(1);
  const [busy, setBusy] = useState(false);
  const [confirmingHints, setConfirmingHints] = useState(false);
  const [error, setError] = useState("");
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const generation = useRef(0);
  useEffect(() => () => { generation.current++; controller.current?.abort(); }, []);
  const enabled = plugin.enabled && server?.enabled;
  const tool = server?.tools.find(t => t.name === view.tool && t.enabled && t.annotations?.readOnlyHint === true && t.owner_read_only);
  const authRequired = server?.oauth && ["logged_out", "pending", "error"].includes(server.oauth.state);
  const ready = enabled && tool && !authRequired && !server?.computer_access;
  const hintCandidates = server?.server_hint_candidates || [];
  const canConfirmReadOnlyHere = Boolean(
    enabled && !authRequired && !server?.computer_access && !tool
    && server?.instance_id && Number.isInteger(server.discovery_revision) && (server.discovery_revision || 0) > 0
    && hintCandidates.some(c => c.name === view.tool),
  );
  async function handleConfirmReadOnly() {
    if (!server?.instance_id || !server.discovery_revision || !canConfirmReadOnlyHere || confirmingHints) return;
    setConfirmingHints(true);
    setError("");
    try {
      await applyMcpServerHints(server.id, {
        expected_revision: server.revision,
        instance_id: server.instance_id,
        discovery_revision: server.discovery_revision,
        tools: hintCandidates.map(({ name, schema_sha256 }) => ({ name, schema_sha256 })),
      });
      onRefresh?.();
    } catch {
      setError(tx("确认只读查询工具失败，请打开连接详情重试。", "Could not confirm read-only tools. Open connection details and retry."));
    } finally {
      setConfirmingHints(false);
    }
  }
  const rawRows = readViewPath(result, view.rowsPath);
  const rows = Array.isArray(rawRows) ? rawRows.filter((row): row is Record<string, unknown> => !!row && typeof row === "object" && !Array.isArray(row)).slice(0, view.pagination.pageSize) : [];
  const pagination = readViewPath(result, view.pagination.path) as Record<string, unknown> | undefined;
  const total = typeof pagination?.total === "number" && Number.isSafeInteger(pagination.total) && pagination.total >= 0 ? pagination.total : null;
  const totalPages = total === null ? 0 : Math.ceil(total / view.pagination.pageSize);
  const invalidate = () => {
    generation.current++; controller.current?.abort(); setBusy(false); setResult(null); setSubmitted(null); setDetail(null); setError("");
  };
  async function query(args: Record<string, string>, targetPage: number) {
    if (!ready) return;
    invalidate();
    const seq = generation.current;
    const abort = new AbortController(); controller.current = abort; setBusy(true);
    try {
      const response = await queryPluginView(plugin, view.id, args, targetPage, abort.signal);
      if (seq !== generation.current || abort.signal.aborted) return;
      const next = response.result;
      const nextRows = readViewPath(next, view.rowsPath);
      const nextPage = readViewPath(next, view.pagination.path) as Record<string, unknown> | undefined;
      if (response.view_id !== view.id || next?.success !== true || !Array.isArray(nextRows)
        || !nextPage || nextPage.page !== targetPage || nextPage.pageSize !== view.pagination.pageSize
        || !Number.isSafeInteger(nextPage.total) || Number(nextPage.total) < 0
        || nextPage.totalPages !== Math.ceil(Number(nextPage.total) / view.pagination.pageSize)
        || nextRows.length > view.pagination.pageSize
        || nextRows.some(row => !row || typeof row !== "object" || Array.isArray(row))) throw new Error("invalid_view_result");
      setResult(next); setSubmitted(args); setPage(targetPage);
    } catch (failure) {
      if (seq !== generation.current || abort.signal.aborted) return;
      const code = failure instanceof AgentExtensionError ? failure.code : "";
      if (["agent_mcp_auth_required", "agent_mcp_oauth_request_failed"].includes(code)) setError(tx("登录已失效，请重新登录后查询。", "Sign in again before querying."));
      else if (["agent_extensions_revision_conflict", "agent_plugin_view_stale", "agent_stale_revision"].includes(code)) setError(tx("插件配置已更新，请重新打开此页面。", "The plugin changed. Reopen this page."));
      else if (code === "agent_plugin_view_read_only_confirmation_required") setError(tx("请先在连接详情确认此工具为只读，再重新打开页面。", "Confirm this tool as read-only in connection details, then reopen this page."));
      else if (code === "agent_plugin_view_unsupported_server") setError(tx("此页面不支持需要桌面控制权限的连接。", "Pages do not support connections that need desktop control."));
      else setError(tx("查询未完成，请检查日期、筛选条件和账号权限后重试。", "Query failed. Check the dates, filters and account permissions, then retry."));
    } finally { if (seq === generation.current) setBusy(false); }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const args = Object.fromEntries(Object.entries(inputs).filter(([, value]) => value.trim() !== ""));
    void query(args, 1);
  }
  return <div className="plugin-view-panel">
    <h4>{view.title}</h4>
    {view.description && <p className="plugin-view-description">{view.description}</p>}
    {!ready && <div className="plugin-view-notice" role="status">
      <span>{!enabled ? tx("先启用插件及其连接。", "Enable the plugin and its connection first.")
        : server?.computer_access ? tx("此页面不支持需要桌面控制权限的连接。", "Pages do not support connections that need desktop control.")
        : authRequired ? tx("请先登录，审批通过后再查询。", "Sign in and complete any required approval before querying.")
        : tx("连接并刷新工具列表，在连接详情确认只读工具后即可查询。", "Connect, refresh tools and confirm read-only tools in connection details before querying.")}</span>
      <div className="plugin-view-notice-actions">
        {canConfirmReadOnlyHere && <button type="button" className="ae-btn ae-btn-sm ae-btn-primary" disabled={confirmingHints} onClick={() => void handleConfirmReadOnly()}>
          <CheckCircle2 size={14} />
          <span>{confirmingHints ? tx("正在确认…", "Confirming…") : tx("确认只读页面工具", "Confirm read-only page tools")}</span>
        </button>}
        {server && <button type="button" className="ae-btn ae-btn-sm" onClick={() => onOpenServer(server.id)}>{tx("登录与连接", "Sign-in and connection")}</button>}
      </div>
    </div>}
    {enabled && authRequired && server && onRefresh ? (
      <div className="plugin-view-inline-auth">
        <McpAuthSettings key={`${server.id}:${server.instance_id}`} server={server} onRefresh={onRefresh} />
      </div>
    ) : null}
    <form className="plugin-view-filters" onSubmit={submit}>
      {view.inputs.map(input => <label key={input.name}>
        <span>{input.label}{input.required ? " *" : ""}</span>
        <input type={input.type} required={input.required} value={inputs[input.name] || ""} maxLength={512}
          onChange={event => { invalidate(); setInputs(current => ({ ...current, [input.name]: event.target.value })); }} />
      </label>)}
      <button type="submit" className="ae-btn ae-btn-primary plugin-view-submit" disabled={!ready || busy}>
        {busy ? <LoaderCircle size={15} className="plugin-view-spin" /> : <Search size={15} />}{busy ? tx("查询中…", "Querying…") : tx("查询", "Query")}
      </button>
    </form>
    {error && <p className="plugin-view-error" role="alert">{error}</p>}
    {!result && !busy && !error && <p className="plugin-view-empty">{tx("选择条件后点击查询。结果仅显示在本页。", "Choose filters and query. Results stay in this page.")}</p>}
    {busy && <p role="status" className="plugin-view-empty">{tx("正在读取…", "Loading…")}</p>}
    {result && <>
      <div className="plugin-view-summary">
        {view.summaryFields.filter(field => readViewPath(result, field.path) !== undefined).map(field => <div key={field.path}>
          <span>{field.label}</span><strong>{formatViewValue(readViewPath(result, field.path), field.format)}</strong>
        </div>)}
      </div>
      {result.truncated === true && <p role="status" className="plugin-view-notice">{tx("服务端已截断部分结果，请缩小查询范围。合计以服务端返回为准。", "The server truncated part of the result. Narrow the filters. Totals are supplied by the server.")}</p>}
      <div className="plugin-view-table" tabIndex={0} role="region" aria-label={`${view.title} ${tx("查询结果", "results")}`}>
        <table><thead><tr>{view.columns.map(col => <th key={col.key} scope="col" className={col.format && col.format !== "text" ? "is-number" : ""}>{col.label}</th>)}<th scope="col">{tx("详情", "Details")}</th></tr></thead>
          <tbody>{rows.map((row, index) => <tr key={index}>{view.columns.map(col => <td key={col.key} className={col.format && col.format !== "text" ? "is-number" : ""} title={formatViewValue(readViewPath(row, col.key), col.format)}>{formatViewValue(readViewPath(row, col.key), col.format)}</td>)}<td><button type="button" className="ae-btn ae-btn-sm" onClick={() => setDetail(row)} aria-label={tx(`查看第 ${index + 1} 行详情`, `View row ${index + 1} details`)}>{tx("查看", "View")}</button></td></tr>)}</tbody>
        </table>
      </div>
      {rows.length === 0 && <p className="plugin-view-empty">{tx("当前筛选条件下没有记录。", "No records match these filters.")}</p>}
      <div className="plugin-view-pagination"><span>{tx(`共 ${total ?? "—"} 条 · 每页 ${view.pagination.pageSize} 条`, `${total ?? "—"} records · ${view.pagination.pageSize} per page`)}</span>
        <div><button type="button" className="ae-btn ae-btn-sm" disabled={busy || page <= 1 || !submitted} onClick={() => submitted && void query(submitted, page - 1)} aria-label={tx("上一页", "Previous page")}><ArrowLeft size={15} /></button>
          <span>{page} / {Math.max(1, totalPages)}</span>
          <button type="button" className="ae-btn ae-btn-sm" disabled={busy || page >= totalPages || !submitted} onClick={() => submitted && void query(submitted, page + 1)} aria-label={tx("下一页", "Next page")}><ArrowRight size={15} /></button></div>
      </div>
      <small className="plugin-view-footnote">{tx("汇总覆盖本次查询范围，以服务端计算结果为准。", "Totals cover the query range and use the server's calculations.")}</small>
    </>}
    {detail && <section className="plugin-view-detail" aria-label={tx("记录详情", "Record details")}><header><strong>{tx("记录详情", "Record details")}</strong><button type="button" className="ae-btn ae-btn-sm" onClick={() => setDetail(null)} aria-label={tx("关闭详情", "Close details")}><X size={15} /></button></header><dl>{view.columns.map(col => <div key={col.key}><dt>{col.label}</dt><dd>{formatViewValue(readViewPath(detail, col.key), col.format)}</dd></div>)}</dl></section>}
  </div>;
}
