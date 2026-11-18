import { T } from "./i18n.js";
import { h, toast } from "./dom.js";
import { state, again, submit, jobBody } from "./api.js";
import { ago, sizeLabel, jobFields } from "./gallery.js";

export function renderDetail(id, editing) {
  const job = state.jobs.find((j) => j.id === id);
  const main = document.getElementById("main");
  if (!job || job.state !== "succeeded") { location.hash = ""; return; }
  const editText = h("textarea", { id: "edit", rows: 3, maxlength: 2000, placeholder: T.editPh });
  main.replaceChildren(h("div", { class: "detail" },
    h("div", { class: "stage panel" }, h("img", { src: `/api/jobs/${job.id}/image`, alt: job.brief })),
    h("aside", {},
      h("button", { type: "button", class: "ghost", style: "align-self:flex-start", onclick: () => { location.hash = ""; } }, "← ", T.back),
      h("div", { class: "panel field" },
        h("div", { class: "actions" },
          h("a", { class: "primary", style: "display:inline-flex;align-items:center;text-decoration:none", href: `/api/jobs/${job.id}/image`, download: `ryn-${job.id.slice(0, 8)}.png` }, T.downloadPng),
          h("button", { type: "button", class: "ghost", onclick: () => again(job) }, T.again),
          job.brief && h("button", { type: "button", class: "ghost", onclick: async () => {
            try { await navigator.clipboard.writeText(job.brief); toast(T.copied); } catch { editText.value = job.brief; editText.select(); }
          } }, T.copy)),
        h("dl", { class: "info" },
          job.brief ? [h("dt", {}, T.info.brief), h("dd", {}, job.brief)] : "",
          ...jobFields(job).flatMap(([name, value]) => [h("dt", {}, name), h("dd", {}, value)]),
          job.words && !job.copy?.title ? [h("dt", {}, T.info.words), h("dd", {}, job.words)] : "",
          h("dt", {}, T.info.size), h("dd", {}, sizeLabel(job)),
          h("dt", {}, T.info.refs), h("dd", {}, T.refsCount(job.references)),
          h("dt", {}, T.info.made), h("dd", {}, `${ago(job.finished_at)} · ${T.seconds(job.seconds || 0)}`))),
      h("form", { class: "panel field", onsubmit: async (e) => {
        e.preventDefault();
        const brief = editText.value.trim();
        if (!brief) { editText.focus(); return; }
        const body = { ...jobBody(job), brief, source_job: job.id };
        if (await submit(body)) location.hash = "";
      } }, h("label", { class: "label", for: "edit" }, T.editTitle), editText,
        h("div", { class: "row" }, h("p", { class: "muted" }, T.editHint), h("button", { type: "submit", class: "primary" }, T.editSubmit))))));
  if (editing) editText.focus();
}
