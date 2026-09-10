import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, it, vi } from "vitest";
import type { ConfirmRequest } from "../domain/types";
import { ConfirmDialog } from "./ui";

it("keeps failed operations reviewable and prevents duplicate submissions", async () => {
  let finish!: () => void;
  const operation = vi.fn().mockRejectedValueOnce(new Error("Storage unavailable. Retry."))
    .mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; }));
  function Harness() {
    const [request, setRequest] = useState<ConfirmRequest | null>({ title: "Clear copies?", body: "Bookmarks remain.", risk: "high", confirmLabel: "Clear copies", onConfirm: operation });
    return <ConfirmDialog request={request} onCancel={() => setRequest(null)} />;
  }
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Clear copies" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Storage unavailable");
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  const button = screen.getByRole("button", { name: "Clear copies" });
  fireEvent.click(button); fireEvent.click(button);
  await waitFor(() => expect(operation).toHaveBeenCalledTimes(2));
  expect(screen.getByRole("button", { name: "Working…" })).toBeDisabled();
  expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
  await act(async () => finish());
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
