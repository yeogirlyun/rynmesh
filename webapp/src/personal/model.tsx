import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { personalLabelKeys } from "./labels";
import type { NodeClient, LLMServiceRecord, ServiceAvailability } from "../domain/nodeClient";
import type { NodeStatus, Peer, PeerHealth } from "../domain/types";
import {
  directoryFromLegacy,
  directoryServices,
  presenceOnline,
  type DirectorySnapshot,
} from "../domain/directory";
import type { SpaceStatus } from "../domain/space";
import { networkFromPeer, type DeviceNetwork } from "./deviceNetwork";

export type Appearance = "light" | "dark" | "system";
export type Device = {
  id: string;
  name: string;
  note: string;
  kind: "desktop" | "laptop";
  own: boolean;
  self?: boolean;
  online: boolean | null;
  hardware?: string;
  network?: DeviceNetwork;
};
export type { ServiceAvailability };
export type Service = {
  id: string;
  title: string;
  description: string;
  brand?: string;
  accessLabel?: string;
  deviceId: string;
  kind: "ai" | "document" | "audio" | "network" | "video";
  // True only when availability is "ready", so consumers never treat an advertisement as usable.
  online: boolean;
  availability?: ServiceAvailability;
  record?: LLMServiceRecord;
  preview?: boolean;
  href?: string;
};
/**
 * A registry advertisement only locates a provider; it grants nothing. The local node
 * already derives the answer (relationship + observed presence + entitlement), so a record
 * that carries `availability` is used as is. The rule below is only the fallback for
 * records without it (older nodes, cached snapshots, fixtures): usable when it is this
 * node's own, the device is owned (signed Personal Space membership), or the provider's
 * authenticated catalog already bound it; liveness prefers this node's probe over the
 * provider's self-reported flag.
 */
export function serviceAvailability(
  record: LLMServiceRecord,
  context: { selfId: string; own: boolean; probed?: boolean | null },
): ServiceAvailability {
  if (record.availability) return record.availability.state;
  const local = record.peer_id === context.selfId || record.local_only === true || record.access === "self";
  const verified = local || context.own || record.binding_verified === true || record.access === "friend";
  if (!verified) return "needs_connection";
  return record.online && context.probed !== false ? "ready" : "offline";
}
type Metadata = Record<
  string,
  { name?: string; note?: string; kind?: Device["kind"] }
