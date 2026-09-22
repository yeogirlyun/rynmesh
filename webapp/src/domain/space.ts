export interface SpaceMember {
  peer_id: string;
  name: string;
  role: "manager" | "device";
  removed: boolean;
}
export interface SpaceStatus {
  space: null | {
    id: string;
    name: string;
    authority: string;
    revision: number;
    valid_until: number;
  };
  members: SpaceMember[];
  self_id: string;
  coordinator: boolean;
  can_manage: boolean;
  membership: "none" | "active" | "expired" | "removed";
  ai_access: "local" | "space";
  pending: { id: string; action: string }[];
  last_error: string;
  invitation: string;
  invites: {
    id: string;
    expires_at: number;
    used: boolean;
    revoked: boolean;
  }[];
}
export function fixtureSpace(): SpaceStatus {
  return {
    space: {
      id: "preview",
      name: "Personal",
      authority: "self",
      revision: 1,
      valid_until: Date.now() / 1000 + 86400,
    },
    members: [
      { peer_id: "self", name: "Laptop", role: "manager", removed: false },
      {
        peer_id: "peer:fixture-llm-provider",
        name: "Home PC",
        role: "device",
        removed: false,
      },
      {
        peer_id: "preview-work",
        name: "Work PC",
        role: "device",
        removed: false,
      },
    ],
    self_id: "self",
    coordinator: true,
    can_manage: true,
    membership: "active",
    ai_access: "local",
    pending: [],
    last_error: "",
    invitation: "",
    invites: [],
  };
}
