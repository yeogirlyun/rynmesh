import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { Link, NavLink, useLocation, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  CheckCircle2,
  ExternalLink,
  FileCode2,
  LoaderCircle,
  Monitor,
  MoreHorizontal,
  Package,
  Play,
  Plus,
  RefreshCw,
  RotateCcw,
  Search,
  Server,
  Settings,
  ShieldAlert,
  Trash2,
  Upload,
  Wrench,
  X,
} from "lucide-react";
import {
  AgentExtensionError,
  ENV_KEY_RE,
  HEADER_KEY_RE,
  MASKED_SECRET_PLACEHOLDER,
  agentExtensions,
  buildNewSecretMap,
  buildSecretPatch,
  createInitialSecretEntries,
  expectedSkillRevision,
  formatMcpArgsInput,
  isRevisionConflictError,
  parseMcpArgsInput,
  reconcileSecretEntriesOnConflict,
  validateSecretEntries,
  type ExtensionImportKind,
  type ExtensionImportPreview,
  type ExtensionImportPreviewRequest,
  type ExtensionMcpOauthClientRegistration,
  type ExtensionMcpServer,
  type ExtensionMcpTool,
  type ExtensionMcpTransport,
  type ExtensionOverview,
  type ExtensionPlugin,
  type ExtensionSkill,
  type SecretEntryDraft,
} from "../domain/agentExtensions";
import { rynAgent } from "../domain/rynAgent";
import { AgentComputerStatusPanel } from "./AgentComputerStatusPanel";
import { PeerProviderSettings } from "./PeerProviderSettings";
import { McpAuthSettings } from "./McpAuthSettings";
import { PluginViews } from "./PluginViews";
import { McpDataPolicy, McpDataPolicySummary } from "./McpDataPolicy";
import { CodexSkillsView } from "./CodexSkillsView";
import "./AgentExtensions.css";

export type ExtensionSection = "plugins" | "skills" | "mcp";

