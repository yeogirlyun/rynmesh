import { jobBody, state, submit } from "./api.js";
import { h } from "./dom.js";
import { A, F, T, V } from "./i18n.js";

export function sizeVariants(job) {
  const scene = state.me.scenes[job.scene];
  if (!scene) return null;
  const options = (scene.sizes || [scene]).filter(size => size.width !== job.width || size.height !== job.height);
  if (!options.length) return null;
  const selected = new Set();
  const send = h("button", {type: "submit", class: "primary", disabled: true}, V.derive);
  const choices = options.map(({width, height}, index) => {
    const input = h("input", {type: "checkbox", onchange: () => {
      if (input.checked) selected.add(index);
      else selected.delete(index);
      for (const checkbox of form.querySelectorAll("input")) checkbox.disabled = !checkbox.checked && selected.size >= 6;
      send.disabled = !selected.size;
    }});
    return h("label", {class: "size-choice"}, input, `${width} × ${height}`);
  });
  const form = h("form", {class: "panel field", onsubmit: async e => {
    e.preventDefault();
    send.disabled = true;
    const sizes = [...selected].map(index => ({width: options[index].width, height: options[index].height}));
    if (await submit({sizes}, `/api/jobs/${job.id}/derive`)) location.hash = "";
    send.disabled = !selected.size;
  }}, h("h2", {}, V.derive), h("div", {class: "chips"}, ...choices), h("p", {class: "muted"}, V.limit), send);
  return form;
}

function editPanel(job, mode) {
  const note = h("textarea", {rows: 3, maxlength: 2000, placeholder: F.optional});
  const fields = {};
  const copyInputs = mode === "text" ? Object.entries({title: 40, subtitle: 60, badge: 20}).map(([key, limit]) => {
    fields[key] = h("input", {type: "text", maxlength: limit, value: job.copy?.[key] || (key === "title" ? job.words : "") || ""});
    return h("label", {class: "copy-input"}, F[key], fields[key]);
  }) : [];
  const send = h("button", {type: "submit", class: "primary"}, T.editSubmit);
  const dialog = h("dialog", {class: "asset-dialog edit-dialog", "aria-label": V[mode]},
    h("header", {class: "row"}, h("h2", {}, V[mode]),
      h("button", {type: "button", class: "ghost", onclick: () => dialog.close()}, A.close)),
    h("form", {class: "field", onsubmit: async e => {
      e.preventDefault();
      send.disabled = true;
      const body = {...jobBody(job), edit: mode, source_job: job.id, brief: note.value.trim()};
      if (mode === "text") {
        body.words = "";
        body.copy = Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value.trim()]));
      }
      if (await submit(body)) { dialog.close(); location.hash = ""; }
      send.disabled = false;
    }}, ...copyInputs, h("label", {class: "field"}, F.brief, note), send));
  dialog.addEventListener("close", () => dialog.remove(), {once: true});
  document.body.append(dialog);
  dialog.showModal();
  (fields.title || note).focus();
}

export function quickEdits(job) {
  return h("section", {class: "panel field"}, h("h2", {}, V.quick), h("div", {class: "actions"},
    ...["background", "text", "angle"].map(mode => h("button", {type: "button", class: "ghost",
      onclick: () => editPanel(job, mode)}, V[mode]))));
}
