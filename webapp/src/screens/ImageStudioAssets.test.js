import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { fireEvent, within } from "@testing-library/dom";
import { browseAssets, saveAsset } from "../../../plugins/image-studio/ryn_image_studio/web/assets.js";
import { state } from "../../../plugins/image-studio/ryn_image_studio/web/api.js";
import { A } from "../../../plugins/image-studio/ryn_image_studio/web/i18n.js";

const mine = {id: "123", kind: "product", name: "Bottle"};
const shared = {id: "456", kind: "style", name: "Palette"};
const library = {mine: [mine], public: [shared]};
const query = () => within(document.querySelector("dialog"));
beforeEach(() => {
  state.me = {is_owner: false};
  document.body.innerHTML = '<div id="toast"></div>';
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(library))));
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event("close")); };
});
afterEach(() => {
  delete HTMLDialogElement.prototype.showModal;
  delete HTMLDialogElement.prototype.close;
  vi.unstubAllGlobals();
});

it("searches public assets and selects the item with its original kind", async () => {
  const choose = vi.fn(() => true);
  await browseAssets({manage: false, choose});
  fireEvent.click(query().getByRole("button", {name: A.public}));
  const search = query().getByRole("searchbox");
  fireEvent.input(search, {target: {value: "xyz"}});
  expect(query().getByText(A.noMatches)).toBeVisible();
  fireEvent.input(search, {target: {value: "PAL"}});
  fireEvent.click(query().getByRole("button", {name: /Palette/}));
  expect(choose).toHaveBeenCalledWith(shared);
});

it.each([false, true])("enforces owner=%s for public management", async owner => {
  state.me.is_owner = owner;
  const removed = vi.fn();
  await browseAssets({manage: true, removed});
  expect(query().getByRole("button", {name: A.remove})).toBeVisible();
  fireEvent.click(query().getByRole("button", {name: A.public}));
  expect(Boolean(query().queryByRole("button", {name: A.remove}))).toBe(owner);
  if (owner) {
    fireEvent.click(query().getByRole("button", {name: A.remove}));
    fireEvent.click(query().getByRole("button", {name: A.confirm}));
    await vi.waitFor(() => expect(removed).toHaveBeenCalledWith(shared.id));
    expect(fetch).toHaveBeenLastCalledWith("/api/assets/456", expect.objectContaining({method: "DELETE"}));
  }
});

it("explains a full library and keeps the name and type available for editing", async () => {
  fetch.mockResolvedValue(new Response(JSON.stringify({code: "image_asset_limit"}), {status: 409}));
  saveAsset({file: new File(["data"], "sample.png", {type: "image/png"}), role: "style"});
  expect(query().queryByRole("checkbox")).toBeNull();
  expect(query().getByRole("combobox")).toHaveValue("style");
  fireEvent.submit(document.querySelector("dialog form"));
  await vi.waitFor(() => expect(query().getByRole("alert")).toHaveTextContent(A.image_asset_limit));
  expect(query().getByRole("textbox")).toHaveValue("sample");
});
