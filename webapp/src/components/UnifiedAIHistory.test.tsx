import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, expect, it, vi } from "vitest";
import { askHistory } from "../domain/askHistory";
import { createConversation, saveConversation, clearConversations } from "../domain/llmConversationStore";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import UnifiedAIHistory from "./UnifiedAIHistory";

afterEach(async () => { vi.restoreAllMocks(); await clearConversations("provider::workspace-history-test"); });
function mount() {
  const client = makeFixtureNodeClient(); client.mode = "live";
  function Workspace() { const location = useLocation(); return <><output>{location.search}</output><UnifiedAIHistory rows={[]} storage="node" onNew={vi.fn()} /></>; }
  render(<MemoryRouter initialEntries={["/ask"]}><Routes><Route element={<Outlet context={{ client }} />}><Route path="/ask" element={<Workspace />} /></Route></Routes></MemoryRouter>);
}
it("keeps equal node and browser IDs distinct and opens each original store", async () => {
  const browser = { ...createConversation({ serviceKey: "provider::workspace-history-test", serviceName: "Original model", providerPeerId: "provider", networkId: "original-network" }), id: "same-id", title: "Browser original" };
  await saveConversation(browser);
  vi.spyOn(askHistory, "list").mockResolvedValue([{ ...browser, title: "Node copy", revision: 1 }]);
  const migrate = vi.spyOn(askHistory, "importLegacy");
  mount(); const user = userEvent.setup();
  await user.click(await screen.findByRole("button", { name: /Browser original/ }));
  expect(screen.getByRole("status")).toHaveTextContent("storage=browser");
  expect(screen.getByRole("status")).toHaveTextContent("network=original-network");
  await user.click(screen.getByRole("button", { name: /Node copy/ }));
  expect(screen.getByRole("status")).toHaveTextContent("storage=node");
  expect(migrate).not.toHaveBeenCalled();
});
it("retains browser originals when node history fails and allows retry", async () => {
  const browser = { ...createConversation({ serviceKey: "provider::workspace-history-test", serviceName: "Original model", providerPeerId: "provider", networkId: "original-network" }), title: "Recoverable browser history" };
  await saveConversation(browser);
  vi.spyOn(askHistory, "list").mockRejectedValueOnce(new Error("offline")).mockResolvedValue([]);
  mount(); const user = userEvent.setup();
  expect(await screen.findByRole("alert")).toHaveTextContent("Node history could not be loaded.");
  expect(screen.getByRole("button", { name: /Recoverable browser history/ })).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Retry history" }));
  expect(await screen.findByRole("button", { name: /Recoverable browser history/ })).toBeInTheDocument();
});
