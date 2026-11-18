import { state, now, act, again, refresh } from "./api.js";
import { T, V, zh, F, FAIL_EN, presetLabel, roleLabel } from "./i18n.js";
import { h } from "./dom.js";

export function ago(ts) {
  const diff = now() - ts;
  if (diff < 60) return T.ago.now;
  if (diff < 3600) return T.ago.min(Math.floor(diff / 60));
  const d = new Date(ts * 1000), today = new Date((now()) * 1000);
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === today.toDateString()) return hm;
  const y = new Date(today); y.setDate(y.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return `${T.ago.yesterday} ${hm}`;
  return `${d.getMonth() + 1}-${d.getDate()} ${hm}`;
}
export const sizeLabel = (job) => {
  const p = state.me?.purposes?.[job.purpose];
  return p ? (zh ? `${p.label} ${p.ratio}` : `${p.ratio}`) + ` · ${job.width} × ${job.height}` : `${T.custom} · ${job.width} × ${job.height}`;
};
const failureText = (job) => zh ? (job.failure_text || T.errors.default) : (FAIL_EN[job.failure] || T.errors.default);


export function jobFields(job) {
  const fields = [];
  if (job.scene) fields.push([F.scene, presetLabel(job.scene, state.me.scenes[job.scene])]);
  for (const key of ["title", "subtitle", "badge"]) {
    if (job.copy?.[key]) fields.push([F[key], job.copy[key]]);
  }
  if (job.style) fields.push([F.style, presetLabel(job.style, state.me.styles[job.style])]);
  (job.reference_roles || []).forEach((role, index) => fields.push([`${F.image} ${index + 1}`, roleLabel(role)]));
  return fields;
}
function card(job) {
  const meta = [];
  let top;
  if (job.state === "queued") {
    top = h("div", { class: "thumb queued" }, h("strong", {}, T.queued(job.position || 1)),
      h("span", { class: "muted" }, T.wait(job.eta_seconds || 0)));
    meta.push(h("div", { class: "row" }, h("span", { class: "muted" }, `${sizeLabel(job)} · ${ago(job.created_at)}`),
      h("button", { type: "button", class: "link danger", onclick: () => act(job, "cancel") }, T.cancel)));
  } else if (job.state === "running") {
    top = h("div", { class: "thumb running" }, h("strong", {}, T.running(T.seconds(Math.max(0, now() - job.started_at)))),
      h("div", { class: "bar" }, h("span")), h("span", { class: "muted" }, T.runningTip));
    meta.push(h("div", { class: "row" }, h("span", { class: "muted" }, sizeLabel(job)),
      h("button", { type: "button", class: "link danger", onclick: () => act(job, "cancel") }, T.cancel)));
  } else if (job.state === "succeeded") {
    top = h("button", { type: "button", class: job.transparent ? "thumb alpha-preview" : "thumb", onclick: () => { location.hash = `#/job/${job.id}`; } },
      h("img", { src: `/api/jobs/${job.id}/${job.transparent ? "image" : "thumb"}`, alt: job.brief || job.copy?.title || T.finished, loading: "lazy" }));
    meta.push(h("div", { class: "row" }, h("span", { class: "ok" }, T.finished), h("span", { class: "muted" }, `${sizeLabel(job)} · ${ago(job.finished_at)}`)),
      h("div", { class: "actions" }, h("a", { class: "ghost", href: `/api/jobs/${job.id}/image`, download: `ryn-${job.id.slice(0, 8)}.png` }, T.download),
        h("button", { type: "button", class: "ghost", onclick: () => again(job) }, T.again),
        h("button", { type: "button", class: "ghost", onclick: () => { location.hash = `#/job/${job.id}/edit`; } }, T.edit)));
  } else {
    const failed = job.state === "failed";
    top = h("div", { class: `thumb ${failed ? "failed" : ""}` }, h("strong", {}, failed ? T.failed : T.cancelled),
      failed ? h("span", {}, failureText(job)) : "");
    meta.push(h("div", { class: "row" }, h("span", { class: "muted" }, `${sizeLabel(job)} · ${ago(job.created_at)}`),
      h("button", { type: "button", class: "link", onclick: () => act(job, "retry") }, T.retry)));
  }
  return h("article", { class: `card ${job.state}` }, top, h("div", { class: "meta" }, h("div", { class: "brief", title: job.brief }, job.brief),
    job.derived_from && h("a", {class: "origin-link", href: `#/job/${job.derived_from}`}, V.source, " · ", job.derived_from.slice(0, 8)),
    h("dl", { class: "job-fields" }, ...jobFields(job).flatMap(([name, value]) => [h("dt", {}, name), h("dd", {}, value)])), meta));
}

export function renderGallery() {
  const gallery = document.getElementById("gallery");
  if (!gallery) return;
  gallery.querySelector("#tabs").replaceChildren(...[["all", T.all], ["active", T.active], ["done", T.done]].map(([id, label]) =>
    h("button", { type: "button", "aria-pressed": String(state.filter === id), onclick: async () => { state.filter = id; await refresh(); } }, label)));
  gallery.querySelector("#grid").replaceChildren(...(state.jobs.length ? state.jobs.map(card)
    : [h("p", { class: "empty", style: "grid-column:1/-1" }, T.empty)]));
}
