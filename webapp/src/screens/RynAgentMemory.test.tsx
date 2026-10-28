import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { rynAgentMemory, validateDestinationTuple, validateMemorySnapshot } from "../domain/rynAgentMemory";
import { MemoryEditor } from "./RynAgentMemory";

const DEST_ALPHA = {
  catalog_model_id: "claude-4-sonnet",
  provider_peer_id: "local_node",
  source_id: "src-anthropic",
  destination_revision: "rev-a1",
};

const DEST_NATIVE_CLI = {
  catalog_model_id: "gemini-cli-local",
  provider_peer_id: "local_node",
  source_id: "",
  destination_revision: "rev-cli-1",
};

const DEST_BETA_STALE = {
  catalog_model_id: "gpt-4o",
  provider_peer_id: "peer-9",
  source_id: "src-peer",
  destination_revision: "rev-old",
};

it("validates domain adapter contract: accepts default empty snapshot with default_exposure selected_models, native CLI empty source_id, preserves generation=0 and project_generation, and rejects invalid enums including unknown activation_policy", async () => {
  const defaultEmptySnapshot = validateMemorySnapshot({
    schema_version: 1,
    generation: 0,
    reset_epoch: 0,
    policy_revision: 0,
    settings: {
      capture_enabled: false,
      use_enabled: true,
      activation_policy: "owner_review",
      extraction_model_id: "",
      extraction_destination: null,
      default_exposure: { mode: "selected_models", allowed_destinations: [] },
      budgets: { max_entries: 200 },
    },
    entries: [],
    candidates: [],
    tombstone_count: 0,
  });

  expect(defaultEmptySnapshot.generation).toBe(0);
  expect(defaultEmptySnapshot.settings.activation_policy).toBe("owner_review");
  expect(defaultEmptySnapshot.settings.default_exposure).toEqual({
    mode: "selected_models",
    allowed_destinations: [],
  });

  // Also accepts activation_policy: "automatic"
  const automaticSnapshot = validateMemorySnapshot({
    ...defaultEmptySnapshot,
    settings: {
      ...defaultEmptySnapshot.settings,
      activation_policy: "automatic",
    },
  });
  expect(automaticSnapshot.settings.activation_policy).toBe("automatic");

  // Rejects unknown or missing settings.activation_policy (never silently invents automatic or owner_review)
  expect(() => validateMemorySnapshot({
    ...defaultEmptySnapshot,
    settings: {
      ...defaultEmptySnapshot.settings,
      activation_policy: "always_allow",
    },
  })).toThrow();
  expect(() => validateMemorySnapshot({
    ...defaultEmptySnapshot,
    settings: {
      capture_enabled: false,
      use_enabled: true,
      extraction_model_id: "",
      extraction_destination: null,
      default_exposure: { mode: "selected_models", allowed_destinations: [] },
    },
  })).toThrow();

  // Native CLI destination with source_id="" is valid, while empty provider_peer_id or destination_revision is rejected
  expect(validateDestinationTuple(DEST_NATIVE_CLI)).toEqual(DEST_NATIVE_CLI);
  expect(() => validateDestinationTuple({ ...DEST_NATIVE_CLI, provider_peer_id: "" })).toThrow();
  expect(() => validateDestinationTuple({ ...DEST_NATIVE_CLI, destination_revision: "" })).toThrow();

  const snapshotWithProjectAndCli = validateMemorySnapshot({
    ...defaultEmptySnapshot,
    settings: {
      ...defaultEmptySnapshot.settings,
      default_exposure: { mode: "restricted", allowed_destinations: [] },
    },
    entries: [
      {
        id: "e-proj",
        scope: { kind: "project", owner_id: "local_owner", project_id: "proj-1", project_generation: 4 },
        category: "architecture",
        fact_key: "db_engine",
        text: "Uses SQLite WAL",
        kind: "fact",
        status: "active",
        revision: 1,
        origin: "manual_owner",
        sources: [
          {
            source_type: "manual_owner",
            task_id: null,
            message_id: null,
            project_id: "proj-1",
            excerpt: "Uses SQLite WAL",
          },
        ],
        supersedes: [],
        superseded_by: null,
        exposure_policy: { mode: "destinations", allowed_destinations: [DEST_NATIVE_CLI], revision: 1 },
        created_at: 100,
        updated_at: 100,
        last_used_at: null,
        reviewed_by_owner_at: 100,
      },
    ],
  });

  expect(snapshotWithProjectAndCli.entries[0].scope).toEqual({
    kind: "project",
    owner_id: "local_owner",
    project_id: "proj-1",
    project_generation: 4,
  });
  expect(snapshotWithProjectAndCli.entries[0].exposure_policy.allowed_destinations).toEqual([DEST_NATIVE_CLI]);
  expect(snapshotWithProjectAndCli.entries[0].sources).toHaveLength(1);
  expect(snapshotWithProjectAndCli.entries[0].sources[0].excerpt).toBe("Uses SQLite WAL");

  // Rejects invalid settings.default_exposure mode (e.g. entry mode "local_only" is not a valid default_exposure mode)
  expect(() => validateMemorySnapshot({
    ...defaultEmptySnapshot,
    settings: {
      ...defaultEmptySnapshot.settings,
      default_exposure: { mode: "local_only", allowed_destinations: [] },
    },
  })).toThrow();

  // Rejects project scope without project_id (never silently converts to personal)
  expect(() => validateMemorySnapshot({
    ...snapshotWithProjectAndCli,
    entries: [
      {
        ...snapshotWithProjectAndCli.entries[0],
        scope: { kind: "project", owner_id: "local_owner", project_id: null, project_generation: null },
      },
    ],
  })).toThrow();

  // Rejects unknown entry exposure_policy mode (never silently broadens or defaults)
  expect(() => validateMemorySnapshot({
    ...snapshotWithProjectAndCli,
    entries: [
      {
        ...snapshotWithProjectAndCli.entries[0],
        exposure_policy: { mode: "selected_models", allowed_destinations: [] },
      },
    ],
  })).toThrow();

  // Verifies GET query uses scope_kind
  let capturedGetScopeKind: string | null = null;
  server.use(
    http.get("*/api/local/agent/memory", ({ request }) => {
      capturedGetScopeKind = new URL(request.url).searchParams.get("scope_kind");
      return HttpResponse.json(snapshotWithProjectAndCli);
    }),
  );
  const fetched = await rynAgentMemory.get({ scope_kind: "project", project_id: "proj-1" });
  expect(capturedGetScopeKind).toBe("project");
  expect(fetched.generation).toBe(0);
});

