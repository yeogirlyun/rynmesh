import { api, errorText, state } from "./api.js";
import { h, toast } from "./dom.js";
import { A } from "./i18n.js";

function panel(title) {
  const content = h("div", {class: "asset-content"});
  const dialog = h("dialog", {class: "asset-dialog", "aria-label": title},
    h("header", {class: "row"}, h("h2", {}, title),
      h("button", {type: "button", class: "ghost", onclick: () => dialog.close()}, A.close)), content);
  dialog.addEventListener("close", () => dialog.remove(), {once: true});
  document.body.append(dialog);
  dialog.showModal();
  return {dialog, content};
}

export function saveAsset(ref) {
  const {dialog, content} = panel(A.saveTo);
  const name = h("input", {type: "text", maxlength: 80, required: true, value: ref.file.name.replace(/\.[^.]+$/, "").slice(0, 80)});
  const kind = h("select", {}, ...["product", "logo", "style"].map(key =>
    h("option", {value: key, selected: key === (ref.role === "style" ? "style" : "product")}, A[key])));
  const publicBox = h("input", {type: "checkbox"});
  const error = h("p", {role: "alert", class: "asset-error"});
  const save = h("button", {type: "submit", class: "primary"}, A.save);
  content.append(h("form", {class: "field", onsubmit: async e => {
    e.preventDefault();
    if (!name.value.trim()) { error.textContent = A.emptyName; name.focus(); return; }
    save.disabled = true;
    const params = new URLSearchParams({name: name.value.trim(), kind: kind.value});
    if (state.me.is_owner && publicBox.checked) params.set("public", "1");
    try {
      await api(`/api/assets?${params}`, {method: "POST", headers: {"Content-Type": ref.file.type}, body: ref.file});
      dialog.close();
      toast(A.saved);
    } catch (err) { error.textContent = errorText(err); }
    finally { save.disabled = false; }
  }}, h("label", {class: "field"}, A.name, name), h("label", {class: "field"}, A.kind, kind),
    state.me.is_owner && h("label", {}, publicBox, " ", A.publicSave), error, save));
  name.focus();
}

export async function browseAssets({manage, choose, removed}) {
  const {dialog, content} = panel(manage ? A.manage : A.choose);
  content.textContent = A.loading;
  let library;
  try { library = await api("/api/assets"); }
  catch (err) { content.textContent = errorText(err); return; }

  let group = "mine";
  const tabs = h("div", {class: "chips"});
  const list = h("div", {class: "asset-grid"});
  const error = h("p", {role: "alert", class: "asset-error"});
  const search = h("input", {type: "search", "aria-label": A.search, placeholder: A.search, oninput: renderList});
  function renderList() {
    const query = search.value.trim().toLocaleLowerCase();
    const items = library[group].filter(a => [a.name, A[a.kind]].some(text => text.toLocaleLowerCase().includes(query)));
    list.replaceChildren(...items.map(asset => {
      const card = h("article", {class: "asset-card"});
      const image = h("img", {src: `/api/assets/${asset.id}/thumb`, alt: "", loading: "lazy"});
      const label = h("strong", {}, asset.name);
      const kind = h("small", {class: "muted"}, A[asset.kind]);
      if (!manage) {
        card.append(h("button", {class: "asset-pick", type: "button", onclick: () => {
          if (choose(asset)) dialog.close();
          else error.textContent = A.limit;
        }}, image, label, kind));
      } else {
        card.append(image, label, kind);
        if (group === "mine" || state.me.is_owner) {
          const actions = h("div", {class: "actions"});
          const remove = h("button", {type: "button", class: "link danger", onclick: () => {
            const confirm = h("button", {type: "button", class: "link danger", onclick: async () => {
              confirm.disabled = true;
              try {
                await api(`/api/assets/${asset.id}`, {method: "DELETE"});
                const collection = library.mine.includes(asset) ? "mine" : "public";
                library[collection] = library[collection].filter(item => item.id !== asset.id);
                removed(asset.id);
                renderList();
              } catch (err) { error.textContent = errorText(err); confirm.disabled = false; }
            }}, A.confirm);
            actions.replaceChildren(confirm, h("button", {type: "button", class: "link",
              onclick: () => actions.replaceChildren(remove)}, A.cancel));
          }}, A.remove);
          actions.append(remove); card.append(actions);
        }
      }
      return card;
    }));
    if (!items.length) list.append(h("p", {class: "muted"}, query ? A.noMatches : A.empty));
  }
  function renderGroup() {
    tabs.replaceChildren(...["mine", "public"].map(key => h("button", {type: "button", class: "chip",
      "aria-pressed": String(group === key), onclick: () => { group = key; error.textContent = ""; renderGroup(); }}, A[key])));
    renderList();
  }
  content.replaceChildren(tabs, search, error, list);
  renderGroup();
}