function parseSection(
  paramSection: string | undefined,
  search: string,
  hash: string,
): ExtensionSection {
  const candidates = [
    paramSection,
    new URLSearchParams(search).get("tab") ?? undefined,
    hash.replace(/^#/, "") || undefined,
  ];
  for (const raw of candidates) {
    if (!raw) continue;
    const normalized = raw.trim().toLowerCase();
    if (normalized === "skills" || normalized === "skill") return "skills";
    if (normalized === "mcp" || normalized === "mcp-servers" || normalized === "servers") return "mcp";
    if (normalized === "plugins" || normalized === "plugin") return "plugins";
  }
  return "plugins";
}

function useExtText() {
  const { i18n } = useTranslation();
  const isZh = (i18n.resolvedLanguage || i18n.language || "en").toLowerCase().startsWith("zh");
  return useCallback((zh: string, en: string) => (isZh ? zh : en), [isZh]);
}

function formatSourceLabel(
  sourceFormat: string,
  builtin: boolean,
  tx: (zh: string, en: string) => string,
): string {
  if (builtin || sourceFormat === "builtin") return tx("Ryn 内置", "Ryn built-in");
  switch (sourceFormat) {
    case "codex_plugin":
    case "codex_toml":
      return "Codex";
    case "claude_plugin":
    case "mcp_json":
      return "Claude / MCP";
    case "ryn_plugin":
      return tx("Ryn 插件包", "Ryn package");
    case "skill_md":
      return tx("本地 SKILL.md", "Local SKILL.md");
    default:
      return sourceFormat || tx("本地", "Local");
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function describeError(
  error: unknown,
  tx: (zh: string, en: string) => string,
): string {
  if (error instanceof AgentExtensionError) {
    switch (error.code) {
      case "agent_extensions_revision_conflict":
        return tx(
          "配置已在其他视图中更新，已为你同步最新版本。你的草稿与密钥输入已保留，请核对后再次保存。",
          "Configuration changed in another view and has been refreshed. Your draft and secret inputs are preserved; review and save again.",
        );
      case "agent_mcp_command_required":
        return tx("STDIO 服务器必须填写启动命令。", "A command is required for STDIO servers.");
      case "agent_mcp_url_required":
        return tx("HTTP 服务器必须填写有效的 http:// 或 https:// 地址。", "A valid http:// or https:// URL is required for HTTP servers.");
      case "agent_mcp_invalid_env_key":
        return tx("环境变量名称格式无效。", "Invalid environment variable name.");
      case "agent_mcp_invalid_header_key":
        return tx("请求头名称格式无效。", "Invalid HTTP header name.");
      case "agent_mcp_invalid_bearer_token_env":
        return tx("Bearer Token 环境变量名称格式无效。", "Invalid Bearer token environment variable name.");
      case "agent_mcp_server_already_exists":
        return tx("同名 MCP 服务器已存在。", "An MCP server with this ID already exists.");
      case "agent_extension_builtin_protected":
        return tx("内置项目受保护，可以停用但不可卸载。", "Built-in extensions are protected and can be disabled but not removed.");
      case "agent_extension_source_not_found":
        return tx("找不到指定的本地路径，请检查路径是否存在。", "The specified local path was not found.");
      case "agent_invalid_plugin_package":
        return tx("无效的插件包：未找到有效的 plugin.json 清单。", "Invalid plugin package: no valid plugin.json manifest found.");
      case "agent_invalid_skill_package":
        return tx("无效的技能包：未找到有效的 SKILL.md 内容。", "Invalid skill package: no valid SKILL.md content found.");
      case "agent_invalid_mcp_config":
        return tx("无效的 MCP 配置文本（支持 .mcp.json 或 Codex config.toml）。", "Invalid MCP configuration (supports .mcp.json or Codex config.toml).");
      default:
        return `${tx("操作未完成", "Operation failed")} (${error.code})`;
    }
  }
  return tx("请求失败，请检查节点连接后重试。", "Request failed. Check node connection and try again.");
}

function getDialogFocusableElements(container: HTMLElement): HTMLElement[] {
  const nodes = container.querySelectorAll<HTMLElement>(
    'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])',
  );
  return Array.from(nodes).filter(el => {
    if (el.hasAttribute("disabled") || el.getAttribute("aria-hidden") === "true") {
      return false;
    }
    const detailsParent = el.closest("details");
    if (detailsParent && !detailsParent.open && el.tagName.toLowerCase() !== "summary") {
      return false;
    }
    return true;
  });
}

function AccessibleModal({
  title,
  subtitle,
  onClose,
  busy = false,
  wide = false,
  footer,
  children,
}: {
  title: string;
  subtitle?: string;
  onClose: () => void;
  busy?: boolean;
  wide?: boolean;
  footer?: ReactNode;
  children: ReactNode;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDialogElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const tx = useExtText();

  useEffect(() => {
    const dialogEl = dialogRef.current;
    const previousFocus = document.activeElement as HTMLElement | null;
    if (dialogEl) {
      if (typeof dialogEl.showModal === "function") {
        try {
          dialogEl.showModal();
        } catch {
          if (!dialogEl.open) {
            dialogEl.setAttribute("open", "");
          }
        }
        if (!dialogEl.open) {
          dialogEl.setAttribute("open", "");
        }
      } else {
        dialogEl.setAttribute("open", "");
      }
    }
    closeButtonRef.current?.focus();
    return () => {
      if (dialogEl) {
        if (typeof dialogEl.close === "function") {
          try {
            dialogEl.close();
          } catch {
            dialogEl.removeAttribute("open");
          }
        } else {
          dialogEl.removeAttribute("open");
        }
        if (dialogEl.open) {
          dialogEl.removeAttribute("open");
        }
      }
      previousFocus?.focus?.();
    };
  }, []);

  return (
    <div
      className="ae-modal-backdrop"
      onClick={event => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <dialog
        ref={dialogRef}
        className={`ae-dialog${wide ? " ae-dialog-workspace" : ""}`}
        aria-labelledby={titleId}
        onCancel={event => {
          event.preventDefault();
          if (!busy) onClose();
        }}
        onClick={event => {
          if (busy) return;
          if (event.target === event.currentTarget) {
            const rect = event.currentTarget.getBoundingClientRect();
            if (
              event.clientX < rect.left ||
              event.clientX > rect.right ||
              event.clientY < rect.top ||
              event.clientY > rect.bottom
            ) {
              onClose();
            }
          }
        }}
        onKeyDown={event => {
          if (event.key === "Escape" && !busy) {
            event.preventDefault();
            event.stopPropagation();
            onClose();
            return;
          }
          if (event.key === "Tab" && dialogRef.current) {
            const focusables = getDialogFocusableElements(dialogRef.current);
            if (focusables.length === 0) {
              event.preventDefault();
              return;
            }
            const first = focusables[0];
            const last = focusables[focusables.length - 1];
            const active = document.activeElement as HTMLElement | null;
            if (event.shiftKey) {
              if (!active || active === first || !dialogRef.current.contains(active)) {
                event.preventDefault();
                last.focus();
              }
            } else {
              if (!active || active === last || !dialogRef.current.contains(active)) {
                event.preventDefault();
                first.focus();
              }
            }
          }
        }}
      >
        <header className="ae-dialog-head">
          <div>
            <h2 id={titleId}>{title}</h2>
            {subtitle ? <p>{subtitle}</p> : null}
          </div>
          <button
            ref={closeButtonRef}
            type="button"
            className="ae-icon-btn"
            aria-label={tx("关闭对话框", "Close dialog")}
            disabled={busy}
            onClick={onClose}
          >
            <X size={18} />
          </button>
        </header>
        <div className="ae-dialog-body">{children}</div>
        {footer ? <footer className="ae-dialog-foot">{footer}</footer> : null}
      </dialog>
    </div>
  );
}

function SwitchButton({
  checked,
  disabled,
  label,
  onChange,
}: {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onChange: (next: boolean) => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      className="ae-switch"
      onClick={() => onChange(!checked)}
    >
      <span className="ae-switch-thumb" />
    </button>
  );
}

function getServerObservedBadge(
  server: ExtensionMcpServer,
  testing: boolean,
  tx: (zh: string, en: string) => string,
  desktopAccess?: boolean,
): { label: string; tone: "neutral" | "success" | "warning" | "danger" | "info" } {
  if (testing) {
    return { label: tx("正在连接…", "Connecting…"), tone: "info" };
  }
  if (!server.enabled) {
    if (!server.parent_enabled && server.component_enabled) {
      return {
        label: tx("已停用（父插件关闭）", "Disabled (parent plugin off)"),
        tone: "warning",
      };
    }
    return { label: tx("已停用", "Disabled"), tone: "neutral" };
  }
  const st = server.status;
  if (st === "NeedsPermission" || (server.computer_access && desktopAccess === false)) {
    return { label: tx("需开启桌面权限", "Needs desktop permission"), tone: "warning" };
  }
  if (st === "NeedsAuth") {
    return { label: tx("缺少认证凭据", "Needs authentication"), tone: "warning" };
  }
  if (st === "Error" || st === "error") {
    return { label: tx("连接错误", "Connection error"), tone: "danger" };
  }
  if (st === "NeedsTest") {
    return { label: tx("未测试", "Not tested"), tone: "neutral" };
  }
  if (st === "connected" || (st === "ready" && Boolean(server.last_tested_at))) {
    return {
      label: tx(`已连接 · ${server.tools_count} 个工具`, `Connected · ${server.tools_count} tools`),
      tone: "success",
    };
  }
  if (st === "ready" && server.builtin) {
    return {
      label: tx(`未测试 · ${server.tools_count} 个内置工具`, `Not tested · ${server.tools_count} built-in tools`),
      tone: "info",
    };
  }
  return { label: tx("未测试", "Not tested"), tone: "neutral" };
}

function getServerStatusNotice(
  server: ExtensionMcpServer,
  tx: (zh: string, en: string) => string,
): {
  text: string;
  tone: "neutral" | "warning" | "error";
  showRetry: boolean;
} | null {
  const rawMessage = server.status_message.trim();
  const isErrorStatus = server.status === "Error" || server.status === "error";
  const isNeedsAuth = server.status === "NeedsAuth";
  const isNeedsPermission = server.status === "NeedsPermission";

  if (!server.parent_enabled && server.component_enabled && !isErrorStatus) {
    return {
      text: tx(
        "父插件当前已停用；启用所属插件后此服务器才会生效。",
        "Parent plugin is disabled.",
      ),
      tone: "neutral",
      showRetry: false,
    };
  }

  if (!rawMessage) {
    return null;
  }

  if (rawMessage === "Parent plugin is disabled." && server.parent_enabled) {
    return null;
  }

  if (isNeedsPermission || rawMessage === "Requires desktop_access in Agent settings.") {
    return {
      text: tx(
        "需要在 Agent 设置中开启「允许电脑和浏览器操作」(desktop_access) 权限。",
        "Requires desktop_access in Agent settings.",
      ),
      tone: "warning",
      showRetry: false,
    };
  }

  if (isNeedsAuth) {
    const envMatch = /^Environment variable (.+) is not set\.$/i.exec(rawMessage);
    const localizedAuthMsg = envMatch
      ? tx(`未设置环境变量 ${envMatch[1]}。`, rawMessage)
      : rawMessage;
    return {
      text: localizedAuthMsg,
      tone: "warning",
      showRetry: true,
    };
  }

  if (isErrorStatus) {
    return {
      text: rawMessage,
      tone: "error",
      showRetry: true,
    };
  }

  if (rawMessage === "Parent plugin is disabled.") {
    return {
      text: tx(
        "父插件当前已停用；启用所属插件后此服务器才会生效。",
        "Parent plugin is disabled.",
      ),
      tone: "neutral",
      showRetry: false,
    };
  }

  return {
    text: rawMessage,
    tone: "neutral",
    showRetry: false,
  };
}

function sameEntityInstance(
  a: { instance_id?: string },
  b: { instance_id?: string },
): boolean {
  if (a.instance_id && b.instance_id) {
    return a.instance_id === b.instance_id;
  }
  return true;
}

interface ServerExplicitProbeResult {
  serverId: string;
  instanceId?: string;
  configGeneration?: number;
  revision: number;
  configSignature: string;
  action: "test" | "restart";
  status: "connected" | "error";
  toolsCount: number;
  error: string;
}

function getServerConfigSignature(server: ExtensionMcpServer): string {
  return JSON.stringify([
    server.instance_id ?? "",
    server.config_generation ?? null,
    server.transport,
    server.command,
    server.args,
    server.cwd,
    server.url,
    server.bearer_token_env,
    server.env_keys,
    server.env_vars ?? [],
    server.header_keys,
    server.env_http_headers ?? {},
    server.oauth_client_registration ?? "auto",
    server.startup_timeout_ms,
    server.tool_timeout_ms,
    server.computer_access,
    server.component_enabled,
    server.parent_enabled,
    server.enabled,
    server.enabled_tools,
    server.disabled_tools,
    server.read_only_tools,
  ]);
}

function canApplyProbeServer(
  current: ExtensionMcpServer,
  started: ExtensionMcpServer,
  incoming: ExtensionMcpServer,
): boolean {
  if (!sameEntityInstance(started, incoming)) return false;
  if (!sameEntityInstance(current, incoming)) return false;
  if (
    started.config_generation !== undefined &&
    incoming.config_generation !== undefined &&
    started.config_generation !== incoming.config_generation
  ) {
    return false;
  }
  if (
    current.config_generation !== undefined &&
    incoming.config_generation !== undefined &&
    current.config_generation > incoming.config_generation
  ) {
    return false;
  }
  if (current.revision > incoming.revision) {
    return false;
  }
  if (getServerConfigSignature(current) !== getServerConfigSignature(started)) {
    return false;
  }
  return true;
}

function mergeReconciledOverview(
  prev: ExtensionOverview | null,
  next: ExtensionOverview,
): ExtensionOverview {
  if (!prev) return next;
  if (next.revision < prev.revision) {
    return prev;
  }
  const prevSkills = new Map(prev.skills.map(s => [s.id, s]));
  const prevServers = new Map(prev.mcp_servers.map(m => [m.id, m]));
  const prevPlugins = new Map(prev.plugins.map(p => [p.id, p]));

  const skills = next.skills.map(s => {
    const old = prevSkills.get(s.id);
    if (old && sameEntityInstance(old, s) && old.revision > s.revision) {
      return old;
    }
    return s;
  });

  const mcp_servers = next.mcp_servers.map(m => {
    const old = prevServers.get(m.id);
    if (old && sameEntityInstance(old, m) && old.revision > m.revision) {
      return old;
    }
    return m;
  });

  const skillsById = new Map(skills.map(s => [s.id, s]));
  const serversById = new Map(mcp_servers.map(m => [m.id, m]));

  const plugins = next.plugins.map(p => {
    const old = prevPlugins.get(p.id);
    const chosen =
      old && sameEntityInstance(old, p) && old.revision > p.revision ? old : p;
    return {
      ...chosen,
      skills: chosen.skills?.map(s => skillsById.get(s.id) ?? s),
      mcp_servers: chosen.mcp_servers?.map(m => serversById.get(m.id) ?? m),
    };
  });

  return {
    ...next,
    revision: Math.max(prev.revision, next.revision),
    plugins,
    skills,
    mcp_servers,
  };
}

function WebAppAccessSummary({ app }: { app: { url: string; permissions: string[] } }) {
  const tx = useExtText();
  const labels: Record<string,string> = {
    tools: tx('查询插件工具', 'Query plugin tools'),
    agent: tx('在助手中打开草稿', 'Open a draft in the assistant'),
    messages: tx('读取和发送消息', 'Read and send messages'),
    models: tx('查看模型和修改共享设置', 'View models and change sharing'),
    peer_provider: tx('连接成员设备和共享模型', 'Connect member devices and share models'),
  };
  return <p>{tx('外部应用', 'External app')}: {app.url}<br />{tx('启用后可使用', 'Access when enabled')}: {app.permissions.map(p => labels[p] || p).join(' · ') || tx('无设备权限', 'No device permissions')}</p>;
}

export default function AgentExtensions() {
  const tx = useExtText();
  const params = useParams<{ section?: string }>();
  const location = useLocation();
  const navigate = useNavigate();

  const activeSection = useMemo(
    () => parseSection(params.section, location.search, location.hash),
    [params.section, location.search, location.hash],
  );

  const baseRoute = location.pathname.startsWith("/agent/extensions")
    ? "/agent/extensions"
    : "/settings/extensions";

  const [overview, setOverview] = useState<ExtensionOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [pageError, setPageError] = useState("");
  const [actionNotice, setActionNotice] = useState<{ tone: "error" | "info"; text: string } | null>(null);
  const [busyIds, setBusyIds] = useState<Record<string, boolean>>({});
  const [testingServerIds, setTestingServerIds] = useState<Record<string, boolean>>({});
  const [serverProbeResults, setServerProbeResults] = useState<
    Record<string, ServerExplicitProbeResult>
  >({});
  const [overviewRevisionStale, setOverviewRevisionStale] = useState(false);
  const [reconcilingOverview, setReconcilingOverview] = useState(false);

  const overviewRef = useRef<ExtensionOverview | null>(null);
  overviewRef.current = overview;
  const overviewReqGenRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const probeReqGenRef = useRef<Record<string, number>>({});
  const activeProbeRunRef = useRef<Record<string, number>>({});

  // Filters & search
  const [pluginSearch, setPluginSearch] = useState("");
  const [pluginFilter, setPluginFilter] = useState<"all" | "installed">("all");
  const [skillSearch, setSkillSearch] = useState("");
  const [mcpSearch, setMcpSearch] = useState("");

  // Active modals
  const [selectedPluginId, setSelectedPluginId] = useState<string | null>(() =>
    new URLSearchParams(location.search).get("plugin"),
  );
  const [selectedPluginViewId, setSelectedPluginViewId] = useState<string | undefined>(
    () => new URLSearchParams(location.search).get("view") ?? undefined,
  );
  useEffect(() => {
    const query = new URLSearchParams(location.search);
    const pluginParam = query.get("plugin");
    const viewParam = query.get("view") ?? undefined;
    if (pluginParam) {
      setSelectedPluginId(pluginParam);
      setSelectedPluginViewId(viewParam);
    }
  }, [location.search]);
  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);
  const [selectedServerId, setSelectedServerId] = useState<string | null>(null);
  const selectedServerIdRef = useRef<string | null>(null);
  selectedServerIdRef.current = selectedServerId;
  const [serverEditorState, setServerEditorState] = useState<{
    mode: "create" | "edit";
    server?: ExtensionMcpServer;
  } | null>(null);
  const [importModalKind, setImportModalKind] = useState<ExtensionImportKind | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<{
    kind: "plugin" | "skill" | "mcp";
    id: string;
    name: string;
    revision: number;
  } | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [mcpModifiedSinceRestart, setMcpModifiedSinceRestart] = useState(false);
  const [mcpRestartingAll, setMcpRestartingAll] = useState(false);
  const [clearingMcpAppData, setClearingMcpAppData] = useState(false);

  const invalidateServerProbe = useCallback((serverId: string) => {
    probeReqGenRef.current[serverId] = (probeReqGenRef.current[serverId] ?? 0) + 1;
    activeProbeRunRef.current[serverId] = (activeProbeRunRef.current[serverId] ?? 0) + 1;
    setTestingServerIds(prev => (prev[serverId] ? { ...prev, [serverId]: false } : prev));
    setServerProbeResults(prev => {
      if (!(serverId in prev)) return prev;
      const next = { ...prev };
      delete next[serverId];
      return next;
    });
  }, []);

  const selectServer = useCallback((nextId: string | null) => {
    const prevId = selectedServerIdRef.current;
    selectedServerIdRef.current = nextId;
    if (prevId && prevId !== nextId) {
      probeReqGenRef.current[prevId] = (probeReqGenRef.current[prevId] ?? 0) + 1;
      activeProbeRunRef.current[prevId] = (activeProbeRunRef.current[prevId] ?? 0) + 1;
      setTestingServerIds(curr => (curr[prevId] ? { ...curr, [prevId]: false } : curr));
    }
    setSelectedServerId(nextId);
    setServerProbeResults(prev => {
      if (Object.keys(prev).length === 0) return prev;
      if (!nextId) return {};
      const kept = prev[nextId];
      return kept ? { [nextId]: kept } : {};
    });
  }, []);

  const openConfirmDelete = useCallback(
    (target: { kind: "plugin" | "skill" | "mcp"; id: string; name: string; revision: number }) => {
      setDeleteError("");
      setConfirmDelete(target);
    },
    [],
  );

  const closeConfirmDelete = useCallback(() => {
    setDeleteError("");
    setConfirmDelete(null);
  }, []);

  const markBusy = useCallback((key: string, value: boolean) => {
    setBusyIds(prev => ({ ...prev, [key]: value }));
  }, []);

  const loadOverview = useCallback(
    async (
      silent = false,
      options?: { expectedEpoch?: number; markStaleOnError?: boolean },
    ): Promise<ExtensionOverview | null> => {
      const reqId = ++overviewReqGenRef.current;
      const epochAtStart = options?.expectedEpoch ?? mutationEpochRef.current;
      if (!silent) setLoading(true);
      setPageError("");
      try {
        const next = await agentExtensions.overview();
        if (
          reqId !== overviewReqGenRef.current ||
          epochAtStart !== mutationEpochRef.current
        ) {
          return null;
        }
        let reconciled: ExtensionOverview = next;
        setOverview(prev => {
          reconciled = mergeReconciledOverview(prev, next);
          return reconciled;
        });
        setOverviewRevisionStale(false);
        return reconciled;
      } catch (error) {
        if (
          reqId === overviewReqGenRef.current &&
          epochAtStart === mutationEpochRef.current
        ) {
          if (options?.markStaleOnError) {
            setOverviewRevisionStale(true);
          }
          setPageError(describeError(error, tx));
        }
        return null;
      } finally {
        if (!silent && reqId === overviewReqGenRef.current) {
          setLoading(false);
        }
      }
    },
    [tx],
  );

  const reconcileOverviewAfterMutation = useCallback(async (): Promise<ExtensionOverview | null> => {
    const epoch = ++mutationEpochRef.current;
    setReconcilingOverview(true);
    try {
      return await loadOverview(true, { expectedEpoch: epoch, markStaleOnError: true });
    } finally {
      setReconcilingOverview(false);
    }
  }, [loadOverview]);

  useEffect(() => {
    void loadOverview(false);
  }, [loadOverview]);

  const pluginsById = useMemo(() => {
    const map = new Map<string, ExtensionPlugin>();
    for (const plugin of overview?.plugins ?? []) {
      map.set(plugin.id, plugin);
    }
    return map;
  }, [overview]);

  const selectedPlugin = useMemo(
    () => (selectedPluginId ? overview?.plugins.find(p => p.id === selectedPluginId) ?? null : null),
    [overview, selectedPluginId],
  );
  const selectedSkill = useMemo(
    () => (selectedSkillId ? overview?.skills.find(s => s.id === selectedSkillId) ?? null : null),
    [overview, selectedSkillId],
  );
  const selectedServer = useMemo(
    () => (selectedServerId ? overview?.mcp_servers.find(m => m.id === selectedServerId) ?? null : null),
    [overview, selectedServerId],
  );

  const activeServerProbeResult = useMemo(() => {
    if (!selectedServer) return null;
    const res = serverProbeResults[selectedServer.id];
    if (!res) return null;
    if (res.serverId !== selectedServer.id) return null;
    if (!sameEntityInstance({ instance_id: res.instanceId }, selectedServer)) {
      return null;
    }
    if (
      res.configGeneration !== undefined &&
      selectedServer.config_generation !== undefined &&
      res.configGeneration !== selectedServer.config_generation
    ) {
      return null;
    }
    if (res.revision !== selectedServer.revision) {
      return null;
    }
    if (res.configSignature !== getServerConfigSignature(selectedServer)) {
      return null;
    }
    return res;
  }, [selectedServer, serverProbeResults]);

  // Handlers
  const handleTogglePlugin = async (plugin: ExtensionPlugin, nextEnabled: boolean) => {
    const key = `plugin:${plugin.id}`;
    markBusy(key, true);
    setActionNotice(null);
    for (const srvId of plugin.mcp_server_ids) {
      invalidateServerProbe(srvId);
    }
    for (const srv of plugin.mcp_servers ?? []) {
      invalidateServerProbe(srv.id);
    }
    mutationEpochRef.current += 1;
    try {
      const updated = await agentExtensions.updatePlugin(plugin.id, {
        enabled: nextEnabled,
        expected_revision: plugin.revision,
      });
      mutationEpochRef.current += 1;
      setOverview(prev =>
        prev
          ? {
              ...prev,
              plugins: prev.plugins.map(p => (p.id === updated.id ? updated : p)),
            }
          : prev,
      );
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleToggleDesktopAccess = async (nextAccess: boolean) => {
    const key = "desktop_access";
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      await rynAgent.updateSettings({ desktop_access: nextAccess });
      mutationEpochRef.current += 1;
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleToggleSkill = async (skill: ExtensionSkill, nextEnabled: boolean) => {
    const key = `skill:${skill.id}`;
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const updated = await agentExtensions.updateSkill(skill.id, {
        enabled: nextEnabled,
        expected_revision: expectedSkillRevision(skill),
      });
      mutationEpochRef.current += 1;
      setOverview(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          skills: prev.skills.map(s => (s.id === updated.id ? updated : s)),
          plugins: prev.plugins.map(p => ({
            ...p,
            skills: p.skills?.map(s => (s.id === updated.id ? updated : s)),
          })),
        };
      });
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleToggleServer = async (server: ExtensionMcpServer, nextEnabled: boolean) => {
    const key = `mcp:${server.id}`;
    invalidateServerProbe(server.id);
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const updated = await agentExtensions.updateMcpServer(server.id, {
        enabled: nextEnabled,
        expected_revision: server.revision,
      });
      setMcpModifiedSinceRestart(true);
      mutationEpochRef.current += 1;
      setOverview(prev => {
        if (!prev) return prev;
        return {
          ...prev,
          mcp_servers: prev.mcp_servers.map(m => (m.id === updated.id ? updated : m)),
          plugins: prev.plugins.map(p => ({
            ...p,
            mcp_servers: p.mcp_servers?.map(m => (m.id === updated.id ? updated : m)),
          })),
        };
      });
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleTestOrRestartServer = async (
    server: ExtensionMcpServer,
    action: "test" | "restart",
  ) => {
    const probeGen = (probeReqGenRef.current[server.id] ?? 0) + 1;
    probeReqGenRef.current[server.id] = probeGen;
    const runId = (activeProbeRunRef.current[server.id] ?? 0) + 1;
    activeProbeRunRef.current[server.id] = runId;
    setServerProbeResults(prev => {
      if (!(server.id in prev)) return prev;
      const next = { ...prev };
      delete next[server.id];
      return next;
    });
    setTestingServerIds(prev => ({ ...prev, [server.id]: true }));
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const response =
        action === "restart"
          ? await agentExtensions.restartMcpServer(server.id, server.revision)
          : await agentExtensions.testMcpServer(server.id, server.revision);
      mutationEpochRef.current += 1;
      const updated = response.server;
      const currentInState = overviewRef.current?.mcp_servers.find(m => m.id === server.id);
      const isCurrentProbe =
        probeReqGenRef.current[server.id] === probeGen &&
        (currentInState
          ? canApplyProbeServer(currentInState, server, updated)
          : sameEntityInstance(server, updated));

      if (isCurrentProbe) {
        setOverview(prev => {
          if (!prev) return prev;
          const applyServer = (m: ExtensionMcpServer) => {
            if (m.id !== updated.id) return m;
            if (!canApplyProbeServer(m, server, updated)) {
              return m;
            }
            return updated;
          };
          return {
            ...prev,
            mcp_servers: prev.mcp_servers.map(applyServer),
            plugins: prev.plugins.map(p => ({
              ...p,
              mcp_servers: p.mcp_servers?.map(applyServer),
            })),
          };
        });

        const rawErr = (response.test_result.error ?? "").trim();
        const probeStatus: "connected" | "error" =
          response.test_result.status === "error" || Boolean(rawErr) ? "error" : "connected";
        const toolsCount =
          typeof response.test_result.tools_count === "number"
            ? response.test_result.tools_count
            : updated.tools_count;

        setServerProbeResults(prev => ({
          ...prev,
          [updated.id]: {
            serverId: updated.id,
            instanceId: updated.instance_id ?? server.instance_id,
            configGeneration: updated.config_generation ?? server.config_generation,
            revision: updated.revision,
            configSignature: getServerConfigSignature(updated),
            action,
            status: probeStatus,
            toolsCount,
            error: rawErr,
          },
        }));

        if (probeStatus === "error" && rawErr) {
          setActionNotice({
            tone: "error",
            text: `${server.name}: ${rawErr}`,
          });
        }
      }
      const reconciled = await reconcileOverviewAfterMutation();
      if (isCurrentProbe && probeReqGenRef.current[server.id] === probeGen && reconciled) {
        const reconciledServer = reconciled.mcp_servers.find(m => m.id === updated.id);
        if (
          !reconciledServer ||
          !canApplyProbeServer(reconciledServer, updated, reconciledServer)
        ) {
          setServerProbeResults(prev => {
            if (!(updated.id in prev)) return prev;
            const next = { ...prev };
            delete next[updated.id];
            return next;
          });
        }
      }
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      const errText = describeError(error, tx);
      if (probeReqGenRef.current[server.id] === probeGen && !isRevisionConflictError(error)) {
        setServerProbeResults(prev => ({
          ...prev,
          [server.id]: {
            serverId: server.id,
            instanceId: server.instance_id,
            configGeneration: server.config_generation,
            revision: server.revision,
            configSignature: getServerConfigSignature(server),
            action,
            status: "error",
            toolsCount: server.tools_count,
            error: errText,
          },
        }));
      }
      if (probeReqGenRef.current[server.id] === probeGen || isRevisionConflictError(error)) {
        setActionNotice({ tone: "error", text: errText });
      }
    } finally {
      if (activeProbeRunRef.current[server.id] === runId) {
        setTestingServerIds(prev => (prev[server.id] ? { ...prev, [server.id]: false } : prev));
      }
    }
  };

  const handleToggleServerTool = async (
    server: ExtensionMcpServer,
    tool: ExtensionMcpTool,
    nextEnabled: boolean,
  ) => {
    const key = `tool:${server.id}:${tool.name}`;
    invalidateServerProbe(server.id);
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const disabledSet = new Set(server.disabled_tools);
      if (nextEnabled) {
        disabledSet.delete(tool.name);
      } else {
        disabledSet.add(tool.name);
      }
      let nextEnabledTools: string[] | null | undefined = undefined;
      if (server.enabled_tools !== null) {
        const allowSet = new Set(server.enabled_tools);
        if (nextEnabled) {
          allowSet.add(tool.name);
        } else {
          allowSet.delete(tool.name);
        }
        nextEnabledTools = Array.from(allowSet);
      }
      const updated = await agentExtensions.updateMcpServer(server.id, {
        disabled_tools: Array.from(disabledSet),
        ...(nextEnabledTools !== undefined ? { enabled_tools: nextEnabledTools } : {}),
        expected_revision: server.revision,
      });
      mutationEpochRef.current += 1;
      setOverview(prev =>
        prev
          ? {
              ...prev,
              mcp_servers: prev.mcp_servers.map(m => (m.id === updated.id ? updated : m)),
              plugins: prev.plugins.map(p => ({
                ...p,
                mcp_servers: p.mcp_servers?.map(m => (m.id === updated.id ? updated : m)),
              })),
            }
          : prev,
      );
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleClearToolAllowlist = async (server: ExtensionMcpServer) => {
    const key = `allowlist:${server.id}`;
    invalidateServerProbe(server.id);
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const updated = await agentExtensions.updateMcpServer(server.id, {
        enabled_tools: null,
        expected_revision: server.revision,
      });
      mutationEpochRef.current += 1;
      setOverview(prev =>
        prev
          ? {
              ...prev,
              mcp_servers: prev.mcp_servers.map(m => (m.id === updated.id ? updated : m)),
              plugins: prev.plugins.map(p => ({
                ...p,
                mcp_servers: p.mcp_servers?.map(m => (m.id === updated.id ? updated : m)),
              })),
            }
          : prev,
      );
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleToggleToolReadOnly = async (
    server: ExtensionMcpServer,
    tool: ExtensionMcpTool,
    nextReadOnly: boolean,
  ) => {
    const key = `ro:${server.id}:${tool.name}`;
    invalidateServerProbe(server.id);
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const roSet = new Set(server.read_only_tools);
      if (nextReadOnly) {
        roSet.add(tool.name);
      } else {
        roSet.delete(tool.name);
      }
      const updated = await agentExtensions.updateMcpServer(server.id, {
        read_only_tools: Array.from(roSet),
        expected_revision: server.revision,
      });
      mutationEpochRef.current += 1;
      setOverview(prev =>
        prev
          ? {
              ...prev,
              mcp_servers: prev.mcp_servers.map(m => (m.id === updated.id ? updated : m)),
              plugins: prev.plugins.map(p => ({
                ...p,
                mcp_servers: p.mcp_servers?.map(m => (m.id === updated.id ? updated : m)),
              })),
            }
          : prev,
      );
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleConfirmDelete = async () => {
    if (!confirmDelete) return;
    setDeleteBusy(true);
    setDeleteError("");
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      if (confirmDelete.kind === "plugin") {
        const targetPlugin = overview?.plugins.find(p => p.id === confirmDelete.id);
        for (const srvId of targetPlugin?.mcp_server_ids ?? []) {
          invalidateServerProbe(srvId);
        }
        for (const srv of overview?.mcp_servers ?? []) {
          if (srv.plugin_id === confirmDelete.id) {
            invalidateServerProbe(srv.id);
          }
        }
        await agentExtensions.deletePlugin(confirmDelete.id, confirmDelete.revision);
        if (selectedPluginId === confirmDelete.id) setSelectedPluginId(null);
      } else if (confirmDelete.kind === "skill") {
        await agentExtensions.deleteSkill(confirmDelete.id, confirmDelete.revision);
        if (selectedSkillId === confirmDelete.id) setSelectedSkillId(null);
      } else {
        invalidateServerProbe(confirmDelete.id);
        await agentExtensions.deleteMcpServer(confirmDelete.id, confirmDelete.revision);
        if (selectedServerId === confirmDelete.id) selectServer(null);
      }
      mutationEpochRef.current += 1;
      closeConfirmDelete();
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      const errText = describeError(error, tx);
      if (isRevisionConflictError(error)) {
        const refreshed = await reconcileOverviewAfterMutation();
        if (refreshed) {
          const nextItem =
            confirmDelete.kind === "plugin"
              ? refreshed.plugins.find(p => p.id === confirmDelete.id)
              : confirmDelete.kind === "skill"
                ? refreshed.skills.find(s => s.id === confirmDelete.id)
                : refreshed.mcp_servers.find(m => m.id === confirmDelete.id);
          if (nextItem) {
            setConfirmDelete({ ...confirmDelete, revision: nextItem.revision });
            setDeleteError(errText);
          } else {
            closeConfirmDelete();
          }
        } else {
          setDeleteError(errText);
        }
      } else {
        setDeleteError(errText);
      }
      setActionNotice({ tone: "error", text: errText });
    } finally {
      setDeleteBusy(false);
    }
  };

  const handleUpdateServerOauthRegistration = async (
    server: ExtensionMcpServer,
    nextMethod: ExtensionMcpOauthClientRegistration,
  ) => {
    const key = `oauth:${server.id}`;
    markBusy(key, true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const updated = await agentExtensions.updateMcpServer(server.id, {
        oauth_client_registration: nextMethod,
        expected_revision: server.revision,
      });
      setMcpModifiedSinceRestart(true);
      mutationEpochRef.current += 1;
      setOverview(prev =>
        prev
          ? {
              ...prev,
              mcp_servers: prev.mcp_servers.map(m => (m.id === updated.id ? updated : m)),
              plugins: prev.plugins.map(p => ({
                ...p,
                mcp_servers: p.mcp_servers?.map(m => (m.id === updated.id ? updated : m)),
              })),
            }
          : prev,
      );
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      if (isRevisionConflictError(error)) {
        await reconcileOverviewAfterMutation();
      }
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      markBusy(key, false);
    }
  };

  const handleRestartAllMcpServers = async () => {
    setMcpRestartingAll(true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const nextOverview = await agentExtensions.restartAllMcpServers();
      mutationEpochRef.current += 1;
      setOverview(prev => mergeReconciledOverview(prev, nextOverview));
      setMcpModifiedSinceRestart(false);
      setActionNotice({
        tone: "info",
        text: tx("已重启所有 MCP 服务器会话。", "Restarted MCP server sessions."),
      });
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      setMcpRestartingAll(false);
    }
  };

  const handleClearMcpAppData = async () => {
    setClearingMcpAppData(true);
    setActionNotice(null);
    mutationEpochRef.current += 1;
    try {
      const nextOverview = await agentExtensions.clearMcpAppData();
      mutationEpochRef.current += 1;
      setOverview(prev => mergeReconciledOverview(prev, nextOverview));
      setActionNotice({
        tone: "info",
        text: tx("已清除 MCP 应用数据", "Cleared MCP app data"),
      });
      await reconcileOverviewAfterMutation();
    } catch (error) {
      mutationEpochRef.current += 1;
      setActionNotice({ tone: "error", text: describeError(error, tx) });
    } finally {
      setClearingMcpAppData(false);
    }
  };

  // Filtered lists
  const filteredPlugins = useMemo(() => {
    const q = pluginSearch.trim().toLowerCase();
    return (overview?.plugins ?? []).filter(plugin => {
      if (pluginFilter === "installed") {
        const installedOrEnabled = !plugin.builtin || plugin.component_enabled;
        if (!installedOrEnabled) return false;
      }
      if (!q) return true;
      return (
        plugin.title.toLowerCase().includes(q) ||
        plugin.name.toLowerCase().includes(q) ||
        plugin.description.toLowerCase().includes(q) ||
        plugin.source_format.toLowerCase().includes(q)
      );
    });
  }, [overview, pluginSearch, pluginFilter]);

  const filteredSkills = useMemo(() => {
    const q = skillSearch.trim().toLowerCase();
    return (overview?.skills ?? []).filter(skill => {
      if (!q) return true;
      return (
        skill.title.toLowerCase().includes(q) ||
        skill.id.toLowerCase().includes(q) ||
        skill.description.toLowerCase().includes(q) ||
        skill.source_format.toLowerCase().includes(q)
      );
    });
  }, [overview, skillSearch]);

  const filteredServers = useMemo(() => {
    const q = mcpSearch.trim().toLowerCase();
    return (overview?.mcp_servers ?? []).filter(server => {
      if (!q) return true;
      return (
        server.name.toLowerCase().includes(q) ||
        server.id.toLowerCase().includes(q) ||
        server.transport.toLowerCase().includes(q) ||
        server.command.toLowerCase().includes(q) ||
        server.url.toLowerCase().includes(q)
      );
    });
  }, [overview, mcpSearch]);

  const createActionsBlocked = overviewRevisionStale || reconcilingOverview;

  return (
    <div className="ae-page">
      <header className="ae-header">
        <div className="ae-header-copy">
          <div className="ae-header-top">
            <Link className="ae-back-link" to="/agent">
              <ArrowLeft size={14} />
              <span>{tx("返回 Ryn Agent", "Back to Ryn Agent")}</span>
            </Link>
          </div>
          <h1>{tx("Agent 扩展", "Agent extensions")}</h1>
          <p>
            {tx(
              "管理本机 Ryn Agent 的插件包、工作流技能（Skills）与 MCP 工具服务器。所有工具调用均遵循当前任务的权限与确认设置。",
              "Manage local Ryn Agent plugins, workflow skills, and MCP tool servers. All tool executions follow each task's permission and confirmation mode.",
            )}
          </p>
        </div>
        <div className="ae-header-actions">
          <button
            type="button"
            className="ae-btn"
            disabled={loading}
            onClick={() => void loadOverview(false)}
          >
            <RefreshCw size={15} />
            <span>{tx("刷新", "Refresh")}</span>
          </button>
        </div>
      </header>

      <nav className="ae-tabs" aria-label={tx("扩展分类导航", "Extension sections")}>
        <NavLink
          to={`${baseRoute}/plugins`}
          end
          aria-current={activeSection === "plugins" ? "page" : undefined}
          className={({ isActive }) =>
            `ae-tab${isActive || activeSection === "plugins" ? " active" : ""}`
          }
        >
          <Package size={16} />
          <span>{tx("插件", "Plugins")}</span>
          <span className="ae-tab-count">{overview?.plugins.length ?? 0}</span>
        </NavLink>
        <NavLink
          to={`${baseRoute}/skills`}
          end
          aria-current={activeSection === "skills" ? "page" : undefined}
          className={({ isActive }) =>
            `ae-tab${isActive || activeSection === "skills" ? " active" : ""}`
          }
        >
          <BookOpen size={16} />
          <span>{tx("技能", "Skills")}</span>
          <span className="ae-tab-count">{overview?.skills.length ?? 0}</span>
        </NavLink>
        <NavLink
          to={`${baseRoute}/mcp`}
          end
          aria-current={activeSection === "mcp" ? "page" : undefined}
          className={({ isActive }) =>
            `ae-tab${isActive || activeSection === "mcp" ? " active" : ""}`
          }
        >
          <Server size={16} />
          <span>{tx("MCP 服务器", "MCP servers")}</span>
          <span className="ae-tab-count">{overview?.mcp_servers.length ?? 0}</span>
        </NavLink>
      </nav>

      {pageError ? (
        <div className="ae-banner ae-banner-error" role="alert">
          <span>{pageError}</span>
          <button type="button" className="ae-btn ae-btn-sm" onClick={() => void loadOverview(false)}>
            {tx("重试", "Retry")}
          </button>
        </div>
      ) : null}

      {overviewRevisionStale && !reconcilingOverview ? (
        <div className="ae-banner ae-banner-warning" role="alert">
          <span>
            {tx(
              "扩展全局版本号尚未同步完成，新建与导入操作已暂停。请点击同步最新版本。",
              "Global extension revision is out of sync. Create and import actions are paused until the latest overview is reloaded.",
            )}
          </span>
          <button
            type="button"
            className="ae-btn ae-btn-sm"
            onClick={() => void reconcileOverviewAfterMutation()}
          >
            {tx("同步最新版本", "Sync latest revision")}
          </button>
        </div>
      ) : null}

      {actionNotice ? (
        <div
          className={`ae-banner ${actionNotice.tone === "error" ? "ae-banner-error" : ""}`}
          role="alert"
        >
          <span>{actionNotice.text}</span>
          <button
            type="button"
            className="ae-btn ae-btn-sm"
            onClick={() => setActionNotice(null)}
          >
            {tx("关闭", "Dismiss")}
          </button>
        </div>
      ) : null}

      {loading && !overview ? (
        <div className="ae-empty" role="status">
          <LoaderCircle size={24} />
          <span>{tx("正在加载扩展配置…", "Loading extensions…")}</span>
        </div>
      ) : null}

      {overview && activeSection === "plugins" ? (
        <section aria-label={tx("插件", "Plugins")}>
          <div className="ae-toolbar">
            <div className="ae-toolbar-left">
              <label className="ae-search">
                <Search size={16} />
                <input
                  type="search"
                  aria-label={tx("搜索插件", "Search plugins")}
                  placeholder={tx("搜索插件名称或来源…", "Search plugins by name or source…")}
                  value={pluginSearch}
                  onChange={event => setPluginSearch(event.target.value)}
                />
              </label>
              <div className="ae-segmented" role="group" aria-label={tx("插件筛选", "Plugin filter")}>
                <button
                  type="button"
                  aria-pressed={pluginFilter === "all"}
                  onClick={() => setPluginFilter("all")}
                >
                  {tx("全部", "All")}
                </button>
                <button
                  type="button"
                  aria-pressed={pluginFilter === "installed"}
                  onClick={() => setPluginFilter("installed")}
                >
                  {tx("已安装", "Installed")}
                </button>
              </div>
            </div>
            <div className="ae-toolbar-right">
              <button
                type="button"
                className="ae-btn ae-btn-primary"
                disabled={createActionsBlocked}
                onClick={() => setImportModalKind("plugin")}
              >
                <Upload size={15} />
                <span>{tx("导入插件", "Import plugin")}</span>
              </button>
            </div>
          </div>

          {filteredPlugins.length === 0 ? (
            <div className="ae-empty">
              <Package size={26} />
              <p>{tx("没有匹配的插件。", "No matching plugins.")}</p>
            </div>
          ) : (
            <div className="ae-plugin-grid">
              {filteredPlugins.map(plugin => {
                const isComputerUse = plugin.id === "computer-use";
                const winSupported =
                  plugin.capabilities?.windows_supported ?? overview.windows_supported ?? true;
                const needsDesktopPerm =
                  isComputerUse && plugin.component_enabled && !overview.desktop_access;
                const isBusy = Boolean(busyIds[`plugin:${plugin.id}`]);

                return (
                  <article key={plugin.id} className="ae-plugin-card">
                    <div>
                      <div className="ae-plugin-card-top">
                        <div className="ae-plugin-identity">
                          <div className="ae-plugin-icon">
                            {isComputerUse ? <Monitor size={20} /> : <Package size={20} />}
                          </div>
                          <div className="ae-plugin-title-wrap">
                            <div className="ae-plugin-title-row">
                              <h3>{plugin.title}</h3>
                              <span className="ae-badge">v{plugin.version}</span>
                            </div>
                            <div className="ae-plugin-meta">
                              <span className="ae-badge ae-badge-info">
                                {formatSourceLabel(plugin.source_format, plugin.builtin, tx)}
                              </span>
                              {isComputerUse && !winSupported ? (
                                <span className="ae-badge ae-badge-warning">
                                  {tx("仅支持 Windows", "Windows only")}
                                </span>
                              ) : needsDesktopPerm ? (
                                <span className="ae-badge ae-badge-warning">
                                  {tx("需开启桌面权限", "Needs desktop permission")}
                                </span>
                              ) : plugin.enabled ? (
                                <span className="ae-badge ae-badge-success">
                                  <span className="ae-dot" />
                                  {tx("已启用", "Enabled")}
                                </span>
                              ) : (
                                <span className="ae-badge">{tx("已停用", "Disabled")}</span>
                              )}
                              {overview.mcp_servers.some(
                                m =>
                                  (m.plugin_id === plugin.id || plugin.mcp_server_ids.includes(m.id)) &&
                                  m.oauth?.state === "logged_in",
                              ) ? (
                                <span className="ae-badge ae-badge-success">
                                  {tx("已登录", "Signed in")}
                                </span>
                              ) : null}
                            </div>
                          </div>
                        </div>
                        <SwitchButton
                          checked={plugin.component_enabled}
                          disabled={isBusy || (isComputerUse && !winSupported)}
                          label={tx(`启用 ${plugin.title}`, `Enable ${plugin.title}`)}
                          onChange={next => void handleTogglePlugin(plugin, next)}
                        />
                      </div>
                      <p className="ae-plugin-desc" style={{ marginTop: 12 }}>
                        {plugin.description}
                      </p>
                    </div>

                    <div className="ae-plugin-footer">
                      <div className="ae-plugin-meta">
                        {(plugin.views?.length ?? 0) > 0 ? (
                          <span className="ae-badge ae-badge-info">
                            {tx(`${plugin.views!.length} 个报表页面`, `${plugin.views!.length} pages`)}
                          </span>
                        ) : null}
                        {plugin.skill_ids.length > 0 ? (
                          <span className="ae-badge">
                            {tx(`${plugin.skill_ids.length} 个技能`, `${plugin.skill_ids.length} skills`)}
                          </span>
                        ) : null}
                        {plugin.mcp_server_ids.length > 0 ? (
                          <span className="ae-badge">
                            {tx(
                              `${plugin.mcp_server_ids.length} 个 MCP 服务`,
                              `${plugin.mcp_server_ids.length} MCP servers`,
                            )}
                          </span>
                        ) : null}
                        {plugin.unsupported_components.length > 0 || plugin.warnings.length > 0 ? (
                          <span className="ae-badge ae-badge-warning">
                            <AlertTriangle size={12} />
                            {tx("含兼容性提示", "Compatibility notes")}
                          </span>
                        ) : null}
                      </div>
                      <button
                        type="button"
                        className="ae-btn ae-btn-sm"
                        onClick={() => setSelectedPluginId(plugin.id)}
                      >
                        {plugin.views?.length ? tx("打开插件", "Open plugin") : tx("查看详情", "View details")}
                      </button>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </section>
      ) : null}

      {overview && activeSection === "skills" ? (
        <CodexSkillsView
          overview={overview}
          busyIds={busyIds}
          createActionsBlocked={createActionsBlocked}
          onToggleSkill={(skill, next) => handleToggleSkill(skill, next)}
          onRequestRemoveSkill={skill =>
            openConfirmDelete({
              kind: "skill",
              id: skill.id,
              name: skill.title,
              revision: skill.revision,
            })
          }
          onOpenImportModal={() => setImportModalKind("skill")}
          onOverviewChange={nextOverview => {
            mutationEpochRef.current += 1;
            setOverview(nextOverview);
            setOverviewRevisionStale(false);
          }}
        />
      ) : null}

      {overview && activeSection === "mcp" ? (
        <section aria-label={tx("MCP 服务器", "MCP servers")}>
          <div className="ae-codex-mcp-header">
            <div>
              <h2>{tx("MCP 服务器", "MCP servers")}</h2>
              <p>{tx("连接外部工具和数据来源", "Connect external tools and data sources")}</p>
            </div>
          </div>

          <div className="ae-toolbar">
            <div className="ae-toolbar-left">
              <label className="ae-search">
                <Search size={16} />
                <input
                  type="search"
                  aria-label={tx("搜索 MCP 服务器", "Search MCP servers")}
                  placeholder={tx("搜索 MCP 服务器", "Search MCP servers")}
                  value={mcpSearch}
                  onChange={event => setMcpSearch(event.target.value)}
                />
              </label>
            </div>
            <div className="ae-toolbar-right">
              <button
                type="button"
                className="ae-btn"
                disabled={createActionsBlocked}
                onClick={() => setImportModalKind("mcp")}
              >
                <Upload size={15} />
                <span>{tx("导入配置", "Import configuration")}</span>
              </button>
              <button
                type="button"
                className="ae-btn ae-btn-primary"
                disabled={createActionsBlocked}
                onClick={() => setServerEditorState({ mode: "create" })}
              >
                <Plus size={15} />
                <span>{tx("添加服务器", "Add server")}</span>
              </button>
            </div>
          </div>

          {(() => {
            const customServers = filteredServers.filter(server => !server.plugin_id);
            const pluginServers = filteredServers.filter(server => Boolean(server.plugin_id));
            const hasSearch = mcpSearch.trim().length > 0;

            const renderMcpServerRow = (server: ExtensionMcpServer) => {
              const isTesting = Boolean(testingServerIds[server.id]);
              const isBusy =
                Boolean(busyIds[`mcp:${server.id}`]) ||
                Boolean(busyIds[`oauth:${server.id}`]) ||
                isTesting;
              const badge = getServerObservedBadge(
                server,
                isTesting,
                tx,
                overview.desktop_access,
              );
              const statusNotice = getServerStatusNotice(server, tx);
              const parentPlugin = server.plugin_id ? pluginsById.get(server.plugin_id) : undefined;
              const endpointSummary =
                server.transport === "streamable_http"
                  ? server.url
                  : [server.command, ...server.args].filter(Boolean).join(" ");
              const needsAuth = server.status === "NeedsAuth";

              return (
                <article key={server.id} className="ae-row">
                  <div className="ae-row-main">
                    <div className="ae-plugin-icon">
                      <Server size={18} />
                    </div>
                    <div className="ae-row-body">
                      <div className="ae-row-title">
                        <h3>{server.name}</h3>
                        <code>{server.id}</code>
                        <span className="ae-badge">
                          {server.transport === "streamable_http" ? "HTTP" : "STDIO"}
                        </span>
                        {server.builtin ? (
                          <span className="ae-badge ae-badge-info">
                            {tx("内置", "Built-in")}
                          </span>
                        ) : parentPlugin ? (
                          <span className="ae-badge">
                            {tx(`插件：${parentPlugin.title}`, `Plugin: ${parentPlugin.title}`)}
                          </span>
                        ) : null}
                        <span
                          className={`ae-badge ${
                            badge.tone === "success"
                              ? "ae-badge-success"
                              : badge.tone === "warning"
                                ? "ae-badge-warning"
                                : badge.tone === "danger"
                                  ? "ae-badge-danger"
                                  : badge.tone === "info"
                                    ? "ae-badge-info"
                                    : ""
                          }`}
                        >
                          <span className="ae-dot" />
                          {badge.label}
                        </span>
                      </div>
                      {server.description ? (
                        <p className="ae-row-desc">{server.description}</p>
                      ) : null}
                      <div className="ae-row-sub">
                        {endpointSummary ? <code>{endpointSummary}</code> : null}
                        <span>
                          {tx(
                            `已发现 ${server.tools_count} 个工具`,
                            `${server.tools_count} tool${server.tools_count === 1 ? "" : "s"} discovered`,
                          )}
                        </span>
                        {server.env_keys.length > 0 ? (
                          <span>
                            {tx(
                              `${server.env_keys.length} 个加密环境变量`,
                              `${server.env_keys.length} saved env var${server.env_keys.length === 1 ? "" : "s"}`,
                            )}
                          </span>
                        ) : null}
                        {(server.env_vars?.length ?? 0) > 0 ? (
                          <span>
                            {tx(
                              `${server.env_vars!.length} 个传递环境变量`,
                              `${server.env_vars!.length} passthrough env var${server.env_vars!.length === 1 ? "" : "s"}`,
                            )}
                          </span>
                        ) : null}
                        {server.header_keys.length > 0 ? (
                          <span>
                            {tx(
                              `${server.header_keys.length} 个加密请求头`,
                              `${server.header_keys.length} saved header${server.header_keys.length === 1 ? "" : "s"}`,
                            )}
                          </span>
                        ) : null}
                        {Object.keys(server.env_http_headers ?? {}).length > 0 ? (
                          <span>
                            {tx(
                              `${Object.keys(server.env_http_headers!).length} 个环境变量标头`,
                              `${Object.keys(server.env_http_headers!).length} env header${
                                Object.keys(server.env_http_headers!).length === 1 ? "" : "s"
                              }`,
                            )}
                          </span>
                        ) : null}
                      </div>
                      {statusNotice ? (
                        <div
                          className={
                            statusNotice.tone === "error"
                              ? "ae-row-error"
                              : statusNotice.tone === "warning"
                                ? "ae-row-warning"
                                : "ae-row-notice"
                          }
                          role={statusNotice.tone === "error" ? "alert" : "status"}
                        >
                          <span>{statusNotice.text}</span>
                          {statusNotice.showRetry ? (
                            <button
                              type="button"
                              className="ae-btn ae-btn-sm"
                              disabled={isBusy}
                              onClick={() => void handleTestOrRestartServer(server, "test")}
                            >
                              <RotateCcw size={13} />
                              <span>{tx("重试连接", "Retry connection")}</span>
                            </button>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                  </div>

                  <div className="ae-row-actions">
                    {needsAuth && !server.builtin ? (
                      <>
                        <select
                          className="ae-select-sm"
                          aria-label={tx(
                            `${server.name} OAuth 客户端注册方式`,
                            `${server.name} OAuth client registration`,
                          )}
                          value={server.oauth_client_registration ?? "auto"}
                          disabled={isBusy}
                          onChange={event =>
                            void handleUpdateServerOauthRegistration(
                              server,
                              event.target.value as ExtensionMcpOauthClientRegistration,
                            )
                          }
                        >
                          <option value="auto">{tx("自动", "Auto")}</option>
                          <option value="cimd">
                            {tx(
                              "客户端 ID 元数据文档 (CIMD)",
                              "Client ID Metadata Document (CIMD)",
                            )}
                          </option>
                          <option value="dcr">
                            {tx("动态客户端注册 (DCR)", "Dynamic Client Registration (DCR)")}
                          </option>
                        </select>
                        <button
                          type="button"
                          className="ae-btn ae-btn-sm"
                          disabled={isBusy}
                          onClick={() => selectServer(server.id)}
                        >
                          <span>
                            {isTesting
                              ? tx("启动中…", "Starting…")
                              : tx("进行身份验证", "Authenticate")}
                          </span>
                        </button>
                      </>
                    ) : null}
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm"
                      disabled={isBusy}
                      onClick={() => void handleTestOrRestartServer(server, "test")}
                    >
                      <Play size={13} />
                      <span>
                        {isTesting ? tx("正在测试…", "Testing…") : tx("测试连接", "Test connection")}
                      </span>
                    </button>
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm"
                      onClick={() => selectServer(server.id)}
                    >
                      {tx("查看详情", "View details")}
                    </button>
                    {!server.builtin ? (
                      <button
                        type="button"
                        className="ae-icon-btn"
                        title={tx("设置", "Settings")}
                        aria-label={tx(`${server.name} 设置`, `${server.name} settings`)}
                        disabled={isBusy}
                        onClick={() => setServerEditorState({ mode: "edit", server })}
                      >
                        <Settings size={15} />
                      </button>
                    ) : null}
                    <SwitchButton
                      checked={server.component_enabled}
                      disabled={isBusy}
                      label={tx(`启用服务器 ${server.name}`, `Enable server ${server.name}`)}
                      onChange={next => void handleToggleServer(server, next)}
                    />
                    <details
                      className="ae-menu"
                      onKeyDown={event => {
                        if (event.key === "Escape") {
                          event.currentTarget.open = false;
                        }
                      }}
                    >
                      <summary
                        className="ae-icon-btn"
                        aria-label={tx(
                          `${server.name} 的更多操作`,
                          `More actions for ${server.name}`,
                        )}
                      >
                        <MoreHorizontal size={16} />
                      </summary>
                      <div className="ae-menu-panel">
                        <button
                          type="button"
                          onClick={event => {
                            const details = event.currentTarget.closest("details");
                            if (details) details.open = false;
                            selectServer(server.id);
                          }}
                        >
                          <Wrench size={14} />
                          <span>{tx("工具与详情", "Tools & details")}</span>
                        </button>
                        <button
                          type="button"
                          disabled={isBusy}
                          onClick={event => {
                            const details = event.currentTarget.closest("details");
                            if (details) details.open = false;
                            void handleTestOrRestartServer(server, "restart");
                          }}
                        >
                          <RotateCcw size={14} />
                          <span>{tx("重启并检测", "Restart & probe")}</span>
                        </button>
                        {!server.builtin ? (
                          <button
                            type="button"
                            onClick={event => {
                              const details = event.currentTarget.closest("details");
                              if (details) details.open = false;
                              setServerEditorState({ mode: "edit", server });
                            }}
                          >
                            <FileCode2 size={14} />
                            <span>{tx("编辑配置", "Edit configuration")}</span>
                          </button>
                        ) : null}
                        {server.deletable && !server.builtin ? (
                          <button
                            type="button"
                            className="ae-menu-danger"
                            onClick={event => {
                              const details = event.currentTarget.closest("details");
                              if (details) details.open = false;
                              openConfirmDelete({
                                kind: "mcp",
                                id: server.id,
                                name: server.name,
                                revision: server.revision,
                              });
                            }}
                          >
                            <Trash2 size={14} />
                            <span>{tx("移除服务器", "Remove server")}</span>
                          </button>
                        ) : null}
                      </div>
                    </details>
                  </div>
                </article>
              );
            };

            if (filteredServers.length === 0 && hasSearch) {
              return (
                <div className="ae-empty">
                  <Server size={26} />
                  <p>{tx("未找到 MCP 服务器", "No MCP servers found")}</p>
                </div>
              );
            }

            return (
              <div className="ae-codex-mcp-stack">
                {/* Card 1: Servers (settings.mcp.myServers) */}
                <div className="ae-codex-card">
                  <div className="ae-codex-card-head">
                    <h3>{tx("服务器", "Servers")}</h3>
                    <div className="ae-codex-card-actions">
                      {mcpModifiedSinceRestart ? (
                        <button
                          type="button"
                          className="ae-btn ae-btn-sm"
                          disabled={mcpRestartingAll}
                          onClick={() => void handleRestartAllMcpServers()}
                        >
                          <RotateCcw size={13} />
                          <span>{mcpRestartingAll ? tx("正在重启…", "Restarting…") : tx("重启", "Restart")}</span>
                        </button>
                      ) : null}
                      <button
                        type="button"
                        className="ae-btn ae-btn-sm"
                        disabled={createActionsBlocked}
                        onClick={() => setServerEditorState({ mode: "create" })}
                      >
                        <Plus size={13} />
                        <span>{tx("连接至自定义 MCP", "Connect to a custom MCP")}</span>
                      </button>
                    </div>
                  </div>
                  {customServers.length === 0 ? (
                    <div className="ae-codex-empty-row">
                      <span>{tx("未连接 MCP 服务器", "No MCP servers connected")}</span>
                    </div>
                  ) : (
                    <div className="ae-list">{customServers.map(renderMcpServerRow)}</div>
                  )}
                </div>

                {/* Card 2: From plugins (settings.mcp.fromPlugins) */}
                {pluginServers.length > 0 ? (
                  <div className="ae-codex-card">
                    <div className="ae-codex-card-head">
                      <h3>{tx("来自插件", "From plugins")}</h3>
                    </div>
                    <div className="ae-list">{pluginServers.map(renderMcpServerRow)}</div>
                  </div>
                ) : null}

                {/* Card 3: MCP Apps (settings.mcp.apps.title) */}
                <div className="ae-codex-card">
                  <div className="ae-codex-card-head">
                    <h3>{tx("MCP 应用", "MCP Apps")}</h3>
                  </div>
                  <div className="ae-codex-app-data-row">
                    <div className="ae-setting-copy">
                      <strong>{tx("应用数据", "App data")}</strong>
                      <small>
                        {tx(
                          "清除所有 MCP 应用的站点数据和缓存",
                          "Clear site data and cache from all MCP apps",
                        )}
                      </small>
                    </div>
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm"
                      disabled={clearingMcpAppData}
                      onClick={() => void handleClearMcpAppData()}
                    >
                      <span>
                        {clearingMcpAppData
                          ? tx("正在清除…", "Clearing…")
                          : tx("清除所有应用数据", "Clear all app data")}
                      </span>
                    </button>
                  </div>
                </div>
              </div>
            );
          })()}
        </section>
      ) : null}

      {/* Plugin Details Dialog */}
      {selectedPlugin && overview && !confirmDelete ? (
        <PluginDetailsModal
          plugin={selectedPlugin}
          overview={overview}
          busyIds={busyIds}
          initialViewId={selectedPluginViewId}
          onRefresh={() => { void loadOverview(true); }}
          onClose={() => {
            setSelectedPluginId(null);
            setSelectedPluginViewId(undefined);
          }}
          onTogglePlugin={next => void handleTogglePlugin(selectedPlugin, next)}
          onToggleDesktopAccess={next => void handleToggleDesktopAccess(next)}
          onToggleSkill={(skill, next) => void handleToggleSkill(skill, next)}
          onToggleServer={(server, next) => void handleToggleServer(server, next)}
          onOpenSkill={skillId => {
            setSelectedPluginId(null);
            setSelectedSkillId(skillId);
          }}
          onOpenServer={serverId => {
            setSelectedPluginId(null);
            selectServer(serverId);
          }}
          onRequestUninstall={() =>
            openConfirmDelete({
              kind: "plugin",
              id: selectedPlugin.id,
              name: selectedPlugin.title,
              revision: selectedPlugin.revision,
            })
          }
        />
      ) : null}

      {/* Skill Details Dialog */}
      {selectedSkill && !confirmDelete ? (
        <SkillDetailsModal
          skill={selectedSkill}
          parentPlugin={selectedSkill.plugin_id ? pluginsById.get(selectedSkill.plugin_id) : undefined}
          busy={Boolean(busyIds[`skill:${selectedSkill.id}`])}
          onClose={() => setSelectedSkillId(null)}
          onToggle={next => void handleToggleSkill(selectedSkill, next)}
          onRequestRemove={() =>
            openConfirmDelete({
              kind: "skill",
              id: selectedSkill.id,
              name: selectedSkill.title,
              revision: selectedSkill.revision,
            })
          }
        />
      ) : null}

      {/* MCP Server Details Dialog */}
      {selectedServer && !confirmDelete ? (
        <McpServerDetailsModal
          server={selectedServer}
          onRefresh={() => { void loadOverview(true); }}
          desktopAccess={overview?.desktop_access}
          testing={Boolean(testingServerIds[selectedServer.id])}
          probeResult={activeServerProbeResult}
          busyIds={busyIds}
          onClose={() => selectServer(null)}
          onToggleServer={next => void handleToggleServer(selectedServer, next)}
          onTest={() => void handleTestOrRestartServer(selectedServer, "test")}
          onRestart={() => void handleTestOrRestartServer(selectedServer, "restart")}
          onEdit={() => {
            const target = selectedServer;
            selectServer(null);
            setServerEditorState({ mode: "edit", server: target });
          }}
          onToggleTool={(tool, next) => void handleToggleServerTool(selectedServer, tool, next)}
          onClearAllowlist={() => void handleClearToolAllowlist(selectedServer)}
          onToggleToolReadOnly={(tool, next) =>
            void handleToggleToolReadOnly(selectedServer, tool, next)
          }
          onRequestDelete={() =>
            openConfirmDelete({
              kind: "mcp",
              id: selectedServer.id,
              name: selectedServer.name,
              revision: selectedServer.revision,
            })
          }
        />
      ) : null}

      {/* Add / Edit MCP Server Dialog */}
      {serverEditorState && overview ? (
        <McpServerFormModal
          mode={serverEditorState.mode}
          initialServer={serverEditorState.server}
          overviewRevision={overview.revision}
          onClose={() => setServerEditorState(null)}
          onSaved={async (savedServer, runTestAfter) => {
            invalidateServerProbe(savedServer.id);
            setMcpModifiedSinceRestart(true);
            setServerEditorState(null);
            await reconcileOverviewAfterMutation();
            if (runTestAfter) {
              await handleTestOrRestartServer(savedServer, "test");
            }
          }}
          onRequestDelete={
            serverEditorState.mode === "edit" &&
            serverEditorState.server &&
            serverEditorState.server.deletable &&
            !serverEditorState.server.builtin
              ? () => {
                  const target = serverEditorState.server!;
                  setServerEditorState(null);
                  openConfirmDelete({
                    kind: "mcp",
                    id: target.id,
                    name: target.name,
                    revision: target.revision,
                  });
                }
              : undefined
          }
          onConflictRefetch={async () => {
            const latest = await reconcileOverviewAfterMutation();
            if (!latest) return null;
            if (serverEditorState.mode === "edit" && serverEditorState.server) {
              const updatedServer =
                latest.mcp_servers.find(s => s.id === serverEditorState.server?.id) ?? null;
              return {
                overview: latest,
                server: updatedServer,
              };
            }
            return {
              overview: latest,
              server: null,
            };
          }}
        />
      ) : null}

      {/* Import Preview & Commit Dialog */}
      {importModalKind && overview ? (
        <ExtensionImportModal
          initialKind={importModalKind}
          onClose={() => setImportModalKind(null)}
          onCommitted={nextOverview => {
            mutationEpochRef.current += 1;
            setOverview(nextOverview);
            setOverviewRevisionStale(false);
            const committedKind = importModalKind;
            setImportModalKind(null);
            if (committedKind === "plugin") navigate(`${baseRoute}/plugins`);
            else if (committedKind === "skill") navigate(`${baseRoute}/skills`);
            else if (committedKind === "mcp") navigate(`${baseRoute}/mcp`);
          }}
          onConflictRefetch={async () => {
            const latest = await reconcileOverviewAfterMutation();
            return latest?.revision ?? null;
          }}
        />
      ) : null}

      {/* Delete / Uninstall Confirmation Dialog */}
      {confirmDelete ? (
        <AccessibleModal
          title={
            confirmDelete.kind === "plugin"
              ? tx(`卸载插件 ${confirmDelete.name}`, `Uninstall plugin ${confirmDelete.name}`)
              : confirmDelete.kind === "skill"
                ? tx(`移除技能 ${confirmDelete.name}`, `Remove skill ${confirmDelete.name}`)
                : tx(`移除服务器 ${confirmDelete.name}`, `Remove server ${confirmDelete.name}`)
          }
          onClose={() => {
            if (!deleteBusy) closeConfirmDelete();
          }}
          busy={deleteBusy}
          footer={
            <div className="ae-dialog-foot-right">
              <button
                type="button"
                className="ae-btn"
                disabled={deleteBusy}
                onClick={closeConfirmDelete}
              >
                {tx("取消", "Cancel")}
              </button>
              <button
                type="button"
                className="ae-btn ae-btn-danger"
                disabled={deleteBusy}
                onClick={() => void handleConfirmDelete()}
              >
                <Trash2 size={14} />
                <span>
                  {deleteBusy
                    ? tx("正在移除…", "Removing…")
                    : confirmDelete.kind === "plugin"
                      ? tx("确认卸载", "Confirm uninstall")
                      : tx("确认移除", "Confirm remove")}
                </span>
              </button>
            </div>
          }
        >
          {deleteError ? (
            <div className="ae-banner ae-banner-error" role="alert">
              <span>{deleteError}</span>
            </div>
          ) : null}
          <p style={{ margin: 0, lineHeight: 1.5 }}>
            {confirmDelete.kind === "plugin"
              ? tx(
                  `确定卸载插件包「${confirmDelete.name}」吗？该插件包内包含的技能与 MCP 服务器配置也将一并移除。已有任务的历史记录与回执不会丢失。`,
                  `Are you sure you want to uninstall "${confirmDelete.name}"? Bundled skills and MCP server configurations from this plugin package will also be removed. Existing task history and receipts remain intact.`,
                )
              : confirmDelete.kind === "skill"
                ? tx(
                    `确定移除已导入的技能「${confirmDelete.name}」吗？已创建任务的技能快照将保留，新任务将不再提供此技能。`,
                    `Are you sure you want to remove the imported skill "${confirmDelete.name}"? Snapshots on existing tasks are preserved, and new tasks will no longer list this skill.`,
                  )
                : tx(
                    `确定移除 MCP 服务器「${confirmDelete.name}」及其保存的凭据吗？`,
                    `Are you sure you want to remove MCP server "${confirmDelete.name}" and its saved credentials?`,
                  )}
          </p>
        </AccessibleModal>
      ) : null}
    </div>
  );
}

function PluginDetailsModal({
  plugin,
  overview,
  busyIds,
  initialViewId,
  onRefresh,
  onClose,
  onTogglePlugin,
  onToggleDesktopAccess,
  onToggleSkill,
  onToggleServer,
  onOpenSkill,
  onOpenServer,
  onRequestUninstall,
}: {
  plugin: ExtensionPlugin;
  overview: ExtensionOverview;
  busyIds: Record<string, boolean>;
  initialViewId?: string;
  onRefresh?: () => void;
  onClose: () => void;
  onTogglePlugin: (next: boolean) => void;
  onToggleDesktopAccess: (next: boolean) => void;
  onToggleSkill: (skill: ExtensionSkill, next: boolean) => void;
  onToggleServer: (server: ExtensionMcpServer, next: boolean) => void;
  onOpenSkill: (skillId: string) => void;
  onOpenServer: (serverId: string) => void;
  onRequestUninstall: () => void;
}) {
  const tx = useExtText();
  const isComputerUse = plugin.id === "computer-use";
  const winSupported =
    plugin.capabilities?.windows_supported ?? overview.windows_supported ?? true;

  const bundledSkills = useMemo(() => {
    if (plugin.skills && plugin.skills.length > 0) return plugin.skills;
    return overview.skills.filter(s => s.plugin_id === plugin.id || plugin.skill_ids.includes(s.id));
  }, [plugin, overview.skills]);

  const bundledServers = useMemo(() => {
    if (plugin.mcp_servers && plugin.mcp_servers.length > 0) return plugin.mcp_servers;
    return overview.mcp_servers.filter(
      m => m.plugin_id === plugin.id || plugin.mcp_server_ids.includes(m.id),
    );
  }, [plugin, overview.mcp_servers]);

  return (
    <AccessibleModal
      title={plugin.title}
      wide={Boolean(plugin.views?.length)}
      subtitle={`${formatSourceLabel(plugin.source_format, plugin.builtin, tx)} · v${plugin.version}`}
      onClose={onClose}
      footer={
        <>
          <div>
            {plugin.deletable && !plugin.builtin ? (
              <button type="button" className="ae-btn ae-btn-danger" onClick={onRequestUninstall}>
                <Trash2 size={14} />
                <span>{tx("卸载插件", "Uninstall plugin")}</span>
              </button>
            ) : null}
          </div>
          <div className="ae-dialog-foot-right">
            <button type="button" className="ae-btn" onClick={onClose}>
              {tx("关闭", "Close")}
            </button>
          </div>
        </>
      }
    >
      <p style={{ margin: 0, lineHeight: 1.5 }}>{plugin.description}</p>
      {plugin.web_app ? <WebAppAccessSummary app={plugin.web_app} /> : null}

      <PluginViews plugin={plugin} servers={bundledServers} initialViewId={initialViewId} onOpenServer={onOpenServer} onRefresh={onRefresh} />

      <details className="ae-plugin-settings" open={!plugin.enabled || !plugin.views?.length ? true : undefined}>
        <summary>{tx("插件设置与连接", "Plugin settings & connections")}</summary>

      {plugin.peer_provider ? <PeerProviderSettings key={`${plugin.id}:${plugin.instance_id}`} plugin={plugin} /> : null}

      <div className="ae-setting-row">
        <div className="ae-setting-copy">
          <strong>{tx("启用插件包", "Enable plugin package")}</strong>
          <small>
            {tx(
              "停用插件包会立即停用其包含的所有技能与 MCP 服务器，但会保留子组件的单独开关偏好。",
              "Disabling the plugin package immediately disables all bundled skills and MCP servers while retaining their individual switch preferences.",
            )}
          </small>
        </div>
        <SwitchButton
          checked={plugin.component_enabled}
          disabled={Boolean(busyIds[`plugin:${plugin.id}`]) || (isComputerUse && !winSupported)}
          label={tx(`启用插件包 ${plugin.title}`, `Enable plugin package ${plugin.title}`)}
          onChange={onTogglePlugin}
        />
      </div>

      {isComputerUse ? (
        <>
          <div className="ae-setting-row">
            <div className="ae-setting-copy">
              <strong>
                {tx("允许电脑和浏览器操作", "Allow computer and browser operations")}
              </strong>
              <small>
                {tx(
                  "对应 Agent 设置中的 desktop_access 权限。需要同时开启此权限与 Computer Use 插件，任务才能调用桌面工具。",
                  "Controls the desktop_access permission in Agent settings. Both this permission and the Computer Use plugin must be enabled for tasks to use desktop tools.",
                )}
              </small>
            </div>
            <SwitchButton
              checked={overview.desktop_access}
              disabled={Boolean(busyIds.desktop_access)}
              label={tx("允许电脑和浏览器操作", "Allow computer and browser operations")}
              onChange={onToggleDesktopAccess}
            />
          </div>

          <AgentComputerStatusPanel
            refreshKey={`${overview.revision}:${plugin.revision}:${plugin.enabled ? 1 : 0}:${plugin.component_enabled ? 1 : 0}:${overview.desktop_access ? 1 : 0}`}
          />

          <div className="ae-section-card">
            <h3>{tx("平台支持与能力边界", "Platform support & capabilities")}</h3>
            <div className="ae-plugin-meta">
              <span className={`ae-badge ${winSupported ? "ae-badge-success" : "ae-badge-warning"}`}>
                {winSupported
                  ? tx("当前系统支持 Windows 桌面控制", "Windows desktop control supported")
                  : tx("当前系统非 Windows · 桌面控制不可用", "Non-Windows platform · Unavailable")}
              </span>
              <span className={`ae-badge ${overview.desktop_access ? "ae-badge-success" : "ae-badge-warning"}`}>
                {overview.desktop_access
                  ? tx("桌面权限：已开启", "Desktop permission: Granted")
                  : tx("桌面权限：未开启", "Desktop permission: Off")}
              </span>
            </div>
            {plugin.capabilities?.observable?.length ? (
              <div>
                <strong style={{ fontSize: 13 }}>
                  {tx("可观察内容（只读）：", "What it can observe (read-only):")}
                </strong>
                <ul style={{ margin: "6px 0 0", paddingLeft: 20, fontSize: 13 }}>
                  {plugin.capabilities.observable.map((item, idx) => (
                    <li key={idx}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {plugin.capabilities?.controllable?.length ? (
              <div>
                <strong style={{ fontSize: 13 }}>
                  {tx("可操作内容（遵循任务确认模式）：", "What it can control (follows task confirmation mode):")}
                </strong>
                <ul style={{ margin: "6px 0 0", paddingLeft: 20, fontSize: 13 }}>
                  {plugin.capabilities.controllable.map((item, idx) => (
                    <li key={idx}>{item}</li>
                  ))}
                </ul>
              </div>
            ) : null}
            {plugin.capabilities?.permission_note ? (
              <small style={{ color: "var(--pf-muted)" }}>{plugin.capabilities.permission_note}</small>
            ) : null}
          </div>
        </>
      ) : null}

      {bundledSkills.length > 0 ? (
        <div className="ae-section-card">
          <h3>{tx(`包含的技能 (${bundledSkills.length})`, `Bundled skills (${bundledSkills.length})`)}</h3>
          <div className="ae-tool-list">
            {bundledSkills.map(skill => (
              <div key={skill.id} className="ae-tool-row">
                <div className="ae-setting-copy">
                  <strong>
                    {skill.title} <code>v{skill.version}</code>
                  </strong>
                  <small>{skill.description}</small>
                  {!skill.parent_enabled && skill.component_enabled ? (
                    <small style={{ color: "#b45309" }}>
                      {tx(
                        "当前未生效：父插件已停用（技能自身开关保持开启）",
                        "Currently inactive: parent plugin is disabled (skill preference remains on)",
                      )}
                    </small>
                  ) : null}
                </div>
                <div className="ae-tool-controls">
                  <button
                    type="button"
                    className="ae-btn ae-btn-sm"
                    onClick={() => onOpenSkill(skill.id)}
                  >
                    {tx("查看技能", "View skill")}
                  </button>
                  <SwitchButton
                    checked={skill.component_enabled}
                    disabled={Boolean(busyIds[`skill:${skill.id}`])}
                    label={tx(`启用包内技能 ${skill.title}`, `Enable bundled skill ${skill.title}`)}
                    onChange={next => onToggleSkill(skill, next)}
                  />
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {bundledServers.length > 0 ? (
        <div className="ae-section-card">
          <h3>
            {tx(
              `包含的 MCP 服务器 (${bundledServers.length})`,
              `Bundled MCP servers (${bundledServers.length})`,
            )}
          </h3>
          <div className="ae-tool-list">
            {bundledServers.map(server => {
              const badge = getServerObservedBadge(server, false, tx, overview.desktop_access);
              const statusSummary = badge.label.includes(String(server.tools_count))
                ? tx(`状态：${badge.label}`, `Status: ${badge.label}`)
                : tx(
                    `状态：${badge.label} · ${server.tools_count} 个工具`,
                    `Status: ${badge.label} · ${server.tools_count} tools`,
                  );
              return (
                <div key={server.id} className="ae-tool-row">
                  <div className="ae-setting-copy">
                    <strong>
                      {server.name} ({server.transport === "streamable_http" ? "HTTP" : "STDIO"})
                    </strong>
                    <small>{statusSummary}</small>
                    {!server.parent_enabled && server.component_enabled ? (
                      <small style={{ color: "#b45309" }}>
                        {tx(
                          "当前未生效：父插件已停用（服务器自身开关保持开启）",
                          "Currently inactive: parent plugin is disabled (server preference remains on)",
                        )}
                      </small>
                    ) : null}
                  </div>
                  <div className="ae-tool-controls">
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm"
                      onClick={() => onOpenServer(server.id)}
                    >
                      {tx("查看服务器与工具", "Inspect server & tools")}
                    </button>
                    <SwitchButton
                      checked={server.component_enabled}
                      disabled={Boolean(busyIds[`mcp:${server.id}`])}
                      label={tx(`启用包内服务器 ${server.name}`, `Enable bundled server ${server.name}`)}
                      onChange={next => onToggleServer(server, next)}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {plugin.unsupported_components.length > 0 || plugin.warnings.length > 0 ? (
        <div className="ae-section-card">
          <h3>{tx("兼容性与未支持的组件", "Compatibility & unsupported components")}</h3>
          {plugin.unsupported_components.length > 0 ? (
            <div className="ae-plugin-meta">
              <span style={{ fontSize: 13 }}>
                {tx("已忽略的组件类型：", "Ignored component types:")}
              </span>
              {plugin.unsupported_components.map(comp => (
                <span key={comp} className="ae-badge ae-badge-warning">
                  {comp}
                </span>
              ))}
            </div>
          ) : null}
          {plugin.warnings.length > 0 ? (
            <ul style={{ margin: 0, paddingLeft: 20, fontSize: 13 }}>
              {plugin.warnings.map((warn, index) => (
                <li key={index}>{warn}</li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
      </details>
    </AccessibleModal>
  );
}

function SkillDetailsModal({
  skill,
  parentPlugin,
  busy,
  onClose,
  onToggle,
  onRequestRemove,
}: {
  skill: ExtensionSkill;
  parentPlugin?: ExtensionPlugin;
  busy: boolean;
  onClose: () => void;
  onToggle: (next: boolean) => void;
  onRequestRemove: () => void;
}) {
  const tx = useExtText();
  return (
    <AccessibleModal
      title={skill.title}
      subtitle={`$${skill.id} · v${skill.version} · ${
        skill.builtin ? tx("内置技能", "Built-in skill") : formatSourceLabel(skill.source_format, false, tx)
      }`}
      onClose={onClose}
      footer={
        <>
          <div>
            {skill.deletable && !skill.builtin ? (
              <button type="button" className="ae-btn ae-btn-danger" onClick={onRequestRemove}>
                <Trash2 size={14} />
                <span>{tx("移除技能", "Remove skill")}</span>
              </button>
            ) : (
              <span style={{ fontSize: 12.5, color: "var(--pf-muted)" }}>
                {tx("内置技能可停用，但不可删除。", "Built-in skills can be disabled but cannot be removed.")}
              </span>
            )}
          </div>
          <div className="ae-dialog-foot-right">
            <button type="button" className="ae-btn" onClick={onClose}>
              {tx("关闭", "Close")}
            </button>
          </div>
        </>
      }
    >
      <div className="ae-setting-row">
        <div className="ae-setting-copy">
          <strong>{tx("启用此技能", "Enable this skill")}</strong>
          <small>
            {!skill.parent_enabled && skill.component_enabled
              ? tx(
                  `所属插件「${parentPlugin?.title || skill.plugin_id}」已停用，此技能在父插件启用前不会对新任务生效。`,
                  `Parent plugin "${parentPlugin?.title || skill.plugin_id}" is disabled. This skill remains inactive until the parent plugin is enabled.`,
                )
              : tx(
                  "启用后，新任务可在技能选择器中勾选此技能，或在对话中通过 $技能名 引用。",
                  "When enabled, new tasks can select this skill or invoke it with $skill-name.",
                )}
          </small>
        </div>
        <SwitchButton
          checked={skill.component_enabled}
          disabled={busy}
          label={tx(`启用技能 ${skill.title}`, `Enable skill ${skill.title}`)}
          onChange={onToggle}
        />
      </div>

      {skill.warnings.length > 0 ? (
        <div className="ae-banner ae-banner-warning" role="status">
          <div>
            <strong>{tx("兼容性与文件引用提示", "Compatibility & file reference warnings")}</strong>
            <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
              {skill.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        </div>
      ) : null}

      <div className="ae-section-card">
        <h3>{tx(`技能包文件 (${skill.files.length})`, `Skill package files (${skill.files.length})`)}</h3>
        {skill.files.length === 0 ? (
          <small>{tx("无附加文件", "No attached files")}</small>
        ) : (
          <ul className="ae-file-list">
            {skill.files.map(file => (
              <li key={file.path}>
                <code>{file.path}</code>
                <span>{formatBytes(file.size)}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="ae-section-card">
        <h3>{tx("SKILL.md", "SKILL.md")}</h3>
        <pre className="ae-code-preview">{skill.content}</pre>
      </div>
    </AccessibleModal>
  );
}

function McpServerDetailsModal({
  server,
  onRefresh,
  desktopAccess,
  testing,
  probeResult,
  busyIds,
  onClose,
  onToggleServer,
  onTest,
  onRestart,
  onEdit,
  onToggleTool,
  onClearAllowlist,
  onToggleToolReadOnly,
  onRequestDelete,
}: {
  server: ExtensionMcpServer;
  onRefresh: () => void;
  desktopAccess?: boolean;
  testing: boolean;
  probeResult?: ServerExplicitProbeResult | null;
  busyIds: Record<string, boolean>;
  onClose: () => void;
  onToggleServer: (next: boolean) => void;
  onTest: () => void;
  onRestart: () => void;
  onEdit: () => void;
  onToggleTool: (tool: ExtensionMcpTool, next: boolean) => void;
  onClearAllowlist: () => void;
  onToggleToolReadOnly: (tool: ExtensionMcpTool, next: boolean) => void;
  onRequestDelete: () => void;
}) {
  const tx = useExtText();
  const badge = getServerObservedBadge(server, testing, tx, desktopAccess);
  const statusNotice = getServerStatusNotice(server, tx);
  const showProbeResult =
    Boolean(probeResult) &&
    !testing &&
    !(
      probeResult?.status === "error" &&
      statusNotice?.tone === "error" &&
      Boolean(probeResult.error) &&
      statusNotice.text.includes(probeResult.error)
    );

  return (
    <AccessibleModal
      title={server.name}
      subtitle={`${server.id} · ${server.transport === "streamable_http" ? "Streamable HTTP" : "STDIO"}`}
      onClose={onClose}
      footer={
        <>
          <div>
            {server.deletable && !server.builtin ? (
              <button type="button" className="ae-btn ae-btn-danger" onClick={onRequestDelete}>
                <Trash2 size={14} />
                <span>{tx("移除服务器", "Remove server")}</span>
              </button>
            ) : null}
          </div>
          <div className="ae-dialog-foot-right">
            {!server.builtin ? (
              <button type="button" className="ae-btn" onClick={onEdit}>
                <FileCode2 size={14} />
                <span>{tx("编辑配置", "Edit")}</span>
              </button>
            ) : null}
            <button type="button" className="ae-btn" disabled={testing} onClick={onRestart}>
              <RotateCcw size={14} />
              <span>{tx("重启", "Restart")}</span>
            </button>
            <button
              type="button"
              className="ae-btn ae-btn-primary"
              disabled={testing}
              onClick={onTest}
            >
              <Play size={14} />
              <span>{testing ? tx("正在测试…", "Testing…") : tx("测试连接", "Test connection")}</span>
            </button>
          </div>
        </>
      }
    >
      <div className="ae-setting-row">
        <div className="ae-setting-copy">
          <strong>{tx("启用此 MCP 服务器", "Enable this MCP server")}</strong>
          <small>
            {tx(
              "仅启用配置不会假装已连接；点击「测试连接」将实际启动或连接服务器并获取工具列表。",
              "Enabling configuration does not fake a connection; click 'Test connection' to initialize the server and discover its tools.",
            )}
          </small>
        </div>
        <SwitchButton
          checked={server.component_enabled}
          disabled={Boolean(busyIds[`mcp:${server.id}`]) || testing}
          label={tx(`启用服务器 ${server.name}`, `Enable server ${server.name}`)}
          onChange={onToggleServer}
        />
      </div>

      <div className="ae-section-card">
        <h3>{tx("连接与传输摘要", "Connection & transport summary")}</h3>
        <div className="ae-plugin-meta">
          <span
            className={`ae-badge ${
              badge.tone === "success"
                ? "ae-badge-success"
                : badge.tone === "warning"
                  ? "ae-badge-warning"
                  : badge.tone === "danger"
                    ? "ae-badge-danger"
                    : "ae-badge-info"
            }`}
          >
            <span className="ae-dot" />
            {badge.label}
          </span>
          {server.last_tested_at ? (
            <span className="ae-badge">
              {tx(`上次测试：${server.last_tested_at}`, `Last tested: ${server.last_tested_at}`)}
            </span>
          ) : (
            <span className="ae-badge">{tx("尚未测试", "Never tested")}</span>
          )}
          <span className="ae-badge">
            {tx(
              `启动超时：${server.startup_timeout_ms}ms · 调用超时：${server.tool_timeout_ms}ms`,
              `Startup: ${server.startup_timeout_ms}ms · Tool timeout: ${server.tool_timeout_ms}ms`,
            )}
          </span>
        </div>

        <McpAuthSettings key={`${server.id}:${server.instance_id}`} server={server} onRefresh={onRefresh} />
        <McpDataPolicy key={`policy:${server.id}:${server.instance_id}`} server={server} onRefresh={onRefresh} />

        {server.transport === "stdio" ? (
          <div style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
            <div>
              <strong>{tx("命令：", "Command: ")}</strong>
              <code>{[server.command, ...server.args].filter(Boolean).join(" ") || "—"}</code>
            </div>
            {server.cwd ? (
              <div>
                <strong>{tx("工作目录：", "Working directory: ")}</strong>
                <code>{server.cwd}</code>
              </div>
            ) : null}
            {server.env_keys.length > 0 ? (
              <div>
                <strong>{tx("已保存环境变量：", "Saved environment variables: ")}</strong>
                {server.env_keys.map(k => (
                  <span key={k} className="ae-badge" style={{ marginRight: 6 }}>
                    {k}={server.masked_env[k]?.masked || MASKED_SECRET_PLACEHOLDER}
                  </span>
                ))}
              </div>
            ) : null}
            {(server.env_vars?.length ?? 0) > 0 ? (
              <div>
                <strong>{tx("环境变量传递：", "Environment variable passthrough: ")}</strong>
                {server.env_vars!.map(v => (
                  <span key={v} className="ae-badge" style={{ marginRight: 6 }}>
                    {v}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        ) : (
          <div style={{ fontSize: 13, display: "flex", flexDirection: "column", gap: 4 }}>
            <div>
              <strong>{tx("地址：", "URL: ")}</strong>
              <code>{server.url || "—"}</code>
            </div>
            {server.bearer_token_env ? (
              <div>
                <strong>{tx("令牌环境变量：", "Bearer Token Env: ")}</strong>
                <code>{server.bearer_token_env}</code>
              </div>
            ) : null}
            {server.header_keys.length > 0 ? (
              <div>
                <strong>{tx("已保存请求头：", "Saved HTTP headers: ")}</strong>
                {server.header_keys.map(k => (
                  <span key={k} className="ae-badge" style={{ marginRight: 6 }}>
                    {k}: {server.masked_headers[k]?.masked || MASKED_SECRET_PLACEHOLDER}
                  </span>
                ))}
              </div>
            ) : null}
            {Object.keys(server.env_http_headers ?? {}).length > 0 ? (
              <div>
                <strong>
                  {tx("来自环境变量的标头：", "Headers from environment variables: ")}
                </strong>
                {Object.entries(server.env_http_headers!).map(([hk, hv]) => (
                  <span key={hk} className="ae-badge" style={{ marginRight: 6 }}>
                    {`${hk} ← $${hv}`}
                  </span>
                ))}
              </div>
            ) : null}
          </div>
        )}

        {statusNotice ? (
          <div
            className={
              statusNotice.tone === "error"
                ? "ae-row-error"
                : statusNotice.tone === "warning"
                  ? "ae-row-warning"
                  : "ae-row-notice"
            }
            role={statusNotice.tone === "error" ? "alert" : "status"}
          >
            <span>{statusNotice.text}</span>
            {statusNotice.showRetry ? (
              <button type="button" className="ae-btn ae-btn-sm" disabled={testing} onClick={onTest}>
                <RotateCcw size={13} />
                <span>{tx("重试", "Retry")}</span>
              </button>
            ) : null}
          </div>
        ) : null}

        {showProbeResult && probeResult ? (
          <div
            className={probeResult.status === "error" ? "ae-row-error" : "ae-row-success"}
            role={probeResult.status === "error" ? "alert" : "status"}
            data-testid="mcp-explicit-probe-result"
          >
            <span>
              <strong>
                {probeResult.status === "error"
                  ? probeResult.action === "restart"
                    ? tx("最近重启失败：", "Last restart failed: ")
                    : tx("最近测试失败：", "Last test failed: ")
                  : probeResult.action === "restart"
                    ? tx("最近重启成功：", "Last restart succeeded: ")
                    : tx("最近测试成功：", "Last test succeeded: ")}
              </strong>
              {probeResult.status === "error"
                ? probeResult.error || tx("连接探测失败。", "Connection probe failed.")
                : tx(
                    `连接探测通过（发现 ${probeResult.toolsCount} 个工具）${
                      !server.enabled ? "；当前服务器仍处于停用状态。" : "。"
                    }`,
                    `Connection probe passed (${probeResult.toolsCount} tools discovered)${
                      !server.enabled ? "; server remains disabled." : "."
                    }`,
                  )}
            </span>
          </div>
        ) : null}
      </div>

      <div className="ae-section-card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <h3>
            {tx(
              `工具列表与权限策略 (${server.tools.length})`,
              `Discovered tools & policy (${server.tools.length})`,
            )}
          </h3>
          {server.enabled_tools !== null ? (
            <button
              type="button"
              className="ae-btn ae-btn-sm"
              disabled={Boolean(busyIds[`allowlist:${server.id}`])}
              onClick={onClearAllowlist}
            >
              {tx(
                `当前为白名单模式（允许 ${server.enabled_tools.length} 项）· 点击允许全部`,
                `Allowlist active (${server.enabled_tools.length} allowed) · Allow all tools`,
              )}
            </button>
          ) : null}
        </div>
        <small style={{ color: "var(--pf-muted)" }}>
          {tx(
            "安全边界：第三方 MCP 工具默认视为写入/高风险操作（需遵循确认模式），服务器自报的 readOnlyHint 仅作参考，除非你明确将其标记为只读。",
            "Security boundary: third-party MCP tools default to write/approval-required. Server-reported readOnlyHint is advisory only unless you explicitly mark a tool as read-only.",
          )}
        </small>

        {server.tools.length === 0 ? (
          <p style={{ margin: "6px 0 0", fontSize: 13, color: "var(--pf-muted)" }}>
            {tx(
              "暂未发现工具。请点击「测试连接」从服务器获取实时工具列表。",
              "No tools discovered yet. Click 'Test connection' to fetch the live tool catalog from the server.",
            )}
          </p>
        ) : (
          <div className="ae-tool-list">
            {server.tools.map(tool => {
              const toolBusy =
                Boolean(busyIds[`tool:${server.id}:${tool.name}`]) ||
                Boolean(busyIds[`ro:${server.id}:${tool.name}`]);
              return (
                <div key={tool.name} className="ae-tool-row">
                  <div className="ae-setting-copy" style={{ flex: "1 1 240px" }}>
                    <div className="ae-row-title">
                      <strong>{tool.title || tool.name}</strong>
                      <code>{tool.name}</code>
                      <span
                        className={`ae-badge ${
                          tool.effective_effect === "read" ? "ae-badge-info" : "ae-badge-warning"
                        }`}
                      >
                        {tool.effective_effect === "read"
                          ? tx("只读", "Read-only")
                          : tx("写入 / 需确认", "Write / approval")}
                      </span>
                      {tool.annotations?.readOnlyHint && !tool.owner_read_only ? (
                        <span className="ae-badge">
                          {tx("服务端提示：readOnlyHint", "Hint: readOnlyHint")}
                        </span>
                      ) : null}
                    </div>
                    {tool.description ? <small>{tool.description}</small> : null}
                  </div>
                  <div className="ae-tool-controls">
                    {!server.builtin ? (
                      <label
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          gap: 6,
                          fontSize: 12.5,
                          cursor: "pointer",
                        }}
                      >
                        <input
                          type="checkbox"
                          checked={tool.owner_read_only}
                          disabled={toolBusy}
                          aria-label={tx(
                            `将工具 ${tool.name} 标记为只读`,
                            `Mark ${tool.name} as read-only`,
                          )}
                          onChange={event =>
                            onToggleToolReadOnly(tool, event.target.checked)
                          }
                        />
                        <span>{tx("标记为只读", "Owner read-only")}</span>
                      </label>
                    ) : null}
                    <SwitchButton
                      checked={tool.component_enabled}
                      disabled={toolBusy}
                      label={tx(`启用工具 ${tool.name}`, `Enable tool ${tool.name}`)}
                      onChange={next => onToggleTool(tool, next)}
                    />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </AccessibleModal>
  );
}

function McpServerFormModal({
  mode,
  initialServer,
  overviewRevision,
  onClose,
  onSaved,
  onRequestDelete,
  onConflictRefetch,
}: {
  mode: "create" | "edit";
  initialServer?: ExtensionMcpServer;
  overviewRevision: number;
  onClose: () => void;
  onSaved: (server: ExtensionMcpServer, runTestAfter: boolean) => Promise<void>;
  onRequestDelete?: () => void;
  onConflictRefetch: () => Promise<{
    overview: ExtensionOverview;
    server: ExtensionMcpServer | null;
  } | null>;
}) {
  const tx = useExtText();
  const [baselineServer, setBaselineServer] = useState<ExtensionMcpServer | null>(
    initialServer ?? null,
  );
  const [name, setName] = useState(initialServer?.name ?? "");
  const [description, setDescription] = useState(initialServer?.description ?? "");
  const [transport, setTransport] = useState<ExtensionMcpTransport>(
    initialServer?.transport ?? "stdio",
  );
  const [enabled, setEnabled] = useState<boolean>(initialServer?.component_enabled ?? false);
  const [command, setCommand] = useState(initialServer?.command ?? "");
  const [argsText, setArgsText] = useState(
    initialServer ? formatMcpArgsInput(initialServer.args) : "",
  );
  const [cwd, setCwd] = useState(initialServer?.cwd ?? "");
  const [url, setUrl] = useState(initialServer?.url ?? "");
  const [bearerTokenEnv, setBearerTokenEnv] = useState(initialServer?.bearer_token_env ?? "");
  const [startupTimeout, setStartupTimeout] = useState(
    String(initialServer?.startup_timeout_ms ?? 10000),
  );
  const [toolTimeout, setToolTimeout] = useState(
    String(initialServer?.tool_timeout_ms ?? 30000),
  );
  const [computerAccess, setComputerAccess] = useState(
    Boolean(initialServer?.computer_access ?? false),
  );

  const [envEntries, setEnvEntries] = useState<SecretEntryDraft[]>(() =>
    initialServer
      ? createInitialSecretEntries(initialServer.masked_env, initialServer.env_keys)
      : [],
  );
  const [envVarPassthrough, setEnvVarPassthrough] = useState<string[]>(() =>
    initialServer?.env_vars ? [...initialServer.env_vars] : [],
  );
  const [headerEntries, setHeaderEntries] = useState<SecretEntryDraft[]>(() =>
    initialServer
      ? createInitialSecretEntries(initialServer.masked_headers, initialServer.header_keys)
      : [],
  );
  const [envHttpHeaderEntries, setEnvHttpHeaderEntries] = useState<
    Array<{ id: string; key: string; value: string }>
  >(() =>
    initialServer?.env_http_headers
      ? Object.entries(initialServer.env_http_headers).map(([key, value], index) => ({
          id: `envhdr-${index}-${key}`,
          key,
          value,
        }))
      : [],
  );
  const [expectedRevision, setExpectedRevision] = useState<number>(
    mode === "edit" && initialServer ? initialServer.revision : overviewRevision,
  );
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState("");
  const [replacedOrDeleted, setReplacedOrDeleted] = useState(false);
  const [rebasedFieldNotes, setRebasedFieldNotes] = useState<string[]>([]);
  const [overlappingConflictFields, setOverlappingConflictFields] = useState<string[]>([]);

  const parsedArgsPreview = useMemo(() => parseMcpArgsInput(argsText), [argsText]);
  const argRows = parsedArgsPreview.ok ? parsedArgsPreview.args : [];

  const updateArgRows = (nextArgs: string[]) => {
    setArgsText(nextArgs.length === 0 ? "" : JSON.stringify(nextArgs));
  };

  const applyServerSnapshotToAllScalarFields = (srv: ExtensionMcpServer) => {
    setName(srv.name);
    setDescription(srv.description ?? "");
    setTransport(srv.transport);
    setEnabled(srv.component_enabled);
    setCommand(srv.command);
    setArgsText(formatMcpArgsInput(srv.args));
    setCwd(srv.cwd);
    setUrl(srv.url);
    setBearerTokenEnv(srv.bearer_token_env);
    setEnvVarPassthrough(srv.env_vars ? [...srv.env_vars] : []);
    setEnvHttpHeaderEntries(
      srv.env_http_headers
        ? Object.entries(srv.env_http_headers).map(([key, value], index) => ({
            id: `envhdr-${index}-${key}`,
            key,
            value,
          }))
        : [],
    );
    setStartupTimeout(String(srv.startup_timeout_ms));
    setToolTimeout(String(srv.tool_timeout_ms));
    setComputerAccess(Boolean(srv.computer_access));
  };

  const addSecretRow = (kind: "env" | "header") => {
    const row: SecretEntryDraft = {
      id: `new-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
      key: "",
      value: "",
      savedKey: null,
      isSavedMasked: false,
    };
    if (kind === "env") setEnvEntries(prev => [...prev, row]);
    else setHeaderEntries(prev => [...prev, row]);
  };

  const submitForm = async (runTestAfter: boolean) => {
    if (replacedOrDeleted || overlappingConflictFields.length > 0) {
      return;
    }
    setFormError("");
    const trimmedName = name.trim();
    if (!trimmedName) {
      setFormError(tx("请填写服务器名称。", "Server name is required."));
      return;
    }
    if (transport === "stdio" && !command.trim()) {
      setFormError(tx("STDIO 服务器必须填写启动命令。", "Command is required for STDIO servers."));
      return;
    }
    if (
      transport === "streamable_http" &&
      (!url.trim() || (!url.trim().startsWith("http://") && !url.trim().startsWith("https://")))
    ) {
      setFormError(
        tx(
          "Streamable HTTP 服务器必须填写 http:// 或 https:// 开头的 URL。",
          "A valid http:// or https:// URL is required for Streamable HTTP servers.",
        ),
      );
      return;
    }

    const parsedArgsResult = parseMcpArgsInput(argsText);
    if (!parsedArgsResult.ok) {
      setFormError(
        tx(
          '启动参数 JSON 数组格式无效，请填写字符串数组，例如：["--label", "  spaced  ", ""]。',
          'Invalid arguments JSON array. Provide a valid JSON array of strings, e.g. ["--label", "  spaced  ", ""].',
        ),
      );
      return;
    }
    const parsedArgs = parsedArgsResult.args;

    const activeSecretEntries = transport === "stdio" ? envEntries : headerEntries;
    const secretKind = transport === "stdio" ? "env" : "header";
    const secretErr = validateSecretEntries(activeSecretEntries, secretKind);
    if (secretErr) {
      if (secretErr.code === "duplicate_key") {
        setFormError(
          tx(`存在重复的键名：${secretErr.key}`, `Duplicate key name: ${secretErr.key}`),
        );
      } else if (secretErr.code === "renamed_masked_key") {
        setFormError(
          tx(
            `已保存的密钥「${secretErr.key}」在重命名时必须输入新值。`,
            `Renamed saved key "${secretErr.key}" requires entering a new secret value.`,
          ),
        );
      } else if (secretErr.code === "empty_secret_value") {
        setFormError(
          tx(
            `请为「${secretErr.key}」填写非空密钥值，或移除该行。`,
            `Enter a non-empty secret value for "${secretErr.key}" or remove the row.`,
          ),
        );
      } else {
        setFormError(
          tx(
            `键名格式无效：${secretErr.key || "(空)"}`,
            `Invalid key name: ${secretErr.key || "(empty)"}`,
          ),
        );
      }
      return;
    }

    const startupMs = Math.max(1000, Math.min(60000, Number(startupTimeout) || 10000));
    const toolMs = Math.max(1000, Math.min(120000, Number(toolTimeout) || 30000));
    const currentBase = baselineServer ?? initialServer ?? null;
    const resolvedCommand =
      mode === "edit" && currentBase && command === currentBase.command
        ? currentBase.command
        : command.trim();
    const resolvedCwd =
      mode === "edit" && currentBase && cwd === currentBase.cwd ? currentBase.cwd : cwd.trim();

    const normalizedEnvVars: string[] = [];
    if (transport === "stdio") {
      for (const rawVar of envVarPassthrough) {
        const trimmedVar = rawVar.trim();
        if (!trimmedVar) continue;
        if (!ENV_KEY_RE.test(trimmedVar)) {
          setFormError(
            tx(
              `传递环境变量名称格式无效：${trimmedVar}`,
              `Invalid passthrough environment variable name: ${trimmedVar}`,
            ),
          );
          return;
        }
        if (!normalizedEnvVars.includes(trimmedVar)) {
          normalizedEnvVars.push(trimmedVar);
        }
      }
    }

    const normalizedEnvHttpHeaders: Record<string, string> = {};
    if (transport === "streamable_http") {
      for (const row of envHttpHeaderEntries) {
        const hk = row.key.trim();
        const hv = row.value.trim();
        if (!hk && !hv) continue;
        if (!hk || !HEADER_KEY_RE.test(hk)) {
          setFormError(
            tx(
              `环境变量标头名称格式无效：${hk || "(空)"}`,
              `Invalid environment header name: ${hk || "(empty)"}`,
            ),
          );
          return;
        }
        if (!hv || !ENV_KEY_RE.test(hv)) {
          setFormError(
            tx(
              `标头「${hk}」对应的环境变量名称格式无效：${hv || "(空)"}`,
              `Invalid environment variable name for header "${hk}": ${hv || "(empty)"}`,
            ),
          );
          return;
        }
        normalizedEnvHttpHeaders[hk] = hv;
      }
    }

    setSubmitting(true);
    try {
      let savedServer: ExtensionMcpServer;
      if (mode === "create") {
        const envMap = transport === "stdio" ? buildNewSecretMap(envEntries) : undefined;
        const headerMap =
          transport === "streamable_http" ? buildNewSecretMap(headerEntries) : undefined;
        savedServer = await agentExtensions.createMcpServer({
          name: trimmedName,
          description: description.trim(),
          transport,
          enabled,
          ...(transport === "stdio"
            ? {
                command: resolvedCommand,
                args: parsedArgs,
                ...(resolvedCwd ? { cwd: resolvedCwd } : {}),
                ...(envMap ? { env: envMap } : {}),
                ...(normalizedEnvVars.length > 0 ? { env_vars: normalizedEnvVars } : {}),
              }
            : {
                url: url.trim(),
                ...(bearerTokenEnv.trim() ? { bearer_token_env: bearerTokenEnv.trim() } : {}),
                ...(headerMap ? { headers: headerMap } : {}),
                ...(Object.keys(normalizedEnvHttpHeaders).length > 0
                  ? { env_http_headers: normalizedEnvHttpHeaders }
                  : {}),
              }),
          startup_timeout_ms: startupMs,
          tool_timeout_ms: toolMs,
          computer_access: computerAccess,
          expected_revision: expectedRevision,
        });
      } else {
        const origEnvKeys = currentBase
          ? Object.keys(currentBase.masked_env).length > 0
            ? Object.keys(currentBase.masked_env)
            : currentBase.env_keys
          : [];
        const origHeaderKeys = currentBase
          ? Object.keys(currentBase.masked_headers).length > 0
            ? Object.keys(currentBase.masked_headers)
            : currentBase.header_keys
          : [];
        const envPatch =
          transport === "stdio" ? buildSecretPatch(envEntries, origEnvKeys) : undefined;
        const headerPatch =
          transport === "streamable_http"
            ? buildSecretPatch(headerEntries, origHeaderKeys)
            : undefined;
        savedServer = await agentExtensions.updateMcpServer(initialServer!.id, {
          name: trimmedName,
          description: description.trim(),
          transport,
          enabled,
          ...(transport === "stdio"
            ? {
                command: resolvedCommand,
                args: parsedArgs,
                cwd: resolvedCwd,
                ...(envPatch ? { env: envPatch } : {}),
                env_vars: normalizedEnvVars,
              }
            : {
                url: url.trim(),
                bearer_token_env: bearerTokenEnv.trim(),
                ...(headerPatch ? { headers: headerPatch } : {}),
                env_http_headers: normalizedEnvHttpHeaders,
              }),
          startup_timeout_ms: startupMs,
          tool_timeout_ms: toolMs,
          computer_access: computerAccess,
          expected_revision: expectedRevision,
        });
      }
      await onSaved(savedServer, runTestAfter);
    } catch (error) {
      if (isRevisionConflictError(error)) {
        const synced = await onConflictRefetch();
        if (!synced) {
          setFormError(describeError(error, tx));
          return;
        }
        if (mode === "create") {
          setExpectedRevision(synced.overview.revision);
          setFormError(describeError(error, tx));
          return;
        }
        const latest = synced.server;
        const base = baselineServer ?? initialServer;
        if (!latest || !base) {
          setReplacedOrDeleted(true);
          setFormError(
            tx(
              "该 MCP 服务器已在其他视图中被移除，无法继续保存此草稿。请关闭对话框。",
              "This MCP server was removed in another view and can no longer be saved. Close this dialog.",
            ),
          );
          return;
        }
        if (
          (base.instance_id || latest.instance_id) &&
          base.instance_id !== latest.instance_id
        ) {
          setReplacedOrDeleted(true);
          setFormError(
            tx(
              "同名 MCP 服务器已在其他视图中删除并重建（instance_id 已变更），旧草稿已中止以防止覆盖新服务器。请关闭并重新打开。",
              "This MCP server was deleted and recreated as a new instance in another view (instance_id changed). Saving is disabled to avoid overwriting the replacement server; close and reopen.",
            ),
          );
          return;
        }

        const rebasedNotes: string[] = [];
        const overlaps: string[] = [];

        const rebaseField = <T,>(
          label: string,
          currentVal: T,
          baseVal: T,
          latestVal: T,
          applyVal: (val: T) => void,
          formatVal: (val: T) => string = v => String(v),
        ) => {
          const userDirty = currentVal !== baseVal;
          const remoteChanged = latestVal !== baseVal;
          if (!userDirty) {
            applyVal(latestVal);
            if (remoteChanged) {
              rebasedNotes.push(`${label}: ${formatVal(latestVal)}`);
            }
          } else if (remoteChanged && currentVal !== latestVal) {
            overlaps.push(`${label} (${tx("远程最新值", "remote")}: ${formatVal(latestVal)})`);
          }
        };

        rebaseField(tx("服务器名称", "name"), name, base.name, latest.name, setName);
        rebaseField(
          tx("用途说明", "description"),
          description,
          base.description ?? "",
          latest.description ?? "",
          setDescription,
        );
        rebaseField(
          tx("传输类型", "transport"),
          transport,
          base.transport,
          latest.transport,
          setTransport,
        );
        rebaseField(
          tx("启用状态", "enabled"),
          enabled,
          base.component_enabled,
          latest.component_enabled,
          setEnabled,
        );
        rebaseField(tx("命令", "command"), command, base.command, latest.command, setCommand);
        rebaseField(
          tx("启动参数", "args"),
          argsText,
          formatMcpArgsInput(base.args),
          formatMcpArgsInput(latest.args),
          setArgsText,
        );
        rebaseField(tx("工作目录", "cwd"), cwd, base.cwd, latest.cwd, setCwd);
        rebaseField("URL", url, base.url, latest.url, setUrl);
        rebaseField(
          "bearer_token_env",
          bearerTokenEnv,
          base.bearer_token_env,
          latest.bearer_token_env,
          setBearerTokenEnv,
        );
        rebaseField(
          "startup_timeout_ms",
          startupTimeout,
          String(base.startup_timeout_ms),
          String(latest.startup_timeout_ms),
          setStartupTimeout,
        );
        rebaseField(
          "tool_timeout_ms",
          toolTimeout,
          String(base.tool_timeout_ms),
          String(latest.tool_timeout_ms),
          setToolTimeout,
        );
        rebaseField(
          "computer_access",
          computerAccess,
          Boolean(base.computer_access),
          Boolean(latest.computer_access),
          setComputerAccess,
        );

        const baseEnvKeys =
          Object.keys(base.masked_env).length > 0 ? Object.keys(base.masked_env) : base.env_keys;
        const baseHeaderKeys =
          Object.keys(base.masked_headers).length > 0
            ? Object.keys(base.masked_headers)
            : base.header_keys;
        setEnvEntries(prev =>
          reconcileSecretEntriesOnConflict(prev, baseEnvKeys, latest.masked_env, latest.env_keys),
        );
        setHeaderEntries(prev =>
          reconcileSecretEntriesOnConflict(
            prev,
            baseHeaderKeys,
            latest.masked_headers,
            latest.header_keys,
          ),
        );

        setBaselineServer(latest);
        setExpectedRevision(latest.revision);
        setRebasedFieldNotes(rebasedNotes);
        setOverlappingConflictFields(overlaps);
      }
      setFormError(describeError(error, tx));
    } finally {
      setSubmitting(false);
    }
  };

  const saveDisabled =
    submitting || replacedOrDeleted || overlappingConflictFields.length > 0;

  return (
    <AccessibleModal
      title={
        mode === "create"
          ? tx("添加 MCP 服务器", "Add MCP server")
          : tx(
              `编辑服务器 ${baselineServer?.name ?? initialServer?.name ?? ""}`,
              `Edit server ${baselineServer?.name ?? initialServer?.name ?? ""}`,
            )
      }
      subtitle={tx(
        "配置本机 STDIO 进程或 Streamable HTTP 远端 MCP 服务器。已保存的环境变量与请求头密钥默认掩码保护。",
        "Configure a local STDIO process or a Streamable HTTP MCP server. Saved environment variables and headers remain masked.",
      )}
      onClose={onClose}
      busy={submitting}
      footer={
        <div className="ae-dialog-foot-right">
          <button type="button" className="ae-btn" disabled={submitting} onClick={onClose}>
            {tx("取消", "Cancel")}
          </button>
          <button
            type="button"
            className="ae-btn"
            disabled={saveDisabled}
            onClick={() => void submitForm(true)}
          >
            <Play size={14} />
            <span>{tx("保存并测试连接", "Save & test connection")}</span>
          </button>
          <button
            type="button"
            className="ae-btn ae-btn-primary"
            disabled={saveDisabled}
            onClick={() => void submitForm(false)}
          >
            <span>
              {submitting ? tx("正在保存…", "Saving…") : tx("保存服务器", "Save server")}
            </span>
          </button>
        </div>
      }
    >
      {formError ? (
        <div className="ae-banner ae-banner-error" role="alert">
          <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <span>{formError}</span>
            {rebasedFieldNotes.length > 0 ? (
              <small>
                {tx("已自动同步未修改字段：", "Synced untouched fields from latest server: ")}{" "}
                {rebasedFieldNotes.join(" · ")}
              </small>
            ) : null}
            {overlappingConflictFields.length > 0 ? (
              <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 4 }}>
                <small>
                  {tx(
                    "以下字段同时被你和其他视图修改，请确认处理方式：",
                    "The following fields were modified both in your draft and in another view:",
                  )}{" "}
                  {overlappingConflictFields.join(" · ")}
                </small>
                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <button
                    type="button"
                    className="ae-btn ae-btn-sm"
                    onClick={() => setOverlappingConflictFields([])}
                  >
                    {tx("保留我的草稿值", "Keep my draft values")}
                  </button>
                  {baselineServer ? (
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm"
                      onClick={() => {
                        applyServerSnapshotToAllScalarFields(baselineServer);
                        setOverlappingConflictFields([]);
                      }}
                    >
                      {tx("加载远程最新字段值", "Load latest server values")}
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <div className="ae-codex-detail-banner">
        <div>
          <strong>
            {mode === "create"
              ? tx("连接至自定义 MCP", "Connect to a custom MCP")
              : tx(
                  `更新 ${baselineServer?.name ?? initialServer?.name ?? ""} MCP`,
                  `Update ${baselineServer?.name ?? initialServer?.name ?? ""} MCP`,
                )}
          </strong>
          {mode === "create" ? (
            <a
              className="ae-codex-docs-link"
              href="https://developers.openai.com/codex/mcp"
              target="_blank"
              rel="noreferrer"
            >
              <span>{tx("文档", "Docs")}</span>
              <ExternalLink size={12} />
            </a>
          ) : null}
        </div>
        {mode === "edit" && onRequestDelete ? (
          <button
            type="button"
            className="ae-btn ae-btn-sm ae-btn-danger"
            disabled={submitting}
            onClick={onRequestDelete}
          >
            <Trash2 size={13} />
            <span>{tx("卸载", "Uninstall")}</span>
          </button>
        ) : null}
      </div>

      {mode === "edit" ? (
        <div className="ae-codex-switch-notice">
          {tx(
            "如需切换 MCP 服务器类型，请先卸载当前配置。",
            "If you would like to switch MCP server type, please uninstall first.",
          )}
        </div>
      ) : null}

      <div className="ae-form-grid">
        <div className="ae-field">
          <label htmlFor="ae-mcp-name">{tx("服务器名称", "Server name")}</label>
          <input
            id="ae-mcp-name"
            value={name}
            disabled={submitting || replacedOrDeleted}
            placeholder={tx("例如：Filesystem MCP", "e.g. Filesystem MCP")}
            onChange={event => setName(event.target.value)}
          />
        </div>
        <div className="ae-field">
          <label htmlFor="ae-mcp-transport">{tx("传输类型", "Transport")}</label>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <select
              id="ae-mcp-transport"
              value={transport}
              disabled={submitting || replacedOrDeleted}
              onChange={event => setTransport(event.target.value as ExtensionMcpTransport)}
            >
              <option value="stdio">{tx("STDIO", "STDIO")}</option>
              <option value="streamable_http">{tx("Streamable HTTP", "Streamable HTTP")}</option>
            </select>
            <div className="ae-segmented" role="group" aria-label={tx("类型切换", "Transport type toggle")}>
              <button
                type="button"
                aria-pressed={transport === "stdio"}
                disabled={submitting || replacedOrDeleted}
                onClick={() => setTransport("stdio")}
              >
                {tx("STDIO", "STDIO")}
              </button>
              <button
                type="button"
                aria-pressed={transport === "streamable_http"}
                disabled={submitting || replacedOrDeleted}
                onClick={() => setTransport("streamable_http")}
              >
                {tx("流式 HTTP", "Streamable HTTP")}
              </button>
            </div>
          </div>
        </div>
      </div>

      <div className="ae-field">
        <label htmlFor="ae-mcp-desc">{tx("用途说明（可选）", "Description (optional)")}</label>
        <input
          id="ae-mcp-desc"
          value={description}
          disabled={submitting || replacedOrDeleted}
          placeholder={tx("简述此服务器提供的工具用途", "Brief description of the server's tools")}
          onChange={event => setDescription(event.target.value)}
        />
      </div>

      <div className="ae-setting-row">
        <div className="ae-setting-copy">
          <strong>{tx("启用此服务器", "Enable server")}</strong>
          <small>
            {tx(
              "启用后，经过连接验证的工具可加入 Ryn Agent 的模型工具循环。",
              "When enabled, verified tools from this server can participate in Ryn Agent's model-tool loop.",
            )}
          </small>
        </div>
        <SwitchButton
          checked={enabled}
          disabled={submitting || replacedOrDeleted}
          label={tx("启用服务器", "Enable server")}
          onChange={setEnabled}
        />
      </div>

      {transport === "stdio" ? (
        <>
          <div className="ae-form-grid">
            <div className="ae-field">
              <label htmlFor="ae-mcp-command">{tx("命令 (Command)", "Command")}</label>
              <input
                id="ae-mcp-command"
                value={command}
                disabled={submitting || replacedOrDeleted}
                placeholder={tx("例如：npx 或 python", "e.g. npx or python")}
                onChange={event => setCommand(event.target.value)}
              />
            </div>
            <div className="ae-field">
              <label htmlFor="ae-mcp-cwd">
                {tx("工作目录（可选）", "Working directory (optional)")}
              </label>
              <input
                id="ae-mcp-cwd"
                value={cwd}
                disabled={submitting || replacedOrDeleted}
                placeholder={tx("例如：D:\\Tools\\mcp", "e.g. D:\\Tools\\mcp")}
                onChange={event => setCwd(event.target.value)}
              />
            </div>
          </div>

          <div className="ae-section-card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h3>{tx("参数 (Arguments)", "Arguments")}</h3>
              <button
                type="button"
                className="ae-btn ae-btn-sm ae-codex-dashed-btn"
                disabled={submitting || replacedOrDeleted || !parsedArgsPreview.ok}
                onClick={() => updateArgRows([...argRows, ""])}
              >
                <Plus size={13} />
                <span>{tx("添加参数", "Add argument")}</span>
              </button>
            </div>
            {argRows.length > 0 ? (
              <div className="ae-secret-list">
                {argRows.map((argVal, idx) => (
                  <div key={`arg-${idx}`} className="ae-codex-single-row">
                    <input
                      aria-label={tx(`参数 ${idx + 1}`, `Argument ${idx + 1}`)}
                      placeholder={tx(`参数 ${idx + 1}`, `Argument ${idx + 1}`)}
                      value={argVal}
                      disabled={submitting || replacedOrDeleted}
                      onChange={event => {
                        const next = [...argRows];
                        next[idx] = event.target.value;
                        updateArgRows(next);
                      }}
                    />
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm ae-btn-danger"
                      aria-label={tx(`移除参数 ${idx + 1}`, `Remove argument ${idx + 1}`)}
                      disabled={submitting || replacedOrDeleted}
                      onClick={() => updateArgRows(argRows.filter((_, i) => i !== idx))}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
              </div>
            ) : null}
            <div className="ae-field" style={{ marginTop: 6 }}>
              <label htmlFor="ae-mcp-args">
                {tx("启动参数（JSON 字符串数组）", "Arguments (JSON string array)")}
              </label>
              <textarea
                id="ae-mcp-args"
                value={argsText}
                disabled={submitting || replacedOrDeleted}
                placeholder={'["-y", "@modelcontextprotocol/server-filesystem"]'}
                onChange={event => setArgsText(event.target.value)}
              />
              <small>
                {tx(
                  '使用 JSON 字符串数组以原样保留首尾空格与空字符串参数，例如：["--label", "  spaced  ", ""]',
                  'Use a JSON string array to preserve exact whitespace and empty arguments, e.g. ["--label", "  spaced  ", ""]',
                )}
              </small>
            </div>
          </div>

          <div className="ae-section-card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h3>{tx("环境变量 (Environment variables)", "Environment variables")}</h3>
              <button
                type="button"
                className="ae-btn ae-btn-sm"
                disabled={submitting || replacedOrDeleted}
                onClick={() => addSecretRow("env")}
              >
                <Plus size={13} />
                <span>{tx("添加环境变量", "Add variable")}</span>
              </button>
            </div>
            {envEntries.length === 0 ? (
              <small style={{ color: "var(--pf-muted)" }}>
                {tx("未配置环境变量。", "No environment variables configured.")}
              </small>
            ) : (
              <div className="ae-secret-list">
                {envEntries.map((entry, idx) => {
                  const indexLabel = idx + 1;
                  return (
                    <div key={entry.id} className="ae-secret-row">
                      <input
                        aria-label={tx(
                          `环境变量 ${indexLabel} 名称`,
                          `Environment variable ${indexLabel} name`,
                        )}
                        placeholder={tx("变量名，如 API_KEY", "Name, e.g. API_KEY")}
                        value={entry.key}
                        disabled={submitting || replacedOrDeleted || entry.isSavedMasked}
                        onChange={event =>
                          setEnvEntries(prev =>
                            prev.map(row =>
                              row.id === entry.id ? { ...row, key: event.target.value } : row,
                            ),
                          )
                        }
                      />
                      {entry.isSavedMasked ? (
                        <div className="ae-secret-masked-box">
                          <span>
                            {tx("已保存密钥", "Saved secret")} ({MASKED_SECRET_PLACEHOLDER})
                          </span>
                          <button
                            type="button"
                            className="ae-btn ae-btn-sm"
                            disabled={submitting || replacedOrDeleted}
                            onClick={() =>
                              setEnvEntries(prev =>
                                prev.map(row =>
                                  row.id === entry.id
                                    ? { ...row, isSavedMasked: false, value: "" }
                                    : row,
                                ),
                              )
                            }
                          >
                            {tx("替换值", "Replace value")}
                          </button>
                        </div>
                      ) : (
                        <input
                          type="password"
                          aria-label={tx(
                            `环境变量 ${indexLabel} 值`,
                            `Environment variable ${indexLabel} value`,
                          )}
                          placeholder={tx("输入密钥值", "Enter secret value")}
                          value={entry.value}
                          disabled={submitting || replacedOrDeleted}
                          onChange={event =>
                            setEnvEntries(prev =>
                              prev.map(row =>
                                row.id === entry.id ? { ...row, value: event.target.value } : row,
                              ),
                            )
                          }
                        />
                      )}
                      <button
                        type="button"
                        className="ae-btn ae-btn-sm ae-btn-danger"
                        aria-label={tx(
                          `移除环境变量 ${entry.key || indexLabel}`,
                          `Remove environment variable ${entry.key || indexLabel}`,
                        )}
                        disabled={submitting || replacedOrDeleted}
                        onClick={() =>
                          setEnvEntries(prev => prev.filter(row => row.id !== entry.id))
                        }
                      >
                        <Trash2 size={13} />
                        <span>{tx("移除", "Remove")}</span>
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
          <div className="ae-section-card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h3>
                {tx(
                  "环境变量传递 (Environment variable passthrough)",
                  "Environment variable passthrough",
                )}
              </h3>
              <button
                type="button"
                className="ae-btn ae-btn-sm ae-codex-dashed-btn"
                disabled={submitting || replacedOrDeleted}
                onClick={() => setEnvVarPassthrough(prev => [...prev, ""])}
              >
                <Plus size={13} />
                <span>{tx("添加传递变量", "Add passthrough variable")}</span>
              </button>
            </div>
            {envVarPassthrough.length === 0 ? (
              <small style={{ color: "var(--pf-muted)" }}>
                {tx(
                  "可选：指定直接从本机环境传递给 STDIO 进程的环境变量名（如 OPENAI_API_KEY）。",
                  "Optional: list host environment variable names to pass through to the STDIO process (e.g. OPENAI_API_KEY).",
                )}
              </small>
            ) : (
              <div className="ae-secret-list">
                {envVarPassthrough.map((varName, idx) => (
                  <div key={`envpassthrough-${idx}`} className="ae-codex-single-row">
                    <input
                      aria-label={tx(
                        `传递环境变量 ${idx + 1}`,
                        `Passthrough environment variable ${idx + 1}`,
                      )}
                      placeholder={tx("ENV_VAR_NAME", "ENV_VAR_NAME")}
                      value={varName}
                      disabled={submitting || replacedOrDeleted}
                      onChange={event =>
                        setEnvVarPassthrough(prev =>
                          prev.map((item, i) => (i === idx ? event.target.value : item)),
                        )
                      }
                    />
                    <button
                      type="button"
                      className="ae-btn ae-btn-sm ae-btn-danger"
                      aria-label={tx(
                        `移除传递环境变量 ${varName || idx + 1}`,
                        `Remove passthrough environment variable ${varName || idx + 1}`,
                      )}
                      disabled={submitting || replacedOrDeleted}
                      onClick={() =>
                        setEnvVarPassthrough(prev => prev.filter((_, i) => i !== idx))
                      }
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      ) : (
        <>
          <div className="ae-form-grid">
            <div className="ae-field">
              <label htmlFor="ae-mcp-url">{tx("服务器 URL", "Server URL")}</label>
              <input
                id="ae-mcp-url"
                value={url}
                disabled={submitting || replacedOrDeleted}
                placeholder={tx("https://example.com/mcp", "https://example.com/mcp")}
                onChange={event => setUrl(event.target.value)}
              />
            </div>
            <div className="ae-field">
              <label htmlFor="ae-mcp-bearer-env">
                {tx(
                  "Bearer Token 环境变量名（可选）",
                  "Bearer token environment variable (optional)",
                )}
              </label>
              <input
                id="ae-mcp-bearer-env"
                value={bearerTokenEnv}
                disabled={submitting || replacedOrDeleted}
                placeholder={tx("MCP_BEARER_TOKEN", "MCP_BEARER_TOKEN")}
                onChange={event => setBearerTokenEnv(event.target.value)}
              />
            </div>
          </div>

          <div className="ae-section-card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h3>{tx("HTTP 请求头 (Headers)", "HTTP headers")}</h3>
              <button
                type="button"
                className="ae-btn ae-btn-sm"
                disabled={submitting || replacedOrDeleted}
                onClick={() => addSecretRow("header")}
              >
                <Plus size={13} />
                <span>{tx("添加请求头", "Add header")}</span>
              </button>
            </div>
            {headerEntries.length === 0 ? (
              <small style={{ color: "var(--pf-muted)" }}>
                {tx("未配置自定义请求头。", "No custom HTTP headers configured.")}
              </small>
            ) : (
              <div className="ae-secret-list">
                {headerEntries.map((entry, idx) => {
                  const indexLabel = idx + 1;
                  return (
                    <div key={entry.id} className="ae-secret-row">
                      <input
                        aria-label={tx(`请求头 ${indexLabel} 名称`, `Header ${indexLabel} name`)}
                        placeholder={tx("请求头名称，如 Authorization", "Header name, e.g. Authorization")}
                        value={entry.key}
                        disabled={submitting || replacedOrDeleted || entry.isSavedMasked}
                        onChange={event =>
                          setHeaderEntries(prev =>
                            prev.map(row =>
                              row.id === entry.id ? { ...row, key: event.target.value } : row,
                            ),
                          )
                        }
                      />
                      {entry.isSavedMasked ? (
                        <div className="ae-secret-masked-box">
                          <span>
                            {tx("已保存密钥", "Saved secret")} ({MASKED_SECRET_PLACEHOLDER})
                          </span>
                          <button
                            type="button"
                            className="ae-btn ae-btn-sm"
                            disabled={submitting || replacedOrDeleted}
                            onClick={() =>
                              setHeaderEntries(prev =>
                                prev.map(row =>
                                  row.id === entry.id
                                    ? { ...row, isSavedMasked: false, value: "" }
                                    : row,
                                ),
                              )
                            }
                          >
                            {tx("替换值", "Replace value")}
                          </button>
                        </div>
                      ) : (
                        <input
                          type="password"
                          aria-label={tx(`请求头 ${indexLabel} 值`, `Header ${indexLabel} value`)}
                          placeholder={tx("输入请求头值", "Enter header value")}
                          value={entry.value}
                          disabled={submitting || replacedOrDeleted}
                          onChange={event =>
                            setHeaderEntries(prev =>
                              prev.map(row =>
                                row.id === entry.id ? { ...row, value: event.target.value } : row,
                              ),
                            )
                          }
                        />
                      )}
                      <button
                        type="button"
                        className="ae-btn ae-btn-sm ae-btn-danger"
                        aria-label={tx(
                          `移除请求头 ${entry.key || indexLabel}`,
                          `Remove header ${entry.key || indexLabel}`,
                        )}
                        disabled={submitting || replacedOrDeleted}
                        onClick={() =>
                          setHeaderEntries(prev => prev.filter(row => row.id !== entry.id))
                        }
                      >
                        <Trash2 size={13} />
                        <span>{tx("移除", "Remove")}</span>
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>

          <div className="ae-section-card">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
              <h3>
                {tx(
                  "来自环境变量的标头 (Headers from environment variables)",
                  "Headers from environment variables",
                )}
              </h3>
              <button
                type="button"
                className="ae-btn ae-btn-sm ae-codex-dashed-btn"
                disabled={submitting || replacedOrDeleted}
                onClick={() =>
                  setEnvHttpHeaderEntries(prev => [
                    ...prev,
                    {
                      id: `envhdr-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
                      key: "",
                      value: "",
                    },
                  ])
                }
              >
                <Plus size={13} />
                <span>{tx("添加环境变量标头", "Add environment header")}</span>
              </button>
            </div>
            {envHttpHeaderEntries.length === 0 ? (
              <small style={{ color: "var(--pf-muted)" }}>
                {tx(
                  "可选：从本机环境变量读取 HTTP 标头值（键为标头名，值为环境变量名）。",
                  "Optional: source HTTP headers from host environment variables (Key = header name, Value = env var name).",
                )}
              </small>
            ) : (
              <div className="ae-secret-list">
                {envHttpHeaderEntries.map((row, idx) => {
                  const indexLabel = idx + 1;
                  return (
                    <div key={row.id} className="ae-secret-row">
                      <input
                        aria-label={tx(
                          `环境变量请求头 ${indexLabel} 名称`,
                          `Environment header ${indexLabel} name`,
                        )}
                        placeholder={tx("键 (如 X-Org-Token)", "Key (e.g. X-Org-Token)")}
                        value={row.key}
                        disabled={submitting || replacedOrDeleted}
                        onChange={event =>
                          setEnvHttpHeaderEntries(prev =>
                            prev.map(item =>
                              item.id === row.id ? { ...item, key: event.target.value } : item,
                            ),
                          )
                        }
                      />
                      <input
                        aria-label={tx(
                          `环境变量请求头 ${indexLabel} 变量名`,
                          `Environment header ${indexLabel} variable`,
                        )}
                        placeholder={tx("值 (ENV_VAR_NAME)", "Value (ENV_VAR_NAME)")}
                        value={row.value}
                        disabled={submitting || replacedOrDeleted}
                        onChange={event =>
                          setEnvHttpHeaderEntries(prev =>
                            prev.map(item =>
                              item.id === row.id ? { ...item, value: event.target.value } : item,
                            ),
                          )
                        }
                      />
                      <button
                        type="button"
                        className="ae-btn ae-btn-sm ae-btn-danger"
                        aria-label={tx(
                          `移除环境变量请求头 ${row.key || indexLabel}`,
                          `Remove environment header ${row.key || indexLabel}`,
                        )}
                        disabled={submitting || replacedOrDeleted}
                        onClick={() =>
                          setEnvHttpHeaderEntries(prev => prev.filter(item => item.id !== row.id))
                        }
                      >
                        <Trash2 size={13} />
                        <span>{tx("移除", "Remove")}</span>
                      </button>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        </>
      )}

      <details className="ae-section-card">
        <summary style={{ cursor: "pointer", fontWeight: 600, fontSize: 13.5 }}>
          {tx("高级设置（超时与桌面权限要求）", "Advanced settings (timeouts & computer access)")}
        </summary>
        <div className="ae-form-grid" style={{ marginTop: 10 }}>
          <div className="ae-field">
            <label htmlFor="ae-mcp-startup-timeout">
              {tx("启动超时 (ms)", "Startup timeout (ms)")}
            </label>
            <input
              id="ae-mcp-startup-timeout"
              type="number"
              min={1000}
              max={60000}
              value={startupTimeout}
              disabled={submitting || replacedOrDeleted}
              onChange={event => setStartupTimeout(event.target.value)}
            />
          </div>
          <div className="ae-field">
            <label htmlFor="ae-mcp-tool-timeout">
              {tx("工具调用超时 (ms)", "Tool call timeout (ms)")}
            </label>
            <input
              id="ae-mcp-tool-timeout"
              type="number"
              min={1000}
              max={120000}
              value={toolTimeout}
              disabled={submitting || replacedOrDeleted}
              onChange={event => setToolTimeout(event.target.value)}
            />
          </div>
        </div>
        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginTop: 8,
            fontSize: 13,
            cursor: "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={computerAccess}
            disabled={submitting || replacedOrDeleted}
            onChange={event => setComputerAccess(event.target.checked)}
          />
          <span>
            {tx(
              "此服务器需要桌面控制权限 (desktop_access)",
              "This server requires computer/desktop access permission (desktop_access)",
            )}
          </span>
        </label>
      </details>
    </AccessibleModal>
  );
}

function ExtensionImportModal({
  initialKind,
  onClose,
  onCommitted,
  onConflictRefetch,
}: {
  initialKind: ExtensionImportKind;
  onClose: () => void;
  onCommitted: (overview: ExtensionOverview) => void;
  onConflictRefetch: () => Promise<number | null>;
}) {
  const tx = useExtText();
  const [kind, setKind] = useState<ExtensionImportKind>(initialKind);
  const [sourceMode, setSourceMode] = useState<"path" | "zip" | "skill_text" | "config_text">(
    initialKind === "mcp" ? "config_text" : "path",
  );
  const [localPath, setLocalPath] = useState("");
  const [skillText, setSkillText] = useState("");
  const [configText, setConfigText] = useState("");
  const [filename, setFilename] = useState(
    initialKind === "mcp" ? ".mcp.json" : initialKind === "skill" ? "SKILL.md" : "plugin.zip",
  );
  const [zipBase64, setZipBase64] = useState("");
  const [fileReading, setFileReading] = useState(false);
  const [preview, setPreview] = useState<ExtensionImportPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const fileReadGenRef = useRef(0);
  const activeReaderRef = useRef<FileReader | null>(null);

  const cancelActiveFileRead = useCallback(() => {
    fileReadGenRef.current += 1;
    if (activeReaderRef.current) {
      try {
        activeReaderRef.current.abort();
      } catch {
        // Ignore abort errors
      }
      activeReaderRef.current = null;
    }
    setFileReading(false);
  }, []);

  useEffect(() => {
    return () => {
      fileReadGenRef.current += 1;
      try {
        activeReaderRef.current?.abort();
      } catch {
        // Ignore abort errors
      }
      activeReaderRef.current = null;
    };
  }, []);

  const handleKindChange = (nextKind: ExtensionImportKind) => {
    cancelActiveFileRead();
    setZipBase64("");
    setKind(nextKind);
    setPreview(null);
    setError("");
    if (nextKind === "plugin" && (sourceMode === "skill_text" || sourceMode === "config_text")) {
      setSourceMode("path");
      setFilename("plugin.zip");
    } else if (nextKind === "skill" && sourceMode === "config_text") {
      setSourceMode("skill_text");
      setFilename("SKILL.md");
    } else if (nextKind === "mcp" && sourceMode === "skill_text") {
      setSourceMode("config_text");
      setFilename(".mcp.json");
    }
  };

  const handleFileUpload = (file: File | undefined) => {
    cancelActiveFileRead();
    setPreview(null);
    setError("");
    setZipBase64("");
    if (!file) {
      return;
    }
    const gen = ++fileReadGenRef.current;
    const targetFilename = file.name;
    setFilename(targetFilename);
    setFileReading(true);

    const reader = new FileReader();
    activeReaderRef.current = reader;

    reader.onerror = () => {
      if (gen !== fileReadGenRef.current) return;
      activeReaderRef.current = null;
      setFileReading(false);
      setZipBase64("");
      setError(
        tx(
          `读取文件「${targetFilename}」失败，请重新选择文件。`,
          `Failed to read file "${targetFilename}". Please select the file again.`,
        ),
      );
    };

    reader.onabort = () => {
      if (gen !== fileReadGenRef.current) return;
      activeReaderRef.current = null;
      setFileReading(false);
    };

    if (sourceMode === "zip" || targetFilename.toLowerCase().endsWith(".zip")) {
      reader.onload = () => {
        if (gen !== fileReadGenRef.current) return;
        activeReaderRef.current = null;
        const result = typeof reader.result === "string" ? reader.result : "";
        const b64 = result.includes(",") ? result.split(",", 2)[1] : result;
        setFilename(targetFilename);
        setZipBase64(b64);
        setFileReading(false);
      };
      reader.readAsDataURL(file);
    } else {
      reader.onload = () => {
        if (gen !== fileReadGenRef.current) return;
        activeReaderRef.current = null;
        const text = typeof reader.result === "string" ? reader.result : "";
        setFilename(targetFilename);
        if (kind === "skill") setSkillText(text);
        else if (kind === "mcp") setConfigText(text);
        setFileReading(false);
      };
      reader.readAsText(file);
    }
  };

  const handlePreview = async () => {
    if (fileReading) return;
    setError("");
    setPreview(null);
    const body: ExtensionImportPreviewRequest = { kind };

    if (sourceMode === "path") {
      if (!localPath.trim()) {
        setError(tx("请输入本地文件夹或文件路径。", "Enter a local folder or file path."));
        return;
      }
      body.path = localPath.trim();
    } else if (sourceMode === "zip") {
      if (!zipBase64.trim()) {
        setError(tx("请选择要上传的 ZIP 压缩包。", "Choose a ZIP archive to upload."));
        return;
      }
      body.zip_base64 = zipBase64.trim();
      if (filename.trim()) body.filename = filename.trim();
    } else if (sourceMode === "skill_text") {
      if (!skillText.trim()) {
        setError(tx("请输入或粘贴 SKILL.md 内容。", "Enter or paste SKILL.md content."));
        return;
      }
      body.skill_text = skillText;
      body.filename = filename.trim() || "SKILL.md";
    } else if (sourceMode === "config_text") {
      if (!configText.trim()) {
        setError(
          tx(
            "请输入或粘贴 JSON (.mcp.json) 或 TOML (config.toml) 配置内容。",
            "Enter or paste JSON (.mcp.json) or TOML (config.toml) configuration text.",
          ),
        );
      }
      if (!configText.trim()) return;
      body.config_text = configText;
      body.filename = filename.trim() || ".mcp.json";
    }

    setBusy(true);
    try {
      const result = await agentExtensions.previewImport(body);
      setPreview(result);
    } catch (err) {
      setError(describeError(err, tx));
    } finally {
      setBusy(false);
    }
  };

  const handleCommit = async () => {
    if (!preview || !preview.can_import || fileReading) return;
    setBusy(true);
    setError("");
    try {
      const nextOverview = await agentExtensions.commitImport({
        preview_id: preview.preview_id,
        expected_revision: preview.expected_revision,
      });
      onCommitted(nextOverview);
    } catch (err) {
      if (isRevisionConflictError(err)) {
        const latestRev = await onConflictRefetch();
        if (latestRev !== null) {
          setPreview(prev => (prev ? { ...prev, expected_revision: latestRev } : prev));
        }
      }
      setError(describeError(err, tx));
    } finally {
      setBusy(false);
    }
  };

  const previewDisabled =
    busy || fileReading || (sourceMode === "zip" && !zipBase64.trim());

  return (
    <AccessibleModal
      title={
        kind === "plugin"
          ? tx("导入插件包", "Import plugin package")
          : kind === "skill"
            ? tx("导入技能 (Skill)", "Import skill")
            : tx("导入 MCP 服务器配置", "Import MCP server configuration")
      }
      subtitle={tx(
        "导入前将先进行安全预览与兼容性检查，不会执行包内脚本。导入后的项目默认处于停用状态。",
        "Packages are validated and previewed before import without executing scripts. Imported entries start disabled by default.",
      )}
      onClose={onClose}
      busy={busy}
      footer={
        <div className="ae-dialog-foot-right">
          <button type="button" className="ae-btn" disabled={busy} onClick={onClose}>
            {tx("取消", "Cancel")}
          </button>
          <button
            type="button"
            className="ae-btn"
            disabled={previewDisabled}
            onClick={() => void handlePreview()}
          >
            <Search size={14} />
            <span>
              {fileReading
                ? tx("正在读取文件…", "Reading file…")
                : busy && !preview
                  ? tx("正在检查…", "Inspecting…")
                  : tx("预览导入", "Preview import")}
            </span>
          </button>
          {preview ? (
            <button
              type="button"
              className="ae-btn ae-btn-primary"
              disabled={busy || fileReading || !preview.can_import}
              onClick={() => void handleCommit()}
            >
              <CheckCircle2 size={14} />
              <span>{busy ? tx("正在导入…", "Importing…") : tx("确认导入", "Commit import")}</span>
            </button>
          ) : null}
        </div>
      }
    >
      {error ? (
        <div className="ae-banner ae-banner-error" role="alert">
          <span>{error}</span>
        </div>
      ) : null}

      <div className="ae-form-grid">
        <div className="ae-field">
          <label htmlFor="ae-import-kind">{tx("导入类型", "Import type")}</label>
          <select
            id="ae-import-kind"
            value={kind}
            disabled={busy}
            onChange={event => handleKindChange(event.target.value as ExtensionImportKind)}
          >
            <option value="plugin">{tx("插件包 (Ryn / Codex / Claude)", "Plugin package (Ryn / Codex / Claude)")}</option>
            <option value="skill">{tx("技能 (SKILL.md / 文件夹 / ZIP)", "Skill (SKILL.md / folder / ZIP)")}</option>
            <option value="mcp">{tx("MCP 配置 (.mcp.json / config.toml)", "MCP config (.mcp.json / config.toml)")}</option>
          </select>
        </div>

        <div className="ae-field">
          <label htmlFor="ae-import-source">{tx("来源方式", "Source method")}</label>
          <select
            id="ae-import-source"
            value={sourceMode}
            disabled={busy}
            onChange={event => {
              cancelActiveFileRead();
              setZipBase64("");
              setSourceMode(event.target.value as typeof sourceMode);
              setPreview(null);
              setError("");
            }}
          >
            <option value="path">{tx("本地绝对路径（文件夹或文件）", "Local path (folder or file)")}</option>
            <option value="zip">{tx("上传 ZIP 压缩包", "Upload ZIP archive")}</option>
            {kind === "skill" ? (
              <option value="skill_text">{tx("粘贴或上传独立 SKILL.md", "Paste or upload standalone SKILL.md")}</option>
            ) : null}
            {kind === "mcp" ? (
              <option value="config_text">{tx("粘贴 JSON / TOML 配置文本", "Paste JSON / TOML configuration text")}</option>
            ) : null}
          </select>
        </div>
      </div>

      {sourceMode === "path" ? (
        <div className="ae-field">
          <label htmlFor="ae-import-path">{tx("本地路径", "Local path")}</label>
          <input
            id="ae-import-path"
            value={localPath}
            disabled={busy}
            placeholder={
              kind === "plugin"
                ? "D:\\plugins\\my-codex-plugin"
                : kind === "skill"
                  ? "D:\\skills\\my-skill\\SKILL.md"
                  : "D:\\project\\.mcp.json"
            }
            onChange={event => {
              setLocalPath(event.target.value);
              setPreview(null);
            }}
          />
        </div>
      ) : null}

      {sourceMode === "zip" ? (
        <div className="ae-field">
          <label htmlFor="ae-import-zip">{tx("选择 ZIP 文件", "Choose ZIP file")}</label>
          <input
            id="ae-import-zip"
            type="file"
            accept=".zip,application/zip"
            disabled={busy}
            onChange={event => handleFileUpload(event.target.files?.[0])}
          />
          {fileReading ? (
            <small role="status">{tx("正在读取所选 ZIP 文件…", "Reading selected ZIP file…")}</small>
          ) : null}
        </div>
      ) : null}

      {sourceMode === "skill_text" ? (
        <div className="ae-field">
          <label htmlFor="ae-import-skill-text">{tx("SKILL.md 内容", "SKILL.md content")}</label>
          <textarea
            id="ae-import-skill-text"
            value={skillText}
            disabled={busy}
            placeholder={"---\nname: code-review\ndescription: Structured code review checklist\nversion: 1.0.0\n---\n# Code Review\n..."}
            onChange={event => {
              setSkillText(event.target.value);
              setPreview(null);
            }}
          />
        </div>
      ) : null}

      {sourceMode === "config_text" ? (
        <>
          <div className="ae-field">
            <label htmlFor="ae-import-filename">{tx("配置格式文件名", "Configuration format")}</label>
            <select
              id="ae-import-filename"
              value={filename}
              disabled={busy}
              onChange={event => {
                setFilename(event.target.value);
                setPreview(null);
              }}
            >
              <option value=".mcp.json">
                {tx("JSON 配置 (.mcp.json / mcpServers)", "JSON (.mcp.json / mcpServers)")}
              </option>
              <option value="config.toml">
                {tx(
                  "TOML 配置 (Codex config.toml / mcp_servers)",
                  "TOML (Codex config.toml / mcp_servers)",
                )}
              </option>
            </select>
          </div>
          <div className="ae-field">
            <label htmlFor="ae-import-config-text">
              {tx("MCP 配置内容", "MCP configuration text")}
            </label>
            <textarea
              id="ae-import-config-text"
              value={configText}
              disabled={busy}
              placeholder={'{\n  "mcpServers": {\n    "filesystem": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-filesystem"]\n    }\n  }\n}'}
              onChange={event => {
                setConfigText(event.target.value);
                setPreview(null);
              }}
            />
          </div>
        </>
      ) : null}

      {preview ? (
        <div className="ae-section-card" role="region" aria-label={tx("导入预览结果", "Import preview result")}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", flexWrap: "wrap", gap: 8 }}>
            <h3>
              {tx("预览：", "Preview: ")} {preview.name} <code>v{preview.version}</code>
            </h3>
            <div className="ae-plugin-meta">
              <span className="ae-badge ae-badge-info">
                {formatSourceLabel(preview.source_format, false, tx)}
              </span>
              <span className={`ae-badge ${preview.can_import ? "ae-badge-success" : "ae-badge-danger"}`}>
                {preview.can_import
                  ? tx("可导入（导入后默认停用）", "Ready to import (starts disabled)")
                  : tx("存在冲突或错误，无法导入", "Cannot import")}
              </span>
            </div>
          </div>

          {preview.plugins.length > 0 ? (
            <div>
              <strong style={{ fontSize: 13 }}>
                {tx(`插件 (${preview.plugins.length})：`, `Plugins (${preview.plugins.length}):`)}
              </strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13 }}>
                {preview.plugins.map(p => (
                  <li key={p.id}>
                    <strong>{p.title}</strong> (<code>{p.id}</code> · v{p.version}) — {p.description}
                    {p.web_app ? <WebAppAccessSummary app={p.web_app} /> : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {preview.skills.length > 0 ? (
            <div>
              <strong style={{ fontSize: 13 }}>
                {tx(`技能 (${preview.skills.length})：`, `Skills (${preview.skills.length}):`)}
              </strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13 }}>
                {preview.skills.map(s => (
                  <li key={s.id}>
                    <strong>{s.title}</strong> (<code>${s.id}</code> · v{s.version}) — {s.description}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {preview.mcp_servers.length > 0 ? (
            <div>
              <strong style={{ fontSize: 13 }}>
                {tx(`MCP 服务器 (${preview.mcp_servers.length})：`, `MCP servers (${preview.mcp_servers.length}):`)}
              </strong>
              <ul style={{ margin: "4px 0 0", paddingLeft: 18, fontSize: 13 }}>
                {preview.mcp_servers.map(m => (
                  <li key={m.id}>
                    <strong>{m.name}</strong> (<code>{m.id}</code> · {m.transport}) —{" "}
                    <code>{m.transport === "streamable_http" ? m.url : m.command}</code>
                    {m.login_required ? <span className="ae-badge ae-badge-warning">{tx("导入后需登录", "Sign-in required after import")}</span> : null}
                    <McpDataPolicySummary policy={m.data_policy} compact />
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {preview.unsupported_components.length > 0 ? (
            <div className="ae-plugin-meta">
              <ShieldAlert size={15} />
              <span style={{ fontSize: 12.5 }}>
                {tx("已忽略的不受支持宿主特性：", "Ignored unsupported host components:")}
              </span>
              {preview.unsupported_components.map(comp => (
                <span key={comp} className="ae-badge ae-badge-warning">
                  {comp}
                </span>
              ))}
            </div>
          ) : null}

          {preview.warnings.length > 0 ? (
            <div className="ae-banner ae-banner-warning">
              <ul style={{ margin: 0, paddingLeft: 18, fontSize: 12.5 }}>
                {preview.warnings.map((w, idx) => (
                  <li key={idx}>{w}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      ) : null}
    </AccessibleModal>
  );
}