it("requires fresh explicit destination selection, supports automatic one-action authorization and owner_review mode, locks mode while capture is on, and never writes settings on reload", async () => {
  const settingsCalls: Array<Record<string, unknown>> = [];
  const user = userEvent.setup();
  let serverSettings = {
    capture_enabled: false,
    use_enabled: true,
    activation_policy: "owner_review" as "owner_review" | "automatic",
    extraction_model_id: DEST_ALPHA.catalog_model_id,
    extraction_destination: DEST_ALPHA as typeof DEST_ALPHA | typeof DEST_NATIVE_CLI | null,
    default_exposure: { mode: "selected_models" as const, allowed_destinations: [] as unknown[] },
  };
  let currentGen = 3;

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [
        { label: "Gemini Local CLI", destination: DEST_NATIVE_CLI, available: true },
        { label: "Claude 4 Sonnet · My Anthropic", destination: DEST_ALPHA, available: true },
      ],
      default_destination: DEST_ALPHA,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: currentGen,
      reset_epoch: 0,
      policy_revision: 1,
      settings: serverSettings,
      entries: [],
      candidates: [],
      tombstone_count: 0,
    })),
    http.put("*/api/local/agent/memory/settings", async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      settingsCalls.push(body);
      const nextCapture = typeof body.capture_enabled === "boolean" ? body.capture_enabled : serverSettings.capture_enabled;
      const dest = (body.extraction_destination as typeof DEST_ALPHA | typeof DEST_NATIVE_CLI | null | undefined) ?? serverSettings.extraction_destination;
      const nextPolicy = (body.activation_policy as "owner_review" | "automatic" | undefined) ?? serverSettings.activation_policy;
      const nextExposure = (body.default_exposure as { mode: "selected_models"; allowed_destinations: unknown[] } | undefined) ?? serverSettings.default_exposure;
      currentGen += 1;
      serverSettings = {
        capture_enabled: nextCapture,
        use_enabled: true,
        activation_policy: nextPolicy,
        extraction_model_id: dest?.catalog_model_id || "",
        extraction_destination: dest,
        default_exposure: nextExposure,
      };
      return HttpResponse.json({
        schema_version: 1,
        generation: currentGen,
        reset_epoch: 0,
        policy_revision: 1 + settingsCalls.length,
        settings: serverSettings,
        entries: [],
        candidates: [],
        tombstone_count: 0,
      });
    }),
  );

  render(<MemoryEditor initialOpen />);
  await screen.findByText(/Previously saved extraction destination: Claude 4 Sonnet · My Anthropic/);

  // Saved policy is truthfully shown as Review each memory first without raw enum tokens, while automatic mode is recommended for the next enable
  expect(screen.getByText(/Saved activation policy \(capture currently off\): Review each memory first · Automatic memory is recommended when enabling/)).toBeInTheDocument();
  expect(screen.queryByText(/\(owner_review\)|\(automatic\)|all_selected/)).not.toBeInTheDocument();
  expect(screen.getByText(/asynchronously extracted after ordinary conversations and consolidated with existing memories/i)).toBeInTheDocument();
  expect(screen.getByText(/Ordinary conversations do not need a 'remember' command; manual 'Add fact' below is an optional shortcut/i)).toBeInTheDocument();

  const modeSelect = screen.getByLabelText("Memory activation mode") as HTMLSelectElement;
  expect(modeSelect.value).toBe("automatic");
  expect(modeSelect).toBeEnabled();
  expect(screen.getByRole("option", { name: "Automatic memory (stable facts saved automatically) · Recommended" })).toBeInTheDocument();
  expect(screen.getByRole("option", { name: "Review each memory first" })).toBeInTheDocument();

  const captureCheckbox = screen.getByRole("checkbox", { name: "Extract and consolidate long-term memory from conversations" });
  const destSelect = screen.getByLabelText("Background extraction destination") as HTMLSelectElement;

  // Even though extraction_destination is non-null in saved settings, capture is false so checkbox stays disabled until owner explicitly picks a destination this session
  expect(destSelect.value).toBe("");
  expect(captureCheckbox).not.toBeChecked();
  expect(captureCheckbox).toBeDisabled();

  // Select native CLI destination (with source_id: "")
  await user.selectOptions(destSelect, JSON.stringify([
    DEST_NATIVE_CLI.catalog_model_id,
    DEST_NATIVE_CLI.provider_peer_id,
    DEST_NATIVE_CLI.source_id,
    DEST_NATIVE_CLI.destination_revision,
  ]));
  expect(captureCheckbox).toBeEnabled();
  // Shows clear one-time consent for automatic mode before enabling (plain copy, no raw enum names)
  expect(screen.getByText(/One-time authorization \(Automatic memory\): Enabling the toggle below authorizes the exact selected model \(Gemini Local CLI\)/)).toBeInTheDocument();
  expect(screen.getByText(/only this selected model; never automatic paid fallback/)).toBeInTheDocument();

  // Enable capture under automatic mode -> sends one-action authorization with activation_policy="automatic" and default_exposure selected_models:[DEST_NATIVE_CLI]
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
    expected_generation: 3,
    capture_enabled: true,
    activation_policy: "automatic",
    extraction_destination: DEST_NATIVE_CLI,
    default_exposure: {
      mode: "selected_models",
      allowed_destinations: [DEST_NATIVE_CLI],
    },
  });
  expect(captureCheckbox).toBeChecked();

  // While capture is ON, switching activation mode is locked until capture is stopped first, and pre-enable consent notice is replaced by concise active status
  expect(modeSelect).toBeDisabled();
  expect(screen.getByText(/Activation mode is locked while memory capture is on\. Stop memory capture first before switching modes\./)).toBeInTheDocument();
  expect(screen.getByText(/Automatic memory is active: Gemini Local CLI asynchronously extracts and consolidates stable facts/)).toBeInTheDocument();
  expect(screen.queryByText(/select an available model above first/i)).not.toBeInTheDocument();

  // Disable capture -> clears session consent so re-enabling requires explicit choice again, and unlocks mode selector
  await user.click(captureCheckbox);
  await waitFor(() => expect(settingsCalls).toHaveLength(2));
  expect(Object.keys(settingsCalls[1]).sort()).toEqual(["capture_enabled", "client_request_id", "expected_generation"]);
  expect(settingsCalls[1]).toMatchObject({
    expected_generation: 4,
    capture_enabled: false,
  });
  expect(captureCheckbox).not.toBeChecked();
  expect(destSelect.value).toBe("");
  expect(captureCheckbox).toBeDisabled();
  expect(modeSelect).toBeEnabled();

  // Reloading makes no silent settings write
  await user.click(screen.getByRole("button", { name: "Reload" }));
  await screen.findByText(/Saved activation policy \(capture currently off\): Automatic memory \(stable facts saved automatically\) · Automatic memory is recommended when enabling/);
  expect(settingsCalls).toHaveLength(2);

  // Switch next enable mode to owner_review ("Review each memory first") and select DEST_ALPHA
  await user.selectOptions(modeSelect, "owner_review");
  await user.selectOptions(destSelect, JSON.stringify([
    DEST_ALPHA.catalog_model_id,
    DEST_ALPHA.provider_peer_id,
    DEST_ALPHA.source_id,
    DEST_ALPHA.destination_revision,
  ]));
  expect(screen.getByText(/Authorization \(Review each memory first\): Enabling the toggle below authorizes the exact selected model \(Claude 4 Sonnet · My Anthropic\)/)).toBeInTheDocument();
  expect(screen.getByText(/only facts accepted by you become available for subsequent model use/i)).toBeInTheDocument();
  expect(captureCheckbox).toBeEnabled();

  await user.click(captureCheckbox);
  await waitFor(() => expect(settingsCalls).toHaveLength(3));
  expect(Object.keys(settingsCalls[2]).sort()).toEqual([
    "activation_policy",
    "capture_enabled",
    "client_request_id",
    "default_exposure",
    "expected_generation",
    "extraction_destination",
  ]);
  expect(settingsCalls[2]).toMatchObject({
    expected_generation: 5,
    capture_enabled: true,
    activation_policy: "owner_review",
    extraction_destination: DEST_ALPHA,
    default_exposure: {
      mode: "selected_models",
      allowed_destinations: [DEST_ALPHA],
    },
  });
});

