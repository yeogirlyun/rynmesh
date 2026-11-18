import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { fireEvent, within } from "@testing-library/dom";
import { quickEdits, sizeVariants } from "../../../plugins/image-studio/ryn_image_studio/web/variants.js";
import { state, submit } from "../../../plugins/image-studio/ryn_image_studio/web/api.js";
import { V, F, T } from "../../../plugins/image-studio/ryn_image_studio/web/i18n.js";

vi.mock("../../../plugins/image-studio/ryn_image_studio/web/api.js", async original => ({
  ...await original(), submit: vi.fn(async () => true),
}));
const job = {id: "test-original", scene: "banner", width: 1920, height: 600, transparent: true,
  words: "Legacy", copy: {title: "Original", subtitle: "Sub", badge: "New"}};
beforeEach(() => {
  vi.clearAllMocks();
  document.body.replaceChildren();
  HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new Event("close")); };
});
afterEach(() => {
  delete HTMLDialogElement.prototype.showModal;
  delete HTMLDialogElement.prototype.close;
});

it("excludes the current size, caps selection at six, and sends the selected pixel sizes", async () => {
  const sizes = [job, ...Array.from({length: 7}, (_, i) => ({width: 800 + i, height: 600}))];
  state.me = {scenes: {banner: {sizes}}};
  const form = sizeVariants(job);
  document.body.append(form);
  const checks = within(form).getAllByRole("checkbox");
  expect(checks).toHaveLength(7);
  checks.slice(0, 6).forEach(input => fireEvent.click(input));
  expect(checks[6]).toBeDisabled();
  fireEvent.click(checks[1]);
  expect(checks[6]).toBeEnabled();
  fireEvent.click(checks[6]);
  fireEvent.submit(form);
  expect(submit).toHaveBeenCalledWith({sizes: sizes.slice(1).filter((_, i) => i !== 1)}, "/api/jobs/test-original/derive");
});

it.each(["background", "text", "angle"])("submits a %s edit with optional notes and original identity", async mode => {
  document.body.append(quickEdits(job));
  fireEvent.click(within(document.body).getByRole("button", {name: V[mode]}));
  const dialog = within(document.querySelector("dialog"));
  if (mode === "text") {
    expect(dialog.getByLabelText(F.title)).toHaveValue("Original");
    expect(dialog.getByLabelText(F.subtitle)).toHaveValue("Sub");
    expect(dialog.getByLabelText(F.badge)).toHaveValue("New");
    fireEvent.input(dialog.getByLabelText(F.title), {target: {value: "Revised"}});
  }
  fireEvent.input(dialog.getByLabelText(F.brief), {target: {value: "Extra note"}});
  fireEvent.click(dialog.getByRole("button", {name: T.editSubmit}));
  expect(submit).toHaveBeenCalledWith(expect.objectContaining({edit: mode, source_job: job.id, transparent: true,
    brief: "Extra note", copy: mode === "text" ? {...job.copy, title: "Revised"} : job.copy}));
});
