import { render, screen, waitFor } from "@testing-library/react";
import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { type AgentTask } from "../domain/rynAgent";
import { TaskArtifacts, TurnDeliverables } from "./RynAgentArtifacts";
import "./rynAgentText";

const task = { id: "inventory-task", title: "Inventory", status: "completed", events: [], messages: [],
  result: "Done", control_revision: 1, scope_revision: 1 } as unknown as AgentTask;

it("S05-I30 shows incomplete and omitted changes even when no files were collected", async () => {
  server.use(http.get("*/api/local/agent/tasks/inventory-task/artifacts", () => HttpResponse.json({
    artifacts: [], files: [], incomplete_inventory: true, additional_changes: 36,
    inventory_by_turn: { "0": { incomplete_inventory: true, additional_changes: 36 } },
  })));
  render(<TaskArtifacts task={task} />);
  expect(await screen.findByText("Files produced by the command may be missing from this list")).toBeVisible();
  expect(screen.getByText("36 more file changes")).toBeVisible();
});

it("S05-I30 keeps an earlier incomplete inventory warning in its own turn", async () => {
  server.use(http.get("*/api/local/agent/tasks/inventory-task/artifacts", () => HttpResponse.json({
    artifacts: [], files: [], incomplete_inventory: true, additional_changes: 100,
    inventory_by_turn: { "0": { incomplete_inventory: true, additional_changes: 100 } },
  })));
  render(<TaskArtifacts task={task} maxTurnIndex={1}><TurnDeliverables turnIndex={0} /></TaskArtifacts>);
  expect(await screen.findByText("100 more file changes")).toBeVisible();
  expect(screen.getAllByText("Files produced by the command may be missing from this list")).toHaveLength(1);
});

it("S05-I30 treats legacy listings without inventory fields as complete", async () => {
  let called = false;
  server.use(http.get("*/api/local/agent/tasks/inventory-task/artifacts", () => {
    called = true;
    return HttpResponse.json({ artifacts: [], files: [] });
  }));
  const view = render(<TaskArtifacts task={task} />);
  await waitFor(() => expect(called).toBe(true));
  expect(view.container).toBeEmptyDOMElement();
});
