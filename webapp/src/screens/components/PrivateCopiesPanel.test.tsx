import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { friendsApi } from "../../domain/friendsClient";
import PrivateCopiesPanel from "./PrivateCopiesPanel";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });

it("shows retained data and clears copies only after confirmation", async () => {
  vi.spyOn(friendsApi, "documents").mockResolvedValueOnce({ documents: [{ import_id: "imp", filename: "Private reading.txt", state: "ready", size_bytes: 512, created_at_unix: 0 }] })
    .mockResolvedValue({ documents: [] });
  const clear = vi.spyOn(friendsApi, "clearDocuments").mockResolvedValue({ removed: 1 });
  const user = userEvent.setup();
  render(<PrivateCopiesPanel />);
  await screen.findByRole("button", { name: "Remove Private reading.txt" });
  await user.click(screen.getByRole("button", { name: "Clear document copies" }));
  expect(clear).not.toHaveBeenCalled();
  const request = confirm.mock.calls[0][0];
  expect(request.body).toContain("Messages, cards and reading bookmarks remain");
  await act(() => request.onConfirm());
  expect(clear).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("status")).toHaveTextContent("1 local document copy removed");
  expect(screen.getByRole("button", { name: "Clear document copies" })).toBeDisabled();
});

it("does not report a failed removal as successful", async () => {
  vi.spyOn(friendsApi, "documents").mockResolvedValue({ documents: [{ import_id: "imp", filename: "Private reading.txt", state: "ready", size_bytes: 512, created_at_unix: 0 }] });
  vi.spyOn(friendsApi, "clearDocuments").mockRejectedValue(new Error("Storage unavailable"));
  const user = userEvent.setup();
  render(<PrivateCopiesPanel />);
  await screen.findByRole("button", { name: "Remove Private reading.txt" });
  await user.click(screen.getByRole("button", { name: "Clear document copies" }));
  await expect(confirm.mock.calls[0][0].onConfirm()).rejects.toThrow("Storage unavailable");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Remove Private reading.txt" })).toBeInTheDocument();
});
