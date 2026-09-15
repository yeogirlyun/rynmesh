import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import ReviewedCleanupPanel, { type CleanupApi, type CleanupConfig, type CleanupJob, type CleanupReview } from "./ReviewedCleanupPanel";

interface Review extends CleanupReview { items: number }

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });

const review: Review = { review_token: "a".repeat(64), items: 3 };
const finished: CleanupJob = { id: review.review_token, sequence: 1, done: ["source"], pending: [], cancelled: false, local_copies_complete: true, remote_confirmed: false };

function makeApi(overrides: Partial<CleanupApi<Review>> = {}): CleanupApi<Review> {
  return {
    status: vi.fn().mockResolvedValue(null),
    preview: vi.fn().mockResolvedValue(review),
    begin: vi.fn().mockResolvedValue(finished),
    resume: vi.fn().mockResolvedValue(finished),
    reviewBackups: vi.fn().mockResolvedValue({ review_token: "backup", files: 1, bytes: 1024 }),
    approveBackups: vi.fn().mockResolvedValue(finished),
    ...overrides,
  };
}
const config: CleanupConfig<Review> = {
  noun: "widget", title: "Widget data", scope: "Clears reviewed widgets.",
  result: "Reviewed widgets cleared on this node.", resultConfirmed: "Reviewed widgets cleared on this node. Remote devices confirmed.",
  backupScope: "New widgets remain.",
  labels: { source: "Widget records" },
  counts: (value) => `Widgets: ${value.items}.`,
  invalidReview: () => false,
};

it("shows no previous cleanup, then reviews and clears widget data after confirmation", async () => {
  const api = makeApi();
  const user = userEvent.setup();
  render(<ReviewedCleanupPanel api={api} config={config} />);
  expect(await screen.findByText("No previous widget cleanup.")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Review widget data" }));
  expect(await screen.findByLabelText("Reviewed widget scope")).toBeInTheDocument();
  expect(screen.getByText("Widgets: 3.")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Clear reviewed widget copies" }));
  expect(api.begin).not.toHaveBeenCalled();
  expect(confirm.mock.calls[0][0].body).toBe("Widgets: 3. Clears reviewed widgets.");
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(api.begin).toHaveBeenCalledExactlyOnceWith(review.review_token);
  expect(screen.getByRole("status")).toHaveTextContent("Reviewed widgets cleared on this node.");
});

it("shows remote confirmation copy when the node confirms other devices are cleared", async () => {
  const api = makeApi({ begin: vi.fn().mockResolvedValue({ ...finished, remote_confirmed: true }) });
  const user = userEvent.setup();
  render(<ReviewedCleanupPanel api={api} config={config} />);
  await screen.findByText("No previous widget cleanup.");
  await user.click(screen.getByRole("button", { name: "Review widget data" }));
  await user.click(await screen.findByRole("button", { name: "Clear reviewed widget copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.getByRole("status")).toHaveTextContent("Remote devices confirmed.");
});

it("shows the failure copy and disables review when initial status cannot be loaded", async () => {
  const api = makeApi({ status: vi.fn().mockRejectedValue(new Error("Node unavailable")) });
  render(<ReviewedCleanupPanel api={api} config={config} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("Node unavailable");
  expect(screen.getByRole("button", { name: "Review widget data" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Refresh widget cleanup" })).toBeEnabled();
});

it("retries the original attempt with the same review token after a lost begin response", async () => {
  const api = makeApi({ begin: vi.fn().mockRejectedValueOnce(new Error("Unconfirmed")).mockResolvedValueOnce(finished) });
  const user = userEvent.setup();
  render(<ReviewedCleanupPanel api={api} config={config} />);
  await screen.findByText("No previous widget cleanup.");
  await user.click(screen.getByRole("button", { name: "Review widget data" }));
  await user.click(await screen.findByRole("button", { name: "Clear reviewed widget copies" }));
  await act(() => confirm.mock.calls[0][0].onConfirm());
  expect(screen.getByRole("alert")).toHaveTextContent("Unconfirmed");
  await user.click(screen.getByRole("button", { name: "Retry original widget cleanup" }));
  expect(api.begin).toHaveBeenNthCalledWith(1, review.review_token);
  expect(api.begin).toHaveBeenNthCalledWith(2, review.review_token);
  expect(await screen.findByText("Reviewed local widget copies cleared")).toBeInTheDocument();
});