it("toggles use_enabled and capture_enabled independently and shows truthful active mode/target when loaded with capture already ON without missing-selection prompt", async () => {
  const settingsCalls: Array<Record<string, unknown>> = [];
  const user = userEvent.setup();
  let serverState = {
    generation: 2,
    policy_revision: 1,
    capture_enabled: true,
    use_enabled: true,
    extraction_model_id: DEST_ALPHA.catalog_model_id,
    extraction_destination: DEST_ALPHA,
  };

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [
        { label: "Claude 4 Sonnet · My Anthropic", destination: DEST_ALPHA, available: true },
      ],
      default_destination: DEST_ALPHA,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: serverState.generation,
      reset_epoch: 0,
      policy_revision: serverState.policy_revision,
      settings: {
        capture_enabled: serverState.capture_enabled,
        use_enabled: serverState.use_enabled,
        activation_policy: "automatic",
        extraction_model_id: serverState.extraction_model_id,
        extraction_destination: serverState.extraction_destination,
        default_exposure: { mode: "selected_models", allowed_destinations: [DEST_ALPHA] },
      },
      entries: [],
      candidates: [],
      tombstone_count: 0,
    })),
    http.put("*/api/local/agent/memory/settings", async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      settingsCalls.push(body);
      serverState = {
        ...serverState,
        generation: serverState.generation + 1,
        policy_revision: serverState.policy_revision + 1,
        ...(typeof body.use_enabled === "boolean" ? { use_enabled: body.use_enabled } : {}),
        ...(typeof body.capture_enabled === "boolean" ? { capture_enabled: body.capture_enabled } : {}),
      };
      return HttpResponse.json({
        schema_version: 1,
        generation: serverState.generation,
        reset_epoch: 0,
        policy_revision: serverState.policy_revision,
        settings: {
          capture_enabled: serverState.capture_enabled,
          use_enabled: serverState.use_enabled,
          activation_policy: "automatic",
          extraction_model_id: serverState.extraction_model_id,
          extraction_destination: serverState.extraction_destination,
          default_exposure: { mode: "selected_models", allowed_destinations: [DEST_ALPHA] },
        },
        entries: [],
        candidates: [],
        tombstone_count: 0,
      });
    }),
  );

  render(<MemoryEditor initialOpen />);
  await screen.findByText(/Active extraction destination: Claude 4 Sonnet · My Anthropic/);

  // Loaded with capture already ON: shows truthful saved mode + active target, mode is locked, fresh selector stays empty, and no misleading "select an available model above first" prompt is shown
  expect(screen.getByText("Saved activation policy: Automatic memory (stable facts saved automatically)")).toBeInTheDocument();
  expect(screen.getByText(/Automatic memory is active: Claude 4 Sonnet · My Anthropic asynchronously extracts and consolidates stable facts/)).toBeInTheDocument();
  expect(screen.queryByText(/select an available model above first/i)).not.toBeInTheDocument();
  expect(screen.queryByText(/One-time authorization/i)).not.toBeInTheDocument();

  const modeSelect = screen.getByLabelText("Memory activation mode") as HTMLSelectElement;
  const destSelect = screen.getByLabelText("Background extraction destination") as HTMLSelectElement;
  expect(modeSelect).toBeDisabled();
  expect(destSelect.value).toBe("");
  expect(settingsCalls).toHaveLength(0);

  // Reloading while capture is ON keeps truthful active status, does not pre-populate destSelect, and performs no settings write
  await user.click(screen.getByRole("button", { name: "Reload" }));
  await screen.findByText(/Active extraction destination: Claude 4 Sonnet · My Anthropic/);
  expect(screen.queryByText(/select an available model above first/i)).not.toBeInTheDocument();
  expect(modeSelect).toBeDisabled();
  expect(destSelect.value).toBe("");
  expect(settingsCalls).toHaveLength(0);

  const useCheckbox = screen.getByRole("checkbox", { name: "Use saved memory in tasks" });
  const captureCheckbox = screen.getByRole("checkbox", { name: "Extract and consolidate long-term memory from conversations" });
  expect(useCheckbox).toBeChecked();
  expect(captureCheckbox).toBeChecked();

  // 1. Turn off use_enabled -> only sends use_enabled: false + CAS fields, does not touch capture_enabled or extraction_destination
  await user.click(useCheckbox);
  await waitFor(() => expect(settingsCalls).toHaveLength(1));
  expect(Object.keys(settingsCalls[0]).sort()).toEqual(["client_request_id", "expected_generation", "use_enabled"]);
  expect(settingsCalls[0]).toMatchObject({
    expected_generation: 2,
    use_enabled: false,
  });
  expect(useCheckbox).not.toBeChecked();
  expect(captureCheckbox).toBeChecked();

  // 2. Turn off capture_enabled -> only sends capture_enabled: false + CAS fields, does not touch use_enabled; after OFF, fresh exact selection is required and pre-enable consent notice is shown
  await user.click(captureCheckbox);
  await waitFor(() => expect(settingsCalls).toHaveLength(2));
  expect(Object.keys(settingsCalls[1]).sort()).toEqual(["capture_enabled", "client_request_id", "expected_generation"]);
  expect(settingsCalls[1]).toMatchObject({
    expected_generation: 3,
    capture_enabled: false,
  });
  expect(useCheckbox).not.toBeChecked();
  expect(captureCheckbox).not.toBeChecked();
  expect(captureCheckbox).toBeDisabled();
  expect(destSelect.value).toBe("");
  expect(screen.getByText(/select an available model above first/i)).toBeInTheDocument();

  // 3. Re-enable use_enabled -> does not re-enable capture_enabled
  await user.click(useCheckbox);
  await waitFor(() => expect(settingsCalls).toHaveLength(3));
  expect(Object.keys(settingsCalls[2]).sort()).toEqual(["client_request_id", "expected_generation", "use_enabled"]);
  expect(settingsCalls[2]).toMatchObject({
    expected_generation: 4,
    use_enabled: true,
  });
  expect(useCheckbox).toBeChecked();
  expect(captureCheckbox).not.toBeChecked();
});

