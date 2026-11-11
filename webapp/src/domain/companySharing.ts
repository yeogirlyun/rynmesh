import { nodeControlUrl } from "./nodeUrl";
import { peerProviders } from "./peerProviders";

/** Sharing local models with company colleagues through an installed company plugin. */
export interface CompanySharingState {
  pluginId: string;
  memberName: string;
  /** Local service ids currently shared with the company. */
  selected: string[];
  /** Local service ids that can be shared right now. */
  eligible: string[];
}

export interface CompanyUsageCounts { requests: number; input_tokens: number; output_tokens: number; total_tokens: number }
export interface CompanyUsageRow {
  model_key: string;
  model_name: string;
  peer_id: string;
  member_name: string;
  last_used_at: string | null;
  today: CompanyUsageCounts;
  last_30_days: CompanyUsageCounts;
}
export interface CompanyUsage { day: string; sharing: boolean; published_service_ids: string[]; rows: CompanyUsageRow[] }

/**
 * Null unless a company plugin is signed in, this device is bound, and the
 * company lists the signed-in member as a publisher. Anyone else gets no
 * control at all, rather than one that fails when used.
 */
export async function loadCompanySharing(): Promise<CompanySharingState | null> {
  const response = await fetch(nodeControlUrl("/peer-providers"), { credentials: "include", cache: "no-store" });
  if (!response.ok) return null;
  const raw: unknown = await response.json();
  const providers = raw && typeof raw === "object" && Array.isArray((raw as { providers?: unknown }).providers)
    ? (raw as { providers: Array<Record<string, unknown>> }).providers : [];
  for (const row of providers) {
    if (row?.status !== "connected" || row?.bound !== true || typeof row?.plugin_id !== "string") continue;
    try {
      const workspace = await peerProviders.workspace(row.plugin_id);
      if (!workspace.bound || workspace.current_member?.can_publish_models !== true) continue;
      const [status, models] = await Promise.all([peerProviders.status(row.plugin_id), peerProviders.models(row.plugin_id)]);
      if (!status.bound) continue;
      return { pluginId: row.plugin_id, memberName: workspace.current_member.name,
        selected: status.selected_service_ids, eligible: models.map(model => model.service_id) };
    } catch { continue; }
  }
  return null;
}

/** Changes one model and keeps every other current choice. Returns the saved selection. */
export async function setCompanyShared(pluginId: string, serviceId: string, shared: boolean): Promise<string[]> {
  const current = await peerProviders.status(pluginId);
  const next = new Set(current.selected_service_ids);
  if (shared) next.add(serviceId); else next.delete(serviceId);
  const saved = await peerProviders.publish(pluginId, [...next]);
  const selected = Array.isArray(saved.selected_service_ids)
    ? saved.selected_service_ids.filter((value): value is string => typeof value === "string") : [];
  if (selected.includes(serviceId) !== shared) throw new Error("company_sharing_not_saved");
  return selected;
}

export async function loadCompanyUsage(): Promise<CompanyUsage | null> {
  const response = await fetch(nodeControlUrl("/llm/models/usage/company"), { credentials: "include", cache: "no-store" });
  if (!response.ok) return null;
  const raw = await response.json() as Partial<CompanyUsage>;
  if (!raw || !Array.isArray(raw.rows)) return null;
  return { day: String(raw.day || ""), sharing: raw.sharing === true,
    published_service_ids: Array.isArray(raw.published_service_ids) ? raw.published_service_ids : [], rows: raw.rows };
}
