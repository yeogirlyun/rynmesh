import { nodeControlUrl } from "./nodeUrl";

export type PeerIdentity = { peer_id: string; endpoint: string };
export type Judge = PeerIdentity & {
  encryption_key: string;
  model: string;
  fee: number;
};
export type Manifest = {
  version: string;
  name: string;
  validators: PeerIdentity[];
  judges: Judge[];
  issuance_limit: number;
  work_reward: number;
  appeal_window_s: number;
  covenant: { version: string; principles: string[]; acceptance: string };
};
export type Listing = {
  id: string;
  owner: string;
  kind: "offer" | "request";
  category: string;
  title: string;
  description: string;
  price: number;
  active: boolean;
};
export type Terms = {
  title: string;
  scope: string;
  price: number;
  buyer_dispute_reserve: number;
  provider_dispute_reserve: number;
  platform_commission: number;
  appeal_window_s: number;
  judges: Judge[];
  covenant: Manifest["covenant"];
  delivery: string;
  delivery_window_s?: number;
  review_window_s?: number;
  case_window_s?: number;
  dispute_timeout_share_bps?: number;
  evidence_policy?: string;
};
export type Proposal = {
  id: string;
  listing_id: string;
  buyer: string;
  provider: string;
  proposed_by: string;
  scope: string;
  price: number;
  status: string;
  terms: Terms;
  terms_hash: string;
};
export type Order = {
  id: string;
  buyer: string;
  provider: string;
  terms: Terms;
  price: number;
  status: string;
  evidence_submitted: string[];
  refund_requests: string[];
  waivers: string[];
  round?: number;
  appeal_until?: number;
  ruling_share?: number;
  paid?: number;
  delivery_hash?: string;
  delivery_until?: number;
  review_until?: number;
  case_until?: number;
  settlement_hash?: string;
  order_hash?: string;
};
export type Status = {
  configured: boolean;
  read_only?: boolean;
  protocol_version?: string;
  registry_lan?: boolean;
  peer_id: string;
  encryption_key: string;
  covenant?: Manifest["covenant"];
  actor?: string;
  name?: string;
  network?: string;
  height?: number;
  head?: string;
  manifest?: Manifest;
  issued?: number;
  issuance_limit?: number;
  work_reward?: number;
  wallet?: {
    available: number;
    held: number;
    earned: number;
    label: string;
    devices: string[];
    nonce: number;
  };
  receipts?: { id: string; action: string; status: string }[];
  listings?: Listing[];
  proposals?: Proposal[];
  orders?: Order[];
  pending?: { id: string; action: string; status: string }[];
  registry?: boolean;
  judge?: boolean;
};

export const coin = (minor: number) =>
  `${(minor / 1_000_000).toLocaleString("en-US", { maximumFractionDigits: 6 })} Ryncoin`;
