import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { it, expect, vi } from "vitest";
import NodeSiteOwner from "./NodeSiteOwner";
import { nodeSites } from "../domain/nodeSites";
vi.mock("../domain/nodeSites", () => ({ nodeSites: { mine: vi.fn() } }));
const show = () => render(<MemoryRouter initialEntries={["/apps/sites/example"]}><Routes><Route path="/apps/sites/:siteId" element={<NodeSiteOwner />} /></Routes></MemoryRouter>);
it("hosts an arbitrary installed plugin without application-specific code", async () => {
  vi.mocked(nodeSites.mine).mockResolvedValue({ sites: [{ site_id: "example", title: "Example", owner_path: "/api/local/site-plugins/example/owner/" } as never], members: [] });
  show();
  expect(await screen.findByTitle("Example")).toHaveAttribute("src", expect.stringContaining("/api/local/site-plugins/example/owner/?lang="));
});
it("rejects external or unrelated owner URLs", async () => {
  vi.mocked(nodeSites.mine).mockResolvedValue({ sites: [{ site_id: "example", title: "Example", owner_path: "https://example.invalid/" } as never], members: [] });
  show();
  expect(await screen.findByRole("alert")).toBeInTheDocument();
  expect(screen.queryByTitle("Example")).toBeNull();
});
