import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { MemoryRouter, useLocation } from "react-router-dom";
import CraftApps from "./CraftApps";
import { craftPlugins, verifiedPluginPath, type CraftCatalog, type CraftLaunch, type CraftPlugin } from "../domain/craftPlugins";
import { setLanguagePreference } from "../i18n";
import en from "../locales/craft.en.json";
import zh from "../locales/craft.zh-CN.json";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));

const ids = ["pdfcraft", "wordcraft", "gridcraft", "deckcraft"];
const editorUrls: Record<string, string> = {
  pdfcraft: "http://127.0.0.1:8928/", wordcraft: "http://127.0.0.1:8932/",
  gridcraft: "http://127.0.0.1:8926/", deckcraft: "http://127.0.0.1:8921/", soundcraft: "http://127.0.0.1:8930/",
};
const launchPreview = (id: string): CraftLaunch => ({ app: id, node_id: "this-node", version: "v0.6.1", url: editorUrls[id], ticket: "local-launch-ticket" });
let state: CraftCatalog;
const clone = () => structuredClone(state);
function plugin(id: string): CraftPlugin {
  return { id, name: id, version: "v0.6.1", group: "office", web: true,
    formats: ["pdf"], sha256: "a".repeat(64), source_url: `https://github.com/storytold/${id}`,
    installed: false, enabled: false, running: false, busy: false, agent_tools: [], editing_supported: false, warning: null };
}
function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }
function mount(manage = false) { return render(<MemoryRouter><CraftApps manage={manage} /><Location /></MemoryRouter>); }
function builtin(id: string): CraftPlugin {
  return { ...plugin(id), kind: "builtin_adapter", installable: true, web: false, formats: [],
    group: id === "nas" ? "storage" : "workflow", version: "1.0.0",
    launch_path: id === "nas" ? "/nas" : `/apps/workspaces?app=${id}` };
}
function addAll() {
  for (const id of ["cadcraft", "designcraft", "effectcraft", "figcraft", "filmcraft", "lightcraft", "photocraft", "soundcraft", "vectorcraft"]) {
    state.plugins.push({ ...plugin(id), group: ["effectcraft", "filmcraft", "soundcraft"].includes(id) ? "media" : "design", web: id !== "filmcraft", installable: id !== "filmcraft" });
  }
  state.plugins.push(...["resume", "recruit", "desk", "nas"].map(builtin));
}
function present() { state.setup_completed = true; state.plugins.forEach(p => { p.installed = true; p.enabled = true; p.agent_tools = ["editor_info"]; }); }
beforeAll(() => {
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});
beforeEach(() => {
  Reflect.deleteProperty(window, "__TAURI_INTERNALS__");
  vi.restoreAllMocks(); setLanguagePreference("en");
  vi.mocked(invoke).mockReset().mockResolvedValue(undefined);
  state = { node_id: "this-node", plugins: ids.map(plugin), defaults: ids, selection: ids, setup_completed: false, storage_path: "C:/Local/Ryn/plugins/node" };
  vi.spyOn(craftPlugins, "catalog").mockImplementation(async () => clone());
  vi.spyOn(craftPlugins, "setup").mockImplementation(async () => { state.setup_completed = true; return clone(); });
  vi.spyOn(craftPlugins, "install").mockImplementation(async id => { const p = state.plugins.find(p => p.id === id)!; p.installed = true; p.enabled = true; p.agent_tools = ["editor_info"]; return clone(); });
  vi.spyOn(craftPlugins, "enable").mockImplementation(async (id, enabled) => { const p = state.plugins.find(p => p.id === id)!; p.enabled = enabled; p.agent_tools = enabled ? ["editor_info"] : []; return clone(); });
  vi.spyOn(craftPlugins, "uninstall").mockImplementation(async id => { const p = state.plugins.find(p => p.id === id)!; p.installed = false; p.enabled = false; p.agent_tools = []; return clone(); });
  vi.spyOn(craftPlugins, "preview").mockImplementation(async id => launchPreview(id));
  vi.spyOn(craftPlugins, "launch").mockImplementation(async id => { const { ticket: _ticket, ...value } = launchPreview(id); return value; });
  vi.spyOn(window, "open").mockReturnValue(null);
});

