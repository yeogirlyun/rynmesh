import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { FirstSuccessStatus } from "../domain/types";
import FirstSuccessFlow from "./FirstSuccessFlow";

const ready: FirstSuccessStatus = {
  version: "ryn.first-success.v1",
  phase: "ready",
  completed: false,
  dismissed: false,
  node_ready: true,
  content_ready: true,
  item_count: 6,
  healthy_sources: 4,
  source_count: 4,
  failed_sources: 0,
  degraded: false,
  using_cache: false,
  first_item_opened: false,
  first_signal_recorded: false,
  milestones: { node_ready: 1, content_ready: 1 },
  safe_error: null,
  recoverable_actions: [],
};

describe("FirstSuccessFlow", () => {
  it("moves from a real recommendation to a saved local signal", async () => {
    const client = makeFixtureNodeClient();
    const user = userEvent.setup();

    function Harness() {
      const [status, setStatus] = useState(ready);
      return (
        <MemoryRouter>
          <FirstSuccessFlow client={client} status={status} onStatusChange={setStatus} onClose={vi.fn()} />
        </MemoryRouter>
      );
    }

    render(<Harness />);
    expect(screen.getByRole("heading", { name: "Open one real recommendation" })).toBeInTheDocument();

    const item = await screen.findByRole("button", { name: /Mira Studio micro-essays/ });
    await user.click(item);
    expect(await screen.findByRole("heading", { name: "Save one useful signal" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Save and show me more like this" }));
    expect(await screen.findByRole("heading", { name: "Your Ryn is already learning" })).toBeInTheDocument();
    expect(screen.getByText("Open one").closest("span")).toHaveClass("done");
    expect(screen.getByText("Save a choice").closest("span")).toHaveClass("done");
  });

  it("explains a recoverable source failure without asking for a model or peer", () => {
    const status: FirstSuccessStatus = {
      ...ready,
      phase: "needs_action",
      content_ready: false,
      item_count: 0,
      safe_error: "discovery_unavailable",
      recoverable_actions: ["retry_discovery"],
    };
    render(
      <MemoryRouter>
        <FirstSuccessFlow client={makeFixtureNodeClient()} status={status} onStatusChange={vi.fn()} onClose={vi.fn()} />
      </MemoryRouter>,
    );

    expect(screen.getByRole("heading", { name: "The sources need another try" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry discovery" })).toBeEnabled();
    expect(screen.queryByText(/AI model/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/connect a peer/i)).not.toBeInTheDocument();
  });
});