export function parseInvite(input: string): Manifest {
  const raw = input.trim();
  const decoded = raw.startsWith("rynmesh-exchange://network/")
    ? new TextDecoder().decode(
        Uint8Array.from(
          atob(
            raw.split("/network/")[1].replaceAll("-", "+").replaceAll("_", "/"),
          ),
          (c) => c.charCodeAt(0),
        ),
      )
    : raw;
  const value = JSON.parse(decoded) as Manifest;
  if (
    value.version !== "ryn.exchange.v2" ||
    !Array.isArray(value.validators) ||
    !Array.isArray(value.judges)
  )
    throw new Error("This network invitation is invalid.");
  return value;
}
export function networkInvite(manifest: Manifest): string {
  const encoded = btoa(
    Array.from(new TextEncoder().encode(JSON.stringify(manifest)), (c) =>
      String.fromCharCode(c),
    ).join(""),
  );
  return `rynmesh-exchange://network/${encoded.replaceAll("+", "-").replaceAll("/", "_")}`;
}
const errors: Record<string, string> = {
  exchange_legacy_read_only:
    "This v1 ledger is read-only. Keep its keys and history. A hardened pilot needs a reviewed v2 invitation and a separate node home; balances are not automatically migrated.",
  exchange_dispute_state:
    "This order is already in a case or closed. A dispute cannot reset evidence or bypass the one appeal.",
  exchange_evidence_locked:
    "Your statement for this round is immutable. Use the appeal for a new statement after the first ruling.",
  exchange_settlement_changed:
    "The decision or deadline outcome changed after review. Refresh and review it again before settling or waiving.",
  exchange_admission_exhausted:
    "New activity has reached this pilot's resource budget. Existing orders retain their reserved closing capacity.",
  exchange_account_quota:
    "This identity reached its free admission quota for this pilot. Existing orders can still close.",
  exchange_attempt_limit:
    "This case reached its three model attempts, or this registry reached its temporary verification limit. Review the disclosed deadline outcome; no additional model fees were committed.",
  exchange_work_pending:
    "This identity already has a pending verification receipt. Retry its original job ID or wait ten minutes before a new verification.",
  exchange_registry_endpoint_blocked:
    "This endpoint is on a private network outside the reviewed roster. Each witness operator must explicitly allow private registry probes before verification.",
  exchange_order_changed:
    "This order changed after review. Refresh its state before submitting evidence, requesting models, paying or settling.",
  exchange_review_network_changed:
    "The connected network changed after review. Refresh and review the currency network before signing.",
  exchange_operation_superseded:
    "Another certified operation changed the nonce or reviewed order context. This saved operation was not applied. Review your wallet and orders before creating a new request.",
  exchange_account_changed:
    "This node switched accounts after your review. Refresh and review the payer identity and terms again before signing.",
  exchange_delivery_changed:
    "The delivery changed after you reviewed it. Refresh and read the new delivery before accepting.",
  exchange_pending_required:
    "Resume the saved operation before submitting another mutation from this node.",
  exchange_open_orders_limit:
    "This pilot limits open work to protect access to every held balance. Close existing orders before agreeing more work.",
  exchange_insufficient_balance:
    "The buyer needs the price plus their dispute reserve. The provider also needs their dispute reserve. Earn Ryncoin before agreeing.",
  exchange_profile_required: "Create your free public profile first.",
  exchange_quorum_unavailable:
    "Validator approval is unavailable. The original operation is saved. Refresh, then resume it when the validators reconnect.",
  exchange_proposer_unavailable:
    "The next proposer is offline. Your original operation remains saved. Reconnect that validator, then resume.",
  exchange_models_disagree:
    "The models disagree. Statements stay immutable. Retry the same case within its attempt limit, or review the accepted deadline outcome.",
  exchange_model_uncertain:
    "A model could not decide safely. Retry the same immutable case within its attempt limit, or settle the accepted outcome after its deadline.",
  exchange_evidence_incomplete:
    "Both parties must submit evidence before requesting a decision.",
  exchange_appeal_open:
    "The appeal window is still open. Wait or have both parties waive their appeal before settling.",
  exchange_registry_disabled:
    "Enable registry participation before requesting this verification.",
  exchange_registry_already_rewarded:
    "This identity has already received its registry registration reward.",
  exchange_work_unverified:
    "Independent validators could not verify your registry. Check your endpoint and participation setting.",
  exchange_issuance_exhausted:
    "This alpha network's published issuance budget is exhausted. Existing Ryncoin can still be spent.",
  exchange_network_changed:
    "This node already belongs to a different exchange network. It cannot replace a ledger containing commitments.",
  exchange_private_key_required:
    "Use the original node holding this account’s decryption keys to deliver or submit private evidence.",
  exchange_recipient_invalid:
    "Use a different registered participant’s public identity.",
  exchange_judges_unavailable:
    "Six independent judges must be available outside the buyer and provider identities. Review the network roster with its operators.",
};
async function request<T>(body?: object): Promise<T> {
  const response = await fetch(
    nodeControlUrl(`/exchange${body ? "/action" : ""}`),
    {
      method: body ? "POST" : "GET",
      credentials: "include",
      signal: AbortSignal.timeout(150000),
      headers: { "Content-Type": "application/json" },
      ...(body ? { body: JSON.stringify(body) } : {}),
    },
  );
  if (!response.ok) {
    const value = await response.json().catch(() => ({}));
    throw new Error(
      errors[value.detail] ??
        "This operation could not be confirmed. Refresh and review saved operations before retrying. Funds move only after validator approval.",
    );
  }
  return response.json();
}
export const exchange = {
  status: () => request<Status>(),
  control: (body: object) => request<Status>(body),
  action: (
    action: string,
    value: object,
    operation_id: string,
    actor: string,
    network: string,
  ) =>
    request<{ committed: boolean; status: Status }>({
      action,
      value,
      operation_id,
      actor,
      network,
    }),
  delivery: (order_id: string) =>
    request<{ body: string; hash: string }>({ action: "delivery", order_id }),
};
