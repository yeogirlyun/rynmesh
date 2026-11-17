import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import AccessPanel from "./AccessPanel";

afterEach(() => { cleanup(); delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__; });

it("copies the desktop launch token from Settings without displaying it", async () => {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { configurable: true, value: { invoke: vi.fn(async () => "launch-token") } });
  const user = userEvent.setup();
  const write = vi.spyOn(navigator.clipboard, "writeText");
  render(<AccessPanel />);
  await user.click(screen.getByRole("button", { name: "Copy local token" }));
  expect(write).toHaveBeenCalledWith("launch-token");
  expect(screen.queryByText("launch-token")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();
});
