import { afterEach, describe, expect, it, vi } from "vitest";
import {
  AgentComputerStatusError,
  describeComputerStatusReason,
  getAgentComputerStatus,
  looksLikeUnsafeDiagnosticOrSecret,
  normalizeEvidence,
  normalizeReasonCode,
  normalizeTriStateBoolean,
  validateAndNormalizeComputerStatus,
} from "./agentComputerStatus";

function makeBaseStatusDto(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: 1,
    snapshot_id: "snap-001",
    generated_at: "2026-10-19T08:00:00Z",
    context: {
      task_id: null,
      task_revision: null,
      settings_revision: 3,
      selection_source: "settings",
      model_id: null,
      selection_epoch: "epoch-settings-3",
    },
    plugin: {
      installed: null,
      enabled: null,
      revision: null,
      evidence: { source: "none", observed_at: null },
      reason_code: null,
    },
    native: {
      platform: "windows",
      executable_presence: "unknown",
      status: "unknown",
      version: null,
      protocol_version: null,
      evidence: { source: "none", observed_at: null },
      reason_code: "native_not_verified",
    },
    permissions: {
      agent_enabled: null,
      global_desktop_access: null,
      task_scope: "not_requested",
      task_local_execution: null,
      task_desktop_access: null,
      global_permission_mode: null,
      task_permission_mode: null,
      evidence: { source: "none", observed_at: null },
    },
    session: {
      state: "not_applicable",
      generation: null,
      evidence: { source: "none", observed_at: null },
      reason_code: null,
    },
    model: {
      id: null,
      display_name: null,
      adapter: null,
      availability: "unknown",
      image_input: {
        status: "unknown",
        carrier: "unknown",
        evidence: { source: "none", observed_at: null },
        reason_code: "image_capability_unknown",
      },
    },
    latest_observation: null,
    ...overrides,
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("agentComputerStatus domain validation & normalization", () => {
  it("validates exact v1 wire DTO and preserves tri-state null flags as unknown", () => {
    const dto = validateAndNormalizeComputerStatus(makeBaseStatusDto(), {
      expectedTaskId: null,
    });

    expect(dto.schema_version).toBe(1);
    expect(dto.snapshot_id).toBe("snap-001");
    expect(dto.context.task_id).toBeNull();
    expect(dto.context.selection_source).toBe("settings");
    expect(dto.plugin.installed).toBeNull();
    expect(dto.plugin.enabled).toBeNull();
    expect(dto.native.status).toBe("unknown");
    expect(dto.permissions.agent_enabled).toBeNull();
    expect(dto.permissions.global_desktop_access).toBeNull();
    expect(dto.session.state).toBe("not_applicable");
    expect(dto.model.image_input.status).toBe("unknown");
    expect(dto.latest_observation).toBeNull();
  });

  it("never coerces non-boolean values into true/false for tri-state flags", () => {
    expect(normalizeTriStateBoolean(true)).toBe(true);
    expect(normalizeTriStateBoolean(false)).toBe(false);
    expect(normalizeTriStateBoolean(null)).toBeNull();
    expect(normalizeTriStateBoolean(undefined)).toBeNull();
    expect(normalizeTriStateBoolean(1)).toBeNull();
    expect(normalizeTriStateBoolean(0)).toBeNull();
    expect(normalizeTriStateBoolean("true")).toBeNull();
    expect(normalizeTriStateBoolean("false")).toBeNull();
    expect(normalizeTriStateBoolean({})).toBeNull();

    const dto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        plugin: {
          installed: "true",
          enabled: 1,
          revision: 2,
          evidence: { source: "extension_store", observed_at: "2026-10-19T08:00:00Z" },
          reason_code: null,
        },
        permissions: {
          agent_enabled: "yes",
          global_desktop_access: 1,
          task_scope: "not_requested",
          task_local_execution: true,
          task_desktop_access: true,
          global_permission_mode: "invalid_mode",
          task_permission_mode: "workspace",
          evidence: { source: "agent_store", observed_at: "2026-10-19T08:00:00Z" },
        },
      }),
    );

    expect(dto.plugin.installed).toBeNull();
    expect(dto.plugin.enabled).toBeNull();
    expect(dto.permissions.agent_enabled).toBeNull();
    expect(dto.permissions.global_desktop_access).toBeNull();
    expect(dto.permissions.global_permission_mode).toBe("unknown");
    // Without task context, task permissions are not applicable even if raw payload had booleans
    expect(dto.permissions.task_local_execution).toBeNull();
    expect(dto.permissions.task_desktop_access).toBeNull();
  });

  it("rejects non-v1 schema_version, missing structural sections, and task/model context mismatches", () => {
    expect(() =>
      validateAndNormalizeComputerStatus({ ...makeBaseStatusDto(), schema_version: 2 }),
    ).toThrowError(AgentComputerStatusError);

    expect(() =>
      validateAndNormalizeComputerStatus({ ...makeBaseStatusDto(), native: null }),
    ).toThrowError(AgentComputerStatusError);

    // Requested task_id="task-a", but response returned settings context (task_id=null)
    expect(() =>
      validateAndNormalizeComputerStatus(makeBaseStatusDto(), {
        expectedTaskId: "task-a",
      }),
    ).toThrowError(AgentComputerStatusError);

    // Requested task_id="task-b", but response returned task-a
    expect(() =>
      validateAndNormalizeComputerStatus(
        makeBaseStatusDto({
          context: {
            task_id: "task-a",
            task_revision: 1,
            settings_revision: 1,
            selection_source: "task",
            model_id: "gpt-4.1",
            selection_epoch: "epoch-a",
          },
          model: {
            id: "gpt-4.1",
            display_name: "GPT-4.1",
            adapter: "openai_responses",
            availability: "available",
            image_input: {
              status: "unknown",
              carrier: "api_image_blocks",
              evidence: { source: "none", observed_at: null },
              reason_code: null,
            },
          },
        }),
        { expectedTaskId: "task-b" },
      ),
    ).toThrowError(AgentComputerStatusError);

    // Mismatched context.model_id and model.id must be rejected
    expect(() =>
      validateAndNormalizeComputerStatus(
        makeBaseStatusDto({
          context: {
            task_id: null,
            task_revision: null,
            settings_revision: 1,
            selection_source: "settings",
            model_id: "gpt-4.1",
            selection_epoch: "epoch-1",
          },
          model: {
            id: "other-model",
            display_name: "Other Model",
            adapter: "openai_responses",
            availability: "available",
            image_input: {
              status: "unknown",
              carrier: "api_image_blocks",
              evidence: { source: "none", observed_at: null },
              reason_code: null,
            },
          },
        }),
      ),
    ).toThrowError(AgentComputerStatusError);
  });

  it("enforces native ready rules: present executable without verified version/handshake or with stale evidence is unverified, never ready", () => {
    // Present executable with status="ready" but missing version/protocol/handshake evidence -> downgraded to unverified
    const unverifiedDto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        native: {
          platform: "windows",
          executable_presence: "present",
          status: "ready",
          version: null,
          protocol_version: null,
          evidence: { source: "none", observed_at: null },
          reason_code: null,
        },
      }),
    );
    expect(unverifiedDto.native.status).toBe("unverified");
    expect(unverifiedDto.native.reason_code).toBe("native_not_verified");

    // Stale evidence with status="ready" -> downgraded to unverified with native_evidence_stale
    const staleDto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        native: {
          platform: "windows",
          executable_presence: "present",
          status: "ready",
          version: "1.2.0",
          protocol_version: "1",
          evidence: { source: "native_probe_cache", observed_at: "2026-10-19T07:00:00Z" },
          reason_code: "native_evidence_stale",
        },
      }),
    );
    expect(staleDto.native.status).toBe("unverified");
    expect(staleDto.native.reason_code).toBe("native_evidence_stale");

    // Valid verified native engine stays ready
    const readyDto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        native: {
          platform: "windows",
          executable_presence: "present",
          status: "ready",
          version: "1.2.0",
          protocol_version: "1",
          evidence: { source: "native_probe_cache", observed_at: "2026-10-19T08:00:00Z" },
          reason_code: null,
        },
      }),
    );
    expect(readyDto.native.status).toBe("ready");
    expect(readyDto.native.version).toBe("1.2.0");
    expect(readyDto.native.protocol_version).toBe("1");
  });

  it("enforces model image capability rules for Antigravity, Codex carrier_available, and backend-verified API models", () => {
    // Antigravity CLI is always unsupported with carrier:none
    const agDto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        context: {
          task_id: null,
          task_revision: null,
          settings_revision: 1,
          selection_source: "settings",
          model_id: "ag-model",
          selection_epoch: "epoch-ag",
        },
        model: {
          id: "ag-model",
          display_name: "Antigravity CLI",
          adapter: "antigravity_cli",
          availability: "available",
          image_input: {
            status: "verified",
            carrier: "api_image_blocks",
            evidence: { source: "model_image_probe", observed_at: "2026-10-19T08:00:00Z" },
            reason_code: null,
          },
        },
      }),
    );
    expect(agDto.model.image_input.status).toBe("unsupported");
    expect(agDto.model.image_input.carrier).toBe("none");

    // Codex carrier_available stays carrier_available; claimed verified without probe/receipt downgrades to carrier_available
    const codexUnverified = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        context: {
          task_id: null,
          task_revision: null,
          settings_revision: 1,
          selection_source: "settings",
          model_id: "codex-default",
          selection_epoch: "epoch-codex",
        },
        model: {
          id: "codex-default",
          display_name: "Codex CLI",
          adapter: "codex_cli",
          availability: "available",
          image_input: {
            status: "verified",
            carrier: "codex_turn_input",
            evidence: { source: "model_route_contract", observed_at: "2026-10-19T08:00:00Z" },
            reason_code: null,
          },
        },
      }),
    );
    expect(codexUnverified.model.image_input.status).toBe("carrier_available");

    // API model with model_image_probe evidence and observed_at stays verified
    const apiVerified = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        context: {
          task_id: null,
          task_revision: null,
          settings_revision: 1,
          selection_source: "settings",
          model_id: "gpt-4.1",
          selection_epoch: "epoch-gpt",
        },
        model: {
          id: "gpt-4.1",
          display_name: "GPT-4.1",
          adapter: "openai_responses",
          availability: "available",
          image_input: {
            status: "verified",
            carrier: "api_image_blocks",
            evidence: { source: "model_image_probe", observed_at: "2026-10-19T08:00:00Z" },
            reason_code: null,
          },
        },
      }),
    );
    expect(apiVerified.model.image_input.status).toBe("verified");
  });

  it("preserves independent observation stages and marks mismatched selection_epoch or model_id as matches_current_selection=false", () => {
    const dto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        context: {
          task_id: "task-42",
          task_revision: 7,
          settings_revision: 2,
          selection_source: "task",
          model_id: "codex-new",
          selection_epoch: "epoch-new",
        },
        model: {
          id: "codex-new",
          display_name: "Codex New",
          adapter: "codex_cli",
          availability: "available",
          image_input: {
            status: "carrier_available",
            carrier: "codex_turn_input",
            evidence: { source: "model_route_contract", observed_at: "2026-10-19T08:00:00Z" },
            reason_code: "image_verification_pending",
          },
        },
        latest_observation: {
          id: "obs-100",
          task_revision: 6,
          selection_epoch: "epoch-old",
          model_id: "gpt-4.1-old",
          matches_current_selection: true,
          width: 1920,
          height: 1080,
          stages: {
            saved: {
              state: "recorded",
              evidence: { source: "runtime_receipt", observed_at: "2026-10-19T07:55:00Z" },
            },
            displayed: {
              state: "unknown",
              evidence: { source: "none", observed_at: null },
            },
            submitted: {
              state: "recorded",
              evidence: { source: "runtime_receipt", observed_at: "2026-10-19T07:55:01Z" },
            },
            semantic_observed: {
              state: "recorded",
              evidence: { source: "runtime_receipt", observed_at: "2026-10-19T07:55:03Z" },
            },
          },
        },
      }),
      { expectedTaskId: "task-42" },
    );

    expect(dto.latest_observation).not.toBeNull();
    expect(dto.latest_observation?.matches_current_selection).toBe(false);
    expect(dto.latest_observation?.stages.saved.state).toBe("recorded");
    expect(dto.latest_observation?.stages.displayed.state).toBe("unknown");
    expect(dto.latest_observation?.stages.submitted.state).toBe("recorded");
    expect(dto.latest_observation?.stages.semantic_observed.state).toBe("recorded");
  });

  it("strips unsafe diagnostic strings, paths, secrets, and unknown reason codes", () => {
    expect(looksLikeUnsafeDiagnosticOrSecret("C:\\Users\\admin\\ryn-computer.exe")).toBe(true);
    expect(looksLikeUnsafeDiagnosticOrSecret("/home/user/.codex/auth.json")).toBe(true);
    expect(looksLikeUnsafeDiagnosticOrSecret("Bearer sk-secret1234567890")).toBe(true);
    expect(looksLikeUnsafeDiagnosticOrSecret("Traceback (most recent call last)")).toBe(true);
    expect(looksLikeUnsafeDiagnosticOrSecret("1.2.0")).toBe(false);

    expect(normalizeReasonCode("unknown_raw_exception_trace")).toBe("status_unavailable");
    expect(
      describeComputerStatusReason("unknown_raw_exception_trace", (_zh, en) => en),
    ).toBe("Status unavailable");

    expect(
      normalizeEvidence({ source: "unexpected_source", observed_at: "not-a-date" }),
    ).toEqual({
      source: "none",
      observed_at: null,
    });

    const sanitizedDto = validateAndNormalizeComputerStatus(
      makeBaseStatusDto({
        native: {
          platform: "windows",
          executable_presence: "present",
          status: "unverified",
          version: "C:\\Program Files\\Ryn\\ryn-computer.exe --version 1.0.0",
          protocol_version: "https://internal.example.com/v1",
          evidence: { source: "none", observed_at: null },
          reason_code: "Traceback: api_key=sk-1234567890abcdef",
        },
        model: {
          id: null,
          display_name: "Error: ENOENT C:\\secret\\config.toml",
          adapter: null,
          availability: "unknown",
          image_input: {
            status: "unknown",
            carrier: "unknown",
            evidence: { source: "none", observed_at: null },
            reason_code: null,
          },
        },
      }),
    );

    expect(sanitizedDto.native.version).toBeNull();
    expect(sanitizedDto.native.protocol_version).toBeNull();
    expect(sanitizedDto.native.reason_code).toBe("status_unavailable");
    expect(sanitizedDto.model.display_name).toBeNull();
  });

  it("performs GET-only requests with credentials:include and maps 404/503/invalid responses to safe errors", async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    let nextResponse: () => Response = () =>
      new Response(JSON.stringify({ detail: "Not Found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });

    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: String(input), init });
      return nextResponse();
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(getAgentComputerStatus()).rejects.toMatchObject({
      code: "endpoint_not_found",
      status: 404,
    });

    expect(calls).toHaveLength(1);
    expect(calls[0].url).toContain("/api/local/agent/computer/status");
    expect(calls[0].init?.method).toBe("GET");
    expect(calls[0].init?.credentials).toBe("include");
    expect(calls[0].init?.cache).toBe("no-store");

    // 404 with agent_task_not_found when taskId is supplied
    nextResponse = () =>
      new Response(JSON.stringify({ detail: "agent_task_not_found" }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });

    await expect(getAgentComputerStatus({ taskId: "task-missing" })).rejects.toMatchObject({
      code: "agent_task_not_found",
      status: 404,
    });
    expect(calls).toHaveLength(2);
    expect(calls[1].url).toContain("/api/local/agent/computer/status?task_id=task-missing");
    expect(calls[1].init?.method).toBe("GET");
    expect(calls[1].init?.credentials).toBe("include");
    expect(calls[1].init?.cache).toBe("no-store");

    // 503 maps to agent_computer_status_unavailable without leaking raw detail
    nextResponse = () =>
      new Response(
        JSON.stringify({
          detail: "Traceback: C:\\Users\\secret\\worker.exe failed with token=sk-secret123456",
        }),
        { status: 503, headers: { "Content-Type": "application/json" } },
      );

    try {
      await getAgentComputerStatus();
      expect.unreachable("Should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(AgentComputerStatusError);
      const statusErr = error as AgentComputerStatusError;
      expect(statusErr.code).toBe("agent_computer_status_unavailable");
      expect(statusErr.message).toBe("agent_computer_status_unavailable");
      expect(statusErr.message).not.toContain("C:\\Users");
      expect(statusErr.message).not.toContain("sk-secret");
    }
    expect(calls).toHaveLength(3);
    expect(calls[2].init?.method).toBe("GET");
    expect(calls[2].init?.credentials).toBe("include");
  });

  it("rejects stale model_id, revision bounds, and contradictory permission grants against caller expectations", () => {
    const rawModelA = makeBaseStatusDto({
      context: {
        task_id: "same-task",
        task_revision: 3,
        settings_revision: 4,
        selection_source: "task",
        model_id: "model-a",
        selection_epoch: "epoch-model-a",
      },
      permissions: {
        agent_enabled: true,
        global_desktop_access: true,
        task_scope: "present",
        task_local_execution: true,
        task_desktop_access: true,
        global_permission_mode: "workspace",
        task_permission_mode: "workspace",
        evidence: { source: "agent_store", observed_at: "2026-10-19T08:00:00Z" },
      },
      model: {
        id: "model-a",
        display_name: "Fixture model-a",
        adapter: "codex_cli",
        availability: "available",
        image_input: {
          status: "verified",
          carrier: "codex_turn_input",
          evidence: { source: "model_image_probe", observed_at: "2026-10-19T08:00:00Z" },
          reason_code: null,
        },
      },
    });

    // When expectedModelId is undefined, internally consistent model-a is accepted
    expect(
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
      }).model.id,
    ).toBe("model-a");

    // When expectedModelId is "model-b", stale model-a is rejected even though context.model_id === model.id
    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: "model-b",
      }),
    ).toThrowError(AgentComputerStatusError);

    // Intentional null expectedModelId rejects non-null model_id
    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: null,
      }),
    ).toThrowError(AgentComputerStatusError);

    // Contradictory permission grants are rejected when caller knows permissions are revoked
    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        expectedPermissions: {
          agentEnabled: false,
        },
      }),
    ).toThrowError(AgentComputerStatusError);

    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        expectedPermissions: {
          globalDesktopAccess: false,
        },
      }),
    ).toThrowError(AgentComputerStatusError);

    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        expectedPermissions: {
          taskDesktopAccess: false,
        },
      }),
    ).toThrowError(AgentComputerStatusError);

    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        minTaskRevision: 4,
      }),
    ).toThrowError(AgentComputerStatusError);

    expect(() =>
      validateAndNormalizeComputerStatus(rawModelA, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        minSettingsRevision: 5,
      }),
    ).toThrowError(AgentComputerStatusError);

    // A null/unknown revision cannot prove freshness when an explicit minimum is required
    const rawNullRevisions = makeBaseStatusDto({
      ...rawModelA,
      context: {
        ...(rawModelA.context as Record<string, unknown>),
        task_revision: null,
        settings_revision: null,
      },
    });

    expect(() =>
      validateAndNormalizeComputerStatus(rawNullRevisions, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        minTaskRevision: 0,
      }),
    ).toThrowError(AgentComputerStatusError);

    expect(() =>
      validateAndNormalizeComputerStatus(rawNullRevisions, {
        expectedTaskId: "same-task",
        expectedModelId: "model-a",
        minSettingsRevision: 0,
      }),
    ).toThrowError(AgentComputerStatusError);
  });
});
