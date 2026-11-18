import { state, api, submit, errorText } from "./api.js";
import { h, toast } from "./dom.js";
import { browseAssets, saveAsset } from "./assets.js";
import { A, F, T, V, presetLabel, roleLabel } from "./i18n.js";

const MAX_REFS = 5, MAX_UPLOAD = 8 * 1024 * 1024;
const draft = { scene: "", style: null, width: 1024, height: 1024, custom: false, count: 1, refs: [], busy: false };
const copyLimits = { title: 40, subtitle: 60, badge: 20 };

export async function addFiles(files) {
  for (const file of files) {
    if (draft.refs.length >= MAX_REFS) { toast(A.limit); break; }
    if (!/^image\/(png|jpeg|webp)$/.test(file.type)) { toast(T.errors.image_upload_type); continue; }
    if (file.size > MAX_UPLOAD) { toast(T.errors.image_upload_too_large); continue; }
    const ref = { file, url: URL.createObjectURL(file), id: null, role: "material" };
    draft.refs.push(ref); renderForm();
    try {
      ref.id = (await api("/api/uploads", { method: "POST", headers: { "Content-Type": file.type }, body: file })).upload_id;
    } catch (err) {
      draft.refs = draft.refs.filter((r) => r !== ref); URL.revokeObjectURL(ref.url); toast(errorText(err));
    }
    renderForm();
  }
}


function library(manage) {
  return browseAssets({manage, choose(asset) {
    if (draft.refs.length >= MAX_REFS) return false;
    draft.refs.push({asset: asset.id, url: `/api/assets/${asset.id}/thumb`, name: asset.name,
      role: asset.kind === "style" ? "style" : "material"});
    renderForm();
    return true;
  }, removed(id) {
    draft.refs = draft.refs.filter(ref => ref.asset !== id);
    renderForm();
  }});
}

function sceneSizes(scene) {
  return scene.sizes?.length ? scene.sizes : [scene];
}

function selectScene(id) {
  draft.scene = id;
  const size = sceneSizes(state.me.scenes[id])[0];
  draft.width = size.width;
  draft.height = size.height;
  draft.custom = false;
}

function chip(label, selected, choose) {
  return h("button", { type: "button", class: "chip", "aria-pressed": String(selected),
    onclick: () => { choose(); renderForm(); } }, label);
}

export function renderForm() {
  const form = document.getElementById("form");
  if (!form) return;
  const scene = state.me.scenes[draft.scene];
  form.querySelector("#scenes").replaceChildren(...Object.entries(state.me.scenes).map(([id, item]) =>
    chip(presetLabel(id, item), draft.scene === id, () => selectScene(id))));
  form.querySelector("#styles").replaceChildren(...Object.entries(state.me.styles).map(([id, item]) =>
    chip(presetLabel(id, item), draft.style === id, () => { draft.style = draft.style === id ? null : id; })));
  form.querySelector("#sizes").replaceChildren(...sceneSizes(scene).map(({width, height}) =>
    chip(`${width} × ${height}`, !draft.custom && draft.width === width && draft.height === height,
      () => Object.assign(draft, {width, height, custom: false}))),
    chip(T.custom, draft.custom, () => { draft.custom = true; }));
  form.querySelector("#custom").hidden = !draft.custom;
  for (const key of ["width", "height"]) {
    const input = form.elements.namedItem(key);
    if (document.activeElement !== input) input.value = draft[key];
    input.disabled = !draft.custom;
  }
  form.querySelector("#refs").replaceChildren(...draft.refs.map((ref, index) => {
    const select = h("select", {"aria-label": `${F.image} ${index + 1}`,
      onchange: e => { ref.role = e.target.value; }},
      ...["material", "style"].map(role => h("option", {value: role, selected: ref.role === role}, roleLabel(role))));
    return h("div", {class: "attachment"}, h("div", {class: `ref${ref.file && !ref.id ? " uploading" : ""}`},
      h("img", {src: ref.url, alt: ref.name || ref.file.name}), h("button", {type: "button", class: "x", "aria-label": T.remove,
        onclick: () => { draft.refs = draft.refs.filter(item => item !== ref); if (ref.file) URL.revokeObjectURL(ref.url); renderForm(); }}, "×")), select,
      ref.file && ref.id && h("button", {type: "button", class: "link", onclick: () => saveAsset(ref)}, A.saveTo));
  }));
  form.querySelector("#add").hidden = draft.refs.length >= MAX_REFS;
  form.querySelector("#counts").replaceChildren(...[1, 2, 4].map(n =>
    chip(n, draft.count === n, () => { draft.count = n; })));
  form.querySelector("#ahead").textContent = T.ahead(state.queueLength, Math.max(1, Math.round(state.queueLength * state.average / 60)));
  const button = form.querySelector("#submit");
  button.disabled = draft.busy || draft.refs.some(ref => ref.file && !ref.id);
  button.textContent = draft.busy ? T.submitting : T.submit;
}

