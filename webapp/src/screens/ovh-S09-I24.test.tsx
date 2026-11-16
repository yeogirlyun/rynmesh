import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { MemoryEditor } from "./RynAgentMemory";

const DEST_DEFAULT = {
  catalog_model_id: "claude-4-sonnet",
  provider_peer_id: "local_node",
  source_id: "src-anthropic",
  destination_revision: "rev-a1",
};

const DEST_OTHER = {
  catalog_model_id: "gemini-cli-local",
  provider_peer_id: "local_node",
  source_id: "",
  destination_revision: "rev-cli-1",
};

it("T2: one-click recommended enable sends default destination, automatic policy, and default_exposure; hides manual project ID input and explains automatic mode independence & model switching", async () => {
  const settingsCalls: Array<Record<string, unknown>> = [];
  const user = userEvent.setup();

  server.use(
    http.get("*/api/local/agent/memory/destinations", () =>
      HttpResponse.json({
        destinations: [
          { label: "Claude 4 Sonnet", destination: DEST_DEFAULT, available: true },
          { label: "Gemini Local CLI", destination: DEST_OTHER, available: true },
        ],
        default_destination: DEST_DEFAULT,
      }),
    ),
    http.get("*/api/local/agent/memory", () =>
      HttpResponse.json({
        schema_version: 1,
        generation: 2,
        reset_epoch: 0,
        policy_revision: 0,
        settings: {
          capture_enabled: false,
          use_enabled: true,
          activation_policy: "owner_review",
          extraction_model_id: "",
          extraction_destination: null,
          default_exposure: { mode: "selected_models", allowed_destinations: [] },
        },
        entries: [],
        candidates: [],
        tombstone_count: 0,
      }),
    ),
    http.put("*/api/local/agent/memory/settings", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      settingsCalls.push(body);
      return HttpResponse.json({
        schema_version: 1,
        generation: 3,
        reset_epoch: 0,
        policy_revision: 1,
        settings: {
          capture_enabled: true,
          use_enabled: true,
          activation_policy: "automatic",
          extraction_model_id: DEST_DEFAULT.catalog_model_id,
          extraction_destination: DEST_DEFAULT,
          default_exposure: { mode: "selected_models", allowed_destinations: [DEST_DEFAULT] },
        },
        entries: [],
        candidates: [],
        tombstone_count: 0,
      });
    }),
  );

  // Without projects prop: project scope option is hidden
  const { unmount } = render(<MemoryEditor initialOpen />);
  const quickBtn = await screen.findByRole("button", {
    name: /推荐设置一键开启|Enable with recommended settings/i,
  });

  // Verifies Change 6 & Change 7 explanatory notes in capture controls
  expect(
    screen.getByText(/与任务的.*自动处理.*权限无关|independent of the task 'Automatic' permission mode/i),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/换模型后到条目上点.*允许当前模型使用|click 'Allow current model' on an entry/i),
  ).toBeInTheDocument();

  const scopeSelectNoProj = screen.getByLabelText("Scope") as HTMLSelectElement;
  expect(within(scopeSelectNoProj).queryByRole("option", { name: /Local project only|仅本机项目范围/ })).not.toBeInTheDocument();

  await user.click(quickBtn);
  await waitFor(() => expect(settingsCalls).toHaveLength(1));
  expect(settingsCalls[0]).toMatchObject({
    expected_generation: 2,
    capture_enabled: true,
    activation_policy: "automatic",
    extraction_destination: DEST_DEFAULT,
    default_exposure: {
      mode: "selected_models",
      allowed_destinations: [DEST_DEFAULT],
    },
  });
  unmount();

  // With known projects: shows dropdown ONLY, no free-text Local project ID input
  render(
    <MemoryEditor
      initialOpen
      projects={[
        {
          id: "proj-alpha",
          name: "Alpha Repo",
          path: "/workspace/alpha",
          generation: 1,
          entry_files: [],
          manifests: [],
          directories: [],
        },
      ]}
    />,
  );
  await screen.findByText(/Saved facts/);
  await user.selectOptions(screen.getByLabelText("Scope"), "project");
  expect(screen.getByLabelText(/Select local project|选择本机项目/)).toBeInTheDocument();
  expect(screen.queryByPlaceholderText(/Enter local project ID|输入本机项目 ID/)).not.toBeInTheDocument();
});

