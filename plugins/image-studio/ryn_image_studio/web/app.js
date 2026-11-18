import { state, api, loadJobs, schedule, errorText } from "./api.js";
import { renderShell, render } from "./view.js";
import { h } from "./dom.js";

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
