import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useAppContext } from "../appContext";
import type { DevicePair } from "../domain/deviceSync";
import { deviceErasure, erasureCategories, erasureCopies, type ErasureCategory, type ErasureReview, type ErasureStatus } from "../domain/deviceErasure";
import { Button, Panel } from "./ui";

const labels: Record<string, string> = { queued: "Waiting to contact device", awaiting_owner: "Waiting for owner review on that device", approved: "Owner approved; completion not confirmed", partial: "Local cleanup incomplete", confirmed: "Selected node-managed copies confirmed", rejected: "Owner declined; no completion confirmed", policy_changed: "Pairing or permissions changed", unconfirmed: "Device unavailable or receipt unconfirmed" };
const newId = () => crypto.randomUUID().replaceAll("-", "");
const exclusions = "Browser copies and exports, system backups, independently saved friend copies, and data created after the reviewed plan are excluded.";

export default function DeviceErasurePanel({ devices }: { devices: DevicePair[] }) {
  const { confirm } = useAppContext();
  const [status, setStatus] = useState<ErasureStatus | null>(null);
  const [category, setCategory] = useState<ErasureCategory>("reading");
  const [targets, setTargets] = useState<string[]>([]);
  const [review, setReview] = useState<ErasureReview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stale, setStale] = useState(false);
  const requestId = useRef(newId());
  const mounted = useRef(true);
  const load = useCallback(async () => {
    const result = await deviceErasure.status();
    if (mounted.current) { setStatus(result); setStale(false); }
  }, []);
  useEffect(() => {
    mounted.current = true;
    let running = false;
    const poll = async () => {
      if (running) return;
      running = true;
      try { await load(); } catch { if (mounted.current) setStale(true); }
      finally { running = false; }
    };
    void poll(); const timer = window.setInterval(() => void poll(), 5000);
    return () => { mounted.current = false; window.clearInterval(timer); };
  }, [load]);
  const run = async (value: object) => {
    setBusy(true); setError("");
    try {
      await deviceErasure.action(value);
      if (!mounted.current) return;
      if ((value as { action?: string }).action === "begin") { requestId.current = newId(); setTargets([]); }
      setReview(null); await load();
    }
    catch (cause) { if (mounted.current) setError(cause instanceof Error ? cause.message : "Could not confirm cleanup."); }
    finally { if (mounted.current) setBusy(false); }
  };
  const valid = targets.length > 0 && targets.every((id) => devices.some((device) => device.id === id && device.status === "active"));
  return <Panel><section aria-labelledby="device-cleanup-title">
    <h2 id="device-cleanup-title">Cleanup on your other devices</h2>
    <p>Request cleanup of one whole category on selected computers you own. Each receiving owner must review that computer's exact local plan and approve it there. Sync permission alone never deletes data.</p>
    <p>{exclusions} Existing local cleanup receipts keep their original meaning; only the requests below track other devices.</p>
    {stale ? <p role="status">Cleanup status is out of date. Refresh before relying on it.</p> : null}
    {error ? <p role="alert">{error}</p> : null}
    <label>Cleanup category<select value={category} disabled={busy} onChange={(event) => { setCategory(event.target.value as ErasureCategory); requestId.current = newId(); }}>
      {Object.entries(erasureCategories).map(([id, name]) => <option value={id} key={id}>{name}</option>)}
    </select></label>
    <p>{erasureCopies[category]}</p>
    <fieldset disabled={busy}><legend>Request from these owned devices</legend>{devices.filter((device) => device.status === "active").map((device) => <label key={device.id}>
      <input type="checkbox" checked={targets.includes(device.id)} onChange={(event) => { setTargets((prior) => event.target.checked ? [...prior, device.id] : prior.filter((id) => id !== device.id)); requestId.current = newId(); }} />{device.device.name}
    </label>)}</fieldset>
    <Button disabled={busy || stale || !valid} onClick={() => {
      const saved = { action: "begin", id: requestId.current, category, pair_ids: [...targets] };
      confirm({ title: "Request cleanup on these devices?", body: `${erasureCategories[category]} on ${targets.map((id) => devices.find((device) => device.id === id)!.device.name).join(", ")}. Each device reviews ALL records in that category, including records absent from this computer. This sends a proposal only; approve cleanup separately on each target. ${exclusions}`,
        risk: "high", confirmLabel: "Send cleanup proposal", onConfirm: () => run(saved) });
    }}>Review cleanup request</Button>{" "}
    <Button disabled={busy} onClick={() => void load().catch(() => setStale(true))}>Refresh cleanup status</Button>
    {status?.outgoing.map((job) => <article key={job.id} aria-label="Outgoing cleanup"><h3>{erasureCategories[job.category]}</h3>
      <p>{!stale && job.remote_confirmed ? "All originally selected devices returned a verified completion receipt for their approved plans." : "Completion across the selected devices is not currently confirmed."}</p>
      <ul>{job.targets.map((target) => <li key={target.pair_id}>{target.name}: {labels[target.state] ?? "Unconfirmed"}
        {target.completed_at ? ` · Receipt ${new Date(target.completed_at * 1000).toLocaleString()}` : ""}
        {!['confirmed', 'rejected'].includes(target.state) ? <Button disabled={busy} onClick={() => void run({ action: "exchange", id: job.id, pair_id: target.pair_id })}>Check {target.name}</Button> : null}
      </li>)}</ul>
    </article>)}
    <h3>Requests for this computer</h3>
    {status?.incoming.map((row) => <article key={row.id} aria-label="Incoming cleanup"><p>{row.name} requests: {erasureCategories[row.category]} · {labels[row.state] ?? row.state}</p>
      {!row.available ? <p>Pairing or permissions changed. This proposal cannot authorize cleanup.</p> : row.state === "awaiting_owner" ? <Button disabled={busy || stale} onClick={() => {
        setBusy(true); setError("");
        void deviceErasure.preview(row.id).then((result) => { if (mounted.current) setReview(result); })
          .catch((cause: Error) => { if (mounted.current) setError(cause.message); }).finally(() => { if (mounted.current) setBusy(false); });
      }}>Review this computer's copies</Button> : ['approved', 'partial'].includes(row.state) ? <Button disabled={busy || stale} onClick={() => void run({ action: "resume", id: row.id })}>Continue approved cleanup</Button> : null}
      {row.available && !['confirmed', 'rejected'].includes(row.state) ? <Button disabled={busy} onClick={() => void run({ action: "reject", id: row.id })}>Decline request</Button> : null}
    </article>)}
    {review ? <section aria-label="Local cleanup review"><h3>Review all {erasureCategories[review.category]} on this computer</h3>
      <p>{erasureCopies[review.category]}</p>
      <ul>{Object.entries(review.counts).map(([key, value]) => <li key={key}>{key.replaceAll("_", " ")}: {String(value)}</li>)}</ul>
      <p>This clears the reviewed local category, not just records copied from the requesting computer. {exclusions} Changes after review require a new plan; a partial cleanup cannot be reported as complete.</p>
      <Button disabled={busy || stale} onClick={() => {
        const saved = review;
        confirm({ title: "Erase these reviewed copies on this computer?", body: `Delete the reviewed ${erasureCategories[saved.category]}. ${exclusions} This cannot be undone here.`, risk: "high", confirmLabel: "Erase reviewed local copies",
          onConfirm: () => run({ action: "approve", id: saved.id, review_token: saved.review_token }) });
      }}>Confirm local cleanup</Button>
    </section> : null}
    <p>Declining after cleanup starts does not restore erased copies. Use <Link to="/settings">Privacy settings</Link> to inspect unfinished local cleanup and any changed backup files before continuing.</p>
  </section></Panel>;
}
