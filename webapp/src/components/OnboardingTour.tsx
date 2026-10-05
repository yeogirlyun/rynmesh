import { tr, useUILanguage } from "../uiI18n";
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
  useUILanguage();
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const connected = peers.filter(peer => !peer.isSelf).length;
  async function finish(route = "/") {
    setBusy(true);
    setError("");
    try { await onComplete(); navigate(route); }
    catch (error) { setError(error instanceof Error ? error.message : tr("Could not save. Please try again.")); }
    finally { setBusy(false); }
  }
  return <Modal title={tr("Welcome to your space")} onClose={onClose}>
    <div className="pf-welcome-progress" aria-label={tr("Step {{v0}} of 3", { v0: step + 1 })}>
      {[0, 1, 2].map(index => <span key={index} className={index <= step ? "active" : ""} />)}
    </div>
    <div className="pf-welcome-art">
      {step === 1 ? <ServiceArt kind="ai" /> : <DeviceArt kind={step === 2 ? "laptop" : "desktop"} />}
    </div>
    <h3 className="pf-welcome-title">{[tr("Your devices, working together"), tr("Use the power you already have"), tr("Start with your own devices")][step]}</h3>
    <p className="pf-welcome-copy">{[
      tr("Ryn is running on this computer. Give it a name, add a note, and connect it to the devices you use every day."),
      tr("Run an AI model on your powerful home computer, then use it from another connected device. Services shows which models are ready."),
      tr("Open Devices to discover connections. Each service controls its own access. You can also explore content and message connected devices.")
    ][step]}</p>
    <div className="pf-welcome-status"><Check size={15} /> {tr("This device is ready")} <span>{connected} {tr("other devices found")}</span></div>
    {error && <p role="alert" className="pf-form-error">{error}</p>}
    <div className="pf-dialog-actions">
      <button className="pf-text-button" disabled={busy} onClick={() => void finish()}>{tr("Skip introduction")}</button>
      {step > 0 && <button className="pf-button" onClick={() => setStep(step - 1)}>{tr("Back")}</button>}
      <button className="pf-button primary" disabled={busy} onClick={() => step < 2 ? setStep(step + 1) : void finish("/devices")}>
        {busy ? tr("Saving…") : step < 2 ? tr("Continue") : tr("Open my devices")}<ArrowRight size={16} />
      </button>
    </div>
  </Modal>;
}