it("T3: local_only or non-default-model entries show invisible badge & Allow current model button; Add fact defaults to destinations with default model checked", async () => {
  const createdBodies: Array<Record<string, unknown>> = [];
  const updatedBodies: Array<{ id: string; body: Record<string, unknown> }> = [];
  const user = userEvent.setup();

  server.use(
    http.get("*/api/local/agent/memory/destinations", () =>
      HttpResponse.json({
        destinations: [
          { label: "Claude 4 Sonnet", destination: DEST_DEFAULT, available: true },
          { label: "Gemini Local CLI", destination: DEST_OTHER, available: true },
        ],
        default_destination: DEST_DEFAULT,
      }),
    ),
    http.get("*/api/local/agent/memory", () =>
      HttpResponse.json({
        schema_version: 1,
        generation: 5,
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
            id: "entry-local",
            scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
            category: "preference",
            fact_key: "coffee",
            text: "Prefers pour-over black coffee",
            kind: "owner_preference",
            status: "active",
            revision: 2,
            origin: "manual",
            sources: [],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "local_only", allowed_destinations: [], revision: 1 },
            created_at: 100,
            updated_at: 100,
            last_used_at: null,
            reviewed_by_owner_at: 100,
          },
        ],
        candidates: [],
        tombstone_count: 0,
      }),
    ),
    http.put("*/api/local/agent/memory/entries/:id", async ({ params, request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      updatedBodies.push({ id: String(params.id), body });
      return HttpResponse.json({
        schema_version: 1,
        generation: 6,
        reset_epoch: 0,
        policy_revision: 2,
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
            id: "entry-local",
            scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
            category: "preference",
            fact_key: "coffee",
            text: "Prefers pour-over black coffee",
            kind: "owner_preference",
            status: "active",
            revision: 3,
            origin: "manual",
            sources: [],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "destinations", allowed_destinations: [DEST_DEFAULT], revision: 2 },
            created_at: 100,
            updated_at: 200,
            last_used_at: null,
            reviewed_by_owner_at: 200,
          },
        ],
        candidates: [],
        tombstone_count: 0,
      });
    }),
    http.post("*/api/local/agent/memory/entries", async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      createdBodies.push(body);
      return HttpResponse.json({
        schema_version: 1,
        generation: 7,
        reset_epoch: 0,
        policy_revision: 2,
        settings: {
          capture_enabled: false,
          use_enabled: true,
          activation_policy: "owner_review",
          extraction_model_id: "",
          extraction_destination: null,
          default_exposure: { mode: "selected_models", allowed_destinations: [] },
        },
        entries: [],
        candidates: [],
        tombstone_count: 0,
      });
    }),
  );

  render(<MemoryEditor initialOpen />);
  await screen.findByText("Prefers pour-over black coffee");

  // Entry has invisible badge and Allow current model button
  expect(screen.getByText(/^(?:模型不可见|Invisible to model)$/)).toBeInTheDocument();
  const allowBtn = screen.getByRole("button", { name: /允许当前模型使用|Allow current model/i });
  await user.click(allowBtn);

  await waitFor(() => expect(updatedBodies).toHaveLength(1));
  expect(updatedBodies[0].id).toBe("entry-local");
  expect(updatedBodies[0].body).toMatchObject({
    expected_generation: 5,
    expected_entry_revision: 2,
    exposure_policy: {
      mode: "destinations",
      allowed_destinations: [DEST_DEFAULT],
    },
  });

  // Add fact form defaults to mode="destinations" with DEST_DEFAULT checked
  const policySelect = screen.getByLabelText("Model destination policy") as HTMLSelectElement;
  expect(policySelect.value).toBe("destinations");
  const chooseGroup = screen.getByRole("group", { name: "Choose allowed destinations" });
  const defaultCheckbox = within(chooseGroup).getByRole("checkbox", { name: "Claude 4 Sonnet" });
  expect(defaultCheckbox).toBeChecked();

  await user.type(screen.getByLabelText("New fact text"), "Backend tests use pytest");
  await user.click(screen.getByRole("button", { name: "Add fact" }));

  await waitFor(() => expect(createdBodies).toHaveLength(1));
  expect(createdBodies[0]).toMatchObject({
    expected_generation: 6,
    text: "Backend tests use pytest",
    exposure_policy: {
      mode: "destinations",
      allowed_destinations: [DEST_DEFAULT],
    },
  });

  // Resets back to destinations + DEST_DEFAULT after create succeeds
  expect(policySelect.value).toBe("destinations");
  expect(within(chooseGroup).getByRole("checkbox", { name: "Claude 4 Sonnet" })).toBeChecked();
});

it("T4: falls back to local_only and shows invisible-to-models warning when no default model destination is available", async () => {
  server.use(
    http.get("*/api/local/agent/memory/destinations", () =>
      HttpResponse.json({
        destinations: [],
        default_destination: null,
      }),
    ),
    http.get("*/api/local/agent/memory", () =>
      HttpResponse.json({
        schema_version: 1,
        generation: 1,
        reset_epoch: 0,
        policy_revision: 0,
        settings: {
          capture_enabled: false,
          use_enabled: true,
          activation_policy: "owner_review",
          extraction_model_id: "",
          extraction_destination: null,
          default_exposure: { mode: "selected_models", allowed_destinations: [] },
        },
        entries: [],
        candidates: [],
        tombstone_count: 0,
      }),
    ),
  );

  render(<MemoryEditor initialOpen />);
  const policySelect = (await screen.findByLabelText("Model destination policy")) as HTMLSelectElement;
  await waitFor(() => expect(policySelect.value).toBe("local_only"));
  expect(
    screen.getByRole("option", { name: /仅本机保存（模型看不到）|Store on this device only \(invisible to models\)/i }),
  ).toBeInTheDocument();
  expect(screen.getByText(/^(?:模型看不到|Invisible to models)$/i)).toBeInTheDocument();
});
