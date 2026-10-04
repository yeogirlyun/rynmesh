import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import Exchange from "./Exchange";
import {
  exchange,
  networkInvite,
  parseInvite,
  type Manifest,
  type Status,
} from "../domain/exchange";

const confirm = vi.hoisted(() => vi.fn());
const mode = vi.hoisted(() => ({ current: "live" }));
vi.mock("../appContext", () => ({
  useAppContext: () => ({ confirm, client: { mode: mode.current } }),
}));
const manifest: Manifest = {
  version: "ryn.exchange.v2",
  name: "Alpha",
  validators: ["one", "two", "three", "four"].map((peer_id) => ({
    peer_id,
    endpoint: "http://127.0.0.1:9000",
  })),
  judges: [],
  issuance_limit: 100000000,
  work_reward: 1000000,
  appeal_window_s: 60,
  covenant: {
    version: "v2",
    principles: ["free participation"],
    acceptance: "Buyer accepts or both parties refund.",
  },
};
const terms = {
  title: "Translate a paragraph",
  scope: "Translate 100 words",
  price: 250000,
  buyer_dispute_reserve: 3000,
  provider_dispute_reserve: 3000,
  platform_commission: 0,
  appeal_window_s: 60,
  judges: [],
  covenant: manifest.covenant,
  delivery: "encrypted digital text, at most 32 KiB",
  delivery_window_s: 604800,
  review_window_s: 172800,
  case_window_s: 172800,
};
const state: Status = {
  configured: true,
  peer_id: "buyer",
  actor: "buyer",
  encryption_key: "key",
  name: "Alpha",
  manifest,
  height: 12,
  wallet: {
    available: 1000000,
    held: 0,
    earned: 1000000,
    label: "Buyer",
    devices: [],
    nonce: 3,
  },
  pending: [],
  listings: [],
  orders: [],
  proposals: [
    {
      id: "proposal-id",
      listing_id: "listing-id",
      buyer: "buyer",
      provider: "seller",
      proposed_by: "seller",
      scope: terms.scope,
      price: terms.price,
      status: "offered",
      terms,
      terms_hash: "sha256:exact-terms",
    },
  ],
};
beforeEach(() => {
  vi.restoreAllMocks();
  confirm.mockReset();
  mode.current = "live";
  vi.spyOn(exchange, "status").mockResolvedValue(structuredClone(state));
  vi.spyOn(exchange, "action").mockResolvedValue({
    committed: true,
    status: structuredClone(state),
  });
  vi.spyOn(exchange, "control").mockResolvedValue(structuredClone(state));
  vi.spyOn(exchange, "delivery").mockResolvedValue({
    body: "Private translated paragraph",
    hash: "sha256:delivery",
  });
});

it("reviews exact terms and total hold before spending, with no automatic payment", async () => {
  const user = userEvent.setup();
  render(<Exchange />);
  await user.click(
    await screen.findByRole("button", { name: "Review and agree" }),
  );
  expect(exchange.action).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("0.253 Ryncoin");
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(exchange.action).toHaveBeenCalledWith(
    "agree",
    { proposal_id: "proposal-id", terms_hash: "sha256:exact-terms" },
    expect.any(String),
    "buyer",
  );
});

it("requires a verified readable delivery before buyer acceptance", async () => {
  vi.mocked(exchange.status).mockResolvedValue({
    ...state,
    orders: [
      {
        id: "order-id",
        buyer: "buyer",
        provider: "seller",
        terms,
        price: 250000,
        status: "delivered",
        evidence_submitted: [],
        refund_requests: [],
        waivers: [],
        delivery_hash: "sha256:delivery",
      },
    ],
  });
  const user = userEvent.setup();
  render(<Exchange />);
  await user.click(await screen.findByRole("button", { name: "My orders" }));
  const order = await screen.findByRole("article", {
    name: "Order Translate a paragraph",
  });
  expect(
    within(order).getByRole("button", { name: "Accept and pay provider" }),
  ).toBeDisabled();
  await user.click(
    within(order).getByRole("button", { name: "Read private delivery" }),
  );
  expect(
    await within(order).findByText("Private translated paragraph"),
  ).toBeInTheDocument();
  await user.click(
    within(order).getByRole("button", { name: "Accept and pay provider" }),
  );
  expect(exchange.action).not.toHaveBeenCalled();
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(exchange.action).toHaveBeenCalledWith(
    "accept",
    { order_id: "order-id", delivery_hash: "sha256:delivery" },
    expect.any(String),
    "buyer",
  );
});

it("blocks new spending and resumes the persisted operation after a reload", async () => {
  vi.mocked(exchange.status).mockResolvedValue({
    ...state,
    pending: [{ id: "original-id", action: "agree", status: "pending" }],
  });
  const user = userEvent.setup();
  render(<Exchange />);
  expect(await screen.findByText(/original-id/)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Review and agree" }),
  ).toBeDisabled();
  await user.click(
    screen.getByRole("button", { name: "Resume saved operations" }),
  );
  expect(exchange.control).toHaveBeenCalledWith({ action: "resume" });
  expect(exchange.action).not.toHaveBeenCalled();
});

