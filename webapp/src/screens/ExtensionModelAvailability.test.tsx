import { render, screen, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { server } from "../test/server";
import { ExtensionModelAvailability } from "./ExtensionModelAvailability";

const url = "http://localhost/api/local/agent/extensions/model-availability";
describe("model-specific extension availability", () => {
  it("shows host-projected restrictions and removes them when selecting an allowed model", async () => {
    server.use(http.get(url, ({ request }) => HttpResponse.json({ servers: [
      { name: "Private tools", allowed: new URL(request.url).searchParams.get("model_id") === "approved" },
      { name: "Open tools", allowed: true },
    ] })));
    const view = render(<ExtensionModelAvailability modelID="blocked" />);
    expect(await screen.findByRole("status")).toHaveTextContent("Tools unavailable with this model: Private tools");
    expect(screen.getByRole("status")).not.toHaveTextContent("Open tools");
    view.rerender(<ExtensionModelAvailability modelID="approved" />);
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull());
  });
  it("keeps failures fixed and never displays backend error text", async () => {
    server.use(http.get(url, () => HttpResponse.json({ detail: "private-canary" }, { status: 503 })));
    render(<ExtensionModelAvailability modelID="test" />);
    expect(await screen.findByRole("status")).toHaveTextContent("Calls will still be validated.");
    expect(document.body.textContent).not.toContain("private-canary");
  });
  it("does not fetch before a model is selected", () => {
    render(<ExtensionModelAvailability modelID="" />);
    expect(screen.queryByRole("status")).toBeNull();
  });
});
