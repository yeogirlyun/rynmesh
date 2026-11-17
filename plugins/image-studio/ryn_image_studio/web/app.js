// Image studio page. Runs in the colleague's Ryn, served from the hosting computer.
// Talks only to its own origin (the gateway CSP blocks everything else).

// Ryn passes its own UI language (?lang=); otherwise follow the browser.
const langParam = new URLSearchParams(location.search).get("lang");
const zh = langParam ? /^zh/i.test(langParam) : !/^en\b/i.test(navigator.language || "zh");
const T = zh ? {
  title: "做图", loading: "正在加载…", newImage: "新建一张图", purpose: "用途和尺寸", custom: "自定义",
  output: (w, h) => `输出 ${w} × ${h} PNG`, brief: "想要什么样的图", briefPh: "双十一保温杯主图，白色背景，杯子放中间，右上角红色角标",
  words: "图上要写的字", wordsHint: "（可选，会一字不差地放上去）", wordsPh: "限时 5 折",
  refs: "参考图", refsHint: "（可选，最多 3 张）", refsTip: "商品照片、旧海报都行，可以直接拖进来或粘贴",
  add: "添加", remove: "移除参考图", count: "张数", submit: "提交", submitting: "正在提交…",
  ahead: (n, m) => n ? `前面还有 ${n} 个任务，大约 ${m} 分钟后开始做你的` : "现在提交马上开始做",
  queue: (n) => n ? `排队 ${n} 个` : "现在没人排队", each: (s) => `每张约 ${Math.max(1, Math.round(s / 60))} 分钟`,
  mine: "我的作品", all: "全部", active: "进行中", done: "已完成", empty: "还没有作品。左边写好需求就可以提交。",
  queued: (p) => `排队中 · 第 ${p} 位`, wait: (s) => s > 0 ? `大约 ${Math.max(1, Math.round(s / 60))} 分钟后开始` : "下一个就做你的", running: (t) => `正在生成 · ${t}`, runningTip: "在对方电脑上做",
  finished: "完成", failed: "没做出来", cancelled: "已取消", cancel: "取消", retry: "重试",
  download: "下载", again: "再来一张", edit: "改这张", back: "我的作品", downloadPng: "下载 PNG", copy: "复制描述",
  copied: "已复制", info: { brief: "描述", words: "图上文字", size: "尺寸", refs: "参考图", made: "生成" },
  editTitle: "在这张基础上改", editPh: "背景换成浅红色，加一点雪花，角标改成「买一送一」",
  editHint: "这张图会作为参考图，尺寸不变", editSubmit: "提交修改", sent: "已提交，开始排队", notReady: {
    codex_missing: "对方电脑没有安装 Codex，现在提交的任务会失败。",
    codex_signed_out: "对方电脑的 Codex 没有登录，现在提交的任务会失败。" },
  errors: { image_bad_brief: "先写一下想要什么样的图。", image_too_many_active: "你进行中的任务太多了，等前面的做完再提交。",
    image_upload_too_large: "图片太大了，单张不能超过 8 MB。", image_upload_type: "只支持 PNG、JPG、WebP 图片。",
    image_bad_size: "自定义尺寸要在 256 到 2048 像素之间。", site_offline: "对方电脑离线了。", network: "连不上，稍后再试。",
    default: "没成功，稍后再试。" },
  ago: { now: "刚刚", min: (n) => `${n} 分钟前`, yesterday: "昨天" }, seconds: (s) => s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`,
  refsCount: (n) => n ? `${n} 张` : "无",
} : {
  title: "Image studio", loading: "Loading…", newImage: "New image", purpose: "Use and size", custom: "Custom",
  output: (w, h) => `Output ${w} × ${h} PNG`, brief: "What should it look like", briefPh: "Product shot of a thermos on white, red corner badge",
  words: "Text on the image", wordsHint: " (optional, placed exactly)", wordsPh: "50% off",
  refs: "Reference images", refsHint: " (optional, up to 3)", refsTip: "Product photos or old posters. Drag, drop or paste.",
  add: "Add", remove: "Remove reference", count: "Count", submit: "Submit", submitting: "Submitting…",
  ahead: (n, m) => n ? `${n} ahead of you, starts in about ${m} min` : "Starts right away",
  queue: (n) => n ? `${n} in queue` : "No queue", each: (s) => `about ${Math.max(1, Math.round(s / 60))} min each`,
  mine: "Your images", all: "All", active: "In progress", done: "Finished", empty: "No images yet. Describe one on the left and submit.",
  queued: (p) => `Queued · #${p}`, wait: (s) => s > 0 ? `starts in about ${Math.max(1, Math.round(s / 60))} min` : "you're next", running: (t) => `Generating · ${t}`, runningTip: "On their computer",
  finished: "Done", failed: "Didn't work", cancelled: "Cancelled", cancel: "Cancel", retry: "Retry",
  download: "Download", again: "Another", edit: "Edit this", back: "Your images", downloadPng: "Download PNG", copy: "Copy brief",
  copied: "Copied", info: { brief: "Brief", words: "Text", size: "Size", refs: "References", made: "Made" },
  editTitle: "Change this image", editPh: "Light red background, some snowflakes", editHint: "This image is the reference; the size stays.",
  editSubmit: "Submit change", sent: "Submitted", notReady: {
    codex_missing: "Codex isn't installed on that computer; jobs will fail.",
    codex_signed_out: "Codex isn't signed in on that computer; jobs will fail." },
  errors: { image_bad_brief: "Describe the image first.", image_too_many_active: "Too many jobs in progress. Wait for some to finish.",
    image_upload_too_large: "Images must be 8 MB or smaller.", image_upload_type: "Only PNG, JPG and WebP.",
    image_bad_size: "Custom sizes must be 256–2048 px.", site_offline: "That computer is offline.", network: "Can't connect. Try again later.",
    default: "That didn't work. Try again later." },
  ago: { now: "just now", min: (n) => `${n} min ago`, yesterday: "yesterday" }, seconds: (s) => s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`,
  refsCount: (n) => n ? `${n}` : "none",
};
const FAIL_EN = { codex_missing: "Codex isn't installed there.", codex_signed_out: "Codex isn't signed in there.",
  quota_exhausted: "Image quota used up. Try later.", content_rejected: "Blocked by content review. Rephrase it.",
  timeout: "Timed out.", cancelled: "Cancelled.", interrupted: "That computer restarted.", generation_failed: "Generation failed." };
document.documentElement.lang = zh ? "zh-CN" : "en";
document.title = T.title;

const MAX_REFS = 3, MAX_UPLOAD = 8 * 1024 * 1024;
const state = { me: null, jobs: [], filter: "all", queueLength: 0, average: 60, offset: 0,
  purpose: "main", width: 1024, height: 1024, count: 1, refs: [], busy: false, timer: 0 };

// ---- tiny DOM helper: text always goes through textContent ----
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

function toast(text) {
  const el = document.getElementById("toast");
  el.textContent = text; el.hidden = false;
  clearTimeout(toast.t); toast.t = setTimeout(() => { el.hidden = true; }, 3200);
}

async function api(path, options = {}) {
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
const errorText = (err) => T.errors[err?.message] || T.errors.default;
const now = () => Date.now() / 1000 + state.offset;

function ago(ts) {
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
const sizeLabel = (job) => {
  const p = state.me?.purposes?.[job.purpose];
  return p ? (zh ? `${p.label} ${p.ratio}` : `${p.ratio}`) + ` · ${job.width} × ${job.height}` : `${T.custom} · ${job.width} × ${job.height}`;
};
const failureText = (job) => zh ? (job.failure_text || T.errors.default) : (FAIL_EN[job.failure] || T.errors.default);

// ---- data ----
async function loadJobs() {
  const params = state.filter === "all" ? "" : `?state=${state.filter}`;
  const body = await api(`/api/jobs${params}`);
  state.jobs = body.jobs; state.queueLength = body.queue_length; state.average = body.average_seconds || 60;
  state.offset = body.now - Date.now() / 1000;
}

function schedule() {
  clearTimeout(state.timer);
  const busy = state.jobs.some((j) => j.state === "queued" || j.state === "running");
  state.timer = setTimeout(refresh, busy ? 3000 : 15000);
}

async function refresh() {
  try { await loadJobs(); update(); } catch (err) { if (err.message === "site_offline") toast(errorText(err)); }
  schedule();
}

// ---- references ----
async function addFiles(files) {
  for (const file of files) {
    if (state.refs.length >= MAX_REFS) break;
    if (!/^image\/(png|jpeg|webp)$/.test(file.type)) { toast(T.errors.image_upload_type); continue; }
    if (file.size > MAX_UPLOAD) { toast(T.errors.image_upload_too_large); continue; }
    const ref = { url: URL.createObjectURL(file), id: null };
    state.refs.push(ref); renderForm();
    try {
      ref.id = (await api("/api/uploads", { method: "POST", headers: { "Content-Type": file.type }, body: file })).upload_id;
    } catch (err) {
      state.refs = state.refs.filter((r) => r !== ref); URL.revokeObjectURL(ref.url); toast(errorText(err));
    }
    renderForm();
  }
}

// ---- submitting ----
async function submit(body, { quiet = false } = {}) {
  state.busy = true; renderForm();
  try {
    await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!quiet) toast(T.sent);
    state.filter = state.filter === "done" ? "all" : state.filter;
    await refresh();
    return true;
  } catch (err) { toast(errorText(err)); return false; }
  finally { state.busy = false; renderForm(); }
}

function formBody() {
  const brief = document.getElementById("brief").value.trim();
  const words = document.getElementById("words").value.trim();
  const body = { purpose: state.purpose, brief, words, count: state.count, references: state.refs.filter((r) => r.id).map((r) => r.id) };
  if (state.purpose === "custom") Object.assign(body, { width: state.width, height: state.height });
  return body;
}

function again(job) {
  const body = { purpose: job.purpose, brief: job.brief, words: job.words, count: 1 };
  if (job.purpose === "custom") Object.assign(body, { width: job.width, height: job.height });
  return submit(body);
}

async function act(job, action) {
  try { await api(`/api/jobs/${job.id}/${action}`, { method: "POST" }); await refresh(); }
  catch (err) { toast(errorText(err)); }
}

// ---- rendering ----
function renderShell() {
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

function renderForm() {
  const form = document.getElementById("form");
  if (!form) return;
  const purposes = Object.entries(state.me?.purposes || {});
  const chips = [...purposes.map(([id, p]) => [id, zh ? `${p.label} ${p.ratio}` : p.ratio]), ["custom", T.custom]];
  const current = state.purpose === "custom" ? [state.width, state.height]
    : [state.me.purposes[state.purpose].width, state.me.purposes[state.purpose].height];
  const ahead = state.queueLength;
  form.querySelector("#chips").replaceChildren(...chips.map(([id, label]) => h("button", {
    type: "button", class: "chip", "aria-pressed": String(state.purpose === id),
    onclick: () => { state.purpose = id; renderForm(); } }, label)));
  form.querySelector("#custom").replaceChildren(state.purpose === "custom" ? h("div", { class: "custom" },
    h("label", { class: "sr", for: "cw" }, "width"), h("input", { id: "cw", type: "number", min: 256, max: 2048, value: state.width,
      onchange: (e) => { state.width = Number(e.target.value); renderForm(); } }), "×",
    h("label", { class: "sr", for: "ch" }, "height"), h("input", { id: "ch", type: "number", min: 256, max: 2048, value: state.height,
      onchange: (e) => { state.height = Number(e.target.value); renderForm(); } })) : "");
  form.querySelector("#output").textContent = T.output(...current);
  form.querySelector("#refs").replaceChildren(
    ...state.refs.map((ref) => h("div", { class: `ref${ref.id ? "" : " uploading"}` }, h("img", { src: ref.url, alt: "" }),
      h("button", { type: "button", class: "x", "aria-label": T.remove, onclick: () => {
        state.refs = state.refs.filter((r) => r !== ref); URL.revokeObjectURL(ref.url); renderForm(); } }, "×"))),
    state.refs.length < MAX_REFS ? h("button", { type: "button", class: "add", onclick: () => document.getElementById("file").click() }, "+ ", T.add) : "",
    h("p", { class: "muted", style: "flex:1 1 140px" }, T.refsTip));
  form.querySelector("#counts").replaceChildren(...[1, 2, 4].map((n) => h("button", { type: "button", "aria-pressed": String(state.count === n),
    onclick: () => { state.count = n; renderForm(); } }, n)));
  form.querySelector("#ahead").textContent = T.ahead(ahead, Math.max(1, Math.round(ahead * state.average / 60)));
  const button = form.querySelector("#submit");
  button.disabled = state.busy || state.refs.some((r) => !r.id);
  button.textContent = state.busy ? T.submitting : T.submit;
}

function buildForm() {
  const form = h("form", { id: "form", class: "panel form", onsubmit: async (e) => {
    e.preventDefault();
    const body = formBody();
    if (!body.brief) { toast(T.errors.image_bad_brief); document.getElementById("brief").focus(); return; }
    await submit(body);
  } },
    h("h2", {}, T.newImage),
    h("div", { class: "field" }, h("div", { class: "label" }, T.purpose), h("div", { class: "chips", id: "chips" }), h("div", { id: "custom" }),
      h("p", { class: "muted", id: "output" })),
    h("div", { class: "field" }, h("label", { class: "label", for: "brief" }, T.brief),
      h("textarea", { id: "brief", rows: 4, maxlength: 2000, placeholder: T.briefPh })),
    h("div", { class: "field" }, h("label", { class: "label", for: "words" }, T.words, h("span", {}, T.wordsHint)),
      h("input", { id: "words", type: "text", maxlength: 200, placeholder: T.wordsPh })),
    h("div", { class: "field", id: "dropzone" }, h("div", { class: "label" }, T.refs, h("span", {}, T.refsHint)),
      h("div", { class: "refs", id: "refs" }),
      h("input", { id: "file", type: "file", accept: "image/png,image/jpeg,image/webp", multiple: true, hidden: true,
        onchange: (e) => { addFiles([...e.target.files]); e.target.value = ""; } })),
    h("div", { class: "row" }, h("div", { class: "label" }, T.count), h("div", { class: "seg", id: "counts", role: "group", "aria-label": T.count })),
    h("div", { class: "row submit" }, h("p", { class: "muted", id: "ahead", style: "flex:1 1 160px" }),
      h("button", { type: "submit", class: "primary", id: "submit" }, T.submit)));
  const zone = form.querySelector("#dropzone");
  zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drop-active"); });
  zone.addEventListener("dragleave", () => zone.classList.remove("drop-active"));
  zone.addEventListener("drop", (e) => { e.preventDefault(); zone.classList.remove("drop-active"); addFiles([...e.dataTransfer.files]); });
  return form;
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
    top = h("button", { type: "button", class: "thumb", onclick: () => { location.hash = `#/job/${job.id}`; } },
      h("img", { src: `/api/jobs/${job.id}/thumb`, alt: job.brief, loading: "lazy" }));
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
  return h("article", { class: `card ${job.state}` }, top, h("div", { class: "meta" }, h("div", { class: "brief", title: job.brief }, job.brief), meta));
}