it("joining requires an explicit reviewed invitation and never starts earning automatically", async () => {
  vi.mocked(exchange.status).mockResolvedValue({
    configured: false,
    peer_id: "buyer",
    encryption_key: "key",
  });
  const user = userEvent.setup();
  render(<Exchange />);
  await user.type(
    await screen.findByLabelText("Public network invitation"),
    networkInvite(manifest),
  );
  await user.click(
    screen.getByRole("button", { name: "Review network invitation" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Join reviewed network" }),
  );
  expect(exchange.control).not.toHaveBeenCalled();
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(exchange.control).toHaveBeenCalledWith({
    action: "configure",
    manifest,
  });
  expect(exchange.action).not.toHaveBeenCalled();
});

it("round-trips Unicode invitations and rejects an unrelated link", () => {
  const unicode = { ...manifest, name: "Rynmesh · 日本語" };
  expect(parseInvite(networkInvite(unicode))).toEqual(unicode);
  expect(() => parseInvite("https://example.test")).toThrow();
});

it("never contacts a real wallet or signs operations from fixture mode", async () => {
  mode.current = "fixture";
  render(<Exchange />);
  expect(
    await screen.findByText(/Exchange is unavailable in fixture mode/),
  ).toBeInTheDocument();
  expect(exchange.status).not.toHaveBeenCalled();
  expect(exchange.action).not.toHaveBeenCalled();
  expect(exchange.control).not.toHaveBeenCalled();
});

it("discloses the bounded deadline outcomes before agreeing", async () => {
  const user = userEvent.setup();
  render(<Exchange />);
  await user.click(
    await screen.findByRole("button", { name: "Review and agree" }),
  );
  const body = confirm.mock.calls[0][0].body;
  expect(body).toContain("7 days");
  expect(body).toContain("50/50");
  expect(body).toContain("retains the first ruling");
  expect(body).toContain("One immutable statement");
});

it("keeps legacy balances and private deliveries readable while disabling mutations", async () => {
  vi.mocked(exchange.status).mockResolvedValue({
    ...state,
    read_only: true,
    pending: [{ id: "legacy-pending", action: "agree", status: "pending" }],
    orders: [
      {
        id: "order-id",
        buyer: "buyer",
        provider: "seller",
        terms,
        price: 250000,
        status: "delivered",
        evidence_submitted: [],
        refund_requests: [],
        waivers: [],
        delivery_hash: "sha256:delivery",
      },
    ],
  });
  const user = userEvent.setup();
  render(<Exchange />);
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Legacy v1 ledger: read-only",
  );
  expect(
    screen.getByRole("button", { name: "Review and agree" }),
  ).toBeDisabled();
  expect(
    screen.getByRole("button", { name: "Resume saved operations" }),
  ).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "My orders" }));
  await user.click(
    screen.getByRole("button", { name: "Read private delivery" }),
  );
  expect(
    await screen.findByText("Private translated paragraph"),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "Accept and pay provider" }),
  ).toBeDisabled();
  expect(exchange.action).not.toHaveBeenCalled();
  expect(exchange.control).not.toHaveBeenCalled();
});

it("requires review and binds deadline settlement without running it automatically", async () => {
  vi.mocked(exchange.status).mockResolvedValue({
    ...state,
    orders: [
      {
        id: "order-id",
        buyer: "buyer",
        provider: "seller",
        terms,
        price: 250000,
        status: "disputed",
        round: 0,
        case_until: Math.floor(Date.now() / 1000) - 1,
        settlement_hash: "sha256:reviewed-outcome",
        evidence_submitted: [],
        refund_requests: [],
        waivers: [],
        delivery_hash: "sha256:delivery",
      },
    ],
  });
  const user = userEvent.setup();
  render(<Exchange />);
  await user.click(await screen.findByRole("button", { name: "My orders" }));
  expect(exchange.action).not.toHaveBeenCalled();
  await user.click(
    screen.getByRole("button", { name: "Review deadline settlement" }),
  );
  expect(confirm.mock.calls[0][0].body).toContain("Split the price equally");
  expect(exchange.action).not.toHaveBeenCalled();
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(exchange.action).toHaveBeenCalledWith(
    "timeout",
    { order_id: "order-id", settlement_hash: "sha256:reviewed-outcome" },
    expect.any(String),
    "buyer",
  );
});

it("requires confirmation for immutable evidence and blocks replacement", async () => {
  vi.mocked(exchange.status).mockResolvedValue({
    ...state,
    orders: [
      {
        id: "order-id",
        buyer: "buyer",
        provider: "seller",
        terms,
        price: 250000,
        status: "disputed",
        round: 0,
        evidence_submitted: [],
        refund_requests: [],
        waivers: [],
      },
    ],
  });
  const user = userEvent.setup();
  render(<Exchange />);
  await user.click(await screen.findByRole("button", { name: "My orders" }));
  await user.type(
    screen.getByLabelText("Private evidence for the current panel"),
    "Reviewed statement",
  );
  await user.click(screen.getByRole("button", { name: "Submit evidence" }));
  expect(exchange.action).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toContain("cannot be replaced");
  await act(async () => confirm.mock.calls[0][0].onConfirm());
  expect(exchange.action).toHaveBeenCalledWith(
    "evidence",
    { order_id: "order-id", body: "Reviewed statement" },
    expect.any(String),
    "buyer",
  );
});

it("does not offer a v1 invitation as a hardened join", () => {
  expect(() =>
    parseInvite(networkInvite({ ...manifest, version: "ryn.exchange.v1" })),
  ).toThrow();
});