it("creates, edits, searches, and deletes memory entries with nested scope, exact server destination tuples (including native CLI), and CAS fields", async () => {
  const createdBodies: Array<Record<string, unknown>> = [];
  const updatedBodies: Array<{ id: string; body: Record<string, unknown> }> = [];
  const deletedBodies: Array<{ id: string; body: Record<string, unknown> }> = [];
  const searchParams: Array<{ q: string | null; scope_kind: string | null }> = [];
  const user = userEvent.setup();

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [
        { label: "Claude 4 Sonnet · My Anthropic", destination: DEST_ALPHA, available: true },
        { label: "Gemini Local CLI", destination: DEST_NATIVE_CLI, available: true },
        { label: "GPT-4o · Stale Peer", destination: DEST_BETA_STALE, available: false },
      ],
      default_destination: DEST_ALPHA,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: 10,
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
          id: "entry-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "general",
          fact_key: "ruff_check",
          text: "Always run ruff before finishing Python edits",
          kind: "fact",
          status: "active",
          revision: 2,
          origin: "manual_owner",
          sources: [
            {
              source_type: "manual_owner",
              task_id: null,
              message_id: null,
              project_id: null,
              excerpt: "Always run ruff before finishing Python edits",
            },
          ],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "destinations", allowed_destinations: [DEST_BETA_STALE], revision: 1 },
          created_at: 100,
          updated_at: 100,
          last_used_at: null,
          reviewed_by_owner_at: 100,
        },
      ],
      candidates: [],
      tombstone_count: 0,
    })),
    http.post("*/api/local/agent/memory/entries", async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      createdBodies.push(body);
      const scopeInput = body.scope as { kind: "personal" | "project"; project_id: string | null };
      const exposureInput = body.exposure_policy as { mode: "local_only" | "destinations" | "all_selected"; allowed_destinations: unknown[] };
      return HttpResponse.json({
        schema_version: 1,
        generation: 11,
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
            id: "entry-1",
            scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
            category: "general",
            fact_key: "ruff_check",
            text: "Always run ruff before finishing Python edits",
            kind: "fact",
            status: "active",
            revision: 2,
            origin: "manual_owner",
            sources: [
              {
                source_type: "manual_owner",
                task_id: null,
                message_id: null,
                project_id: null,
                excerpt: "Always run ruff before finishing Python edits",
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "destinations", allowed_destinations: [DEST_BETA_STALE], revision: 1 },
            created_at: 100,
            updated_at: 100,
            last_used_at: null,
            reviewed_by_owner_at: 100,
          },
          {
            id: "entry-2",
            scope: { kind: scopeInput.kind, owner_id: "local_owner", project_id: scopeInput.project_id, project_generation: 2 },
            category: "general",
            fact_key: "frontend_tests",
            text: String(body.text),
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "manual_owner",
            sources: [
              {
                source_type: "manual_owner",
                task_id: null,
                message_id: null,
                project_id: scopeInput.project_id,
                excerpt: String(body.text),
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: exposureInput,
            created_at: 200,
            updated_at: 200,
            last_used_at: null,
            reviewed_by_owner_at: 200,
          },
        ],
        candidates: [],
        tombstone_count: 0,
      });
    }),
    http.put("*/api/local/agent/memory/entries/:id", async ({ params, request }) => {
      const body = await request.json() as Record<string, unknown>;
      updatedBodies.push({ id: String(params.id), body });
      const scopeInput = body.scope as { kind: "personal" | "project"; project_id: string | null };
      const exposureInput = body.exposure_policy as { mode: "local_only" | "destinations" | "all_selected"; allowed_destinations: unknown[] };
      return HttpResponse.json({
        schema_version: 1,
        generation: 12,
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
            id: "entry-1",
            scope: { kind: scopeInput.kind, owner_id: "local_owner", project_id: scopeInput.project_id, project_generation: null },
            category: "general",
            fact_key: "ruff_check",
            text: String(body.text),
            kind: "fact",
            status: "active",
            revision: 3,
            origin: "manual_owner",
            sources: [
              {
                source_type: "manual_owner",
                task_id: null,
                message_id: null,
                project_id: null,
                excerpt: String(body.text),
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: exposureInput,
            created_at: 100,
            updated_at: 300,
            last_used_at: null,
            reviewed_by_owner_at: 300,
          },
          {
            id: "entry-2",
            scope: { kind: "project", owner_id: "local_owner", project_id: "proj-ryn", project_generation: 2 },
            category: "general",
            fact_key: "frontend_tests",
            text: "Frontend tests live in webapp/src",
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "manual_owner",
            sources: [
              {
                source_type: "manual_owner",
                task_id: null,
                message_id: null,
                project_id: "proj-ryn",
                excerpt: "Frontend tests live in webapp/src",
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "destinations", allowed_destinations: [DEST_NATIVE_CLI] },
            created_at: 200,
            updated_at: 200,
            last_used_at: null,
            reviewed_by_owner_at: 200,
          },
        ],
        candidates: [],
        tombstone_count: 0,
      });
    }),
    http.get("*/api/local/agent/memory/search", ({ request }) => {
      const url = new URL(request.url);
      searchParams.push({ q: url.searchParams.get("q"), scope_kind: url.searchParams.get("scope_kind") });
      return HttpResponse.json({
        entries: [
          {
            id: "entry-2",
            scope: { kind: "project", owner_id: "local_owner", project_id: "proj-ryn", project_generation: 2 },
            category: "general",
            fact_key: "frontend_tests",
            text: "Frontend tests live in webapp/src",
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "manual_owner",
            sources: [
              {
                source_type: "manual_owner",
                task_id: null,
                message_id: null,
                project_id: "proj-ryn",
                excerpt: "Frontend tests live in webapp/src",
              },
            ],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "destinations", allowed_destinations: [DEST_NATIVE_CLI] },
            created_at: 200,
            updated_at: 200,
            last_used_at: null,
            reviewed_by_owner_at: 200,
          },
        ],
        total: 1,
        generation: 12,
        reset_epoch: 0,
        policy_revision: 2,
      });
    }),
    http.delete("*/api/local/agent/memory/entries/:id", async ({ params, request }) => {
      const body = await request.json() as Record<string, unknown>;
      deletedBodies.push({ id: String(params.id), body });
      return HttpResponse.json({
        schema_version: 1,
        generation: 13,
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
        tombstone_count: 2,
      });
    }),
  );

  render(<MemoryEditor />);
  await user.click(screen.getByRole("button", { name: "Long-term memory" }));
  await screen.findByText("Always run ruff before finishing Python edits");

  // Entry-1 has a stale/unavailable destination -> shows reselect warning and does not broaden to all_selected
  expect(screen.getByText(/Unavailable — reselect required/)).toBeInTheDocument();

  // New entry defaults to local_only ("Store on this device; never send to models")
  const policySelect = screen.getByLabelText("Model destination policy") as HTMLSelectElement;
  expect(policySelect.value).toBe("local_only");
  expect(screen.getByRole("option", { name: "Store on this device; never send to models" })).toBeInTheDocument();

  // Switch new entry scope to project -> requires explicit local project ID
  await user.selectOptions(screen.getByLabelText("Scope"), "project");
  const addButton = screen.getByRole("button", { name: "Add fact" });
  await user.type(screen.getByLabelText("New fact text"), "Frontend tests live in webapp/src");
  expect(addButton).toBeDisabled();

  await user.type(screen.getByLabelText(/Local project ID/), "proj-ryn");
  await user.selectOptions(policySelect, "destinations");
  // Cannot add when mode="destinations" but no destination is checked
  expect(addButton).toBeDisabled();

  // Authorize native CLI destination (with empty source_id)
  const cliCheckbox = screen.getByRole("checkbox", { name: "Gemini Local CLI" });
  await user.click(cliCheckbox);
  expect(addButton).toBeEnabled();
  await user.click(addButton);

  await waitFor(() => expect(createdBodies).toHaveLength(1));
  expect(createdBodies[0]).toMatchObject({
    expected_generation: 10,
    text: "Frontend tests live in webapp/src",
    scope: { kind: "project", project_id: "proj-ryn" },
    exposure_policy: { mode: "destinations", allowed_destinations: [DEST_NATIVE_CLI] },
  });
  expect(typeof createdBodies[0].client_request_id).toBe("string");
  expect(createdBodies[0]).not.toHaveProperty("source");
  expect(await screen.findByText("Frontend tests live in webapp/src")).toBeInTheDocument();

  // Edit entry-1 whose destination was unavailable -> must reselect an available destination or change mode
  const editButtons = screen.getAllByRole("button", { name: "Edit" });
  await user.click(editButtons[0]);
  const saveChangesButton = screen.getByRole("button", { name: "Save changes" });
  // Stale destination was filtered out of checked keys, so save is disabled until reselected
  expect(saveChangesButton).toBeDisabled();

  const editDestFieldset = screen.getByRole("group", { name: "Allowed destinations for this fact" });
  await user.click(within(editDestFieldset).getByRole("checkbox", { name: "Claude 4 Sonnet · My Anthropic" }));
  const editTextarea = screen.getByLabelText("Edit fact text");
  await user.clear(editTextarea);
  await user.type(editTextarea, "Run ruff and pytest on backend changes");
  expect(saveChangesButton).toBeEnabled();
  await user.click(saveChangesButton);

  await waitFor(() => expect(updatedBodies).toHaveLength(1));
  expect(updatedBodies[0].id).toBe("entry-1");
  expect(updatedBodies[0].body).toMatchObject({
    expected_generation: 11,
    expected_entry_revision: 2,
    text: "Run ruff and pytest on backend changes",
    scope: { kind: "personal", project_id: null },
    exposure_policy: { mode: "destinations", allowed_destinations: [DEST_ALPHA] },
  });
  expect(typeof updatedBodies[0].body.client_request_id).toBe("string");
  expect(await screen.findByText("Run ruff and pytest on backend changes")).toBeInTheDocument();

  // Filter scope to project and search saved facts (verifies scope_kind query param)
  await user.selectOptions(screen.getByLabelText("Scope view"), "project");
  await user.type(screen.getByLabelText("Search saved facts"), "webapp");
  await user.click(screen.getByRole("button", { name: "Search" }));
  await waitFor(() => expect(searchParams).toEqual([{ q: "webapp", scope_kind: "project" }]));
  expect(screen.queryByText("Run ruff and pytest on backend changes")).not.toBeInTheDocument();
  expect(screen.getByText("Frontend tests live in webapp/src")).toBeInTheDocument();

  // Delete entry-2
  await user.click(screen.getByRole("button", { name: "Delete" }));
  await waitFor(() => expect(deletedBodies).toHaveLength(1));
  expect(deletedBodies[0].id).toBe("entry-2");
  expect(deletedBodies[0].body).toMatchObject({
    expected_generation: 12,
    expected_entry_revision: 1,
  });
  expect(typeof deletedBodies[0].body.client_request_id).toBe("string");
  expect(await screen.findByText("No saved facts.")).toBeInTheDocument();
});