function renderGallery() {
  const gallery = document.getElementById("gallery");
  if (!gallery) return;
  gallery.querySelector("#tabs").replaceChildren(...[["all", T.all], ["active", T.active], ["done", T.done]].map(([id, label]) =>
    h("button", { type: "button", "aria-pressed": String(state.filter === id), onclick: async () => { state.filter = id; await refresh(); } }, label)));
  gallery.querySelector("#grid").replaceChildren(...(state.jobs.length ? state.jobs.map(card)
    : [h("p", { class: "empty", style: "grid-column:1/-1" }, T.empty)]));
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

function renderDetail(id, editing) {
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
          h("button", { type: "button", class: "ghost", onclick: async () => {
            try { await navigator.clipboard.writeText(job.brief); toast(T.copied); } catch { editText.value = job.brief; editText.select(); }
          } }, T.copy)),
        h("dl", { class: "info" },
          h("dt", {}, T.info.brief), h("dd", {}, job.brief),
          job.words ? [h("dt", {}, T.info.words), h("dd", {}, job.words)] : "",
          h("dt", {}, T.info.size), h("dd", {}, sizeLabel(job)),
          h("dt", {}, T.info.refs), h("dd", {}, T.refsCount(job.references)),
          h("dt", {}, T.info.made), h("dd", {}, `${ago(job.finished_at)} · ${T.seconds(job.seconds || 0)}`))),
      h("form", { class: "panel field", onsubmit: async (e) => {
        e.preventDefault();
        const brief = editText.value.trim();
        if (!brief) { editText.focus(); return; }
        const body = { purpose: job.purpose, brief, words: job.words, count: 1, source_job: job.id };
        if (job.purpose === "custom") Object.assign(body, { width: job.width, height: job.height });
        if (await submit(body)) location.hash = "";
      } }, h("label", { class: "label", for: "edit" }, T.editTitle), editText,
        h("div", { class: "row" }, h("p", { class: "muted" }, T.editHint), h("button", { type: "submit", class: "primary" }, T.editSubmit))))));
  if (editing) editText.focus();
}

const detailRoute = () => location.hash.match(/^#\/job\/([0-9a-f]{24})(\/edit)?$/);
// Full view change (boot, navigation).
function render() {
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

window.addEventListener("hashchange", () => {
  if (!location.hash.startsWith("#/job/")) document.getElementById("main").replaceChildren();
  render();
});
document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (files.length && !location.hash.startsWith("#/job/")) { e.preventDefault(); addFiles(files); }
});

(async function boot() {
  renderShell();
  try {
    state.me = await api("/api/me");
    state.offset = state.me.now - Date.now() / 1000;
    state.queueLength = state.me.queue_length; state.average = state.me.average_seconds || 60;
    await loadJobs();
    render();
  } catch (err) {
    document.getElementById("main").replaceChildren(h("p", { class: "empty" }, errorText(err)));
  }
  schedule();
})();
