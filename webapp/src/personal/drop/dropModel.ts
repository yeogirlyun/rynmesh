import type { TFunction } from "i18next";
import {
  formatBytes, formatProgressBytes, formatSpeed,
  type PeerMeta, type Relationship, type Route, type Transfer, type TransferAction,
} from "../../domain/dropClient";
import type { Device } from "../model";

// One derived device model for the drop workspace: personal devices (the same
// list the home topology uses) enriched with the daemon's PeerMeta.

export type DropDevice = {
  id: string;
  name: string;
  kind: Device["kind"];
  self: boolean;
  relationship: Relationship;
  online: boolean | null;
  route: Route | null;
  /** Where the graph draws it: directly wired (LAN) or behind the internet hub. */
  scope: "lan" | "remote";
  latency: number | null;
  address: string;
  meta?: PeerMeta;
  device?: Device;
};

export type DeviceFilter = "own" | "friend" | "nearby";

export function buildDropDevices(devices: Device[], peers: Record<string, PeerMeta>): DropDevice[] {
  const result: DropDevice[] = [];
  const seen = new Set<string>();
  for (const device of devices) {
    const meta = peers[device.id];
    seen.add(device.id);
    const networkRoute = device.network?.route;
    const fallbackRoute: Route | null = networkRoute === "lan" || networkRoute === "public" || networkRoute === "relay" ? networkRoute : null;
    const route = meta ? meta.route : fallbackRoute;
    const relationship: Relationship = device.self || device.own ? "own" : meta?.relationship ?? "other";
    result.push({
      id: device.id,
      name: device.name || meta?.name || device.id,
      kind: device.kind,
      self: Boolean(device.self),
      relationship,
      online: device.self ? true : meta ? meta.online : device.online,
      route: device.self ? null : route,
      scope: route === "lan" || (!meta && device.network?.scope === "local") ? "lan" : "remote",
      latency: meta?.latency_ms ?? null,
      address: meta?.address || device.network?.address || "",
      meta,
      device,
    });
  }
  for (const meta of Object.values(peers)) {
    if (seen.has(meta.peer_id)) continue;
    result.push({
      id: meta.peer_id, name: meta.member_name ? `${meta.member_name} · ${meta.name}` : meta.name || meta.peer_id, kind: "desktop", self: false,
      relationship: meta.relationship, online: meta.online, route: meta.route,
      scope: meta.route === "lan" ? "lan" : "remote", latency: meta.latency_ms, address: meta.address, meta,
    });
  }
  return result;
}

export const inFilter = (device: DropDevice, filter: DeviceFilter) => device.relationship === filter;
/** Peers with relationship "other" are hidden from the workspace (not sendable). */
export const isTargetable = (device: DropDevice) => !device.self && device.relationship !== "other";

export function routeLabel(t: TFunction, route: Route | null | undefined) {
  return route === "lan" ? t("drop.routeLan") : route === "public" ? t("drop.routePublic")
    : route === "relay" ? t("drop.routeRelay") : t("drop.routeUnknown");
}

export function relationshipLabel(t: TFunction, device: Pick<DropDevice, "relationship" | "meta">) {
  if (device.relationship === "external") return device.meta?.plugin_id || t("drop.relExternal", { defaultValue: "Plugin connection" });
  if (device.relationship === "own") return t("drop.relOwn");
  if (device.relationship === "friend") return t("drop.relFriend");
  if (device.relationship === "nearby") return device.meta?.verification.verified ? t("drop.relNearbyVerified") : t("drop.relNearby");
  return t("drop.relOther");
}

export function statusLabel(t: TFunction, online: boolean | null) {
  return online === true ? t("drop.online") : online === false ? t("drop.offline") : t("drop.statusUnknown");
}

export function reasonLabel(t: TFunction, reason: string) {
  const known = ["offline", "not_paired", "unverified", "no_route", "relay_unavailable"];
  return known.includes(reason) ? t(`drop.reason_${reason}`) : reason;
}

/** One line under a node or in the detail row: "公网直连 · 38 ms" / "离线". */
export function deviceSummary(t: TFunction, device: DropDevice) {
  if (device.self) return t("drop.thisDeviceSub");
  if (device.online === false) return t("drop.offline");
  if (device.online === null && !device.route) return t("drop.statusUnknown");
  const parts = [routeLabel(t, device.route)];
  if (device.latency !== null && device.latency !== undefined) parts.push(t("drop.latency", { ms: Math.round(device.latency) }));
  return parts.join(" · ");
}

