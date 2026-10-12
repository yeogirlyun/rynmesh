import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { PreferencesEditor, SkillSelection } from "./RynAgentGuidance";

it("loads optional skills explicitly and grants none by default", async () => {
  const changed = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/skills", () => HttpResponse.json({ skills: [{ id: "resource-report", title: "Report", version: "1", description: "Source report" }] })));
  render(<SkillSelection value={[]} onChange={changed} disabled={false} />);
  expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Skills · optional" }));
  const choice = await screen.findByRole("checkbox");
  expect(choice).not.toBeChecked(); expect(changed).not.toHaveBeenCalled();
  await user.click(choice);
  expect(changed).toHaveBeenCalledWith(["resource-report"]);
});

it("saves owner preference edits and deletions with the loaded revision", async () => {
  const received = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/preferences", () => HttpResponse.json({ revision: 4, items: [{ id: "style", text: "Use bullet points" }] })),
    http.put("*/api/local/agent/preferences", async ({ request }) => { const body = await request.json() as { items: unknown[] }; received(body); return HttpResponse.json({ revision: 5, items: body.items }); }));
  render(<PreferencesEditor />);
  await user.click(screen.getByRole("button", { name: "My preferences" }));
  await screen.findByDisplayValue("Use bullet points");
  await user.click(screen.getByRole("button", { name: "Remove" }));
  await user.click(screen.getByRole("button", { name: "Save preferences" }));
  await waitFor(() => expect(received).toHaveBeenCalledWith({ expected_revision: 4, items: [] }));
});

it("does not overwrite a concurrent owner change after a revision conflict", async () => {
  const received = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/preferences", () => HttpResponse.json({ revision: 4, items: [] })),
    http.put("*/api/local/agent/preferences", async ({ request }) => { received(await request.json()); return HttpResponse.json({ detail: "agent_preferences_revision_conflict" }, { status: 409 }); }));
  render(<PreferencesEditor />);
  await user.click(screen.getByRole("button", { name: "My preferences" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "Add preference" })).toBeEnabled());
  await user.click(screen.getByRole("button", { name: "Add preference" }));
  await user.type(screen.getByLabelText("Preference 1"), "Keep reports concise");
  await user.click(screen.getByRole("button", { name: "Save preferences" }));
  await screen.findByRole("alert");
  expect(received).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("button", { name: "Retry original save" })).toBeDisabled();
  expect(screen.getByLabelText("Preference 1")).toBeDisabled();
});
