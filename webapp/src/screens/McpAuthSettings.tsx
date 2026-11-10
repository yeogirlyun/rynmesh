import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { agentExtensions, cancelMcpOauthLogin, clearLocalMcpAuthorizations, getMcpOauthLogin, logoutMcpOauth, startMcpOauthLogin,
  type ExtensionMcpServer, type McpOauthProgress } from "../domain/agentExtensions";
import { reserveMcpAuthBrowser } from "../domain/mcpAuthBrowser";

export function McpAuthSettings({ server, onRefresh }: { server: ExtensionMcpServer; onRefresh: () => void }) {
  const { i18n } = useTranslation();
  const tx = (zh: string, en: string) => i18n.language.startsWith("zh") ? zh : en;
  const [authId, setAuthId] = useState("");
  const [progress, setProgress] = useState<McpOauthProgress["state"] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const [clearConfirm, setClearConfirm] = useState(false);
  const [clientId, setClientId] = useState(server.auth?.client_id || "");
  const epoch = useRef(0);
  const refresh = useRef(onRefresh); refresh.current = onRefresh;
  useEffect(() => {
    epoch.current++; setAuthId(""); setProgress(null); setBusy(false); setError(false); setClearConfirm(false);
    return () => { epoch.current++; };
  }, [server.instance_id, server.config_generation, server.enabled]);
  useEffect(() => { setClientId(server.auth?.client_id || ""); }, [server.instance_id, server.auth?.client_id]);
  useEffect(() => {
    if (!authId || !["waiting", "exchanging"].includes(progress || "")) return;
    const current = epoch.current, abort = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const result = await getMcpOauthLogin(server.id, authId, abort.signal);
        if (abort.signal.aborted || epoch.current !== current) return;
        setProgress(result.state);
        if (result.state === "waiting" || result.state === "exchanging") timer = setTimeout(() => void poll(), 1500);
        else { setAuthId(""); if (result.state === "failed" || result.state === "expired") setError(true); refresh.current(); }
      } catch { if (!abort.signal.aborted && epoch.current === current) { setError(true); setAuthId(""); setProgress("failed"); } }
    };
    timer = setTimeout(() => void poll(), 1500);
    return () => { abort.abort(); clearTimeout(timer); };
  }, [authId, progress, server.id]);

  if (server.transport !== "streamable_http" || server.auth?.mode === "none" || server.bearer_token_env ||
      server.header_keys.some(key => key.toLowerCase() === "authorization") ||
      Object.keys(server.env_http_headers || {}).some(key => key.toLowerCase() === "authorization")) return null;
  const pending = progress === "waiting" || progress === "exchanging" || (progress === null && server.oauth?.state === "pending");
  const loggedIn = server.oauth?.state === "logged_in" && progress !== "cancelled";
  async function act(work: () => Promise<void>) {
    const current = epoch.current;
    setBusy(true); setError(false);
    try { await work(); }
    catch { if (epoch.current === current) setError(true); }
    finally { if (epoch.current === current) setBusy(false); }
  }
  function login() {
    let browser: ReturnType<typeof reserveMcpAuthBrowser>;
    try { browser = reserveMcpAuthBrowser(); } catch { setError(true); return; }
    const current = epoch.current;
    void act(async () => {
      let result;
      try {
        result = await startMcpOauthLogin(server.id, server.revision, i18n.language);
        if (epoch.current !== current) { browser.cancel(); await cancelMcpOauthLogin(server.id, result.auth_id); return; }
        await browser.open(result.authorization_url);
        setAuthId(result.auth_id); setProgress("waiting");
      } catch {
        browser.cancel();
        if (result) await cancelMcpOauthLogin(server.id, result.auth_id).catch(() => undefined);
        throw new Error("mcp_login_failed");
      }
    });
  }
  return <section className="ae-section-card" aria-label={tx("MCP 登录", "MCP sign-in")}>
    <h3>{tx("登录状态", "Sign-in status")}</h3>
    <p role="status">{pending ? tx("等待浏览器授权…", "Waiting for browser authorization…") : loggedIn ? tx("已登录", "Signed in") : tx("未登录", "Signed out")}</p>
    {error ? <p role="alert">{tx("操作未完成。请检查服务器登录配置后重试。", "The operation could not be completed. Check the server sign-in settings and retry.")}</p> : null}
    <details className="ae-auth-advanced">
      <summary>{tx("登录配置", "Sign-in settings")}</summary>
      {server.oauth?.issuer_host ? <p>{tx("授权服务：", "Authorization server: ")}{server.oauth.issuer_host}</p> : null}
      {loggedIn && server.oauth?.expires_at ? <p>{tx("到期时间：", "Expires: ")}{new Date(server.oauth.expires_at * 1000).toLocaleString()}</p> : null}
      <label className="ae-auth-field"><span>{tx("客户端 ID", "Client ID")}</span><input value={clientId} maxLength={256} disabled={busy || pending}
        onChange={event => setClientId(event.target.value)} autoComplete="off" /></label>
      <p>{tx("仅在服务器要求预注册客户端时填写。更改后需要重新登录。", "Set this if the server requires a registered client. Changing it requires signing in again.")}</p>
      <div className="ae-inline-actions">
        <button type="button" className="ae-btn" disabled={busy || pending || clientId.trim() === (server.auth?.client_id || "")}
          onClick={() => void act(async () => {
            await agentExtensions.updateMcpServer(server.id, { expected_revision: server.revision,
              auth: { ...server.auth, mode: server.auth?.mode || "auto", client_id: clientId.trim() || null,
                scopes: server.auth?.scopes || [], callback_port: server.auth?.callback_port ?? null } });
            setProgress(null); refresh.current();
          })}>{tx("保存登录配置", "Save sign-in settings")}</button>
        <button type="button" className="ae-btn" disabled={busy} onClick={() => setClearConfirm(true)}>{tx("清除本机全部 MCP 授权", "Clear all local MCP authorizations")}</button>
      </div>
    </details>
    <div className="ae-inline-actions">
      <button type="button" className="ae-btn ae-btn-primary" disabled={busy || pending || !server.enabled} onClick={login}>{loggedIn ? tx("重新登录", "Sign in again") : tx("登录", "Sign in")}</button>
      {pending ? <button type="button" className="ae-btn" disabled={busy} onClick={() => void act(async () => {
        if (authId) await cancelMcpOauthLogin(server.id, authId);
        else await logoutMcpOauth(server.id, server.revision);
        setAuthId(""); setProgress("cancelled"); refresh.current();
      })}>{tx("取消授权", "Cancel authorization")}</button> : null}
      {loggedIn ? <button type="button" className="ae-btn" disabled={busy} onClick={() => void act(async () => {
        await logoutMcpOauth(server.id, server.revision); setProgress("cancelled"); refresh.current();
      })}>{tx("退出登录", "Sign out")}</button> : null}
    </div>
    {clearConfirm ? <div className="ae-confirm-dialog" role="alertdialog" aria-label={tx("确认清除授权", "Confirm authorization removal")}>
      <p>{tx("这会取消待完成登录并退出本机所有 MCP 服务器。", "This cancels pending sign-ins and signs out of every MCP server on this device.")}</p>
      <div className="ae-inline-actions">
        <button type="button" className="ae-btn ae-btn-danger" disabled={busy} onClick={() => void act(async () => { await clearLocalMcpAuthorizations(); setClearConfirm(false); setAuthId(""); setProgress("cancelled"); refresh.current(); })}>{tx("确认清除", "Confirm removal")}</button>
        <button type="button" className="ae-btn" disabled={busy} onClick={() => setClearConfirm(false)}>{tx("取消", "Cancel")}</button>
      </div>
    </div> : null}
  </section>;
}
