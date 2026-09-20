import { nodeControlUrl } from "./nodeUrl";

export type SharedItem = { id: string; title: string; url: string; added_by: string; read_by: Record<string, boolean>; removed: boolean };
export type SharedList = { id: string; title: string; owner: string; friend: string; local_peer: string; friend_name: string;
  status: string; revision: number; items: Record<string, SharedItem>; pending_count: number; cancelled_count?: number; error?: string; blocked?: boolean };
const messages: Record<string, string> = {
  shared_friend_inactive: "This friendship is no longer active. Saved list copies remain, but new transfers are blocked.",
  shared_list_inactive: "This list is not active. Refresh to review its status.",
  shared_capacity: "This list or its storage reached its limit. Keep this list for reference and start another list.",
  shared_link_invalid: "Use a complete HTTP or HTTPS link without a username or password.",
  shared_request_invalid: "Check the title and link, then retry.",
  shared_operation_changed: "This request changed after it was saved. Refresh the list before retrying.",
};
async function request<T>(body?: object): Promise<T> {
  const response = await fetch(nodeControlUrl(`/shared-reading${body ? "/action" : ""}`), {
    method: body ? "POST" : "GET", credentials: "include", signal: AbortSignal.timeout(15000),
    headers: { "Content-Type": "application/json" }, ...(body ? { body: JSON.stringify(body) } : {}),
  });
  if (!response.ok) { const value = await response.json().catch(() => ({})); throw new Error(messages[value.detail] ?? "Sync could not be confirmed. Your saved operations remain queued; reconnect and retry."); }
  return response.json();
}
export const sharedReading = {
  status: () => request<{ lists: SharedList[] }>(),
  action: (body: object) => request(body),
  discover: (relationship_id: string) => request<{ invitations: { id: string; title: string }[] }>({ action: "discover", relationship_id }),
};
