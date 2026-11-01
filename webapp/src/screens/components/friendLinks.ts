import { tr } from "../../uiI18n";
import type { Tone } from "../../personal/drop/FamilyParts";

export type LinkTone = Extract<Tone, "ok" | "off" | "warn" | "bad">;
export interface LinkInfo { tone: LinkTone; label: string; recovery: string }

/** What a connection-check state means, in words a person can act on. The tone only carries status colour. */
export function linkInfo(state: string): LinkInfo {
  switch (state) {
    case "reachable": return { tone: "ok", label: tr("Verified direct link"), recovery: tr("Your friend's node answered an authenticated check. This does not verify model availability or every service.") };
    case "not_checked": return { tone: "off", label: tr("Not checked"), recovery: tr("Select this friend and check the connection.") };
    case "timed_out": return { tone: "warn", label: tr("Check timed out"), recovery: tr("Check that both nodes are running and the advertised address is reachable, then retry. A timeout alone cannot identify a firewall or NAT problem.") };
    case "unreachable": return { tone: "bad", label: tr("Could not reach this node"), recovery: tr("Check both connections and the friend's advertised address. On different networks, the address must be reachable from here. Then retry.") };
    case "authentication_failed": return { tone: "bad", label: tr("Friendship check rejected"), recovery: tr("Check the friendship on both devices. Access may have been revoked or restored from an older backup. Review a fresh invitation if needed.") };
    case "identity_mismatch": return { tone: "bad", label: tr("A different node answered"), recovery: tr("Verify your friend's current device and address. Do not send private content until the identity matches.") };
    case "unsupported": return { tone: "warn", label: tr("This node does not support connection checks"), recovery: tr("Update the friend's node. This result alone does not mean ordinary messaging is broken.") };
    case "transport_unsupported": return { tone: "warn", label: tr("Transport cannot send this check"), recovery: tr("Select a transport that supports authenticated POST requests, then retry.") };
    case "endpoint_missing": return { tone: "warn", label: tr("No direct address"), recovery: tr("Ask your friend to check their advertised reachable address. Mailbox delivery may still work.") };
    case "endpoint_rejected": return { tone: "warn", label: tr("Address rejected by local policy"), recovery: tr("Review the friend's advertised address. Diagnostics uses the same address restrictions as private sharing.") };
    case "friend_inactive": return { tone: "warn", label: tr("Friendship changed"), recovery: tr("Refresh your friends list and review the current relationship before retrying.") };
    case "rate_limited": return { tone: "warn", label: tr("Too many checks"), recovery: tr("Wait a minute, then retry this connection.") };
    default: return { tone: "bad", label: tr("Response could not be verified"), recovery: tr("The reply did not match this check. Update both nodes and verify the advertised address before retrying.") };
  }
}

/** Text for a thrown error. The node's own messages (see friendsClient) are application copy, so they are translated; anything else passes through. */
export const errorText = (cause: unknown, fallback: string): string => (cause instanceof Error && cause.message ? tr(cause.message) : fallback);
