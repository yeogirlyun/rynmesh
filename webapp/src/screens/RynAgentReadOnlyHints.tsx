import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { agentExtensions, applyMcpServerHints, type ExtensionMcpServer } from "../domain/agentExtensions";

const DISMISSED_KEY = "ryn.agent.read-only-hints-dismissed";

function readDismissed(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem(DISMISSED_KEY) || "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string").slice(-200) : [];
  } catch { return []; }
}

function hintKey(server: ExtensionMcpServer): string {
  return `${server.id}:${server.instance_id}:${server.discovery_revision}`;
}

function needsConfirmation(server: ExtensionMcpServer, dismissed: string[]): boolean {
  return Boolean(server.enabled && !server.computer_access && server.instance_id
    && Number.isInteger(server.discovery_revision) && (server.discovery_revision || 0) > 0
    && (server.server_hint_candidates || []).length > 0 && !dismissed.includes(hintKey(server)));
}

/**
 * A plugin may declare some tools read-only. Until the owner confirms once, Ryn
 * treats them as changes. Offer that confirmation once per plugin discovery.
 */
export default function RynAgentReadOnlyHints() {
  const { i18n } = useTranslation();
  const tx = (cn: string, en: string) => i18n.language.startsWith("zh") ? cn : en;
  const [servers, setServers] = useState<ExtensionMcpServer[]>([]);
  const [dismissed, setDismissed] = useState<string[]>(readDismissed);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    agentExtensions.overview(controller.signal)
      .then(overview => setServers(overview.mcp_servers || []))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const server = servers.find(item => needsConfirmation(item, dismissed));
  if (!server) return null;
  const count = (server.server_hint_candidates || []).length;
  const name = server.title || server.name;

  const dismiss = () => {
    const next = [...dismissed, hintKey(server)];
    setDismissed(next);
    try { localStorage.setItem(DISMISSED_KEY, JSON.stringify(next.slice(-200))); } catch { /* per-viewer convenience */ }
  };
  const allow = async () => {
    if (busy || !server.instance_id || !server.discovery_revision) return;
    setBusy(true); setError("");
    try {
      const updated = await applyMcpServerHints(server.id, {
        expected_revision: server.revision,
        instance_id: server.instance_id,
        discovery_revision: server.discovery_revision,
        tools: (server.server_hint_candidates || []).map(({ name: tool, schema_sha256 }) => ({ name: tool, schema_sha256 })),
      });
      setServers(current => current.map(item => item.id === updated.id ? updated : item));
    } catch {
      setError(tx("确认失败，请到插件设置里重试。", "Could not confirm. Retry from the plugin settings."));
    } finally { setBusy(false); }
  };

  return <div className="ra-banner ra-read-only-hints" role="region" aria-label={tx("插件只读工具确认", "Plugin read-only tools")}>
    <span>{tx(`「${name}」有 ${count} 个工具声明为只读（只查询、不修改），允许它们直接运行吗？`,
      `“${name}” declares ${count} tool${count === 1 ? "" : "s"} read-only (they only look things up). Let them run without asking?`)}</span>
    {error && <span role="alert"> {error}</span>}
    <button type="button" className="ra-primary" disabled={busy} onClick={() => void allow()}>{tx("允许", "Allow")}</button>
    <button type="button" disabled={busy} onClick={dismiss}>{tx("以后再说", "Not now")}</button>
  </div>;
}