export function sendButtonLabel(t: TFunction, device: DropDevice) {
  const mode = device.meta?.send_mode;
  if (mode === "queue") return t("drop.queueButton");
  if (mode === "verify") return t("drop.verifyButton");
  return t("drop.sendButton");
}

export function canSendTo(device: DropDevice) {
  return isTargetable(device) && device.relationship !== "external" && Boolean(device.meta) && device.meta!.file_sendable !== false && device.meta!.send_mode !== "unavailable";
}

export function formatEta(t: TFunction, seconds: number | null | undefined) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return "";
  if (seconds < 60) return t("drop.etaSeconds", { n: Math.max(1, Math.round(seconds)) });
  if (seconds < 3600) return t("drop.etaMinutes", { n: Math.round(seconds / 60) });
  const h = Math.floor(seconds / 3600);
  return t("drop.etaHours", { h, m: Math.round((seconds % 3600) / 60) });
}

const sep = (t: TFunction) => t("drop.listSeparator");

/** Status copy from the HANDOFF table, computed from real transfer data only. */
export function transferStatusText(t: TFunction, transfer: Transfer): string {
  const incoming = transfer.direction === "in";
  switch (transfer.status) {
    case "transferring": {
      const parts = [formatProgressBytes(transfer.confirmed_bytes, transfer.total_bytes)];
      const speed = formatSpeed(transfer.speed_bps);
      if (speed) parts.push(speed);
      const eta = formatEta(t, transfer.eta_s);
      if (eta) parts.push(eta);
      return parts.join(sep(t));
    }
    case "paused": return t("drop.st_paused", { size: formatBytes(transfer.confirmed_bytes) });
    case "awaiting_accept": return incoming ? t("drop.st_awaiting_accept_in") : t("drop.st_awaiting_accept");
    case "completed": return incoming ? t("drop.st_completed_in") : t("drop.st_completed_out");
    case "insufficient_space": {
      const need = transfer.error?.need_bytes;
      const free = transfer.error?.free_bytes;
      return need !== undefined && free !== undefined && need !== null && free !== null
        ? t("drop.st_insufficient_space", { need: formatBytes(need), free: formatBytes(free) })
        : t("drop.st_insufficient_space_unknown");
    }
    case "rejected": return incoming ? t("drop.st_rejected_in") : t("drop.st_rejected");
    case "failed": return transfer.error?.message ? `${t("drop.st_failed")}${sep(t)}${transfer.error.message}` : t("drop.st_failed");
    default: return t(`drop.st_${transfer.status}`);
  }
}

export type ActionButton = { action: TransferAction; label: string; primary?: boolean };

/** Buttons come from Transfer.actions exactly; only their labels depend on status. */
export function transferActionButtons(t: TFunction, transfer: Transfer): ActionButton[] {
  return transfer.actions.map((action, index) => {
    let key: string = `drop.act_${action}`;
    if (action === "retry" && transfer.status === "reconnecting") key = "drop.act_retry_later";
    if (action === "retry" && transfer.status === "verify_failed") key = "drop.act_retry_chunks";
    if (action === "accept" && transfer.status === "insufficient_space") key = "drop.act_change_location";
    return { action, label: t(key), primary: index === 0 && action !== "cancel" && action !== "reject" };
  });
}

export const transferIconKind = (transfer: Pick<Transfer, "kind" | "name">) =>
  transfer.kind === "folder" ? "folder" : transfer.kind === "files" ? "files"
    : /\.(zip|7z|rar|tar|gz|tgz|bz2|xz)$/i.test(transfer.name) ? "archive" : "file";

export function shortId(id: string) {
  return id.length > 20 ? `${id.slice(0, 9)}…${id.slice(-5)}` : id;
}

export function formatRelativeTime(iso: string | null | undefined, language: string) {
  if (!iso) return "";
  const time = new Date(iso).getTime();
  if (!Number.isFinite(time)) return "";
  const diff = (time - Date.now()) / 1000;
  const rtf = new Intl.RelativeTimeFormat(language, { numeric: "auto" });
  const abs = Math.abs(diff);
  if (abs < 60) return rtf.format(Math.round(diff), "second");
  if (abs < 3600) return rtf.format(Math.round(diff / 60), "minute");
  if (abs < 86400) return rtf.format(Math.round(diff / 3600), "hour");
  return rtf.format(Math.round(diff / 86400), "day");
}

export function formatClock(iso: string | null | undefined, language: string) {
  if (!iso) return "";
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return "";
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  return new Intl.DateTimeFormat(language, sameDay ? { hour: "2-digit", minute: "2-digit" }
    : { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}
