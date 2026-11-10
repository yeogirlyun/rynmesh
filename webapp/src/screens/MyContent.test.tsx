import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Outlet, Route, Routes, useLocation } from "react-router-dom";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import MyContent from "./MyContent";

function context(): AppOutletContext {
  return {
    client: makeFixtureNodeClient(),
    node: { node_name: "Test", peer_id: "self", daemon_running: true, registry: "connected",
      peer_count: 0, local_items: 1, fetched_items: 1, pending_recs: 0, version: "test", uptime_seconds: 1 },
    registry: { status: "connected", url: "local" }, peers: [], refreshShell: vi.fn(async () => undefined),
    confirm: vi.fn(), notify: vi.fn(),
  };
}

function SearchEcho() {
  return <div data-testid="ask-location">{useLocation().search}</div>;
}

function setup(entry = "/library") {
  const user = userEvent.setup();
  const appContext = context();
  render(<MemoryRouter initialEntries={[entry]}><Routes><Route element={<Outlet context={appContext} />}>
    <Route path="library" element={<MyContent />} /><Route path="ask" element={<SearchEcho />} />
  </Route></Routes></MemoryRouter>);
  return { user, appContext };
}

afterEach(() => cleanup());

describe("My Content", () => {
  it("shows saved, continue, history, files, sources, and archived views", async () => {
    const { user } = setup();
    expect(await screen.findByText("Designing private assistants people can trust")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(await screen.findByText("42% complete")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "History" }));
    expect(await screen.findByText("A calm guide to friend-to-friend sharing")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Files" }));
    expect(await screen.findByText("Research notes.md")).toBeInTheDocument();
    expect(screen.getByText("Product review.pdf")).toBeInTheDocument();
  });

  it("imports and previews a private managed file", async () => {
    const { user, appContext } = setup();
    await screen.findByText("Designing private assistants people can trust");
    const input = document.querySelector('input[type="file"]') as HTMLInputElement;
    await user.upload(input, new File(["private import"], "meeting.md", { type: "text/markdown" }));
    expect(await screen.findByText("meeting.md")).toBeInTheDocument();
    expect(appContext.notify).toHaveBeenCalledWith("ok", expect.stringContaining("private managed storage"));
    const card = screen.getByText("meeting.md").closest("article")!;
    await user.click(card.querySelector("button")!);
    expect(await screen.findByRole("dialog", { name: "Preview meeting.md" })).toBeInTheDocument();
  });

  it("hands multiple opaque Library IDs to Ask Ryn", async () => {
    const { user } = setup("/library?view=files");
    await screen.findByText("Research notes.md");
    await user.click(screen.getByRole("checkbox", { name: "Select Research notes.md" }));
    await user.click(screen.getByRole("checkbox", { name: "Select Product review.pdf" }));
    await user.click(screen.getByRole("button", { name: "Ask Ryn" }));
    const search = screen.getByTestId("ask-location").textContent ?? "";
    expect(search).toContain("material=import%3Aimp_fixture_notes");
    expect(search).toContain("material=import%3Aimp_fixture_pdf");
    expect(search).not.toContain("Research+notes");
  });

  it("pauses and resumes a source without removing it", async () => {
    const { user } = setup();
    await screen.findByText("Designing private assistants people can trust");
    await user.click(screen.getByRole("button", { name: "Sources & monitors" }));
    const title = await screen.findByText("Local First");
    const row = title.closest("article")!;
    await user.click(row.querySelector("button")!);
    await waitFor(() => expect(screen.getAllByRole("button", { name: "Resume" }).length).toBeGreaterThan(0));
  });

  it("requires confirmation before deleting only Ryn's managed copy", async () => {
    const { user, appContext } = setup("/library?view=files");
    await screen.findByText("Research notes.md");
    const card = screen.getByText("Research notes.md").closest("article")!;
    await user.click(Array.from(card.querySelectorAll("button")).find((button) => button.textContent?.includes("Delete copy"))!);
    expect(appContext.confirm).toHaveBeenCalledOnce();
    const request = vi.mocked(appContext.confirm).mock.calls[0][0];
    expect(request.body).toContain("original file is never modified or removed");
    await request.onConfirm();
    await waitFor(() => expect(screen.queryByText("Research notes.md")).not.toBeInTheDocument());
  });
});
