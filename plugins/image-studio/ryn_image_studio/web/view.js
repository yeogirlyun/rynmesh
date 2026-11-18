import { state } from "./api.js";
import { T } from "./i18n.js";
import { h } from "./dom.js";
import { addFiles, buildForm, renderForm } from "./form.js";
import { renderGallery } from "./gallery.js";
import { renderDetail } from "./detail.js";

export function renderShell() {
  const app = document.getElementById("app");
  app.replaceChildren(
    h("header", { class: "top" }, h("h1", {}, T.title), h("span", { class: "pill", id: "queue" })),
    h("div", { id: "notice" }),
    h("main", { id: "main" }),
  );
}

function renderTop() {
  document.getElementById("queue").textContent = `${T.queue(state.queueLength)} · ${T.each(state.average)}`;
  const notice = document.getElementById("notice");
  const code = state.me?.model?.ready === false ? state.me.model.code : "";
  notice.replaceChildren(code ? h("p", { class: "notice", role: "status" }, T.notReady[code] || T.errors.default) : "");
}

let formEl = null; // built once, so a half-written brief survives opening a result
function renderList() {
  const main = document.getElementById("main");
  if (!document.getElementById("gallery")) {
    formEl = formEl || buildForm();
    main.replaceChildren(h("div", { class: "layout" }, formEl,
      h("section", { class: "panel gallery", id: "gallery", "aria-label": T.mine },
        h("div", { class: "gallery-head" }, h("h2", {}, T.mine), h("div", { class: "tabs", id: "tabs" })),
        h("div", { class: "grid", id: "grid" }))));
  }
  renderForm(); renderGallery();
}

const detailRoute = () => location.hash.match(/^#\/job\/([0-9a-f]{24})(\/edit)?$/);
// Full view change (boot, navigation).
export function render() {
  renderTop();
  const match = detailRoute();
  if (match) renderDetail(match[1], Boolean(match[2]));
  else renderList();
}
// Polling update: never rebuild the detail view (it holds the edit text).
function update() {
  renderTop();
  if (!detailRoute()) { renderForm(); renderGallery(); }
}

window.addEventListener("studio-jobs-loaded", update);
window.addEventListener("hashchange", () => {
  if (!location.hash.startsWith("#/job/")) document.getElementById("main").replaceChildren();
  render();
});
document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (files.length && !location.hash.startsWith("#/job/")) { e.preventDefault(); addFiles(files); }
});