>;
type PersonalState = {
  space: SpaceStatus | null;
  spaceAction: (action: string, body?: Record<string, unknown>) => Promise<SpaceStatus>;
  devices: Device[];
  services: Service[];
  loading: boolean;
  error: string;
  demo: boolean;
  appearance: Appearance;
  setAppearance: (value: Appearance) => void;
  resolveName: (id: string, fallback?: string) => string;
  saveDevice: (
    device: Device,
    name: string,
    note: string,
    kind: Device["kind"],
  ) => Promise<void>;
  refresh: () => Promise<void>;
};
export const PersonalContext = createContext<PersonalState | null>(null);
export function usePersonal() {
  const state = useContext(PersonalContext);
  if (!state) throw new Error("PersonalProvider is required");
  return state;
}
export function readAppearance(): Appearance {
  try {
    const value = localStorage.getItem("ryn.appearance");
    return value === "light" || value === "dark" ? value : "system";
  } catch {
    return "system";
  }
}
export function applyAppearance(value: Appearance) {
  document.documentElement.dataset.theme =
    value === "system"
      ? matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : value;
}
export function useAppearance() {
  const [appearance, setValue] = useState<Appearance>(readAppearance);
  useEffect(() => {
    applyAppearance(appearance);
    const media = matchMedia("(prefers-color-scheme: dark)");
    const update = () => applyAppearance(appearance);
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [appearance]);
  const setAppearance = (value: Appearance) => {
    try {
      localStorage.setItem("ryn.appearance", value);
    } catch {
      /* The session still follows the selected theme. */
    }
    setValue(value);
  };
  return { appearance, setAppearance };
}
export function validateDeviceDetails(
  own: boolean,
  name: string,
  note: string,
) {
  if (own && !name.trim()) return "Enter a device name.";
  if (Array.from(name.trim()).length > 32)
    return "Use 32 characters or fewer for the name.";
  if (Array.from(note).length > 200)
    return "Use 200 characters or fewer for the private note.";
  return "";
}
export function metadataKey(mode: string, id: string) {
  return `ryn.personal.${mode}.${id}`;
}
export function personalHref(path: string, demo: boolean) {
  if (!demo) return path;
  const hashAt = path.indexOf("#");
  const base = hashAt < 0 ? path : path.slice(0, hashAt);
  const hash = hashAt < 0 ? "" : path.slice(hashAt);
  return `${base}${base.includes("?") ? "&" : "?"}client=fixture${hash}`;
}
function loadMetadata(key: string): Metadata {
  try {
    const value = JSON.parse(localStorage.getItem(key) || "{}");
    return value && typeof value === "object" && !Array.isArray(value)
      ? value
      : {};
  } catch {
    return {};
  }
}
export function PersonalProvider({
  client,
  node,
  peers,
  refreshShell,
  children,
}: {
  client: NodeClient;
  node: NodeStatus;
  peers: Peer[];
  refreshShell: () => Promise<void>;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  const theme = useAppearance();
  const demo = client.mode === "fixture";
  const key = metadataKey(client.mode, node.peer_id);
  const [metadata, setMetadata] = useState<Metadata>(() => loadMetadata(key));
  // Preferred source: the node's own snapshot of devices and their services. The two below
  // only feed the fallback for a node (or the fixture client) that has no /directory.
  const [directory, setDirectory] = useState<DirectorySnapshot | null>(null);
  const [records, setRecords] = useState<LLMServiceRecord[]>([]);
  const [health, setHealth] = useState<PeerHealth[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [space, setSpace] = useState<SpaceStatus | null>(null);
  const refresh = useCallback(async () => {
    // One request answers "which devices, what presence, which services". Only a node without
    // /directory (or a failed call) falls back to the separate lists.
    const loadDirectory = async () => {
      if (client.directory) {
        try {
          return { snapshot: await client.directory(), degraded: false };
        } catch {
          /* fall back to the separate lists below */
        }
      }
      const [services, healthRows] = await Promise.allSettled([
        client.listLLMServices(),
        client.peersHealth(),
      ]);
      if (services.status === "fulfilled") setRecords(services.value);
      setHealth(healthRows.status === "fulfilled" ? healthRows.value : []);
      return {
        snapshot: null,
        degraded: services.status === "rejected" || healthRows.status === "rejected",
      };
    };
    const [directoryResult, spaceResult] = await Promise.allSettled([loadDirectory(), client.spaceStatus()]);
    if (directoryResult.status === "fulfilled") setDirectory(directoryResult.value.snapshot);
    // A fresh object each refresh so everything derived from membership is recomputed,
    // even when the node (or a test double) hands back the same instance it mutated.
    if (spaceResult.status === "fulfilled") setSpace({ ...spaceResult.value });
    else setSpace(null);
    setError(
      directoryResult.status === "rejected" || spaceResult.status === "rejected" ||
        (directoryResult.status === "fulfilled" && directoryResult.value.degraded)
        ? "Some device or service information could not be refreshed."
        : "",
    );
    setLoading(false);
  }, [client]);
  // The one view every screen derives from: the node's snapshot when it has one, otherwise the
  // same shape rebuilt from the separate lists. Shell peers it does not list are kept.
  const view = useMemo<DirectorySnapshot>(() => {
    if (!directory) return directoryFromLegacy({ selfId: node.peer_id, peers, records, health });
    const known = new Set(directory.peers.map((entry) => entry.id));
    const extra = peers.filter((peer) => !known.has(peer.id));
    if (!extra.length) return directory;
    const added = directoryFromLegacy({ selfId: node.peer_id, peers: extra, records: [], health: [] });
    return { ...directory, peers: [...directory.peers, ...added.peers] };
  }, [directory, peers, records, health, node.peer_id]);
  const viewRecords = useMemo(() => directoryServices(view), [view]);
  const presenceById = useMemo(
    () => new Map(view.peers.map((entry) => [entry.id, presenceOnline(entry)] as const)),
    [view],
  );
  const spaceAction = async (action: string, body?: Record<string, unknown>) => {
    try {
      const status = await client.spaceAction(action, body);
      setSpace(status);
      return status;
    } catch (error) {
      // A prior request may have succeeded while this window held stale state.
      try { setSpace(await client.spaceStatus()); } catch { /* Keep the last known state. */ }
      throw error;
    }
  };
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => clearInterval(timer);
  }, [refresh]);
  useEffect(() => {
    if (!space?.pending.length) return;
    let cancelled = false;
    const timer = window.setInterval(() => {
      void client.spaceStatus().then(status => {
        if (!cancelled) setSpace(status);
      }).catch(() => { /* The background retry will continue. */ });
    }, 1500);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [client, space?.pending.length]);
  const baseDevices = useMemo<Device[]>(() => {
    if (demo)
      return [
        {
          id: "peer:fixture-llm-provider",
          name: "Home PC",
          note: "Study PC, available in the evening",
          kind: "desktop",
          own: true,
          online: true,
          hardware: "RTX 5090",
          network: { id: "local", label: "Home network", scope: "local", route: "lan", address: "192.168.31.23" },
        },
        {
          id: node.peer_id,
          name: "Laptop",
          note: "For travel",
          kind: "laptop",
          own: true,
          self: true,
          online: true,
          network: { id: "local", label: "Home network", scope: "local", route: "local", address: "192.168.31.10" },
        },
        {
          id: "preview-work",
          name: "Work PC",
          note: "For work",
          kind: "desktop",
          own: true,
          online: false,
          network: { id: "office", label: "Office network", scope: "remote", route: "public", address: "192.168.32.20" },
        },
        {
          id: "preview-alex",
          name: "Alex’s PC",
          note: "Alex’s transcription service",
          kind: "desktop",
          own: false,
          online: true,
        },
        {
          id: "preview-studio",
          name: "Studio PC",
          note: "Video processing",
          kind: "desktop",
          own: false,
          online: false,
        },
        {
          id: "preview-cloud", name: "Cloud server", note: "", kind: "desktop", own: true, online: true,
          network: { id: "public", label: "Public endpoints", scope: "public", route: "public", address: "203.0.113.24" },
        },
      ];
    const result: Device[] = [
      {
        id: node.peer_id,
        name: node.node_name,
        note: "",
        kind: "desktop",
        own: true,
        self: true,
        online: node.daemon_running,
        network: { id: "local", label: "Current network", scope: "local", route: "local" },
      },
    ];
    // Every device the node knows, including providers that only advertised a service:
    // the snapshot already made them devices, so nothing is synthesized here.
    for (const entry of view.peers)
      if (!entry.isSelf && entry.id !== node.peer_id)
        result.push({
          id: entry.id,
          name: entry.name,
          note: "",
          kind: "desktop",
          own: false,
          online: presenceOnline(entry),
          network: networkFromPeer(entry.endpoint, entry.lanEndpoints, entry.reachability?.lan ?? undefined),
        });
    // Only signed, current membership from the local node grants ownership.
    for (const member of space?.members || []) {
      if (member.removed || member.peer_id === node.peer_id) continue;
      const existing = result.find((device) => device.id === member.peer_id);
      if (existing) existing.own = space?.membership === "active";
      else result.push({ id: member.peer_id, name: member.name, note: "", kind: "desktop",
        own: space?.membership === "active", online: presenceById.get(member.peer_id) ?? null });
    }
    return result;
  }, [demo, node, view, presenceById, space]);
  const devices = baseDevices.map((device) => ({
    ...device,
    name: metadata[device.id]?.name || device.name,
    note: metadata[device.id]?.note ?? device.note,
    kind: metadata[device.id]?.kind ?? device.kind,
  }));
  const services: Service[] = viewRecords.map((record) => {
    const { adapter, model_alias, package_id } = record.service;
    const codex = adapter === "codex_cli";
    const claude = adapter === "claude_cli";
    const antigravity = adapter === "antigravity_cli";
    const parts = model_alias.split(" · ");
    const configuredSource = package_id.startsWith("src-") && parts.length > 1;
    const model = configuredSource ? parts.pop() : undefined;
    const title = codex ? "ChatGPT" : claude ? "Claude" : configuredSource ? parts.join(" · ") : model_alias;
    const brand = codex ? "openai" : claude ? "claude" :
      /z\.ai|\bglm\b/i.test(model_alias) ? "zai" :
      /deepseek/i.test(model_alias) ? "deepseek" : /qwen/i.test(model_alias) ? "qwen" :
      /openai|chatgpt/i.test(model_alias) ? "openai" : /ollama/i.test(model_alias) ? "ollama" :
      /lm studio/i.test(model_alias) ? "lmstudio" : /openrouter/i.test(model_alias) ? "openrouter" : undefined;
    const availability = serviceAvailability(record, {
      selfId: node.peer_id,
      own: Boolean(devices.find((device) => device.id === record.peer_id)?.own),
      probed: presenceById.get(record.peer_id),
    });
    return {
      id: `${record.peer_id}/${record.service.package_id}`,
      title: title || t("personal.aiService"),
      description: codex || antigravity ? t("personal.codexCLIChooseAModelInChat") : claude ? t("personal.claudeCodeUsesTheAccountSignedInOnTheDevice") : model ? t("personal.modelModel", { model }) : model_alias,
      brand,
      accessLabel: codex || claude || antigravity ? t("personal.localCLI") : adapter === "ollama" || adapter === "llama_cpp" || record.service.runtime === "native_llama_cpp" ? t("personal.localModel") : "API",
      kind: "ai",
      deviceId: record.peer_id,
      online: availability === "ready",
      availability,
      record,
    };
  });
  if (demo)
    services.push(
      {
        id: "preview-converter",
        title: t("personal.documentConverter"),
        description: t("personal.convertDocuments"),
        kind: "document",
        deviceId: "peer:fixture-llm-provider",
        online: true,
        preview: true,
      },
      {
        id: "preview-transcription",
        title: t("personal.transcription"),
        description: t("personal.turnAudioIntoText"),
        kind: "audio",
        deviceId: "preview-alex",
        online: true,
        preview: true,
      },
    );
  const saveDevice = async (
    device: Device,
    name: string,
    note: string,
    kind: Device["kind"],
  ) => {
    const error = validateDeviceDetails(Boolean(device.self), name, note);
    if (error) throw new Error(error);
    // Remote membership never conveys ownership. Only the local control API can rename a live device.
    // Remote names here are private nicknames; membership is managed separately.
    const next = {
      ...metadata,
      [device.id]: { name: name.trim(), note, kind },
    };
    if (device.self && !demo)
      await client.updateSettings({ node_name: name.trim() });
    localStorage.setItem(key, JSON.stringify(next));
    setMetadata(next);
    if (device.self && !demo) await refreshShell();
  };
  return (
    <PersonalContext.Provider
      value={{
        ...theme,
        space,
        spaceAction,
        devices,
        services,
        demo,
        loading,
        error: personalLabelKeys[error] ? t(`personal.${personalLabelKeys[error]}`) : error,
        refresh,
        saveDevice,
        resolveName: (id, fallback) =>
          devices.find((device) => device.id === id)?.name || fallback || id,
      }}
    >
      {children}
    </PersonalContext.Provider>
  );
}
