import type { EgressStatus } from "./types";

export type EgressCardState =
  | { kind: "available" }
  | { kind: "connecting" }
  | { kind: "connected"; status: EgressStatus; warning: boolean }
  | { kind: "launching"; status: EgressStatus; warning: boolean }
  | { kind: "error"; message: string };

export type EgressCardEvent =
  | { type: "CONNECT" }
  | { type: "RESOLVE"; status: EgressStatus }
  | { type: "STATUS"; status: EgressStatus }
  | { type: "LAUNCH" }
  | { type: "LAUNCHED" }
  | { type: "DISCONNECT" }
  | { type: "ERROR"; message: string };

export const initialEgressCardState: EgressCardState = { kind: "available" };

/** Map a server status into a card state (used on poll + after connect). */
export function deriveFromStatus(status: EgressStatus): EgressCardState {
  if (status.lastError) return { kind: "error", message: status.lastError };
  if (status.connected) return { kind: "connected", status, warning: !status.locVerified };
  return { kind: "available" };
}

export function egressReducer(state: EgressCardState, event: EgressCardEvent): EgressCardState {
  switch (event.type) {
    case "CONNECT":
      return { kind: "connecting" };
    case "RESOLVE":
      // Authoritative result of a connect action — always honored.
      return deriveFromStatus(event.status);
    case "STATUS":
      // Ignore polled status while mid-action so the UI doesn't flicker.
      if (state.kind === "connecting" || state.kind === "launching") return state;
      return deriveFromStatus(event.status);
    case "LAUNCH":
      return state.kind === "connected"
        ? { kind: "launching", status: state.status, warning: state.warning }
        : state;
    case "LAUNCHED":
      return state.kind === "launching"
        ? { kind: "connected", status: state.status, warning: state.warning }
        : state;
    case "DISCONNECT":
      return { kind: "available" };
    case "ERROR":
      return { kind: "error", message: event.message };
    default:
      return state;
  }
}