it("selects the office four and performs no download before confirmation", async () => {
  const user = userEvent.setup(); mount();
  await screen.findByRole("heading", { name: "Choose your tools" });
  expect(screen.getAllByRole("checkbox", { checked: true })).toHaveLength(4);
  expect(craftPlugins.install).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Install & download" }));
  await screen.findByRole("heading", { name: "Apps" });
  expect(vi.mocked(craftPlugins.install).mock.calls.map(([id]) => id)).toEqual(ids);
  expect(craftPlugins.setup).toHaveBeenCalledOnce();
});

it("retains completed downloads after a partial failure and retries only missing plugins", async () => {
  const user = userEvent.setup(); mount();
  const normal = vi.mocked(craftPlugins.install).getMockImplementation()!;
  vi.mocked(craftPlugins.install).mockImplementation(async id => { if (id === "wordcraft") throw Error("craft_checksum_mismatch"); return normal(id); });
  await user.click(await screen.findByRole("button", { name: "Install & download" }));
  await screen.findByRole("alert");
  expect(state.plugins[0].installed).toBe(true);
  expect(state.setup_completed).toBe(false);
  vi.mocked(craftPlugins.install).mockImplementation(normal);
  await user.click(screen.getByRole("button", { name: "Install & download" }));
  await screen.findByRole("heading", { name: "Apps" });
  expect(vi.mocked(craftPlugins.install).mock.calls.filter(([id]) => id === "pdfcraft")).toHaveLength(1);
});

it("supports installing nothing and adding tools later", async () => {
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Add later" }));
  await screen.findByRole("heading", { name: "Apps" });
  expect(craftPlugins.install).not.toHaveBeenCalled();
  expect(screen.getAllByRole("button", { name: "Install" })).toHaveLength(4);
});

it("reviews disable and uninstall while retaining files and preferences by default", async () => {
  present(); const user = userEvent.setup(); mount(true);
  const toggle = await screen.findByRole("switch", { name: "Disable Word documents" });
  expect(screen.getAllByRole("switch")).toHaveLength(4); // No separate Agent switch.
  await user.click(toggle);
  expect(craftPlugins.enable).not.toHaveBeenCalled();
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Disable" }));
  await screen.findByRole("switch", { name: "Enable Word documents" });
  await user.click(screen.getByLabelText("More actions for Word documents"));
  await user.click(within(screen.getByLabelText("More actions for Word documents").closest("details")!).getByRole("button", { name: "Uninstall plugin" }));
  const dialog = screen.getByRole("dialog");
  expect(within(dialog).getByRole("checkbox")).not.toBeChecked();
  expect(dialog).toHaveTextContent("Exported files, Agent task files and browser storage are kept.");
  await user.click(within(dialog).getByRole("button", { name: "Uninstall plugin" }));
  await waitFor(() => expect(craftPlugins.uninstall).toHaveBeenCalledWith("wordcraft", false));
});

