import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { digestApi, type DiscoveryStatus } from "../domain/digestClient";
import SourceHealthPanel from "./SourceHealthPanel";

const status = {
  source_health: [
    { id: "one", title: "Working feed", ok: true, status: "healthy", error: "", item_count: 4, last_checked_unix: 100, last_success_unix: 100, consecutive_failures: 0, using_cached_items: false },
    { id: "two", title: "Cached feed", ok: false, status: "cached", error: "source_fetch_failed", item_count: 3, last_checked_unix: 120, last_success_unix: 90, consecutive_failures: 2, using_cached_items: true },
  ],
} as DiscoveryStatus;

afterEach(() => vi.restoreAllMocks());

describe("Source health", () => {
  it("shows healthy and cached source details and retries only the selected source", async () => {
    const retry = vi.spyOn(digestApi, "retrySource").mockResolvedValue({ status } as Awaited<ReturnType<typeof digestApi.retrySource>>);
    const all = vi.spyOn(digestApi, "refreshDigest");
    const refresh = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<SourceHealthPanel status={status} onRefresh={refresh} />);
    expect(screen.getByText("Available")).toBeInTheDocument();
    expect(screen.getByText("Using cached content")).toBeInTheDocument();
    expect(screen.getByText(/Consecutive failures: 2/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry Cached feed" }));
    expect(retry).toHaveBeenCalledExactlyOnceWith("two");
    expect(all).not.toHaveBeenCalled();
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
  });

  it("offers recovery when a retry fails, preserving the source list", async () => {
    vi.spyOn(digestApi, "retrySource").mockRejectedValue(new Error("private token"));
    const user = userEvent.setup();
    render(<SourceHealthPanel status={status} onRefresh={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: "Retry Cached feed" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("wait and retry");
    expect(screen.queryByText("private token")).not.toBeInTheDocument();
    expect(screen.getByText("Working feed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry Cached feed" })).toBeEnabled();
  });

  it("distinguishes loading and empty states", () => {
    const { rerender } = render(<SourceHealthPanel status={null} onRefresh={vi.fn()} />);
    expect(screen.getByRole("status")).toHaveTextContent("Checking");
    rerender(<SourceHealthPanel status={{ ...status, source_health: [] }} onRefresh={vi.fn()} />);
    expect(screen.getByText("No sources have been configured.")).toBeInTheDocument();
  });
});