it("loads automatically when initialOpen is true and supports two-step cancelable reset", async () => {
  const resetCalls: Array<Record<string, unknown>> = [];
  const user = userEvent.setup();

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [],
      default_destination: null,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: 7,
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
          id: "e1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "general",
          fact_key: "f1",
          text: "Fact to clear",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "manual_owner",
          sources: [],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "local_only", allowed_destinations: [] },
          created_at: 100,
          updated_at: 100,
          last_used_at: null,
          reviewed_by_owner_at: 100,
        },
      ],
      candidates: [],
      tombstone_count: 0,
    })),
    http.post("*/api/local/agent/memory/reset", async ({ request }) => {
      resetCalls.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({
        schema_version: 1,
        generation: 8,
        reset_epoch: 1,
        policy_revision: 1,
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
  // Loads automatically on initialOpen without needing a manual Reload click
  await screen.findByText("Fact to clear");

  await user.click(screen.getByRole("button", { name: "Clear long-term memory…" }));
  const confirmGroup = screen.getByRole("group", { name: "Confirm clearing long-term memory" });
  expect(within(confirmGroup).getByText(/does not remove past chat history or copies already sent to remote models/i)).toBeInTheDocument();

  // Cancel first
  await user.click(within(confirmGroup).getByRole("button", { name: "Cancel" }));
  expect(screen.queryByRole("group", { name: "Confirm clearing long-term memory" })).not.toBeInTheDocument();
  expect(resetCalls).toHaveLength(0);
  expect(screen.getByText("Fact to clear")).toBeInTheDocument();

  // Open again and confirm
  await user.click(screen.getByRole("button", { name: "Clear long-term memory…" }));
  await user.click(screen.getByRole("button", { name: "Confirm clear memory" }));
  await waitFor(() => expect(resetCalls).toHaveLength(1));
  expect(resetCalls[0]).toMatchObject({
    expected_generation: 7,
    scope: null,
  });
  expect(typeof resetCalls[0].client_request_id).toBe("string");
  expect(await screen.findByText("No saved facts.")).toBeInTheDocument();
});

it("preserves client_request_id and payload for retry on unknown 5xx outcomes and preserves user draft on 409 conflict", async () => {
  const addCalls: Array<Record<string, unknown>> = [];
  const rejectCalls: Array<Record<string, unknown>> = [];
  const user = userEvent.setup();
  let currentGeneration = 5;
  let shouldFailReject = true;

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [],
      default_destination: null,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: currentGeneration,
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
          id: "e1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "general",
          fact_key: "f1",
          text: "Original server fact",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "manual_owner",
          sources: [],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "local_only", allowed_destinations: [] },
          created_at: 100,
          updated_at: 100,
          last_used_at: null,
          reviewed_by_owner_at: 100,
        },
      ],
      candidates: [
        {
          id: "c1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "general",
          fact_key: "c1",
          text: "Unconfirmed candidate",
          kind: "fact",
          status: "pending",
          revision: 2,
          origin: "background_extraction",
          job_id: "job-2",
          reset_epoch: 0,
          sources: [],
          target_entry_id: null,
          expected_entry_revision: null,
          exposure_policy: { mode: "local_only", allowed_destinations: [] },
          created_at: 150,
          updated_at: 150,
        },
      ],
      tombstone_count: 0,
    })),
    http.post("*/api/local/agent/memory/candidates/:id/decision", async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      rejectCalls.push(body);
      if (shouldFailReject) {
        return HttpResponse.json({ detail: "agent_memory_unavailable" }, { status: 503 });
      }
      return HttpResponse.json({
        schema_version: 1,
        generation: 6,
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
            id: "e1",
            scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
            category: "general",
            fact_key: "f1",
            text: "Original server fact",
            kind: "fact",
            status: "active",
            revision: 1,
            origin: "manual_owner",
            sources: [],
            supersedes: [],
            superseded_by: null,
            exposure_policy: { mode: "local_only", allowed_destinations: [] },
            created_at: 100,
            updated_at: 100,
            last_used_at: null,
            reviewed_by_owner_at: 100,
          },
        ],
        candidates: [],
        tombstone_count: 0,
      });
    }),
    http.post("*/api/local/agent/memory/entries", async ({ request }) => {
      addCalls.push(await request.json() as Record<string, unknown>);
      return HttpResponse.json({ detail: "agent_memory_generation_conflict" }, { status: 409 });
    }),
  );

  render(<MemoryEditor />);
  await user.click(screen.getByRole("button", { name: "Long-term memory" }));
  await screen.findByText("Original server fact");

  // 1. Server error (503) on candidate reject -> states result is unconfirmed (does NOT claim "was not saved" and does NOT leak request ID jargon), locks other mutations, offers Retry original operation with identical client_request_id & payload
  await user.click(screen.getByRole("button", { name: "Reject" }));
  const unconfirmedAlert = await screen.findByRole("alert");
  expect(unconfirmedAlert).toHaveTextContent("Action result is unconfirmed. Reload to review the current state, or retry the original operation.");
  expect(unconfirmedAlert).not.toHaveTextContent(/was not saved/i);
  expect(unconfirmedAlert).not.toHaveTextContent(/request ID/i);
  expect(screen.getByText("Unconfirmed candidate")).toBeInTheDocument();
  expect(rejectCalls).toHaveLength(1);

  const retryButton = screen.getByRole("button", { name: "Retry original operation" });
  expect(retryButton).toBeEnabled();
  // New writes are locked while an unconfirmed operation is pending
  expect(screen.getByRole("checkbox", { name: "Use saved memory in tasks" })).toBeDisabled();

  // Retry succeeds with exact same client_request_id and expected_generation
  shouldFailReject = false;
  await user.click(retryButton);
  await waitFor(() => expect(rejectCalls).toHaveLength(2));
  expect(rejectCalls[1]).toEqual(rejectCalls[0]);
  expect(screen.queryByText("Unconfirmed candidate")).not.toBeInTheDocument();

  // 2. HTTP 409 conflict on adding entry -> locks controls until reload, does not add to active list, and preserves user draft in textarea
  const draftInput = screen.getByLabelText("New fact text");
  await user.type(draftInput, "Conflicting fact");
  await user.click(screen.getByRole("button", { name: "Add fact" }));
  const conflictAlert = await screen.findByRole("alert");
  expect(conflictAlert).toHaveTextContent(/newer version will not be overwritten/i);
  expect(addCalls).toHaveLength(1);
  expect(addCalls[0]).toMatchObject({
    expected_generation: 6,
    text: "Conflicting fact",
    scope: { kind: "personal", project_id: null },
    exposure_policy: { mode: "local_only", allowed_destinations: [] },
  });

  const savedSection = screen.getByRole("region", { name: "Saved facts" });
  const entriesList = within(savedSection).getByRole("list");
  expect(within(entriesList).queryByText("Conflicting fact")).not.toBeInTheDocument();
  // User draft is preserved in the textarea
  expect(draftInput).toHaveValue("Conflicting fact");

  // Controls are locked until explicit Reload
  expect(screen.getByRole("button", { name: "Add fact" })).toBeDisabled();
  expect(screen.getByRole("checkbox", { name: "Use saved memory in tasks" })).toBeDisabled();

  currentGeneration = 9;
  await user.click(screen.getByRole("button", { name: "Reload" }));
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
  expect(screen.getByRole("checkbox", { name: "Use saved memory in tasks" })).toBeEnabled();
  expect(draftInput).toHaveValue("Conflicting fact");
});

