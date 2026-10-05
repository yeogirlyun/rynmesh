import { tr, useUILanguage } from "../uiI18n";
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
  useUILanguage();
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
      try {
      const prepared = await client.preparePublish({
        ...draft,
        tags: tagText
          .split(",")
          .map((tag) => tag.trim())
          .filter(Boolean),
      });
      setPrep(prepared);
      setStep((current) => Math.min(current + 1, steps.length - 1));
      } catch (error) { notify("danger", error instanceof Error ? error.message : tr("Could not prepare this file.")); }
      finally { setRunning(false); }
      return;
    }
    setStep((current) => Math.min(current + 1, steps.length - 1));
  };

  const publish = () =>
    confirm({
      title: tr("Publish to the network?"),
      body: tr("Publishing signs the manifest and makes this content discoverable according to your node settings. This action should be deliberate."),
      risk: "high",
      confirmLabel: tr("Publish"),
      details: [
        { label: tr("Title"), value: draft.title || tr("Untitled") },
        { label: tr("Visibility"), value: tr(draft.visibility) },
        { label: tr("Manifest"), value: prep?.manifest_hash ?? tr("not prepared") },
        { label: tr("Safety"), value: tr(prep?.safety.outcome ?? "pending") },
      ],
      onConfirm: async () => {
        const result = await client.confirmPublish(prep?.draft_id ?? "draft_unprepared");
        notify(result.ok ? "ok" : "danger", result.ok ? tr("Published through local node") : tr("Publish failed"));
      },
    });

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow={tr("Publish")}
        title={tr("Publish content")}
        context={tr("Prepare a file, choose who can see it, and review before publishing.")}

      />
      <div className="stepper">
        {steps.map((label, index) => (
          <div key={label} className={index < step ? "done" : index === step ? "current" : ""}>
            <span>{index < step ? <BadgeCheck size={15} /> : index + 1}</span>
            <b>{tr(label)}</b>
          </div>
        ))}
      </div>
      <div className="publish-grid">
        <Panel className="publish-step">{renderStep(step, draft, setDraft, tagText, setTagText, prep, running)}</Panel>
        <Panel title={tr("Publish draft")}>
          <KV
            rows={[
              { label: tr("Path"), value: draft.path || tr("not selected") },
              { label: tr("Kind"), value: tr(draft.kind) },
              { label: tr("Title"), value: draft.title || tr("Untitled") },
              { label: tr("Tags"), value: tagText },
              { label: tr("Model used"), value: draft.model_used || tr("none") },
              { label: tr("Visibility"), value: tr(draft.visibility) },
              { label: tr("Content hash"), value: prep ? <Hash value={prep.content_hash} /> : tr("pending") },
              { label: tr("Manifest"), value: prep ? <Hash value={prep.manifest_hash} /> : tr("pending") },
              { label: tr("Safety"), value: prep ? <SafetyBadge outcome={prep.safety.outcome} /> : tr("pending") },
            ]}
          />
        </Panel>
      </div>
      <footer className="publish-footer">
        <Button variant="ghost" disabled={step === 0} onClick={() => setStep((current) => Math.max(0, current - 1))}>
          {tr("Back")}
        </Button>
        {step < steps.length - 1 ? (
          <Button variant="primary" onClick={() => void next()} disabled={running || (step === 0 && !draft.path.trim()) || (step === 1 && !draft.title.trim())}>
            {running ? tr("Running node work") : tr("Continue")}
          </Button>
        ) : (
          <Button variant="danger" icon={UploadCloud} onClick={publish}>
            {tr("Publish")}
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
        <StepIntro icon={<FileText size={24} />} title={tr("Select local content")} body={tr("Local files stay on this machine until you confirm publishing.")} />
        <label className="field">
          <span>{tr("Local path")}</span>
          <input value={draft.path} onChange={(event) => setDraft({ ...draft, path: event.target.value })} placeholder={tr("Full path to a file on this device")} />
        </label>
      </div>
    );
  }
  if (step === 1) {
    return (
      <div className="form-stack">
        <StepIntro icon={<FileText size={24} />} title={tr("Describe the item")} body={tr("These fields become user-facing manifest metadata.")} />
        <label className="field">
          <span>{tr("Title")}</span>
          <input value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} />
        </label>
        <label className="field">
          <span>{tr("Description")}</span>
          <textarea value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} />
        </label>
        <label className="field">
          <span>{tr("Content kind")}</span>
          <select value={draft.kind} onChange={(event) => setDraft({ ...draft, kind: event.target.value as ContentKind })}>
            {["video", "image", "audio", "document", "slides", "dataset", "code", "report", "package", "model"].map((kind) => (
              <option key={kind} value={kind}>{tr(kind)}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>{tr("Tags")}</span>
          <input value={tagText} onChange={(event) => setTagText(event.target.value)} />
        </label>
      </div>
    );
  }
  if (step >= 2 && step <= 4) {
    const labels = [
      [tr("Preview"), tr("The node builds preview bytes and hashes them."), <ScanLine size={24} />],
      [tr("Safety"), tr("The node runs local safety policy and signs a receipt."), <ShieldCheck size={24} />],
      [tr("Manifest"), tr("The node builds provenance, signs the manifest, and validates it locally."), <BadgeCheck size={24} />],
    ] as const;
    const [title, body, icon] = labels[step - 2];
    return (
      <div className="node-work">
        <StepIntro icon={icon} title={title} body={body} />
        <div className="progress-track">
          <span style={{ width: running ? "68%" : prep ? "100%" : "14%" }} />
        </div>
        <Chip tone={prep && !running ? "ok" : "info"}>{running ? tr("Running") : prep ? tr("Done") : tr("Ready")}</Chip>
      </div>
    );
  }
  if (step === 5) {
    return (
      <div className="form-stack">
        <StepIntro icon={<BadgeCheck size={24} />} title={tr("Review before publishing")} body={tr("Confirm that hashes, safety, provenance, and metadata match your intent.")} />
        <KV
          rows={[
            { label: tr("Content hash"), value: prep ? <Hash value={prep.content_hash} /> : tr("pending") },
            { label: tr("Preview hash"), value: prep ? <Hash value={prep.preview_hash} /> : tr("pending") },
            { label: tr("Provenance head"), value: prep ? <Hash value={prep.provenance_head} /> : tr("pending") },
          ]}
        />
      </div>
    );
  }
  return (
    <div className="form-stack">
      <StepIntro icon={<UploadCloud size={24} />} title={tr("Ready to publish")} body={tr("The next action signs and announces the item through your local Ryn node.")} />
      <Chip tone="danger">{tr("Explicit confirmation required")}</Chip>
    </div>
  );
}

function StepIntro({ icon, title, body }: { icon: ReactNode; title: string; body: string }) {
  useUILanguage();
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
