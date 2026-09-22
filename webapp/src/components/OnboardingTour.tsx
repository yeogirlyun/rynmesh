import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { ArrowRight, Check } from "lucide-react";
import { DeviceArt, Modal, ServiceArt } from "../personal/components";
import type { NodeSettings, NodeStatus, Peer } from "../domain/types";

export const ONBOARDING_VERSION = 2;
interface OnboardingTourProps {
  node: NodeStatus;
  settings: NodeSettings;
  peers: Peer[];
  onComplete: () => Promise<void>;
  onClose: () => void;
}
export default function OnboardingTour({ peers, onComplete, onClose }: OnboardingTourProps) {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const connected = peers.filter(peer => !peer.isSelf).length;
  async function finish(route = "/") {
    setBusy(true);
    setError("");
    try { await onComplete(); navigate(route); }
    catch (error) { setError(error instanceof Error ? error.message : "Could not save. Please try again."); }
    finally { setBusy(false); }
  }
  return <Modal title="Welcome to your space" onClose={onClose}>
    <div className="pf-welcome-progress" aria-label={`Step ${step + 1} of 3`}>
      {[0, 1, 2].map(index => <span key={index} className={index <= step ? "active" : ""} />)}
    </div>
    <div className="pf-welcome-art">
      {step === 1 ? <ServiceArt kind="ai" /> : <DeviceArt kind={step === 2 ? "laptop" : "desktop"} />}
    </div>
    <h3 className="pf-welcome-title">{["Your devices, working together", "Use the power you already have", "Start with your own devices"][step]}</h3>
    <p className="pf-welcome-copy">{[
      "Ryn is running on this computer. Give it a name, add a note, and connect it to the devices you use every day.",
      "Run an AI model on your powerful home computer, then use it from another connected device. Services shows which models are ready.",
      "Open Devices to discover connections. Each service controls its own access. You can also explore content and message connected devices."
    ][step]}</p>
    <div className="pf-welcome-status"><Check size={15} /> This device is ready <span>{connected} other devices found</span></div>
    {error && <p role="alert" className="pf-form-error">{error}</p>}
    <div className="pf-dialog-actions">
      <button className="pf-text-button" disabled={busy} onClick={() => void finish()}>Skip introduction</button>
      {step > 0 && <button className="pf-button" onClick={() => setStep(step - 1)}>Back</button>}
      <button className="pf-button primary" disabled={busy} onClick={() => step < 2 ? setStep(step + 1) : void finish("/devices")}>
        {busy ? "Saving…" : step < 2 ? "Continue" : "Open my devices"}<ArrowRight size={16} />
      </button>
    </div>
  </Modal>;
}