function field(title, ...children) {
  return h("fieldset", {class: "field"}, h("legend", {class: "label"}, title), ...children);
}

async function send(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const scene = state.me.scenes[draft.scene];
  const copy = {};
  for (const key of Object.keys(copyLimits)) {
    const value = form.elements.namedItem(key).value.trim();
    if (value) copy[key] = value;
  }
  const brief = form.elements.brief.value.trim();
  if (!brief && !draft.refs.length && !Object.keys(copy).length) {
    toast(T.errors.image_bad_brief); form.elements.brief.focus(); return;
  }
  const purpose = !draft.custom && draft.width === scene.width && draft.height === scene.height ? scene.purpose : "custom";
  draft.busy = true;
  renderForm();
  try {
    await submit({scene: draft.scene, style: draft.style, purpose, width: draft.width, height: draft.height,
      brief, copy, transparent: form.elements.transparent.checked, references: draft.refs.map(ref => ref.asset ? {asset: ref.asset, role: ref.role} : {id: ref.id, role: ref.role}), count: draft.count});
  } finally { draft.busy = false; renderForm(); }
}

export function buildForm() {
  selectScene(state.me.scenes.main ? "main" : Object.keys(state.me.scenes)[0]);
  const form = h("form", {id: "form", class: "panel form", onsubmit: send},
    h("h2", {}, T.newImage),
    field(F.scene, h("div", {class: "chips", id: "scenes"})),
    field(F.images, h("div", {id: "dropzone", class: "field"},
      h("p", {class: "muted"}, F.drop, T.refsHint),
      h("div", {class: "refs", id: "refs"}),
      h("div", {class: "actions"}, h("button", {id: "add", type: "button", class: "ghost", onclick: () => form.elements.file.click()}, "+ ", T.add),
        h("button", {type: "button", class: "ghost", onclick: () => library(false)}, A.choose),
        h("button", {type: "button", class: "link", onclick: () => library(true)}, A.manage)),
      h("input", {name: "file", id: "file", type: "file", accept: "image/png,image/jpeg,image/webp", multiple: true, hidden: true,
        onchange: e => { addFiles([...e.target.files]); e.target.value = ""; }}),
      h("p", {class: "muted"}, F.tip))),
    field(F.copy, ...Object.entries(copyLimits).map(([key, limit]) => h("label", {class: "copy-input"}, F[key],
      h("input", {name: key, type: "text", maxlength: limit, placeholder: F.optional})))),
    field(F.style, h("div", {class: "chips", id: "styles"})),
    field(F.size, h("div", {class: "chips", id: "sizes"}),
      h("div", {class: "custom", id: "custom"}, ...["width", "height"].map(key => h("label", {}, F[key],
        h("input", {name: key, type: "number", min: 256, max: 2048, required: true,
          oninput: e => { draft[key] = Number(e.target.value); }}))))),
    h("div", {class: "field"}, h("label", {}, h("input", {type: "checkbox", name: "transparent"}), " ", V.transparent),
      h("p", {class: "muted"}, V.hint)),
    field(F.brief, h("textarea", {id: "brief", name: "brief", rows: 3, maxlength: 2000, "aria-label": F.brief, placeholder: T.briefPh})),
    h("div", {class: "row"}, h("span", {class: "label"}, T.count), h("div", {class: "seg", id: "counts"})),
    h("div", {class: "row submit"}, h("p", {class: "muted", id: "ahead"}),
      h("button", {type: "submit", class: "primary", id: "submit"}, T.submit)));
  const zone = form.querySelector("#dropzone");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drop-active"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drop-active"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drop-active"); addFiles([...e.dataTransfer.files]); });
  return form;
}