it("does not preview, launch or open an editor when the catalog loads", async () => {
  present(); mount();
  await screen.findByRole("button", { name: "Open Word documents" });
  expect(craftPlugins.preview).not.toHaveBeenCalled();
  expect(craftPlugins.launch).not.toHaveBeenCalled();
  expect(window.open).not.toHaveBeenCalled();
  expect(invoke).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("opens a verified local editor with one click using the manager's launch ticket", async () => {
  present(); const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Open Word documents" }));
  await waitFor(() => expect(window.open).toHaveBeenCalledWith(editorUrls.wordcraft, "_blank", "noopener,noreferrer"));
  expect(craftPlugins.preview).toHaveBeenCalledExactlyOnceWith("wordcraft");
  expect(craftPlugins.launch).toHaveBeenCalledExactlyOnceWith("wordcraft", "local-launch-ticket");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(await screen.findByRole("link", { name: "Open in new window" })).toHaveAttribute("href", "http://127.0.0.1:8932/");
});

it.each([
  ["foreign-node", { node_id: "different-node" }],
  ["external", { url: "https://evil.test/run" }],
])("blocks a %s preview before requesting a launch", async (_label, invalid) => {
  present(); vi.mocked(craftPlugins.preview).mockResolvedValue({ ...launchPreview("wordcraft"), ...invalid });
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Open Word documents" }));
  await screen.findByRole("alert");
  expect(craftPlugins.launch).not.toHaveBeenCalled(); expect(window.open).not.toHaveBeenCalled();
  expect(invoke).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it.each([
  ["foreign-node", { node_id: "different-node" }],
  ["external", { url: "https://evil.test/run" }],
])("blocks a %s launch response even after a valid preview", async (_label, invalid) => {
  present(); vi.mocked(craftPlugins.launch).mockResolvedValue({ ...launchPreview("wordcraft"), ...invalid });
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Open Word documents" }));
  await screen.findByRole("alert");
  expect(craftPlugins.launch).toHaveBeenCalledWith("wordcraft", "local-launch-ticket");
  expect(window.open).not.toHaveBeenCalled();
  expect(invoke).not.toHaveBeenCalled();
  expect(screen.queryByRole("link", { name: "Open in new window" })).not.toBeInTheDocument();
});

it("opens a local editor directly from plugin management in Ryn", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  present(); const user = userEvent.setup(); mount(true);
  const wordCard = (await screen.findByRole("heading", { name: "Word documents" })).closest("article")!;
  expect(within(wordCard).getByText("Local editor")).toBeInTheDocument();
  await user.click(within(wordCard).getByRole("button", { name: "Open" }));
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("open_craft_browser", { appId: "wordcraft" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(window.open).not.toHaveBeenCalled();
  expect(await screen.findByText("The Ryn editor window is open and the editor is loading.")).toBeInTheDocument();
});

it("opens SoundCraft directly on every explicit click and obtains a fresh launch ticket", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  addAll(); present();
  vi.mocked(craftPlugins.preview)
    .mockResolvedValueOnce({ ...launchPreview("soundcraft"), ticket: "first-ticket" })
    .mockResolvedValueOnce({ ...launchPreview("soundcraft"), ticket: "second-ticket" });
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Audio & video" }));
  const open = screen.getByRole("button", { name: "Open Audio editing" });
  await user.click(open);
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(1));
  expect(craftPlugins.launch).toHaveBeenNthCalledWith(1, "soundcraft", "first-ticket");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  await user.click(open);
  await waitFor(() => expect(invoke).toHaveBeenCalledTimes(2));
  expect(craftPlugins.preview).toHaveBeenCalledTimes(2);
  expect(craftPlugins.launch).toHaveBeenNthCalledWith(2, "soundcraft", "second-ticket");
  expect(invoke).toHaveBeenLastCalledWith("open_craft_browser", { appId: "soundcraft" });
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(window.open).not.toHaveBeenCalled();
});

it.each(["preview", "launch"] as const)("allows retrying directly after a %s failure", async phase => {
  present(); vi.mocked(craftPlugins[phase]).mockRejectedValueOnce(new Error("plugin_operation_failed"));
  const user = userEvent.setup(); mount();
  const open = await screen.findByRole("button", { name: "Open Word documents" });
  await user.click(open);
  await screen.findByRole("alert");
  expect(window.open).not.toHaveBeenCalled();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(open).toBeEnabled();
  await user.click(open);
  await waitFor(() => expect(window.open).toHaveBeenCalledOnce());
  expect(craftPlugins.preview).toHaveBeenCalledTimes(2);
  expect(craftPlugins.launch).toHaveBeenCalledTimes(phase === "preview" ? 1 : 2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("reports native window failure and allows retrying without confirmation", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
  vi.mocked(invoke).mockRejectedValueOnce(new Error("plugin_window_failed"));
  present(); const user = userEvent.setup(); mount();
  const open = await screen.findByRole("button", { name: "Open Word documents" });
  await user.click(open);
  expect(await screen.findByRole("alert")).toHaveTextContent("The Ryn editor window could not open");
  expect(screen.queryByText("The Ryn editor window is open and the editor is loading.")).not.toBeInTheDocument();
  expect(screen.queryByRole("link", { name: "Open in new window" })).not.toBeInTheDocument();
  expect(window.open).not.toHaveBeenCalled();
  await user.click(open);
  expect(await screen.findByText("The Ryn editor window is open and the editor is loading.")).toBeInTheDocument();
  expect(craftPlugins.preview).toHaveBeenCalledTimes(2);
  expect(invoke).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

it("does not present information-only Agent tools as document editing", async () => {
  present(); state.plugins[1].warning = "cjk_font_coverage"; const user = userEvent.setup(); mount();
  expect(await screen.findByText("Limited Chinese font support")).toBeInTheDocument();
  await user.click((await screen.findAllByRole("button", { name: "Agent" }))[0]);
  expect(screen.getByRole("dialog")).toHaveTextContent("Document editing tools are not connected yet.");
  expect(craftPlugins.launch).not.toHaveBeenCalled();
});

it("includes complete English and Chinese resources and switches the UI", async () => {
  function keys(value: object, prefix = ""): string[] { return Object.entries(value).flatMap(([key, item]) => typeof item === "object" ? keys(item, `${prefix}${key}.`) : [`${prefix}${key}`]); }
  expect(keys(en).sort()).toEqual(keys(zh).sort());
  present(); setLanguagePreference("zh-CN"); mount();
  await screen.findByRole("heading", { name: "应用" });
  expect(screen.getByRole("button", { name: "打开 Word 文档" })).toBeEnabled();
  expect(screen.getByRole("link", { name: "管理插件" })).toBeInTheDocument();
});

it("offers all seventeen entries while selecting only the four defaults", async () => {
  addAll(); const user = userEvent.setup(); mount();
  await screen.findByRole("heading", { name: "Choose your tools" });
  await user.click(screen.getByText("Explore other tools"));
  expect(screen.getAllByRole("checkbox")).toHaveLength(17);
  expect(screen.getAllByRole("checkbox", { checked: true })).toHaveLength(4);
  const film = screen.getByText("Video editing").closest("label")!;
  expect(within(film).getByRole("checkbox")).toBeDisabled();
  expect(within(screen.getByText("Recruiting").closest("label")!).getByRole("checkbox")).not.toBeChecked();
  expect(craftPlugins.install).not.toHaveBeenCalled();
});

it("registers a lightweight business plugin without claiming a download and opens its fixed route", async () => {
  addAll(); state.setup_completed = true; const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Business apps" }));
  await user.click(screen.getByRole("button", { name: "Install Resume & cover letters" }));
  expect(await screen.findByRole("button", { name: "Add plugins" })).toBeEnabled();
  expect(screen.queryByRole("button", { name: "Install & download" })).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Add plugins" }));
  await user.click(await screen.findByRole("button", { name: "Open Resume & cover letters" }));
  await waitFor(() => expect(screen.getByTestId("location")).toHaveTextContent("/apps/workspaces?app=resume"));
  expect(craftPlugins.install).toHaveBeenCalledWith("resume");
  expect(craftPlugins.preview).not.toHaveBeenCalled();
  expect(window.open).not.toHaveBeenCalled();
});

it("manages NAS and business adapters in the same list with one switch per plugin", async () => {
  present(); const nas = builtin("nas"), resume = builtin("resume");
  for (const app of [nas, resume]) { app.installed = true; app.enabled = true; app.agent_tools = ["real_operation"]; }
  state.plugins.push(nas, resume); const user = userEvent.setup(); mount(true);
  expect(await screen.findByRole("switch", { name: "Disable NAS plugin" })).toBeInTheDocument();
  expect(screen.getAllByRole("switch")).toHaveLength(6);
  await user.click(screen.getByLabelText("More actions for Resume & cover letters"));
  await user.click(within(screen.getByLabelText("More actions for Resume & cover letters").closest("details")!).getByRole("button", { name: "Uninstall plugin" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("Original business data and connection settings are kept.");
  await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Uninstall plugin" }));
  await waitFor(() => expect(craftPlugins.uninstall).toHaveBeenCalledWith("resume", false));
});

it("rejects forged built-in launch routes and explains NAS resource permissions", async () => {
  const nas = builtin("nas"); nas.installed = true; nas.enabled = true; nas.agent_tools = ["ryn_read_resource"];
  state.plugins.push(nas); state.setup_completed = true;
  expect(() => verifiedPluginPath({ ...nas, launch_path: "https://evil.test" })).toThrow("plugin_launch_rejected");
  expect(() => verifiedPluginPath({ ...nas, id: "shell", launch_path: "/nas" })).toThrow("plugin_launch_rejected");
  const user = userEvent.setup(); mount();
  await user.click(await screen.findByRole("button", { name: "Storage & files" }));
  await user.click(screen.getByRole("button", { name: "Agent" }));
  expect(screen.getByRole("dialog")).toHaveTextContent("File access keeps existing resource scope and permissions");
  expect(craftPlugins.launch).not.toHaveBeenCalled();
});
