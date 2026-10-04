import { useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Panel } from "../components/ui";
import { coin, exchange, type Order, type Terms } from "../domain/exchange";

export function termsSummary(terms: Terms): string {
  if (terms.delivery_window_s === undefined)
    return "Legacy terms: preserve this ledger for review.";
  return `Delivery within ${terms.delivery_window_s / 86400} days; review within ${(terms.review_window_s ?? 0) / 86400} days from first delivery; each dispute round lasts ${(terms.case_window_s ?? 0) / 86400} days. One immutable statement per party per round. Deadline outcomes: undelivered work is refunded, delivered work is paid after review, an unresolved first dispute with delivery splits the price 50/50 (without delivery it refunds), and an unresolved appeal retains the first ruling. Committed judge fees remain paid.`;
}

export type Run = (action: string, value: object) => Promise<boolean>;

export default function OrderCard({
  order,
  actor,
  busy,
  run,
}: {
  order: Order;
  actor: string;
  busy: boolean;
  run: Run;
}) {
  const { confirm } = useAppContext();
  const [body, setBody] = useState("");
  const [evidence, setEvidence] = useState("");
  const [delivery, setDelivery] = useState("");
  const [readHash, setReadHash] = useState("");
  const [readError, setReadError] = useState("");
  const [reading, setReading] = useState(false);
  const buyer = order.buyer === actor;
  const closed = ["accepted", "refunded", "resolved"].includes(order.status);
  const working = ["working", "delivered"].includes(order.status);
  const due = (
    {
      working: order.delivery_until,
      delivered: order.review_until,
      disputed: order.case_until,
      ruling_ready: order.appeal_until,
    } as Record<string, number | undefined>
  )[order.status];
  const timeoutOutcome =
    order.status === "working" ||
    (order.status === "disputed" && order.round === 0 && !order.delivery_hash)
      ? "Refund the full price to the buyer."
      : order.status === "delivered"
        ? "Pay the full price to the provider."
        : order.status === "disputed" && order.round === 0
          ? "Split the price equally between buyer and provider."
          : `Pay ${(order.ruling_share ?? 0) / 100}% of the price to the provider, retaining the published ruling.`;
  const request = (action: string) =>
    run(action, {
      order_id: order.id,
      reviewed_order: order.order_hash,
      ...(action === "accept" ? { delivery_hash: readHash } : {}),
      ...(["timeout", "finalize", "waive"].includes(action)
        ? { settlement_hash: order.settlement_hash }
        : {}),
    });
  const review = (action: string, title: string, detail: string) =>
    confirm({
      title,
      body: detail,
      risk: "high",
      confirmLabel: title,
      onConfirm: async () => {
        await request(action);
      },
    });
  return (
    <Panel>
      <article aria-label={`Order ${order.terms.title}`}>
        <h3>{order.terms.title}</h3>
        <p>
          {buyer ? "You are buying" : "You are providing"} · {coin(order.price)}{" "}
          · {order.status.replaceAll("_", " ")}
        </p>
        <p>{order.terms.scope}</p>
        <p>{termsSummary(order.terms)}</p>
        <details>
          <summary>Accepted terms and public receipt</summary>
          <p>Order: {order.id}</p>
          <p>Buyer: {order.buyer}</p>
          <p>Provider: {order.provider}</p>
          <p>
            Commission: {coin(0)}. Buyer dispute reserve:{" "}
            {coin(order.terms.buyer_dispute_reserve)}. Provider dispute reserve:{" "}
            {coin(order.terms.provider_dispute_reserve)}. Unused reserves return
            when the order closes.
          </p>
          <p>
            {order.terms.delivery}. Appeal window: {order.terms.appeal_window_s}{" "}
            seconds. Each dispute round charges the quoted three judges only
            when their consistent signed decision is committed.
          </p>
          <p>{order.terms.covenant.acceptance}</p>
          <ul>
            {order.terms.judges.map((j) => (
              <li key={j.peer_id}>
                {j.model} · {coin(j.fee)} · {j.peer_id}
              </li>
            ))}
          </ul>
        </details>
        {!buyer && working ? (
          <>
            <label>
              Private digital delivery
              <textarea
                value={body}
                onChange={(e) => setBody(e.target.value)}
                maxLength={32000}
                disabled={busy}
              />
            </label>
            <Button
              disabled={busy || !body.trim()}
              onClick={() =>
                void run("deliver", {
                  order_id: order.id,
                  reviewed_order: order.order_hash,
                  body,
                }).then((ok) => {
                  if (ok) setBody("");
                })
              }
            >
              Send encrypted delivery
            </Button>
          </>
        ) : null}
        {order.delivery_hash ? (
          <>
            <Button
              disabled={reading}
              onClick={() => {
                setReading(true);
                setReadError("");
                void exchange
                  .delivery(order.id)
                  .then((result) => {
                    setDelivery(result.body);
                    setReadHash(result.hash);
                  })
                  .catch((cause) => setReadError(cause.message))
                  .finally(() => setReading(false));
              }}
            >
              Read private delivery
            </Button>
            {readError ? <p role="alert">{readError}</p> : null}
            {delivery ? (
              <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {delivery}
              </pre>
            ) : null}
          </>
        ) : null}
        {buyer && order.status === "delivered" ? (
          <Button
            disabled={busy || readHash !== order.delivery_hash}
            onClick={() =>
              review(
                "accept",
                "Accept and pay provider",
                `Release ${coin(order.price)} to the provider for the delivery you reviewed. This closes the order and refunds unused dispute reserves.`,
              )
            }
          >
            Accept and pay provider
          </Button>
        ) : null}
        {!closed ? (
          <>
            <Button
              disabled={busy || order.refund_requests.includes(actor)}
              onClick={() =>
                review(
                  "refund",
                  "Agree to a full refund",
                  "Only when both parties agree will the price and all unused reserves be refunded. This does not unilaterally cancel an order.",
                )
              }
            >
              Agree to full refund
            </Button>
            {order.refund_requests.length ? (
              <p>
                {order.refund_requests.length} of 2 refund approvals received.
              </p>
            ) : null}
            <Button
              disabled={busy || !working || order.terms.judges.length !== 6}
              onClick={() =>
                review(
                  "dispute",
                  "Open a dispute",
                  "Both parties submit private evidence to three quoted AI judges. Fees are paid from the reserved Ryncoin on a committed consistent decision. The other three judges handle one appeal. Uncertain or conflicting results use the accepted deadline outcome after the case window.",
                )
              }
            >
              Open dispute
            </Button>
          </>
        ) : null}
        {order.status === "disputed" ? (
          <>
            <p>
              Dispute round {(order.round ?? 0) + 1}. Evidence received from{" "}
              {order.evidence_submitted.length} of 2 parties.
            </p>
            <label>
              Private evidence for the current panel
              <textarea
                value={evidence}
                onChange={(e) => setEvidence(e.target.value)}
                maxLength={8000}
                disabled={busy}
              />
            </label>
            <p>
              Your statement and any readable delivery are sealed to this panel.
              Explain the accepted terms, what was delivered, and any missing
              work.
            </p>
            <Button
              disabled={
                busy ||
                !evidence.trim() ||
                order.evidence_submitted.includes(actor)
              }
              onClick={() =>
                confirm({
                  title: "Submit immutable evidence?",
                  body: "One statement per party per round. This statement cannot be replaced after submission. Review its facts carefully. A new statement is possible in the one appeal.",
                  risk: "high",
                  confirmLabel: "Submit evidence",
                  onConfirm: async () => {
                    if (
                      await run("evidence", {
                        order_id: order.id,
                        reviewed_order: order.order_hash,
                        body: evidence,
                      })
                    )
                      setEvidence("");
                  },
                })
              }
            >
              Submit evidence
            </Button>{" "}
            <Button
              disabled={busy || order.evidence_submitted.length !== 2}
              onClick={() =>
                review(
                  "rule",
                  "Request AI decision",
                  "Request the three named models. A consistent committed decision pays their quoted Ryncoin fees and starts the appeal window. An uncertain or unavailable model uses the agreed deadline outcome if the case cannot resolve.",
                )
              }
            >
              Request AI decision
            </Button>
          </>
        ) : null}
        {order.status === "ruling_ready" ? (
          <>
            <p>
              Decision: {(order.ruling_share ?? 0) / 100}% of the price to the
              provider. Appeal deadline:{" "}
              {new Date((order.appeal_until ?? 0) * 1000).toLocaleString()}.
            </p>
            {order.round === 0 ? (
              <Button
                disabled={
                  busy || Date.now() >= (order.appeal_until ?? 0) * 1000
                }
                onClick={() =>
                  review(
                    "appeal",
                    "Appeal the decision",
                    "A different panel of three models will review new evidence from both parties. Their quoted fees come from the remaining reserves. Funds stay held until the appeal is decided.",
                  )
                }
              >
                Appeal decision
              </Button>
            ) : null}{" "}
            <Button
              disabled={busy || order.waivers.includes(actor)}
              onClick={() =>
                review(
                  "waive",
                  "Waive remaining appeal time",
                  "If both parties waive the window, either can finalize this decision immediately.",
                )
              }
            >
              Waive remaining appeal time
            </Button>{" "}
            <Button
              disabled={
                busy ||
                (order.waivers.length < 2 &&
                  Date.now() < (order.appeal_until ?? 0) * 1000)
              }
              onClick={() =>
                review(
                  "finalize",
                  "Settle this decision",
                  "Apply the model decision and return unused reserves. This closes the order.",
                )
              }
            >
              Settle decision
            </Button>
          </>
        ) : null}
        {!closed && due !== undefined ? (
          <>
            <p>
              Current deadline: {new Date(due * 1000).toLocaleString()}.{" "}
              {timeoutOutcome} Unused reserves return; committed judge fees
              remain paid. Refresh after the deadline; no automatic settlement
              runs.
            </p>
            <Button
              disabled={busy || Date.now() < due * 1000}
              onClick={() =>
                review(
                  "timeout",
                  "Review deadline settlement",
                  `${timeoutOutcome} This applies the fallback you accepted in the terms, not a new AI judgment. Committed judge fees remain paid.`,
                )
              }
            >
              Review deadline settlement
            </Button>
          </>
        ) : null}
        {closed ? (
          <p>
            Closed. Provider received {coin(order.paid ?? 0)}. The final receipt
            remains auditable.
          </p>
        ) : null}
      </article>
    </Panel>
  );
}
