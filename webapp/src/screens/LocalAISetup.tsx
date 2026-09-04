import { Bot, Check, Cpu, HardDrive, RefreshCw, ShieldCheck, Sparkles, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import { Button, Chip, LoadingPanel, PageHeader, Panel } from "../components/ui";
import type { LLMSetupJob, LocalAIProfile, LocalAIReadiness, LocalAIRecommendations } from "../domain/nodeClient";
import styles from "./LocalAISetup.module.css";

const activeStates = new Set(["queued", "running", "cancelling"]);

function mib(value: number) {
  return value >= 1024 ? `${(value / 1024).toFixed(1)} GB` : `${Math.round(value)} MB`;
}

function stageLabel(stage: string) {
  const labels: Record<string, string> = {
    queued: "Waiting to start",
    hardware: "Checking this device",
    verify_manifest: "Checking reviewed manifests",
    download_model: "Downloading the model",
    download_runtime: "Downloading the private runtime",
    extract_runtime: "Preparing the runtime",
    start_runtime: "Starting local AI",
    health_check: "Checking readiness",
    self_test: "Creating the first private answer",
    completed: "Ready",
    cancelling: "Stopping safely",
  };
  return labels[stage] ?? "Preparing local AI";
}

export default function LocalAISetup() {
  const { client, notify } = useAppContext();
  const [catalog, setCatalog] = useState<LocalAIRecommendations | null>(null);
  const [readiness, setReadiness] = useState<LocalAIReadiness | null>(null);
  const [job, setJob] = useState<LLMSetupJob | null>(null);
  const [selected, setSelected] = useState<LocalAIProfile["id"] | "">("");
  const [accepted, setAccepted] = useState(false);
  const [answer, setAnswer] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);

  const load = useCallback(async () => {
    const current = ++generation.current;
    const [nextCatalog, nextReadiness, nextJob] = await Promise.all([
      client.getLocalAIRecommendations(),
      client.getLocalAIReadiness(),
      client.getLLMSetupStatus(),
    ]);
    if (current !== generation.current) return;
    setCatalog(nextCatalog);
    setReadiness(nextReadiness);
    setJob(nextJob);
    setSelected((value) => value || nextCatalog.recommended_profile || "");
  }, [client]);

  useEffect(() => {
    void load().catch(() => setError("Ryn could not inspect local AI readiness. Other features remain available."));
    return () => { generation.current += 1; };
  }, [load]);

  useEffect(() => {
    if (!job?.job_id || !activeStates.has(job.state)) return;
    let cancelled = false;
    let timer = 0;
    const jobId = job.job_id;
    const poll = async () => {
      try {
        const next = await client.getLLMSetupStatus();
        if (cancelled || next.job_id !== jobId) return;
        setJob(next);
        if (next.state === "succeeded") {
          setReadiness(await client.getLocalAIReadiness());
          notify("ok", "Private local AI passed its first-answer test");
          return;
        }
        if (["failed", "cancelled"].includes(next.state)) return;
      } catch {
        if (!cancelled) setError("Setup is still running, but status is temporarily unavailable.");
      }
      if (!cancelled) timer = window.setTimeout(poll, document.visibilityState === "visible" ? 1000 : 5000);
    };
    timer = window.setTimeout(poll, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [client, job?.job_id, job?.state, notify]);

  const profile = useMemo(
    () => catalog?.profiles.find((item) => item.id === selected) ?? null,
    [catalog, selected],
  );

  const start = async () => {
    if (!profile || !accepted) return;
    setBusy(true);
    setError("");
    try {
      const next = await client.startLLMSetup({ profile: profile.id, accept_license: true });
      setJob(next);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Local AI setup could not start.");
    } finally {
      setBusy(false);
    }
  };

  const retry = async () => {
    if (!job?.job_id) return;
    setBusy(true);
    setError("");
    try {
      setJob(await client.retryLLMSetup(job.job_id));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Retry could not start.");
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    if (!job?.job_id) return;
    setBusy(true);
    try {
      setJob(await client.cancelLLMSetup(job.job_id));
    } finally {
      setBusy(false);
    }
  };

  const runFirstAnswer = async () => {
    setBusy(true);
    setError("");
    try {
      const result = await client.testLocalAI();
      setAnswer(result.answer);
    } catch {
      setError("The model is installed but the first answer failed. Retry the readiness test.");
    } finally {
      setBusy(false);
    }
  };

  if (!catalog || !readiness) return <LoadingPanel />;

  if (readiness.ready) {
    return (
      <div className="screen-stack">
        <PageHeader eyebrow="Private AI" title="Local AI is ready" context="The model runs on this device and listens only on loopback." />
        <Panel className={styles.complete}>
          <span className={styles.completeIcon}><Check size={34} /></span>
          <Chip tone="ok">private · local · ready</Chip>
          <h2>{readiness.model_alias}</h2>
          <p>The installer verified the model and runtime, started it locally, and completed a real inference self-test.</p>
          {answer ? <blockquote className={styles.answer}>{answer}</blockquote> : null}
          <div className="button-row">
            <Button variant="primary" icon={Sparkles} onClick={() => void runFirstAnswer()} disabled={busy}>
              {busy ? "Asking…" : answer ? "Ask the test question again" : "Get my first answer"}
            </Button>
            <Button onClick={() => window.location.assign("/ask")}>Continue to Ask Ryn</Button>
          </div>
          <small>The fixed readiness question is not added to your conversations or recommendation profile.</small>
        </Panel>
      </div>
    );
  }

  const installing = Boolean(job?.job_id && activeStates.has(job.state));
  const failed = Boolean(job?.job_id && ["failed", "cancelled"].includes(job.state));

  return (
    <div className="screen-stack">
      <PageHeader
        eyebrow="Private AI"
        title="Enable AI on this device"
        context="Ryn checks your device, recommends one reviewed model, and verifies a real first answer—without a terminal."
        actions={<Button icon={RefreshCw} onClick={() => void load()}>Check again</Button>}
      />

      <div className={styles.deviceGrid}>
        <Panel className={styles.deviceCard}>
          <Cpu size={22} />
          <span>Available memory</span>
          <strong>{mib(catalog.memory_available_mib)}</strong>
        </Panel>
        <Panel className={styles.deviceCard}>
          <HardDrive size={22} />
          <span>Free disk</span>
          <strong>{mib(catalog.disk_available_mib)}</strong>
        </Panel>
        <Panel className={styles.deviceCard}>
          <ShieldCheck size={22} />
          <span>Runtime</span>
          <strong>{catalog.runtime_support.managed ? "Reviewed native" : "Not published"}</strong>
        </Panel>
      </div>

      {installing || failed ? (
        <Panel className={styles.progressPanel}>
          <div className={styles.progressHead}>
            <div><span className="eyebrow">Installation</span><h2>{stageLabel(job?.stage ?? "")}</h2></div>
            <Chip tone={failed ? "danger" : "info"}>{job?.progress ?? 0}%</Chip>
          </div>
          <progress value={job?.progress ?? 0} max={100} aria-label="Local AI setup progress" />
          <p role="status" aria-live="polite">{job?.message}</p>
          {job?.bytes_total ? <small>{mib((job.bytes_done ?? 0) / 1024 / 1024)} of {mib(job.bytes_total / 1024 / 1024)} downloaded · attempt {job.attempt ?? 1}</small> : null}
          <div className="button-row">
            {installing && job?.cancelable !== false ? <Button icon={X} onClick={() => void cancel()} disabled={busy}>Cancel safely</Button> : null}
            {failed && job?.retryable ? <Button variant="primary" icon={RefreshCw} onClick={() => void retry()} disabled={busy}>Retry and resume</Button> : null}
          </div>
        </Panel>
      ) : (
        <Panel>
          <div className={styles.recommendHead}>
            <div><span className="eyebrow">Recommended for this device</span><h2>{profile?.model_id ?? "No reviewed profile available"}</h2></div>
            {profile ? <Chip tone={profile.supported ? "ok" : "warn"}>{profile.id}</Chip> : null}
          </div>
          {profile ? (
            <>
              <p>{profile.recommendation_reason}</p>
              <div className={styles.facts}>
                <span><b>{mib(profile.download_mib)}</b> download</span>
                <span><b>{mib(profile.estimated_memory_mib)}</b> estimated memory</span>
                <span><b>{profile.context_window}</b> context</span>
                <span><b>CPU-safe</b> default</span>
              </div>
              {!profile.supported ? <p className={styles.warning}>{profile.unsupported_reason}</p> : null}
              <details className={styles.otherProfiles}>
                <summary>Compare model profiles</summary>
                {catalog.profiles.map((item) => (
                  <label key={item.id} className={!item.supported ? styles.disabledProfile : ""}>
                    <input type="radio" name="profile" value={item.id} checked={selected === item.id}
                      disabled={!item.supported || installing} onChange={() => setSelected(item.id)} />
                    <span><b>{item.id}</b><small>{item.model_id} · {mib(item.download_mib)}</small></span>
                  </label>
                ))}
              </details>
              <label className={styles.license}>
                <input type="checkbox" checked={accepted} onChange={(event) => setAccepted(event.target.checked)} />
                <span>I accept the model and runtime licenses and understand that about {mib(profile.estimated_disk_mib)} of free space is required.</span>
              </label>
              <div className="button-row">
                <Button variant="primary" icon={Bot} onClick={() => void start()} disabled={!profile.supported || !accepted || busy}>
                  {busy ? "Starting…" : "Install and verify local AI"}
                </Button>
                <Link to="/services/manage">Advanced model connection</Link>
              </div>
            </>
          ) : (
            <div className={styles.warning}>
              No reviewed native runtime fits this platform. Existing Ollama or loopback APIs can still be connected under Advanced model connection.
            </div>
          )}
        </Panel>
      )}

      {error ? <p className={styles.error} role="alert">{error}</p> : null}
    </div>
  );
}
