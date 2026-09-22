import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { NodeClient, LLMServiceRecord } from "../domain/nodeClient";
import type { NodeStatus, Peer } from "../domain/types";
import type { SpaceStatus } from "../domain/space";

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
};
export type Service = {
  id: string;
  title: string;
  description: string;
  deviceId: string;
  kind: "ai" | "document" | "audio" | "network" | "video";
  online: boolean;
  record?: LLMServiceRecord;
  preview?: boolean;
  href?: string;
};
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
  const theme = useAppearance();
  const demo = client.mode === "fixture";
  const key = metadataKey(client.mode, node.peer_id);
  const [metadata, setMetadata] = useState<Metadata>(() => loadMetadata(key));
  const [records, setRecords] = useState<LLMServiceRecord[]>([]);
  const [health, setHealth] = useState<Record<string, boolean>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [space, setSpace] = useState<SpaceStatus | null>(null);
  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([
      client.listLLMServices(),
      client.peersHealth(),
      client.spaceStatus(),
    ]);
    if (results[0].status === "fulfilled") setRecords(results[0].value);
    if (results[2].status === "fulfilled") setSpace(results[2].value);
    else setSpace(null);
    if (results[1].status === "fulfilled")
      setHealth(
        Object.fromEntries(
          results[1].value.map((item) => [item.peerId, item.online]),
        ),
      );
    setError(
      results.some((result) => result.status === "rejected")
        ? "Some device or service information could not be refreshed."
        : "",
    );
    setLoading(false);
  }, [client]);
  const spaceAction = async (action: string, body?: Record<string, unknown>) => {
    const status = await client.spaceAction(action, body);
    setSpace(status);
    return status;
  };
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15000);
    return () => clearInterval(timer);
  }, [refresh]);
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
        },
        {
          id: node.peer_id,
          name: "Laptop",
          note: "For travel",
          kind: "laptop",
          own: true,
          self: true,
          online: true,
        },
        {
          id: "preview-work",
          name: "Work PC",
          note: "For work",
          kind: "desktop",
          own: true,
          online: false,
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
      },
    ];
    for (const peer of peers)
      if (!peer.isSelf && peer.id !== node.peer_id)
        result.push({
          id: peer.id,
          name: peer.name,
          note: "",
          kind: "desktop",
          own: false,
          online: health[peer.id] ?? null,
        });
    for (const record of records)
      if (!result.some((device) => device.id === record.peer_id))
        result.push({
          id: record.peer_id,
          name: record.node_name || record.peer_id,
          note: "",
          kind: "desktop",
          own: false,
          online: record.online,
        });
    // Only signed, current membership from the local node grants ownership.
    for (const member of space?.members || []) {
      if (member.removed || member.peer_id === node.peer_id) continue;
      const existing = result.find((device) => device.id === member.peer_id);
      if (existing) existing.own = space?.membership === "active";
      else result.push({ id: member.peer_id, name: member.name, note: "", kind: "desktop",
        own: space?.membership === "active", online: health[member.peer_id] ?? null });
    }
    return result;
  }, [demo, node, peers, health, records, space]);
  const devices = baseDevices.map((device) => ({
    ...device,
    name: metadata[device.id]?.name || device.name,
    note: metadata[device.id]?.note ?? device.note,
    kind: metadata[device.id]?.kind ?? device.kind,
  }));
  const services: Service[] = records.map((record) => ({
    id: `${record.peer_id}/${record.service.package_id}`,
    title: "AI chat",
    description: "Chat with a model on this device",
    kind: "ai",
    deviceId: record.peer_id,
    online: record.online,
    record,
  }));
  if (demo)
    services.push(
      {
        id: "preview-converter",
        title: "Document converter",
        description: "Convert documents",
        kind: "document",
        deviceId: "peer:fixture-llm-provider",
        online: true,
        preview: true,
      },
      {
        id: "preview-transcription",
        title: "Transcription",
        description: "Turn audio into text",
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
        error,
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