it("displays accurate labels for manual, accepted_extraction, explicit_owner_chat, automatic_consolidation, and neutral unknown origins without leaking raw origin IDs, and retries unknown-write capture enable with identical mode, exposure tuple, and client_request_id", async () => {
  const settingsCalls: Array<Record<string, unknown>> = [];
  const user = userEvent.setup();
  let shouldFailSettings = true;

  server.use(
    http.get("*/api/local/agent/memory/destinations", () => HttpResponse.json({
      destinations: [
        { label: "Claude 4 Sonnet · My Anthropic", destination: DEST_ALPHA, available: true },
      ],
      default_destination: DEST_ALPHA,
    })),
    http.get("*/api/local/agent/memory", () => HttpResponse.json({
      schema_version: 1,
      generation: 4,
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
          id: "auto-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "preference",
          fact_key: "changelog_style",
          text: "Prefers concise changelogs grouped by subsystem",
          kind: "fact",
          status: "active",
          revision: 2,
          origin: "automatic_consolidation",
          sources: [
            {
              source_type: "task_message",
              task_id: "task-88",
              message_id: "msg-4",
              project_id: null,
              excerpt: "Please keep changelogs concise and grouped by subsystem",
            },
          ],
          supersedes: ["auto-0"],
          superseded_by: null,
          exposure_policy: { mode: "destinations", allowed_destinations: [DEST_ALPHA], revision: 1 },
          created_at: 200,
          updated_at: 250,
          last_used_at: 260,
          reviewed_by_owner_at: null,
        },
        {
          id: "accepted-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "workflow",
          fact_key: "pkg_manager",
          text: "Uses pnpm workspaces for monorepos",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "accepted_extraction",
          sources: [
            {
              source_type: "task_message",
              task_id: "task-90",
              message_id: "msg-1",
              project_id: null,
              excerpt: "We use pnpm workspaces",
            },
          ],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "destinations", allowed_destinations: [DEST_ALPHA], revision: 1 },
          created_at: 260,
          updated_at: 260,
          last_used_at: null,
          reviewed_by_owner_at: 260,
        },
        {
          id: "manual-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "general",
          fact_key: "manual_fact",
          text: "Directly added via manual API",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "manual",
          sources: [],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "local_only", allowed_destinations: [], revision: 1 },
          created_at: 270,
          updated_at: 270,
          last_used_at: null,
          reviewed_by_owner_at: 270,
        },
        {
          id: "explicit-chat-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "preference",
          fact_key: "chat_saved",
          text: "Saved explicitly during chat",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "explicit_owner_chat",
          sources: [],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "local_only", allowed_destinations: [], revision: 1 },
          created_at: 280,
          updated_at: 280,
          last_used_at: null,
          reviewed_by_owner_at: 280,
        },
        {
          id: "unknown-1",
          scope: { kind: "personal", owner_id: "local_owner", project_id: null, project_generation: null },
          category: "workflow",
          fact_key: "unknown_origin_note",
          text: "Record with future origin identifier",
          kind: "fact",
          status: "active",
          revision: 1,
          origin: "custom_future_origin_v2",
          sources: [],
          supersedes: [],
          superseded_by: null,
          exposure_policy: { mode: "destinations", allowed_destinations: [DEST_ALPHA], revision: 1 },
          created_at: 300,
          updated_at: 300,
          last_used_at: null,
          reviewed_by_owner_at: null,
        },
      ],
      candidates: [],
      tombstone_count: 1,
    })),
    http.put("*/api/local/agent/memory/settings", async ({ request }) => {
      const body = await request.json() as Record<string, unknown>;
      settingsCalls.push(body);
      if (shouldFailSettings) {
        return HttpResponse.json({ detail: "agent_memory_unavailable" }, { status: 503 });
      }
      return HttpResponse.json({
        schema_version: 1,
        generation: 5,
        reset_epoch: 0,
        policy_revision: 2,
        settings: {
          capture_enabled: true,
          use_enabled: true,
          activation_policy: "automatic",
          extraction_model_id: DEST_ALPHA.catalog_model_id,
          extraction_destination: DEST_ALPHA,
          default_exposure: { mode: "selected_models", allowed_destinations: [DEST_ALPHA] },
        },
        entries: [],
        candidates: [],
        tombstone_count: 1,
      });
    }),
  );

  render(<MemoryEditor initialOpen />);
  const savedSection = await screen.findByRole("region", { name: "Saved facts" });
  const savedList = within(savedSection).getByRole("list");
  const items = within(savedList).getAllByRole("listitem");

  // 1. automatic_consolidation -> Automatic consolidation + source excerpt
  expect(within(items[0]).getByText("Prefers concise changelogs grouped by subsystem")).toBeInTheDocument();
  expect(within(items[0]).getByText(/Origin: Automatic consolidation/)).toBeInTheDocument();
  expect(within(items[0]).getByText(/Chat task-88 · Message msg-4/)).toBeInTheDocument();
  expect(within(items[0]).getByText(/Please keep changelogs concise and grouped by subsystem/)).toBeInTheDocument();
  expect(within(items[0]).queryByText(/Manual entry|Added manually/)).not.toBeInTheDocument();

  // 2. accepted_extraction -> Reviewed and saved + source excerpt (not labeled Automatic memory)
  expect(within(items[1]).getByText("Uses pnpm workspaces for monorepos")).toBeInTheDocument();
  expect(within(items[1]).getByText(/Origin: Reviewed and saved/)).toBeInTheDocument();
  expect(within(items[1]).getByText(/Chat task-90 · Message msg-1/)).toBeInTheDocument();
  expect(within(items[1]).getByText(/We use pnpm workspaces/)).toBeInTheDocument();
  expect(within(items[1]).queryByText(/Automatic memory|Automatic consolidation|Manual entry/)).not.toBeInTheDocument();

  // 3. manual -> Manual entry + Added manually (not labeled Automatic memory)
  expect(within(items[2]).getByText("Directly added via manual API")).toBeInTheDocument();
  expect(within(items[2]).getByText(/Origin: Manual entry · Source: Added manually/)).toBeInTheDocument();
  expect(within(items[2]).queryByText(/Automatic memory/)).not.toBeInTheDocument();

  // 4. explicit_owner_chat -> Explicitly saved from chat
  expect(within(items[3]).getByText("Saved explicitly during chat")).toBeInTheDocument();
  expect(within(items[3]).getByText(/Origin: Explicitly saved from chat · Source: Explicitly saved from chat/)).toBeInTheDocument();

  // 5. unknown origin -> neutral Saved memory label, does not infer manual or automatic, and does not display raw internal origin ID
  expect(within(items[4]).getByText("Record with future origin identifier")).toBeInTheDocument();
  expect(within(items[4]).getByText(/Origin: Saved memory · Source: Saved memory/)).toBeInTheDocument();
  expect(within(items[4]).queryByText(/custom_future_origin_v2|Manual entry|Added manually|Automatic memory|Automatic consolidation/)).not.toBeInTheDocument();

  // Select destination and enable automatic capture -> first attempt fails with 503 (unknown write outcome)
  const destSelect = screen.getByLabelText("Background extraction destination");
  await user.selectOptions(destSelect, JSON.stringify([
    DEST_ALPHA.catalog_model_id,
    DEST_ALPHA.provider_peer_id,
    DEST_ALPHA.source_id,
    DEST_ALPHA.destination_revision,
  ]));
  const captureCheckbox = screen.getByRole("checkbox", { name: "Extract and consolidate long-term memory from conversations" });
  await user.click(captureCheckbox);

  await screen.findByRole("alert");
  expect(settingsCalls).toHaveLength(1);
  expect(Object.keys(settingsCalls[0]).sort()).toEqual([
    "activation_policy",
    "capture_enabled",
    "client_request_id",
    "default_exposure",
    "expected_generation",
    "extraction_destination",
  ]);

  // Retry original operation preserves exact client_request_id, activation_policy, extraction_destination, and default_exposure tuple
  shouldFailSettings = false;
  await user.click(screen.getByRole("button", { name: "Retry original operation" }));
  await waitFor(() => expect(settingsCalls).toHaveLength(2));
  expect(settingsCalls[1]).toEqual(settingsCalls[0]);
  expect(settingsCalls[1]).toMatchObject({
    expected_generation: 4,
    capture_enabled: true,
    activation_policy: "automatic",
    extraction_destination: DEST_ALPHA,
    default_exposure: {
      mode: "selected_models",
      allowed_destinations: [DEST_ALPHA],
    },
  });
  expect(captureCheckbox).toBeChecked();
});


