import { BadgeCheck, FileText, ScanLine, ShieldCheck, UploadCloud } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, Hash, KV, PageHeader, Panel, SafetyBadge } from "../components/ui";
import type { ContentKind, PublishDraft, PublishPrepResult } from "../domain/types";

const steps = [
  "Select",
  "Describe",
  "Preview",
  "Safety",
  "Manifest",
  "Review",
  "Publish",
] as const;

export default function Publish() {
  const { client, confirm, notify } = useAppContext();
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<PublishDraft>({
    path: "",
    kind: "document",
    title: "",
    description: "",
    tags: [],
    model_used: "",
    visibility: "network",
  });
  const [tagText, setTagText] = useState("draft, rynmesh");
  const [prep, setPrep] = useState<PublishPrepResult | null>(null);
  const [running, setRunning] = useState(false);

  useEffect(() => {
    let active = true;
    void client.getSettings().then((settings) => {
      if (active) {
        setDraft((current) => ({ ...current, visibility: settings.publish_visibility }));
      }
    });
    return () => {
      active = false;
    };
  }, [client]);

  const next = async () => {
    if (step === 1 || step === 2 || step === 3 || step === 4) {
      setRunning(true);
      const prepared = await client.preparePublish({
        ...draft,
        tags: tagText
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
      });
      setPrep(prepared);
      window.setTimeout(() => {
        setRunning(false);
        setStep((current) => Math.min(current + 1, steps.length - 1));
      }, 260);
      return;
    }
    setStep((current) => Math.min(current + 1, steps.length - 1));
  };

  const publish = () =>
    confirm({
      title: "Publish to the network?",
      body: "Publishing signs the manifest and makes this content discoverable according to your node settings. This action should be deliberate.",
      risk: "high",
      confirmLabel: "Publish",
      details: [
        { label: "Title", value: draft.title || "Untitled" },
        { label: "Visibility", value: draft.visibility },
        { label: "Manifest", value: prep?.manifest_hash ?? "not prepared" },
        { label: "Safety", value: prep?.safety.outcome ?? "pending" },
      ],
      onConfirm: async () => {
        const result = await client.confirmPublish(prep?.draft_id ?? "draft_unprepared");
        notify(result.ok ? "ok" : "danger", result.ok ? "Published through local node" : "Publish failed");
      },
    });

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Publish"
        title="Prepare content for Rynmesh"
        context="The node hashes, scans, signs provenance, validates the manifest, and publishes only after confirmation."
        actions={<Chip tone="warn">high-risk final action</Chip>}
      />
      <div className="stepper">
        {steps.map((label, index) => (
          <div key={label} className={index < step ? "done" : index === step ? "current" : ""}>
            <span>{index < step ? <BadgeCheck size={15} /> : index + 1}</span>
            <b>{label}</b>
          </div>
        ))}
      </div>
      <div className="publish-grid">
        <Panel className="publish-step">{renderStep(step, draft, setDraft, tagText, setTagText, prep, running)}</Panel>
        <Panel title="Publish draft">
          <KV
            rows={[
              { label: "Path", value: draft.path || "not selected" },
              { label: "Kind", value: draft.kind },
              { label: "Title", value: draft.title || "Untitled" },
              { label: "Tags", value: tagText },
              { label: "Model used", value: draft.model_used || "none" },
              { label: "Visibility", value: draft.visibility },
              { label: "Content hash", value: prep ? <Hash value={prep.content_hash} /> : "pending" },
              { label: "Manifest", value: prep ? <Hash value={prep.manifest_hash} /> : "pending" },
              { label: "Safety", value: prep ? <SafetyBadge outcome={prep.safety.outcome} /> : "pending" },
            ]}
          />
        </Panel>
      </div>
      <footer className="publish-footer">
        <Button variant="ghost" disabled={step === 0} onClick={() => setStep((current) => Math.max(0, current - 1))}>
          Back
        </Button>
        {step < steps.length - 1 ? (
          <Button variant="primary" onClick={() => void next()} disabled={running}>
            {running ? "Running node work" : "Continue"}
          </Button>
        ) : (
          <Button variant="danger" icon={UploadCloud} onClick={publish}>
            Publish
          </Button>
        )}
      </footer>
    </div>
  );
}

function renderStep(
  step: number,
  draft: PublishDraft,
  setDraft: (draft: PublishDraft) => void,
  tagText: string,
  setTagText: (value: string) => void,
  prep: PublishPrepResult | null,
  running: boolean,
) {
  if (step === 0) {
    return (
      <div className="form-stack">
        <StepIntro icon={<FileText size={24} />} title="Select local content" body="Local files stay on this machine until you confirm publishing." />
        <label className="field">
          <span>Local path</span>
          <input value={draft.path} onChange={(event) => setDraft({ ...draft, path: event.target.value })} placeholder="/Users/me/content/report.pdf" />
        </label>
      </div>
    );
  }
  if (step === 1) {
    return (
      <div className="form-stack">
        <StepIntro icon={<FileText size={24} />} title="Describe the item" body="These fields become user-facing manifest metadata." />
        <label className="field">
          <span>Title</span>
          <input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
        </label>
        <label className="field">
          <span>Description</span>
          <textarea value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
        </label>
        <label className="field">
          <span>Content kind</span>
          <select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as ContentKind })}>
            {["video", "image", "audio", "document", "slides", "dataset", "code", "report", "package", "model"].map((kind) => (
              <option key={kind}>{kind}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Tags</span>
          <input value={tagText} onChange={(event) => setTagText(event.target.value)} />
        </label>
      </div>
    );
  }
  if (step >= 2 && step <= 4) {
    const labels = [
      ["Preview", "The node builds preview bytes and hashes them.", <ScanLine size={24} />],
      ["Safety", "The node runs local safety policy and signs a receipt.", <ShieldCheck size={24} />],
      ["Manifest", "The node builds provenance, signs the manifest, and validates it locally.", <BadgeCheck size={24} />],
    ] as const;
    const [title, body, icon] = labels[step - 2];
    return (
      <div className="node-work">
        <StepIntro icon={icon} title={title} body={body} />
        <div className="progress-track">
          <span style={{ width: running ? "68%" : prep ? "100%" : "14%" }} />
        </div>
        <Chip tone={prep && !running ? "ok" : "info"}>{running ? "Running" : prep ? "Done" : "Ready"}</Chip>
      </div>
    );
  }
  if (step === 5) {
    return (
      <div className="form-stack">
        <StepIntro icon={<BadgeCheck size={24} />} title="Review before publishing" body="Confirm that hashes, safety, provenance, and metadata match your intent." />
        <KV
          rows={[
            { label: "Content hash", value: prep ? <Hash value={prep.content_hash} /> : "pending" },
            { label: "Preview hash", value: prep ? <Hash value={prep.preview_hash} /> : "pending" },
            { label: "Provenance head", value: prep ? <Hash value={prep.provenance_head} /> : "pending" },
          ]}
        />
      </div>
    );
  }
  return (
    <div className="form-stack">
      <StepIntro icon={<UploadCloud size={24} />} title="Ready to publish" body="The next action signs and announces the item through your local Ryn node." />
      <Chip tone="danger">Explicit confirmation required</Chip>
    </div>
  );
}

function StepIntro({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  return (
    <div className="step-intro">
      {icon}
      <div>
        <h2>{title}</h2>
        <p>{body}</p>
      </div>
    </div>
  );
}
