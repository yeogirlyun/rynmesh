import { nodeControlUrl } from "./nodeUrl";

export type FriendWeek = { timezone: string; week_start: string; as_of: string; available_count: number; incomplete: boolean;
  items: { relationship_id: string; peer_id: string; node_name: string; publication_id: string; revision: number;
    published_at: number; title: string; checked_at: number | null; access_unconfirmed: boolean }[] };
export async function friendWeek(signal: AbortSignal): Promise<FriendWeek> {
  const response = await fetch(nodeControlUrl("/friend-feed/weekly"), { credentials: "include", signal });
  if (!response.ok) throw new Error("Friend recap is unavailable. Reconnect to your node and retry.");
  return response.json();
}
