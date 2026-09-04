export interface FriendRecord {
  relationship_id: string;
  peer_id: string;
  node_name: string;
  endpoint: string;
  permissions: string[];
  status: "active" | "revoked";
  created_at: string;
  revoked_at?: string;
  revocation_delivery?: "pending" | "delivered";
}

export interface FriendInvitePreview {
  invite_id: string;
  peer_id: string;
  node_name: string;
  endpoint: string;
  permissions: string[];
  created_at: string;
  expires_at: string;
  endpoints?: string[];
  network_id?: string;
  address_category?: string;
  fingerprint?: string;
}

export interface FriendInviteResult {
  invite_uri: string;
  invite: FriendInvitePreview;
}

export interface FriendMessage {
  msg_id: string;
  dir: "in" | "out";
  from: string;
  to: string;
  text?: string;
  ts?: string;
  kind?: string;
  delivered?: boolean;
  delivery_state?: "queued" | "delivered" | "failed";
  attachment?: { filename: string; mime: string; size?: number };
}

export interface FriendContentCard {
  card_id: string;
  from: string;
  created_at: string;
  card: { library_id: string; title: string; summary: string; kind: string; source: string };
}
