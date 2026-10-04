import { useEffect, useRef, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, PageHeader, Panel } from "../components/ui";
import {
  coin,
  exchange,
  networkInvite,
  parseInvite,
  type Manifest,
  type Status,
} from "../domain/exchange";

import OrderCard, { termsSummary, type Run } from "./ExchangeOrder";

const newId = () => crypto.randomUUID().replaceAll("-", "");

export default function Exchange() {
  const { confirm, client } = useAppContext();
  const fixture = client?.mode === "fixture";
  const [state, setState] = useState<Status | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [fresh, setFresh] = useState(false);
  const [tab, setTab] = useState("market");
  const [invite, setInvite] = useState("");
  const [reviewed, setReviewed] = useState<Manifest | null>(null);
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState("request");
  const [category, setCategory] = useState("");
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [price, setPrice] = useState("0.25");
  const [selected, setSelected] = useState("");
  const [scope, setScope] = useState("");
  const [quote, setQuote] = useState("0.25");
  const [recipient, setRecipient] = useState("");
  const [transferAmount, setTransferAmount] = useState("");
  const [endpoint, setEndpoint] = useState("");
  const [device, setDevice] = useState("");
  const attempt = useRef({ fingerprint: "", id: "" });
  const rewardJob = useRef(newId());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    if (fixture)
      return () => {
        mounted.current = false;
      };
    void exchange
      .status()
      .then((value) => {
        if (mounted.current) {
          setState(value);
          setFresh(true);
        }
      })
      .catch((cause) => {
        if (mounted.current) setError(cause.message);
      });
    return () => {
      mounted.current = false;
    };
  }, [fixture]);
  const run: Run = async (action, value) => {
    if (state?.read_only || busy || !fresh || state?.pending?.length)
      return false;
    const fingerprint = JSON.stringify([action, value]);
    if (attempt.current.fingerprint !== fingerprint)
      attempt.current = { fingerprint, id: newId() };
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await exchange.action(
        action,
        value,
        attempt.current.id,
        state?.actor ?? state?.peer_id ?? "",
      );
      if (mounted.current) {
        setState(result.status);
        setFresh(true);
        setNotice("Confirmed by validators. The public receipt is saved.");
        attempt.current = { fingerprint: "", id: "" };
      }
      return true;
    } catch (cause) {
      if (mounted.current) {
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not confirm this operation.",
        );
        setFresh(false);
      }
      return false;
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const control = async (value: object) => {
    setBusy(true);
    setError("");
    try {
      const result = await exchange.control(value);
      if (mounted.current) {
        setState(result);
        setFresh(true);
      }
    } catch (cause) {
      if (mounted.current) {
        setError(
          cause instanceof Error
            ? cause.message
            : "Could not reach the network.",
        );
        setFresh(false);
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  };
  const actor = state?.actor ?? state?.peer_id ?? "";
  const disabled =
    Boolean(state?.read_only) ||
    busy ||
    !fresh ||
    Boolean(state?.pending?.length);
  if (fixture)
    return (
      <div className="screen-stack">
        <PageHeader
          eyebrow="Live network required"
          title="Exchange"
          context="Exchange is unavailable in fixture mode. Connect the live local node to review real Ryncoin and sign operations."
        />
      </div>
    );
  return (
    <div className="screen-stack exchange-screen">
      <PageHeader
        eyebrow="Direct value exchange · alpha"
        title="Exchange"
        context="Offer or find lawful, ethical digital work. Agree a price, deliver privately, and earn or spend internal Ryncoin with zero platform commission."
      />
      <p>
        This release uses a configured validator group and a capped experimental
        currency. It has no fiat conversion. Offline proposers can pause
        transactions. Rules and receipts are public; private delivery and
        evidence are encrypted.
      </p>
      {state?.read_only ? (
        <p role="alert">
          Legacy v1 ledger: read-only. Balances and history are preserved. Keep
          the original keys and ledger; a v2 pilot needs a reviewed invitation
          and a separate node home. No automatic migration or reset is
          performed.
        </p>
      ) : null}
      {error ? <p role="alert">{error}</p> : null}
      {notice ? <p role="status">{notice}</p> : null}
      {!fresh ? (
        <p role="status">
          Current network state is unavailable or loading. Refresh before
          signing another operation.
        </p>
      ) : null}
      {state?.configured ? (
        <>
          <Button
            disabled={busy}
            onClick={() => void control({ action: "refresh" })}
          >
            Refresh network
          </Button>
          <p>
            {state.name} · ledger height {state.height}. Available:{" "}
            {coin(state.wallet?.available ?? 0)} · Held:{" "}
            {coin(state.wallet?.held ?? 0)}
          </p>
          {state.pending?.length ? (
            <Panel>
              <h2>Saved operations awaiting confirmation</h2>
              <ul>
                {state.pending.map((p) => (
                  <li key={p.id}>
                    {p.action} · {p.id}
                  </li>
                ))}
              </ul>
              <p>
                Resume the original signed operation. Creating a replacement
                payment could pay twice.
              </p>
              <Button
                disabled={busy || Boolean(state.read_only)}
                onClick={() => void control({ action: "resume" })}
              >
                Resume saved operations
              </Button>
            </Panel>
          ) : null}
          <div role="group" aria-label="Exchange views">
            {["market", "orders", "wallet & network"].map((name) => (
              <Button
                key={name}
                variant={tab === name ? "primary" : "standard"}
                onClick={() => setTab(name)}
              >
                {name === "market"
                  ? "Offers & requests"
                  : name === "orders"
                    ? "My orders"
                    : "Wallet & network"}
              </Button>
            ))}
          </div>
          {!state.wallet?.label ? (
            <Panel>
              <h2>Create your free public profile</h2>
              <label>
                Display name
                <input
                  value={label}
                  onChange={(e) => setLabel(e.target.value)}
                  maxLength={100}
                  disabled={disabled}
                />
              </label>
              <p>
                Your name, identity and encryption public key are visible in the
                network. Participation costs zero Ryncoin.
              </p>
              <Button
                disabled={disabled || !label.trim()}
                onClick={() => void run("profile", { label })}
              >
                Create free profile
              </Button>
            </Panel>
          ) : null}
          {tab === "market" ? (
            <>
              <Panel>
                <h2>Post an offer or request</h2>
                <p>
                  Describe any lawful, ethical digital value. No category
                  approval is required. These fields are public. Do not include
                  private personal information.
                </p>
                <label>
                  Purpose
                  <select
                    value={kind}
                    onChange={(e) => setKind(e.target.value)}
                    disabled={disabled}
                  >
                    <option value="request">I need something</option>
                    <option value="offer">I can provide something</option>
                  </select>
                </label>
                <label>
                  Category
                  <input
                    value={category}
                    onChange={(e) => setCategory(e.target.value)}
                    disabled={disabled}
                    maxLength={100}
                    placeholder="Translation, editing, research, scripts…"
                  />
                </label>
                <label>
                  Title
                  <input
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    disabled={disabled}
                    maxLength={200}
                  />
                </label>
                <label>
                  Description
                  <textarea
                    value={description}
                    onChange={(e) => setDescription(e.target.value)}
                    disabled={disabled}
                    maxLength={4000}
                  />
                </label>
                <label>
                  Asking price or budget in Ryncoin
                  <input
                    value={price}
                    onChange={(e) => setPrice(e.target.value)}
                    disabled={disabled}
                    inputMode="decimal"
                  />
                </label>
                <Button
                  disabled={
                    disabled ||
                    !state.wallet?.label ||
                    !category.trim() ||
                    !title.trim() ||
                    !description.trim()
                  }
                  onClick={() =>
                    void run("listing", {
                      kind,
                      category,
                      title,
                      description,
                      price,
                    }).then((ok) => {
                      if (ok) {
                        setTitle("");
                        setDescription("");
                      }
                    })
                  }
                >
                  Publish free listing
                </Button>
              </Panel>
              <Panel>
                <h2>Browse offers and requests</h2>
                {state.listings
                  ?.filter((l) => l.active)
                  .map((l) => (
                    <article key={l.id}>
                      <h3>{l.title}</h3>
                      <p>
                        {l.kind} · {l.category} · {coin(l.price)}
                      </p>
                      <p>{l.description}</p>
                      {l.owner === actor ? (
                        <Button
                          disabled={disabled}
                          onClick={() =>
                            void run("withdraw", { listing_id: l.id })
                          }
                        >
                          Withdraw listing
                        </Button>
                      ) : (
                        <Button
                          disabled={disabled || !state.wallet?.label}
                          onClick={() => {
                            setSelected(l.id);
                            setQuote(String(l.price / 1_000_000));
                            setScope("");
                          }}
                        >
                          Propose agreed work
                        </Button>
                      )}
                    </article>
                  ))}
                {!state.listings?.some((l) => l.active) ? (
                  <p>
                    No active listings yet. Post your first offer or request.
                  </p>
                ) : null}
                {selected ? (
                  <>
                    <h3>
                      Propose:{" "}
                      {state.listings?.find((l) => l.id === selected)?.title}
                    </h3>
                    <label>
                      Exact work and acceptance terms
                      <textarea
                        value={scope}
                        onChange={(e) => setScope(e.target.value)}
                        disabled={disabled}
                        maxLength={4000}
                      />
                    </label>
                    <label>
                      Your negotiated price in Ryncoin
                      <input
                        value={quote}
                        onChange={(e) => setQuote(e.target.value)}
                        disabled={disabled}
                        inputMode="decimal"
                      />
                    </label>
                    <p>
                      The other party reviews these public terms and must agree
                      before funds are held. Friendship is not required.
                    </p>
                    <Button
                      disabled={disabled || !scope.trim()}
                      onClick={() =>
                        void run("propose", {
                          listing_id: selected,
                          scope,
                          price: quote,
                        }).then((ok) => {
                          if (ok) setSelected("");
                        })
                      }
                    >
                      Send proposal
                    </Button>
                  </>
                ) : null}
              </Panel>
              <Panel>
                <h2>Proposals to review</h2>
                {state.proposals
                  ?.filter((p) => p.status === "offered")
                  .map((p) => (
                    <article key={p.id}>
                      <h3>{p.terms.title}</h3>
                      <p>{p.scope}</p>
                      <p>
                        Price: {coin(p.price)} · Commission: {coin(0)}.
                      </p>
                      <p>
                        Buyer reserves{" "}
                        {coin(p.price + p.terms.buyer_dispute_reserve)}.
                        Provider reserves{" "}
                        {coin(p.terms.provider_dispute_reserve)}. Dispute
                        reserves are refunded when unused.
                      </p>
                      <p>
                        {p.terms.delivery}.{" "}
                        {p.terms.judges.length
                          ? `${p.terms.judges.length} quoted models, one appeal, ${p.terms.appeal_window_s} seconds per appeal window.`
                          : "No AI panel: buyer acceptance, mutual refund or the disclosed deadline settlement."}
                      </p>
                      <p>{termsSummary(p.terms)}</p>
                      <details>
                        <summary>Public covenant and judge fees</summary>
                        <p>{p.terms.covenant.acceptance}</p>
                        <ul>
                          {p.terms.judges.map((j) => (
                            <li key={j.peer_id}>
                              {j.model} · {coin(j.fee)} · {j.peer_id}
                            </li>
                          ))}
                        </ul>
                        <p>Buyer: {p.buyer}</p>
                        <p>Provider: {p.provider}</p>
                      </details>
                      {p.proposed_by === actor ? (
                        <p>Waiting for the other party to agree.</p>
                      ) : (
                        <Button
                          disabled={disabled}
                          onClick={() =>
                            confirm({
                              title: "Agree and reserve Ryncoin?",
                              body: `${p.scope}\nPrice: ${coin(p.price)}. You reserve ${coin(actor === p.buyer ? p.price + p.terms.buyer_dispute_reserve : p.terms.provider_dispute_reserve)}. No commission. Disputes charge the named models' fees; unused reserves return at closure. ${termsSummary(p.terms)} These exact public terms become binding for this order.`,
                              risk: "high",
                              confirmLabel: "Agree and reserve",
                              onConfirm: async () => {
                                if (
                                  await run("agree", {
                                    proposal_id: p.id,
                                    terms_hash: p.terms_hash,
                                  })
                                )
                                  setTab("orders");
                              },
                            })
                          }
                        >
                          Review and agree
                        </Button>
                      )}
                    </article>
                  ))}
                {!state.proposals?.some((p) => p.status === "offered") ? (
                  <p>No proposals awaiting agreement.</p>
                ) : null}
              </Panel>
            </>
          ) : tab === "orders" ? (
            <>
              {state.orders?.map((order) => (
                <OrderCard
                  key={order.id}
                  order={order}
                  actor={actor}
                  busy={disabled}
                  run={run}
                />
              ))}
              {!state.orders?.length ? (
                <Panel>
                  <p>Agree a proposal to open your first order.</p>
                </Panel>
              ) : null}
            </>
          ) : (
            <>
              <Panel>
                <h2>Wallet and contribution</h2>
                <p>
                  Available {coin(state.wallet?.available ?? 0)} · Held{" "}
                  {coin(state.wallet?.held ?? 0)} · Earned{" "}
                  {coin(state.wallet?.earned ?? 0)}.
                </p>
                <p>
                  Ryncoin is separate from nontransferable reputation and
                  simulated Task Balance. Network supply:{" "}
                  {coin(state.issued ?? 0)} of a published{" "}
                  {coin(state.issuance_limit ?? 0)} budget.
                </p>
                <p>
                  To start with zero coins, serve this alpha network's registry
                  manifest. Independent validators must verify it before issuing
                  the one-time {coin(state.work_reward ?? 0)} registration
                  reward. Repeat or unverified claims receive no reward.
                </p>
                <Button
                  disabled={disabled}
                  onClick={() =>
                    void control({
                      action: "options",
                      value: { registry: !state.registry },
                    })
                  }
                >
                  {state.registry
                    ? "Stop registry participation"
                    : "Enable registry participation"}
                </Button>
                <label>
                  Your reachable node endpoint
                  <input
                    value={endpoint}
                    onChange={(e) => setEndpoint(e.target.value)}
                    disabled={disabled}
                    placeholder="http://192.168.1.20:9000"
                  />
                </label>
                <Button
                  disabled={
                    disabled ||
                    !state.registry ||
                    !state.wallet?.label ||
                    !endpoint.trim()
                  }
                  onClick={() =>
                    void run("reward", { job_id: rewardJob.current, endpoint })
                  }
                >
                  Verify registry and earn registration reward
                </Button>
              </Panel>
              <Panel>
                <h2>Recent local operation receipts</h2>
                <ul>
                  {state.receipts?.map((r) => (
                    <li key={r.id}>
                      {r.action} · {r.status} · {r.id}
                    </li>
                  ))}
                </ul>
                <p>
                  A superseded operation was not applied because another
                  authorized operation used the account nonce first. Refresh and
                  review your orders before submitting it again.
                </p>
              </Panel>
              <Panel>
                <h2>Direct Ryncoin transfer</h2>
                <p>
                  Send available Ryncoin to a registered public identity. Direct
                  transfers have no escrow, no dispute procedure, and no
                  platform fee. Review the identity with the recipient.
                </p>
                <label>
                  Recipient public identity
                  <input
                    value={recipient}
                    onChange={(e) => setRecipient(e.target.value)}
                    disabled={disabled}
                  />
                </label>
                <label>
                  Amount in Ryncoin
                  <input
                    value={transferAmount}
                    onChange={(e) => setTransferAmount(e.target.value)}
                    disabled={disabled}
                    inputMode="decimal"
                  />
                </label>
                <Button
                  disabled={
                    disabled || !recipient.trim() || !transferAmount.trim()
                  }
                  onClick={() =>
                    confirm({
                      title: "Send a direct transfer?",
                      body: `Send ${transferAmount} Ryncoin to ${recipient}. This is a final transfer with no escrow or dispute procedure. Verify the public identity.`,
                      risk: "high",
                      confirmLabel: "Send Ryncoin",
                      onConfirm: async () => {
                        if (
                          await run("transfer", {
                            recipient,
                            amount: transferAmount,
                          })
                        )
                          setTransferAmount("");
                      },
                    })
                  }
                >
                  Review direct transfer
                </Button>
              </Panel>
              <Panel>
                <h2>Private registry probe policy</h2>
                <p>
                  Witness operators control their outbound network access.
                  Private endpoints outside the reviewed roster are blocked
                  unless this operator explicitly allows them. This permits
                  claimant-selected requests inside your LAN.
                </p>
                <Button
                  disabled={disabled}
                  onClick={() =>
                    confirm({
                      title: "Change private registry probe policy?",
                      body: state.registry_lan
                        ? "Stop probes to private registry endpoints outside the roster."
                        : "Allow participants to ask this node to POST registry challenges to private LAN addresses. Enable only for this controlled pilot.",
                      risk: "high",
                      confirmLabel: "Apply probe policy",
                      onConfirm: async () => {
                        await control({
                          action: "options",
                          value: { registry_lan: !state.registry_lan },
                        });
                      },
                    })
                  }
                >
                  {state.registry_lan
                    ? "Block private registry probes"
                    : "Allow private registry probes"}
                </Button>
              </Panel>
              <Panel>
                <h2>Network rules</h2>
                <p>Network fingerprint: {state.network}</p>
                <p>Your identity: {actor}</p>
                <p>
                  This alpha roster is immutable. Validators require more than
                  two-thirds signed approval. A proposer outage can stop
                  progress; refresh or resume never clears vote locks.
                </p>
                <ul>
                  {state.manifest?.covenant.principles.map((p) => (
                    <li key={p}>{p}</li>
                  ))}
                </ul>
                <details>
                  <summary>Validators and published configuration</summary>
                  <pre
                    style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
                  >
                    {JSON.stringify(state.manifest, null, 2)}
                  </pre>
                </details>
                <details>
                  <summary>Public network invitation</summary>
                  <textarea
                    readOnly
                    aria-label="Public network invitation"
                    value={state.manifest ? networkInvite(state.manifest) : ""}
                  />
                </details>
                {state.manifest?.judges.some(
                  (j) => j.peer_id === state.peer_id,
                ) ? (
                  <>
                    <p>
                      Judge participation uses the exact local Ollama model
                      named in the manifest. Independence and impartiality
                      require deployment review.
                    </p>
                    <Button
                      disabled={disabled}
                      onClick={() =>
                        void control({
                          action: "options",
                          value: { judge: !state.judge },
                        })
                      }
                    >
                      {state.judge
                        ? "Disable judge participation"
                        : "Enable quoted judge model"}
                    </Button>
                  </>
                ) : null}
              </Panel>
              <Panel>
                <h2>Authorized signing devices</h2>
                <p>
                  Granting a public identity lets that device sign and spend for
                  this account. It does not transfer delivery decryption keys.
                  Keep the original node keys and complete ledger, including
                  vote locks, in a private backup.
                </p>
                <label>
                  Device public identity
                  <input
                    value={device}
                    onChange={(e) => setDevice(e.target.value)}
                    disabled={disabled}
                  />
                </label>
                <Button
                  disabled={
                    disabled || actor !== state.peer_id || !device.trim()
                  }
                  onClick={() =>
                    confirm({
                      title: "Authorize spending on this device?",
                      body: "This device can sign orders and spend your Ryncoin. Verify its public identity directly before authorizing it. Decryption keys are not copied.",
                      risk: "high",
                      confirmLabel: "Authorize device",
                      onConfirm: async () => {
                        await run("device", { peer_id: device, allowed: true });
                      },
                    })
                  }
                >
                  Authorize signing device
                </Button>
                <ul>
                  {state.wallet?.devices.map((id) => (
                    <li key={id}>
                      {id}{" "}
                      <Button
                        disabled={disabled || actor !== state.peer_id}
                        onClick={() =>
                          void run("device", { peer_id: id, allowed: false })
                        }
                      >
                        Revoke device
                      </Button>
                    </li>
                  ))}
                </ul>
              </Panel>
            </>
          )}
        </>
      ) : (
        <Panel>
          <h2>Join an exchange network</h2>
          <p>
            Participation is free. Ask the group for its public invitation, then
            review its validators, issuance budget, covenant, appeal window, and
            quoted models before joining. This node cannot later replace that
            ledger with another network.
          </p>
          <label>
            Public network invitation
            <textarea
              value={invite}
              onChange={(e) => {
                setInvite(e.target.value);
                setReviewed(null);
              }}
              disabled={busy}
            />
          </label>
          <Button
            disabled={busy || !invite.trim()}
            onClick={() => {
              try {
                setReviewed(parseInvite(invite));
                setError("");
              } catch {
                setError("This network invitation is invalid.");
              }
            }}
          >
            Review network invitation
          </Button>
          {reviewed ? (
            <>
              <h3>{reviewed.name}</h3>
              <p>
                {reviewed.validators.length} validators · Issuance budget{" "}
                {coin(reviewed.issuance_limit)} · Registry reward{" "}
                {coin(reviewed.work_reward)} · {reviewed.judges.length} judges.
              </p>
              <p>
                Appeal window {reviewed.appeal_window_s} seconds. Joining does
                not mint coins or accept an order.
              </p>
              <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                {JSON.stringify(reviewed, null, 2)}
              </pre>
              <Button
                disabled={busy}
                onClick={() =>
                  confirm({
                    title: "Join this alpha network?",
                    body: "Trust this reviewed validator roster and covenant for the experimental ledger. The network rules are fixed. This joins your node without moving funds.",
                    risk: "medium",
                    confirmLabel: "Join network",
                    onConfirm: async () => {
                      await control({
                        action: "configure",
                        manifest: reviewed,
                      });
                    },
                  })
                }
              >
                Join reviewed network
              </Button>
            </>
          ) : null}
        </Panel>
      )}
    </div>
  );
}
