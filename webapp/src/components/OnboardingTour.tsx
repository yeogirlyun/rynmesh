import {
  ArrowRight,
  Bot,
  CheckCircle2,
  CircleDashed,
  Clock3,
  Coins,
  Compass,
  MessageCircle,
  Newspaper,
  Server,
  ShieldCheck,
  Sparkles,
  Users,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { RynMark } from "../brand/RynBrand";
import type { NodeSettings, NodeStatus, Peer } from "../domain/types";
import { Button, Chip, IconButton } from "./ui";

export const ONBOARDING_VERSION = 1;

interface OnboardingTourProps {
  node: NodeStatus;
  settings: NodeSettings;
  peers: Peer[];
  onComplete: () => Promise<void>;
  onClose: () => void;
}

interface CapabilityProps {
  icon: typeof Sparkles;
  title: string;
  children: ReactNode;
  status?: "available" | "planned";
}

function Capability({ icon: Icon, title, children, status = "available" }: CapabilityProps) {
  return (
    <div className={`tour-capability tour-capability-${status}`}>
      <span className="tour-capability-icon">
        <Icon size={19} />
      </span>
      <div>
        <div className="tour-capability-title">
          <strong>{title}</strong>
          <Chip tone={status === "available" ? "ok" : "muted"}>
            {status === "available" ? "available now" : "planned"}
          </Chip>
        </div>
        <p>{children}</p>
      </div>
    </div>
  );
}

function StatusLine({ available, children }: { available: boolean; children: ReactNode }) {
  const Icon = available ? CheckCircle2 : CircleDashed;
  return (
    <li className={available ? "tour-status-available" : "tour-status-planned"}>
      <Icon size={15} />
      <span>{children}</span>
    </li>
  );
}

export default function OnboardingTour({
  node,
  settings,
  peers,
  onComplete,
  onClose,
}: OnboardingTourProps) {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [finishing, setFinishing] = useState(false);
  const self = peers.find((peer) => peer.isSelf) ?? peers[0];
  const otherPeers = peers.filter((peer) => !peer.isSelf).length;
  const steps = 6;

  const finish = async (route?: string) => {
    setFinishing(true);
    try {
      await onComplete();
      if (route) navigate(route);
    } finally {
      setFinishing(false);
    }
  };

  return (
    <div className="tour-backdrop" role="presentation">
      <section
        className="tour-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="tour-title"
      >
        <header className="tour-header">
          <div className="tour-brand">
            <RynMark size={38} />
            <div>
              <span className="eyebrow">Ryn companion · getting started</span>
              <strong>{step + 1} of {steps}</strong>
            </div>
          </div>
          <IconButton icon={X} label="Close getting started" onClick={onClose} />
        </header>

        <div className="tour-progress" aria-label={`Step ${step + 1} of ${steps}`}>
          {Array.from({ length: steps }, (_, index) => (
            <span key={index} className={index <= step ? "active" : ""} />
          ))}
        </div>

        <div className="tour-body">
          {step === 0 ? (
            <>
              <div className="tour-hero-mark"><Bot size={30} /></div>
              <span className="eyebrow">Your personal assistant is local</span>
              <h1 id="tour-title">Your Ryn node is running</h1>
              <p className="tour-lead">
                This desktop app controls a private node on your machine. The node owns your
                identity, stores your preferences, verifies network evidence, and mediates every
                peer operation.
              </p>
              <div className="tour-runtime-grid">
                <div><Server size={18} /><span>Node daemon</span><strong>Online</strong></div>
                <div><ShieldCheck size={18} /><span>Registry</span><strong>{node.registry}</strong></div>
                <div><Bot size={18} /><span>AI model</span><strong>{settings.ai_model || "not selected"}</strong></div>
                <div><Users size={18} /><span>Other nodes</span><strong>{otherPeers}</strong></div>
              </div>
              <div className="tour-honesty-callout">
                <Clock3 size={18} />
                <p>
                  <strong>What runs in the background:</strong> the node daemon stays online and
                  the discovery agent reviews its default public catalog about every 30 minutes.
                  Opening Recommendations or pressing Refresh shows the latest local ranking.
                </p>
              </div>
            </>
          ) : null}

          {step === 1 ? (
            <>
              <span className="eyebrow">Recommendation readiness</span>
              <h1 id="tour-title">Real content starts automatically</h1>
              <p className="tour-lead">
                Ryn should never leave you wondering whether an agent is still working.
              </p>
              <div className="tour-timing-list">
                <div>
                  <Chip tone="ok">Automatic</Chip>
                  <strong>Default public recommendations</strong>
                  <p>
                    Ryn begins with real items from a broad public catalog spanning YouTube,
                    Reddit, research, technology news, podcasts, audiobooks, and visual work. No
                    account, API key, or preference form is required.
                  </p>
                </div>
                <div>
                  <Chip tone="info">Usually seconds</Chip>
                  <strong>Your Daily Digest</strong>
                  <p>
                    Feedback and written direction reshape ranking immediately. Adding a YouTube
                    channel, subreddit, or RSS feed is optional and simply expands the catalog.
                  </p>
                </div>
                <div>
                  <Chip tone="muted">No fixed wait</Chip>
                  <strong>Real mesh recommendations</strong>
                  <p>
                    These arrive only after another connected node publishes content. With
                    {otherPeers ? ` ${otherPeers} other node${otherPeers === 1 ? "" : "s"} visible` : " no other nodes visible"},
                    there may be nothing to rank yet.
                  </p>
                </div>
              </div>
            </>
          ) : null}

          {step === 2 ? (
            <>
              <span className="eyebrow">Personal assistant stage</span>
              <h1 id="tour-title">What works today</h1>
              <p className="tour-lead">These capabilities are implemented in this build.</p>
              <div className="tour-capability-grid">
                <Capability icon={Newspaper} title="Daily Digest">
                  Automatically review public sources, then expand with optional RSS, YouTube,
                  Reddit, saved links, and watched pages.
                </Capability>
                <Capability icon={Sparkles} title="Search & Ask">
                  Ask the selected model about evidence already visible to your local node.
                </Capability>
                <Capability icon={Compass} title="Recommendation profile">
                  Choose topics and platforms, write directions, and train ranking with More,
                  Less, and Hide.
                </Capability>
                <Capability icon={MessageCircle} title="Encrypted peer chat">
                  Exchange end-to-end encrypted messages and small attachments once peers are
                  connected.
                </Capability>
                <Capability icon={Bot} title="Local model control">
                  Detect and choose an Ollama model for private briefings, summaries, and Search
                  & Ask without a required cloud account.
                </Capability>
                <Capability icon={ShieldCheck} title="Signed publishing">
                  Publish a local file through the node so its manifest, provenance, and content
                  identity can be verified by peers.
                </Capability>
              </div>
            </>
          ) : null}

          {step === 3 ? (
            <>
              <span className="eyebrow">From one node to a friend mesh</span>
              <h1 id="tour-title">Why invite 2–5 trusted people?</h1>
              <p className="tour-lead">
                A small friend mesh adds human context and useful machines without requiring a
                public social network.
              </p>
              <div className="tour-two-column">
                <div>
                  <h2>Available with connected peers</h2>
                  <ul className="tour-status-list">
                    <StatusLine available>Discover and inspect signed peer identities.</StatusLine>
                    <StatusLine available>Exchange encrypted messages and attachments.</StatusLine>
                    <StatusLine available>Publish and fetch signed, provenance-tracked content.</StatusLine>
                    <StatusLine available>Record validated credit and serve receipts.</StatusLine>
                  </ul>
                </div>
                <div>
                  <h2>Friend Mesh roadmap</h2>
                  <ul className="tour-status-list">
                    <StatusLine available={false}>One-click invite links and QR joining.</StatusLine>
                    <StatusLine available={false}>Friend-attributed items inside the Daily Digest.</StatusLine>
                    <StatusLine available={false}>Safe multi-user VPN/egress sharing.</StatusLine>
                    <StatusLine available={false}>Agent-to-agent jobs within owner-approved budgets.</StatusLine>
                  </ul>
                </div>
              </div>
            </>
          ) : null}

          {step === 4 ? (
            <>
              <span className="eyebrow">Credits and trust</span>
              <h1 id="tour-title">Reputation today—not money</h1>
              <p className="tour-lead">
                Your current node score is <strong>{self?.credits?.toLocaleString() ?? "0"}</strong> credits.
                Credits record validated contribution and help rank trust and distribution.
              </p>
              <div className="tour-credit-card">
                <Coins size={28} />
                <div>
                  <h2>What credits do now</h2>
                  <p>
                    Signed ledger events represent useful work such as serving content. They are
                    non-transferable reputation: they cannot be bought, sold, redeemed, or
                    promised a monetary value.
                  </p>
                </div>
              </div>
              <div className="tour-honesty-callout">
                <CircleDashed size={18} />
                <p>
                  <strong>Planned, not guaranteed:</strong> credits may meter useful node services
                  and influence discovery. Transferability would only be considered after real
                  utility, anti-abuse hardening, and legal review.
                </p>
              </div>
            </>
          ) : null}

          {step === 5 ? (
            <>
              <span className="eyebrow">Choose your first useful action</span>
              <h1 id="tour-title">Make Ryn yours</h1>
              <p className="tour-lead">
                There is no setup queue to wait for. Pick a path now; you can reopen this guide
                from the question-mark button in the top bar.
              </p>
              <div className="tour-action-grid">
                <button type="button" onClick={() => void finish("/recommendations")}>
                  <Sparkles size={22} />
                  <strong>Choose my interests</strong>
                  <span>Set topics, platforms, and written direction.</span>
                  <ArrowRight size={17} />
                </button>
                <button type="button" onClick={() => void finish("/digest")}>
                  <Newspaper size={22} />
                  <strong>Enjoy recommended content</strong>
                  <span>Open the live feed; add personal sources only if you want to.</span>
                  <ArrowRight size={17} />
                </button>
                <button type="button" onClick={() => void finish("/peers")}>
                  <Users size={22} />
                  <strong>Inspect the mesh</strong>
                  <span>See connected nodes, trust, services, and credits.</span>
                  <ArrowRight size={17} />
                </button>
              </div>
            </>
          ) : null}
        </div>

        <footer className="tour-footer">
          <button
            className="tour-skip"
            type="button"
            disabled={finishing}
            onClick={() => void finish()}
          >
            {step === steps - 1 ? "Finish on Home" : "Skip tour"}
          </button>
          <div className="button-row">
            {step > 0 ? <Button onClick={() => setStep((value) => value - 1)}>Back</Button> : null}
            {step < steps - 1 ? (
              <Button variant="primary" onClick={() => setStep((value) => value + 1)}>
                Next <ArrowRight size={15} />
              </Button>
            ) : null}
          </div>
        </footer>
      </section>
    </div>
  );
}
