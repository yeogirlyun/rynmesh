import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { MemoryEditor, PreferencesEditor, SkillSelection } from "./RynAgentGuidance";

const DEST_LOCAL_API = {
  catalog_model_id: "gpt-4.1-mini",
  provider_peer_id: "local_node",
  source_id: "src-openai",
  destination_revision: "rev-101",
};

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

it("separates active facts from pending candidates and requires explicit destination choice before enabling capture", async () => {
  const settingsCalls: Array<Record<string, unknown>> = [];
  const decisionCalls: Array<{ id: string; body: Record<string, unknown> }> = [];
  const user = userEvent.setup();

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [
        { label: "GPT-4.1 Mini · My OpenAI", destination: DEST_LOCAL_API, available: true },
      ],
      default_destination: DEST_LOCAL_API,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: 0,
      reset_epoch: 0,
      policy_revision: 1,
      settings: {
        capture_enabled: false,
        use_enabled: true,
        activation_policy: "owner_review",
        extraction_model_id: "",
        extraction_destination: null,
        default_exposure: { mode: "selected_models", allowed_destinations: [] },
      },
      entries: [
        {
          id: "fact-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "preference",
          fact_key: "ts_strict",
          text: "Owner prefers TypeScript strict mode",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "manual_owner",
          sources: [
            {
              source_type: "task_message",
              task_id: "task-42",
              message_id: "msg-2",
              conversation_revision: 1,
              context_epoch: 0,
              source_revision: 1,
              content_sha256: "abc123",
              author_role: "owner",
              project_id: null,
              excerpt: "Always enable strict TypeScript",
            },
          ],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "local_only", allowed_destinations: [], revision: 1 },
          created_at: 1000,
          updated_at: 1000,
          last_used_at: null,
          reviewed_by_owner_at: 1000,
        },
      ],
      candidates: [
        {
          id: "cand-1",
          scope: { kind: "project", owner_id: "local_owner", project_id: "proj-alpha", project_generation: 3 },
          category: "workflow",
          fact_key: "test_runner",
          text: "Project uses Vitest for unit tests",
          kind: "fact",
          status: "pending",
          revision: 3,
          origin: "background_extraction",
          job_id: "job-1",
          reset_epoch: 0,
          sources: [
            {
              source_type: "task_message",
              task_id: "task-99",
              message_id: "msg-7",
              project_id: "proj-alpha",
              excerpt: "We run vitest in this repo",
            },
          ],
          target_entry_id: null,
          expected_entry_revision: null,
          exposure_policy: { mode: "destinations", allowed_destinations: [DEST_LOCAL_API], revision: 1 },
          created_at: 2000,
          updated_at: 2000,
        },
      ],
      tombstone_count: 0,
    })),
    http.put("*/api/local/agent/memory/settings", async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      settingsCalls.push(body);
      const dest = (body.extraction_destination as { catalog_model_id?: string } | null | undefined) ?? null;
      return HttpResponse.json({
        schema_version: 1,
        generation: 1,
        reset_epoch: 0,
        policy_revision: 2,
        settings: {
          capture_enabled: Boolean(body.capture_enabled),
          use_enabled: true,
          activation_policy: (body.activation_policy as "automatic" | "owner_review") || "automatic",
          extraction_model_id: dest?.catalog_model_id || "",
          extraction_destination: dest,
          default_exposure: (body.default_exposure as { mode: "selected_models"; allowed_destinations: unknown[] }) || {
            mode: "selected_models",
            allowed_destinations: dest ? [dest] : [],
          },
        },
        entries: [
          {
            id: "fact-1",
            scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
            category: "preference",
            fact_key: "ts_strict",
            text: "Owner prefers TypeScript strict mode",
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "manual_owner",
            sources: [
              {
                source_type: "task_message",
                task_id: "task-42",
                message_id: "msg-2",
                project_id: null,
                excerpt: "Always enable strict TypeScript",
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "local_only", allowed_destinations: [], revision: 1 },
            created_at: 1000,
            updated_at: 1000,
            last_used_at: null,
            reviewed_by_owner_at: 1000,
          },
        ],
        candidates: [
          {
            id: "cand-1",
            scope: { kind: "project", owner_id: "local_owner", project_id: "proj-alpha", project_generation: 3 },
            category: "workflow",
            fact_key: "test_runner",
            text: "Project uses Vitest for unit tests",
            kind: "fact",
            status: "pending",
            revision: 3,
            origin: "background_extraction",
            job_id: "job-1",
            reset_epoch: 0,
            sources: [
              {
                source_type: "task_message",
                task_id: "task-99",
                message_id: "msg-7",
                project_id: "proj-alpha",
                excerpt: "We run vitest in this repo",
              },
            ],
            target_entry_id: null,
            expected_entry_revision: null,
            exposure_policy: { mode: "destinations", allowed_destinations: [DEST_LOCAL_API], revision: 1 },
            created_at: 2000,
            updated_at: 2000,
          },
        ],
        tombstone_count: 0,
      });
    }),
    http.post("*/api/local/agent/memory/candidates/:id/decision", async ({ params, request }) => {
      const body = await request.json() as Record<string, unknown>;
      decisionCalls.push({ id: String(params.id), body });
      return HttpResponse.json({
        schema_version: 1,
        generation: 2,
        reset_epoch: 0,
        policy_revision: 2,
        settings: {
          capture_enabled: true,
          use_enabled: true,
          activation_policy: "automatic",
          extraction_model_id: DEST_LOCAL_API.catalog_model_id,
          extraction_destination: DEST_LOCAL_API,
          default_exposure: { mode: "selected_models", allowed_destinations: [DEST_LOCAL_API] },
        },
        entries: [
          {
            id: "fact-1",
            scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
            category: "preference",
            fact_key: "ts_strict",
            text: "Owner prefers TypeScript strict mode",
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "manual_owner",
            sources: [
              {
                source_type: "task_message",
                task_id: "task-42",
                message_id: "msg-2",
                project_id: null,
                excerpt: "Always enable strict TypeScript",
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "local_only", allowed_destinations: [], revision: 1 },
            created_at: 1000,
            updated_at: 1000,
            last_used_at: null,
            reviewed_by_owner_at: 1000,
          },
          {
            id: "fact-2",
            scope: { kind: "project", owner_id: "local_owner", project_id: "proj-alpha", project_generation: 3 },
            category: "workflow",
            fact_key: "test_runner",
            text: "Project uses Vitest for unit tests",
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "accepted_extraction",
            sources: [
              {
                source_type: "task_message",
                task_id: "task-99",
                message_id: "msg-7",
                project_id: "proj-alpha",
                excerpt: "We run vitest in this repo",
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "destinations", allowed_destinations: [DEST_LOCAL_API], revision: 1 },
            created_at: 2000,
            updated_at: 2005,
            last_used_at: null,
            reviewed_by_owner_at: 2005,
          },
        ],
        candidates: [],
        tombstone_count: 0,
      });
    }),
  );

  render(<MemoryEditor />);
  await user.click(screen.getByRole("button", { name: "Long-term memory" }));

  const savedSection = await screen.findByRole("region", { name: "Saved facts" });
  const savedList = within(savedSection).getByRole("list");
  const candidateSection = screen.getByRole("region", { name: "Candidate memories (pending review)" });

  expect(within(savedList).getByText("Owner prefers TypeScript strict mode")).toBeInTheDocument();
  expect(within(savedList).getByText(/Chat task-42 · Message msg-2/)).toBeInTheDocument();
  expect(within(savedList).getByText(/Always enable strict TypeScript/)).toBeInTheDocument();
  expect(within(savedList).getByText(/Store on this device; never send to models/)).toBeInTheDocument();
  expect(within(savedSection).queryByText("Project uses Vitest for unit tests")).not.toBeInTheDocument();
  expect(within(candidateSection).getByText("Project uses Vitest for unit tests")).toBeInTheDocument();
  expect(within(candidateSection).getByText(/Local project: proj-alpha/)).toBeInTheDocument();
  expect(within(candidateSection).queryByText(/gen 3/)).not.toBeInTheDocument();

  // Truthfully displays saved owner_review without raw enum tokens while recommending Automatic memory for the next enable
  expect(screen.getByText(/Saved activation policy \(capture currently off\): Review each memory first · Automatic memory is recommended when enabling/)).toBeInTheDocument();
  expect(screen.queryByText(/\(owner_review\)/)).not.toBeInTheDocument();
  const modeSelect = screen.getByLabelText("Memory activation mode") as HTMLSelectElement;
  expect(modeSelect.value).toBe("automatic");
  expect(modeSelect).toBeEnabled();

  const useCheckbox = screen.getByRole("checkbox", { name: "Use saved memory in tasks" });
  const captureCheckbox = screen.getByRole("checkbox", { name: "Extract and consolidate long-term memory from conversations" });
  expect(useCheckbox).toBeChecked();
  expect(captureCheckbox).not.toBeChecked();
  // Capture cannot be enabled until owner explicitly selects an available destination (default_destination is not silently reused)
  expect(captureCheckbox).toBeDisabled();

  const destSelect = screen.getByLabelText("Background extraction destination");
  await user.selectOptions(destSelect, JSON.stringify([
    DEST_LOCAL_API.catalog_model_id,
    DEST_LOCAL_API.provider_peer_id,
    DEST_LOCAL_API.source_id,
    DEST_LOCAL_API.destination_revision,
  ]));
  expect(captureCheckbox).toBeEnabled();

  await user.click(captureCheckbox);
  await waitFor(() => expect(settingsCalls).toHaveLength(1));
  expect(settingsCalls[0]).not.toHaveProperty("extraction_model_id");
  expect(Object.keys(settingsCalls[0]).sort()).toEqual([
    "activation_policy",
    "capture_enabled",
    "client_request_id",
    "default_exposure",
    "expected_generation",
    "extraction_destination",
  ]);
  expect(settingsCalls[0]).toMatchObject({
    expected_generation: 0,
    capture_enabled: true,
    activation_policy: "automatic",
    extraction_destination: DEST_LOCAL_API,
    default_exposure: {
      mode: "selected_models",
      allowed_destinations: [DEST_LOCAL_API],
    },
  });
  expect(typeof settingsCalls[0].client_request_id).toBe("string");
  expect(captureCheckbox).toBeChecked();
  expect(modeSelect).toBeDisabled();

  await user.click(within(candidateSection).getByRole("button", { name: "Accept" }));
  await waitFor(() => expect(decisionCalls).toHaveLength(1));
  expect(decisionCalls[0].id).toBe("cand-1");
  expect(decisionCalls[0].body).toMatchObject({
    expected_generation: 1,
    expected_candidate_revision: 3,
    decision: "accept",
  });
  expect(typeof decisionCalls[0].body.client_request_id).toBe("string");

  expect(within(savedSection).getByText("Project uses Vitest for unit tests")).toBeInTheDocument();
  expect(within(savedSection).getByText(/Origin: Reviewed and saved/)).toBeInTheDocument();
  expect(within(candidateSection).queryByText("Project uses Vitest for unit tests")).not.toBeInTheDocument();
});
