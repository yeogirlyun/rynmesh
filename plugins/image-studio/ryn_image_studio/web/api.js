import { T } from "./i18n.js";
import { toast } from "./dom.js";

export const state = { me: null, jobs: [], filter: "all", queueLength: 0, average: 60, offset: 0, timer: 0 };

export async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, { cache: "no-store", ...options }); }
  catch { throw new Error("network"); }
  const text = await response.text();
  let body = null;
  try { body = text ? JSON.parse(text) : null; } catch { body = null; }
  if (!response.ok) {
    const code = body?.code || (response.status === 502 ? "site_offline" : "default");
    throw new Error(code);
  }
  return body;
}
export const errorText = (err) => T.errors[err?.message] || T.errors.default;
export const now = () => Date.now() / 1000 + state.offset;

// ---- data ----
export async function loadJobs() {
  const params = state.filter === "all" ? "" : `?state=${state.filter}`;
  const body = await api(`/api/jobs${params}`);
  state.jobs = body.jobs; state.queueLength = body.queue_length; state.average = body.average_seconds || 60;
  state.offset = body.now - Date.now() / 1000;
}

export function schedule() {
  clearTimeout(state.timer);
  const busy = state.jobs.some((j) => j.state === "queued" || j.state === "running");
  state.timer = setTimeout(refresh, busy ? 3000 : 15000);
}

export async function refresh() {
  try { await loadJobs(); window.dispatchEvent(new Event("studio-jobs-loaded")); } catch (err) { if (err.message === "site_offline") toast(errorText(err)); }
  schedule();
}

export async function submit(body) {
  try {
    await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(T.sent);
    state.filter = state.filter === "done" ? "all" : state.filter;
    await refresh();
    return true;
  } catch (err) { toast(errorText(err)); return false; }
}

export async function act(job, action) {
  try { await api(`/api/jobs/${job.id}/${action}`, { method: "POST" }); await refresh(); }
  catch (err) { toast(errorText(err)); }
}

export function jobBody(job) {
  return { purpose: job.purpose, width: job.width, height: job.height, scene: job.scene, style: job.style,
    copy: job.copy, brief: job.brief, words: job.words, count: 1 };
}
export function again(job) {
  const body = jobBody(job);
  if (job.references) body.source_job = job.id;
  return submit(body);
}
