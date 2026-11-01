import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { AgentAPIError, type AgentProject } from "../domain/rynAgent";
import {
  destinationTupleKey,
  rynAgentMemory,
  sameDestinationTuple,
  type AgentMemoryActivationPolicy,
  type AgentMemoryCandidate,
  type AgentMemoryCandidateDecisionInput,
  type AgentMemoryCreateEntryInput,
  type AgentMemoryDeleteEntryInput,
  type AgentMemoryDestinationOption,
  type AgentMemoryDestinationTuple,
  type AgentMemoryEntry,
  type AgentMemoryExposureMode,
  type AgentMemoryExposurePolicy,
  type AgentMemoryExposurePolicyInput,
  type AgentMemoryResetInput,
  type AgentMemoryScopeInput,
  type AgentMemoryScopeKind,
  type AgentMemorySettingsPatch,
  type AgentMemorySnapshot,
  type AgentMemorySourceRecord,
  type AgentMemoryUpdateEntryInput,
} from "../domain/rynAgentMemory";

function useText() {
  const { i18n } = useTranslation();
  return (cn: string, en: string) => i18n.language.startsWith("zh") ? cn : en;
}

export interface MemoryEditorProps {
  projects?: AgentProject[];
  currentProject?: AgentProject;
  initialOpen?: boolean;
}

type PendingMemoryMutation =
  | { kind: "settings"; body: AgentMemorySettingsPatch }
  | { kind: "create_entry"; body: AgentMemoryCreateEntryInput }
  | { kind: "update_entry"; id: string; body: AgentMemoryUpdateEntryInput }
  | { kind: "delete_entry"; id: string; body: AgentMemoryDeleteEntryInput }
  | { kind: "decide_candidate"; id: string; body: AgentMemoryCandidateDecisionInput }
  | { kind: "reset"; body: AgentMemoryResetInput };

function formatOriginLabel(
  origin: string | undefined,
  text: (cn: string, en: string) => string,
): string {
  if (origin === "manual" || origin === "manual_owner") {
    return text("手动添加", "Manual entry");
  }
  if (origin === "accepted_extraction") {
    return text("已审核保存", "Reviewed and saved");
  }
  if (origin === "explicit_owner_chat") {
    return text("从对话明确保存", "Explicitly saved from chat");
  }
  if (origin === "automatic_consolidation") {
    return text("自动提取与整合", "Automatic consolidation");
  }
  if (origin === "background_extraction") {
    return text("后台提取", "Background extraction");
  }
  return text("已保存记忆", "Saved memory");
}

function formatSourcesSummary(
  sources: AgentMemorySourceRecord[],
  text: (cn: string, en: string) => string,
  origin?: string,
): string {
  if (!sources.length) {
    if (origin === "manual" || origin === "manual_owner") {
      return text("手动添加", "Added manually");
    }
    return formatOriginLabel(origin, text);
  }
  return sources.map(src => {
    const typeLabel = src.source_type === "task_message"
      ? [
          src.task_id ? `${text("对话", "Chat")} ${src.task_id}` : text("本机对话", "Local chat"),
          src.message_id ? `${text("消息", "Message")} ${src.message_id}` : "",
        ].filter(Boolean).join(" · ")
      : src.source_type === "manual_owner"
        ? text("手动添加", "Added manually")
        : text("已保存记忆", "Saved memory");
    const excerpt = src.excerpt?.trim();
    return excerpt ? `${typeLabel} (“${excerpt}”)` : typeLabel;
  }).join("；");
}

function exposureModeLabel(mode: AgentMemoryExposureMode, text: (cn: string, en: string) => string): string {
  if (mode === "destinations") return text("仅向所选接收方发送", "Send only to selected destinations");
  if (mode === "all_selected") return text("允许所有所选任务模型", "Allow all selected task models");
  return text("仅本机保存，不发送给模型", "Store on this device; never send to models");
}

function formatExposureSummary(
  policy: AgentMemoryExposurePolicy,
  destinations: AgentMemoryDestinationOption[],
  text: (cn: string, en: string) => string,
): { summary: string; hasUnavailable: boolean } {
  if (policy.mode === "local_only") {
    return { summary: exposureModeLabel("local_only", text), hasUnavailable: false };
  }
  if (policy.mode === "all_selected") {
    return { summary: exposureModeLabel("all_selected", text), hasUnavailable: false };
  }
  if (!policy.allowed_destinations.length) {
    return {
      summary: `${exposureModeLabel("destinations", text)} (${text("未选择可用接收方", "No destination selected")})`,
      hasUnavailable: true,
    };
  }
  let hasUnavailable = false;
  const labels = policy.allowed_destinations.map(tuple => {
    const match = destinations.find(option => sameDestinationTuple(option.destination, tuple));
    if (!match || !match.available) {
      hasUnavailable = true;
      return `${match?.label || tuple.catalog_model_id} (${text("不可用，需重新选择", "Unavailable — reselect required")})`;
    }
    return match.label;
  });
  return {
    summary: `${exposureModeLabel("destinations", text)}: ${labels.join(", ")}`,
    hasUnavailable,
  };
}

export function MemoryEditor({ projects = [], currentProject, initialOpen = false }: MemoryEditorProps) {
  const text = useText();
  const [open, setOpen] = useState(initialOpen);
  const [snapshot, setSnapshot] = useState<AgentMemorySnapshot | null>(null);
  const [destinations, setDestinations] = useState<AgentMemoryDestinationOption[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [needsReload, setNeedsReload] = useState(false);
  const [pendingMutation, setPendingMutation] = useState<PendingMemoryMutation | null>(null);
  const [scopeFilter, setScopeFilter] = useState<"all" | AgentMemoryScopeKind>("all");

  // Explicit owner choice for enabling natural memory extraction in the current session
  const [selectedExtractionKey, setSelectedExtractionKey] = useState("");
  // Proposed activation mode for the next enable action when capture is OFF (defaults to recommended "automatic")
  const [nextActivationPolicy, setNextActivationPolicy] = useState<AgentMemoryActivationPolicy>("automatic");

  // Search state
  const [searchQuery, setSearchQuery] = useState("");
  const [searchResults, setSearchResults] = useState<AgentMemoryEntry[] | null>(null);

  // Add entry draft state (always defaults to local_only; never defaults to all_selected)
  const [newText, setNewText] = useState("");
  const [newScopeKind, setNewScopeKind] = useState<AgentMemoryScopeKind>(currentProject ? "project" : "personal");
  const [newProjectID, setNewProjectID] = useState(currentProject?.id || "");
  const [newExposureMode, setNewExposureMode] = useState<AgentMemoryExposureMode>("local_only");
  const [newDestinationKeys, setNewDestinationKeys] = useState<string[]>([]);

  // Edit entry state
  const [editingID, setEditingID] = useState<string | null>(null);
  const [editText, setEditText] = useState("");
  const [editScopeKind, setEditScopeKind] = useState<AgentMemoryScopeKind>("personal");
  const [editProjectID, setEditProjectID] = useState("");
  const [editExposureMode, setEditExposureMode] = useState<AgentMemoryExposureMode>("local_only");
  const [editDestinationKeys, setEditDestinationKeys] = useState<string[]>([]);

  // Two-step reset state
  const [confirmingReset, setConfirmingReset] = useState(false);

  const loadSeq = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      loadSeq.current++;
    };
  }, []);

  const load = async () => {
    const seq = ++loadSeq.current;
    setBusy(true);
    setError("");
    try {
      const [loadedSnapshot, loadedDestinations] = await Promise.all([
        rynAgentMemory.get(),
        rynAgentMemory.destinations().catch(() => ({ destinations: [] as AgentMemoryDestinationOption[], default_destination: null })),
      ]);
      if (!mounted.current || seq !== loadSeq.current) return;
      setSnapshot(loadedSnapshot);
      setDestinations(loadedDestinations.destinations);
      // Never pre-populate explicit extraction consent from saved settings or default_destination on load/reload
      setSelectedExtractionKey("");
      setNeedsReload(false);
      setSearchResults(null);
      setConfirmingReset(false);
    } catch {
      if (!mounted.current || seq !== loadSeq.current) return;
      setError(text(
        "无法读取或核对长期记忆数据格式，请重新读取。",
        "Unable to load or verify long-term memory state. Reload to try again.",
      ));
    } finally {
      if (mounted.current && seq === loadSeq.current) setBusy(false);
    }
  };

  useEffect(() => {
    if (initialOpen && !snapshot && !busy) {
      void load();
    }
  }, []);

  const executeMutation = async (mutation: PendingMemoryMutation) => {
    setBusy(true);
    setError("");
    try {
      let next: AgentMemorySnapshot;
      switch (mutation.kind) {
        case "settings":
          next = await rynAgentMemory.updateSettings(mutation.body);
          break;
        case "create_entry":
          next = await rynAgentMemory.createEntry(mutation.body);
          break;
        case "update_entry":
          next = await rynAgentMemory.updateEntry(mutation.id, mutation.body);
          break;
        case "delete_entry":
          next = await rynAgentMemory.deleteEntry(mutation.id, mutation.body);
          break;
        case "decide_candidate":
          next = await rynAgentMemory.decideCandidate(mutation.id, mutation.body);
          break;
        case "reset":
          next = await rynAgentMemory.reset(mutation.body);
          break;
      }
      if (!mounted.current) return;
      setSnapshot(next);
      // Require a fresh explicit destination choice if capture is now disabled
      if (!next.settings.capture_enabled) {
        setSelectedExtractionKey("");
      }
      setPendingMutation(null);
      setNeedsReload(false);
      setSearchResults(null);
      if (mutation.kind === "create_entry") {
        setNewText("");
        setNewExposureMode("local_only");
        setNewDestinationKeys([]);
      } else if (mutation.kind === "update_entry") {
        setEditingID(null);
      } else if (mutation.kind === "delete_entry" && editingID === mutation.id) {
        setEditingID(null);
      } else if (mutation.kind === "reset") {
        setConfirmingReset(false);
        setEditingID(null);
      }
    } catch (reason) {
      if (!mounted.current) return;
      if (reason instanceof AgentAPIError && reason.status === 409) {
        setPendingMutation(null);
        setNeedsReload(true);
        setSelectedExtractionKey("");
        setError(text(
          "长期记忆已变化，或上次操作已生效。请重新读取后核对；不会覆盖新版本。",
          "Long-term memory changed, or the previous operation already took effect. Reload and review; the newer version will not be overwritten.",
        ));
      } else if (reason instanceof AgentAPIError && reason.status >= 400 && reason.status < 500) {
        setPendingMutation(null);
        setSelectedExtractionKey("");
        setError(text(
          "请求未通过校验，请核对范围或接收方选择后再试。",
          "The request was rejected. Review the scope or destination selection before trying again.",
        ));
      } else {
        setPendingMutation(mutation);
        setError(text(
          "操作结果未确认，请重新读取核对当前状态，或重试原操作。",
          "Action result is unconfirmed. Reload to review the current state, or retry the original operation.",
        ));
      }
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const availableDestinations = destinations.filter(item => item.available);
  const selectedExtractionOption = availableDestinations.find(
    item => destinationTupleKey(item.destination) === selectedExtractionKey,
  ) || null;
  const savedExtractionDest = snapshot?.settings.extraction_destination || null;
  const savedExtractionOption = savedExtractionDest
    ? destinations.find(opt => sameDestinationTuple(opt.destination, savedExtractionDest)) || null
    : null;
  const savedExtractionUnavailable = Boolean(
    savedExtractionDest && (!savedExtractionOption || !savedExtractionOption.available),
  );

  const toggleUseMemory = (use_enabled: boolean) => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    void executeMutation({
      kind: "settings",
      body: {
        expected_generation: snapshot.generation,
        client_request_id: crypto.randomUUID(),
        use_enabled,
      },
    });
  };

  const toggleCaptureMemory = (capture_enabled: boolean) => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    if (capture_enabled) {
      if (!selectedExtractionOption) return;
      const chosenTuple = selectedExtractionOption.destination;
      void executeMutation({
        kind: "settings",
        body: {
          expected_generation: snapshot.generation,
          client_request_id: crypto.randomUUID(),
          capture_enabled: true,
          activation_policy: nextActivationPolicy,
          extraction_destination: chosenTuple,
          default_exposure: {
            mode: "selected_models",
            allowed_destinations: [chosenTuple],
          },
        },
      });
    } else {
      void executeMutation({
        kind: "settings",
        body: {
          expected_generation: snapshot.generation,
          client_request_id: crypto.randomUUID(),
          capture_enabled: false,
        },
      });
    }
  };

  const resolveSelectedTuples = (keys: string[]): AgentMemoryDestinationTuple[] =>
    availableDestinations
      .filter(option => keys.includes(destinationTupleKey(option.destination)))
      .map(option => option.destination);

  const buildExposurePolicyInput = (
    mode: AgentMemoryExposureMode,
    keys: string[],
  ): AgentMemoryExposurePolicyInput | null => {
    if (mode === "local_only") {
      return { mode: "local_only", allowed_destinations: [] };
    }
    if (mode === "all_selected") {
      return { mode: "all_selected", allowed_destinations: [] };
    }
    const tuples = resolveSelectedTuples(keys);
    if (!tuples.length) return null;
    return { mode: "destinations", allowed_destinations: tuples };
  };

  const buildScopeInput = (kind: AgentMemoryScopeKind, projectID: string): AgentMemoryScopeInput | null => {
    if (kind === "personal") return { kind: "personal", project_id: null };
    const trimmed = projectID.trim();
    if (!trimmed) return null;
    return { kind: "project", project_id: trimmed };
  };

  const addEntry = () => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    const trimmed = newText.trim();
    const scope = buildScopeInput(newScopeKind, newProjectID);
    const exposure_policy = buildExposurePolicyInput(newExposureMode, newDestinationKeys);
    if (!trimmed || !scope || !exposure_policy) return;
    void executeMutation({
      kind: "create_entry",
      body: {
        expected_generation: snapshot.generation,
        client_request_id: crypto.randomUUID(),
        text: trimmed,
        scope,
        exposure_policy,
      },
    });
  };

  const startEditing = (entry: AgentMemoryEntry) => {
    if (busy || needsReload || pendingMutation) return;
    setEditingID(entry.id);
    setEditText(entry.text);
    setEditScopeKind(entry.scope.kind);
    setEditProjectID(entry.scope.project_id || currentProject?.id || "");
    setEditExposureMode(entry.exposure_policy.mode);
    const validKeys = entry.exposure_policy.allowed_destinations
      .filter(tuple => availableDestinations.some(opt => sameDestinationTuple(opt.destination, tuple)))
      .map(destinationTupleKey);
    setEditDestinationKeys(validKeys);
  };

  const saveEdit = (entry: AgentMemoryEntry) => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    const trimmed = editText.trim();
    const scope = buildScopeInput(editScopeKind, editProjectID);
    const exposure_policy = buildExposurePolicyInput(editExposureMode, editDestinationKeys);
    if (!trimmed || !scope || !exposure_policy) return;
    void executeMutation({
      kind: "update_entry",
      id: entry.id,
      body: {
        expected_generation: snapshot.generation,
        expected_entry_revision: entry.revision,
        client_request_id: crypto.randomUUID(),
        text: trimmed,
        scope,
        exposure_policy,
      },
    });
  };

  const deleteEntry = (entry: AgentMemoryEntry) => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    void executeMutation({
      kind: "delete_entry",
      id: entry.id,
      body: {
        expected_generation: snapshot.generation,
        expected_entry_revision: entry.revision,
        client_request_id: crypto.randomUUID(),
      },
    });
  };

  const decideCandidate = (candidate: AgentMemoryCandidate, decision: "accept" | "reject") => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    void executeMutation({
      kind: "decide_candidate",
      id: candidate.id,
      body: {
        expected_generation: snapshot.generation,
        expected_candidate_revision: candidate.revision,
        client_request_id: crypto.randomUUID(),
        decision,
      },
    });
  };

  const runSearch = async () => {
    if (!snapshot || busy) return;
    const trimmed = searchQuery.trim();
    if (!trimmed) {
      setSearchResults(null);
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await rynAgentMemory.search({
        q: trimmed,
        ...(scopeFilter !== "all" ? { scope_kind: scopeFilter } : {}),
      });
      if (mounted.current) setSearchResults(result.entries);
    } catch {
      if (mounted.current) setError(text("无法搜索记忆，请重试。", "Unable to search memory. Retry."));
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  const confirmReset = () => {
    if (!snapshot || busy || needsReload || pendingMutation) return;
    void executeMutation({
      kind: "reset",
      body: {
        expected_generation: snapshot.generation,
        client_request_id: crypto.randomUUID(),
        scope: null,
      },
    });
  };

  const locked = busy || needsReload || Boolean(pendingMutation);
  const knownProjects = [...new Map([
    ...(currentProject ? [[currentProject.id, currentProject] as const] : []),
    ...projects.map(project => [project.id, project] as const),
  ]).values()];

  const projectDisplayName = (projectID?: string | null) => {
    if (!projectID) return text("未指定项目", "Unspecified project");
    const match = knownProjects.find(project => project.id === projectID);
    return match ? `${match.name} (${match.id})` : projectID;
  };

  const activeEntries = (searchResults ?? snapshot?.entries ?? []).filter(entry =>
    entry.status === "active" && (scopeFilter === "all" ? true : entry.scope.kind === scopeFilter),
  );
  const pendingCandidates = (snapshot?.candidates ?? []).filter(candidate =>
    candidate.status === "pending" && (scopeFilter === "all" ? true : candidate.scope.kind === scopeFilter),
  );

  const newPolicyValid = newExposureMode !== "destinations" || resolveSelectedTuples(newDestinationKeys).length > 0;
  const editPolicyValid = editExposureMode !== "destinations" || resolveSelectedTuples(editDestinationKeys).length > 0;

  return (
    <div className="ra-guidance ra-memory">
      <button
        type="button"
        className="ra-link"
        aria-expanded={open}
        onClick={() => {
          const nextOpen = !open;
          setOpen(nextOpen);
          if (nextOpen && !snapshot && !busy) void load();
        }}
      >
        {text("长期记忆", "Long-term memory")}
      </button>
      {open && (
        <section aria-label={text("长期记忆管理", "Long-term memory")}>
          <p>
            {text(
              "长期记忆是本机加密保存、可编辑的低优先级背景资料。当前任务要求和手动偏好始终优先；记忆不会增加工具权限或自动授权操作。请勿保存密钥。",
              "Long-term memory is encrypted on this device as editable, low-priority background context. Current task requirements and manual preferences always take priority; memory grants no tool permissions. Do not store secrets.",
            )}
          </p>
          <p>
            {text(
              "开启自然长期记忆后，系统会在日常对话后异步提取稳定的第一人称事实并与旧记忆整合；后续任务先使用精简摘要，仅在需要时按需调取来源细节（异步整合存在延迟，不承诺即时或完美记忆）。日常对话无需输入“记住”指令；下方的手动“添加事实”仅作为可选快捷方式。",
              "When natural long-term memory is enabled, stable first-person facts are asynchronously extracted after ordinary conversations and consolidated with existing memories; later tasks use compact summaries first and recall source details on demand (consolidation is asynchronous and does not guarantee instant or perfect memory). Ordinary conversations do not need a 'remember' command; manual 'Add fact' below is an optional shortcut.",
            )}
          </p>

          {busy && !snapshot && <p role="status">{text("正在加载记忆…", "Loading memory…")}</p>}
          {error && <p role="alert">{error}</p>}

          <div className="ra-approval-actions">
            <button type="button" className="ra-link" disabled={busy} onClick={() => void load()}>
              {text("重新读取", "Reload")}
            </button>
            {pendingMutation && (
              <button
                type="button"
                className="ra-primary"
                disabled={busy || needsReload}
                onClick={() => void executeMutation(pendingMutation)}
              >
                {text("重试原操作", "Retry original operation")}
              </button>
            )}
          </div>

          {snapshot && (
            <>
              <fieldset className="ra-tool-permissions">
                <legend>{text("记忆开关", "Memory controls")}</legend>
                <label className="ra-setting">
                  <span>
                    {text("在对话中使用已保存记忆", "Use saved memory in tasks")}
                    <small>
                      {text(
                        "独立开关。开启后，仅符合当前范围（个人或对应本机项目）与模型接收方政策的有效记忆（先使用精简摘要，按需调取来源细节）会作为低优先级参考提供给模型。",
                        "Independent switch. When enabled, only active memories matching the active scope (personal or matching local project) and per-entry model destination limits are included as low-priority context (compact summaries first, with source details recalled on demand).",
                      )}
                    </small>
                  </span>
                  <input
                    type="checkbox"
                    aria-label={text("在对话中使用已保存记忆", "Use saved memory in tasks")}
                    checked={snapshot.settings.use_enabled}
                    disabled={locked}
                    onChange={event => toggleUseMemory(event.target.checked)}
                  />
                </label>

                <div className="ra-memory-capture-controls">
                  <p>
                    {snapshot.settings.capture_enabled
                      ? `${text("当前已保存激活模式", "Saved activation policy")}: ${
                          snapshot.settings.activation_policy === "automatic"
                            ? text("自动记忆（稳定事实自动保存）", "Automatic memory (stable facts saved automatically)")
                            : text("每条记忆先经我确认", "Review each memory first")
                        }`
                      : `${text("已保存激活策略（当前未开启提取）", "Saved activation policy (capture currently off)")}: ${
                          snapshot.settings.activation_policy === "automatic"
                            ? text("自动记忆（稳定事实自动保存）", "Automatic memory (stable facts saved automatically)")
                            : text("每条记忆先经我确认", "Review each memory first")
                        } · ${text("开启时推荐使用自动记忆", "Automatic memory is recommended when enabling")}`}
                  </p>
                  {savedExtractionDest && (
                    <p>
                      {snapshot.settings.capture_enabled
                        ? `${text("当前已启用提取目标", "Active extraction destination")}: ${savedExtractionOption?.label || savedExtractionDest.catalog_model_id}`
                        : `${text("上次保存的提取目标", "Previously saved extraction destination")}: ${savedExtractionOption?.label || savedExtractionDest.catalog_model_id}`}
                    </p>
                  )}
                  <label>
                    <span>
                      {text("记忆激活模式", "Memory activation mode")}
                      <small>
                        {snapshot.settings.capture_enabled
                          ? text(
                              "长期记忆提取已开启，激活模式已锁定。如需切换模式，请先关闭长期记忆提取，再重新选择模式并开启。",
                              "Activation mode is locked while memory capture is on. Stop memory capture first before switching modes.",
                            )
                          : text(
                              "选择下次开启长期记忆提取时的激活方式。推荐使用“自动记忆（稳定事实自动保存）”；也可选择“每条记忆先经我确认”。",
                              "Choose the activation mode for your next enable action. 'Automatic memory (stable facts saved automatically)' is recommended, or choose 'Review each memory first'.",
                            )}
                      </small>
                    </span>
                    <select
                      aria-label={text("记忆激活模式", "Memory activation mode")}
                      value={snapshot.settings.capture_enabled ? snapshot.settings.activation_policy : nextActivationPolicy}
                      disabled={locked || snapshot.settings.capture_enabled}
                      onChange={event => setNextActivationPolicy(event.target.value as AgentMemoryActivationPolicy)}
                    >
                      <option value="automatic">
                        {text(
                          "自动记忆（稳定事实自动保存）· 推荐",
                          "Automatic memory (stable facts saved automatically) · Recommended",
                        )}
                      </option>
                      <option value="owner_review">
                        {text("每条记忆先经我确认", "Review each memory first")}
                      </option>
                    </select>
                  </label>
                  <label>
                    <span>
                      {text("后台提取目标接收方", "Background extraction destination")}
                      <small>
                        {text(
                          "首次开启或每次重新开启前，必须明确选择一个当前可用的已核对模型接收方（含本机 CLI）。不会自动选择上次保存的目标、默认目标或收费来源。",
                          "Before first enabling or re-enabling, explicitly choose a currently available verified model destination (including native CLI). Never auto-selects a previous destination, default destination, or paid source.",
                        )}
                      </small>
                    </span>
                    <select
                      aria-label={text("后台提取目标接收方", "Background extraction destination")}
                      value={selectedExtractionKey}
                      disabled={locked}
                      onChange={event => setSelectedExtractionKey(event.target.value)}
                    >
                      <option value="">{text("请选择提取模型接收方", "Choose an extraction destination")}</option>
                      {destinations.map(option => {
                        const key = destinationTupleKey(option.destination);
                        return (
                          <option key={key} value={key} disabled={!option.available}>
                            {option.label}{!option.available ? ` · ${text("不可用", "Unavailable")}` : ""}
                          </option>
                        );
                      })}
                    </select>
                  </label>
                  {savedExtractionUnavailable && (
                    <p role="status">
                      {text(
                        "原后台提取接收方已不可用或配置已变更，请重新选择可用接收方。",
                        "The previously configured extraction destination is unavailable or changed. Reselect an available destination.",
                      )}
                    </p>
                  )}
                  {snapshot.settings.capture_enabled ? (
                    <p className="ra-memory-active-status">
                      {snapshot.settings.activation_policy === "automatic"
                        ? text(
                            `自动记忆运行中：当前提取目标（${savedExtractionOption?.label || savedExtractionDest?.catalog_model_id || text("已保存目标", "Saved destination")}）会在对话后异步提取并整合稳定事实，且仅在独立“在对话中使用已保存记忆”开启并允许时于后续对话中向该所选模型提供记忆。如需切换模式或目标，请先关闭下方开关。`,
                            `Automatic memory is active: ${savedExtractionOption?.label || savedExtractionDest?.catalog_model_id || "Saved destination"} asynchronously extracts and consolidates stable facts after conversations, and receives saved memories in later chats only when 'Use saved memory in tasks' is on and allowed. Stop capture below first to switch modes or destination.`,
                          )
                        : text(
                            `“每条记忆先经我确认”运行中：当前提取目标（${savedExtractionOption?.label || savedExtractionDest?.catalog_model_id || text("已保存目标", "Saved destination")}）会在对话后异步提取候选事实；所有提取事实均需经你接受后，才会在“在对话中使用已保存记忆”开启且允许时用于后续对话。如需切换模式或目标，请先关闭下方开关。`,
                            `Review each memory first is active: ${savedExtractionOption?.label || savedExtractionDest?.catalog_model_id || "Saved destination"} asynchronously proposes candidate facts after conversations, and only facts you accept become available in later chats when 'Use saved memory in tasks' is on and allowed. Stop capture below first to switch modes or destination.`,
                          )}
                    </p>
                  ) : (
                    <p className="ra-memory-consent-notice">
                      {nextActivationPolicy === "automatic"
                        ? text(
                            `一次性授权说明（自动记忆）：开启下方开关即明确授权所选的确切模型（${selectedExtractionOption ? selectedExtractionOption.label : "请先在上方选择可用模型"}）接收符合条件的对话内容以异步提取并整合旧记忆，且仅在独立“在对话中使用已保存记忆”开启并允许时于后续对话中向该所选模型提供已保存记忆（仅限该所选模型，不会自动切换收费来源）。稳定的第一人称事实会自动保存生效，无需逐条确认；存在歧义或冲突的条目会留待确认。`,
                            `One-time authorization (Automatic memory): Enabling the toggle below authorizes the exact selected model (${selectedExtractionOption ? selectedExtractionOption.label : "select an available model above first"}) to receive eligible conversations for asynchronous extraction and consolidation with old memories, and to receive the saved memories in later chats only when 'Use saved memory in tasks' is on and allowed (only this selected model; never automatic paid fallback). Stable first-person facts are saved automatically without per-fact acceptance, while ambiguous or conflicting items remain pending review.`,
                          )
                        : text(
                            `授权说明（每条记忆先经我确认）：开启下方开关即明确授权所选的确切模型（${selectedExtractionOption ? selectedExtractionOption.label : "请先在上方选择可用模型"}）接收符合条件的对话内容以异步提取候选记忆（仅限该所选模型，不会自动切换收费来源）。所有提取出的事实均保持待确认状态，只有你明确接受后才会在“在对话中使用已保存记忆”开启且允许时用于后续对话。`,
                            `Authorization (Review each memory first): Enabling the toggle below authorizes the exact selected model (${selectedExtractionOption ? selectedExtractionOption.label : "select an available model above first"}) to receive eligible conversations to propose candidate memories (only this selected model; never automatic paid fallback). All extracted facts remain pending review; only facts accepted by you become available for subsequent model use when 'Use saved memory in tasks' is on and allowed.`,
                          )}
                    </p>
                  )}
                  <label className="ra-setting">
                    <span>
                      {text("从日常对话中提取并整合长期记忆", "Extract and consolidate long-term memory from conversations")}
                      <small>
                        {text(
                          "默认关闭，加载页面绝不会在后台静默发起外部请求。开启操作本身即完成对上方所选模型接收方与激活模式的确认。",
                          "Off by default; loading never makes silent external requests. Enabling this toggle confirms authorization for the selected model destination and activation mode above.",
                        )}
                      </small>
                    </span>
                    <input
                      type="checkbox"
                      aria-label={text("从日常对话中提取并整合长期记忆", "Extract and consolidate long-term memory from conversations")}
                      checked={snapshot.settings.capture_enabled}
                      disabled={locked || (!snapshot.settings.capture_enabled && !selectedExtractionOption)}
                      onChange={event => toggleCaptureMemory(event.target.checked)}
                    />
                  </label>
                </div>
              </fieldset>

              <div className="ra-memory-filter">
                <label>
                  <span>{text("查看范围", "Scope view")}</span>
                  <select
                    aria-label={text("查看范围", "Scope view")}
                    value={scopeFilter}
                    disabled={busy}
                    onChange={event => setScopeFilter(event.target.value as "all" | AgentMemoryScopeKind)}
                  >
                    <option value="all">{text("全部范围（个人与项目）", "All scopes (personal & project)")}</option>
                    <option value="personal">{text("仅个人范围", "Personal scope only")}</option>
                    <option value="project">{text("仅本机项目范围", "Local project scope only")}</option>
                  </select>
                </label>
                <div className="ra-memory-search">
                  <input
                    type="search"
                    aria-label={text("搜索已保存事实", "Search saved facts")}
                    placeholder={text("搜索已保存事实…", "Search saved facts…")}
                    value={searchQuery}
                    disabled={busy}
                    onChange={event => {
                      setSearchQuery(event.target.value);
                      if (!event.target.value.trim()) setSearchResults(null);
                    }}
                  />
                  <button type="button" className="ra-link" disabled={busy || !searchQuery.trim()} onClick={() => void runSearch()}>
                    {text("搜索", "Search")}
                  </button>
                  {searchResults !== null && (
                    <button type="button" className="ra-link" disabled={busy} onClick={() => { setSearchQuery(""); setSearchResults(null); }}>
                      {text("清除搜索", "Clear search")}
                    </button>
                  )}
                </div>
              </div>

              <section aria-label={text("已保存事实", "Saved facts")}>
                <h3>{text("已保存事实（已生效）", "Saved facts (active)")} · {activeEntries.length}</h3>
                <p>
                  {text(
                    "个人范围与本机项目范围严格隔离：项目记忆仅在对应本机项目任务中可见，不会扩大到个人或其他项目。",
                    "Personal and local project scopes are strictly isolated: project facts apply only to that local project and never expand to personal scope.",
                  )}
                </p>
                {activeEntries.length === 0 ? (
                  <p>{text("暂无已保存的事实。", "No saved facts.")}</p>
                ) : (
                  <ul className="ra-memory-entries">
                    {activeEntries.map(entry => {
                      const exposureInfo = formatExposureSummary(entry.exposure_policy, destinations, text);
                      return (
                        <li key={entry.id} className="ra-memory-entry">
                          {editingID === entry.id ? (
                            <div className="ra-memory-edit-form">
                              <label>
                                <span>{text("事实内容", "Fact text")}</span>
                                <textarea
                                  aria-label={text("编辑事实内容", "Edit fact text")}
                                  value={editText}
                                  maxLength={1000}
                                  disabled={locked}
                                  onChange={event => setEditText(event.target.value)}
                                />
                              </label>
                              <label>
                                <span>{text("适用范围", "Scope")}</span>
                                <select
                                  aria-label={text("编辑适用范围", "Edit scope")}
                                  value={editScopeKind}
                                  disabled={locked}
                                  onChange={event => setEditScopeKind(event.target.value as AgentMemoryScopeKind)}
                                >
                                  <option value="personal">{text("个人范围", "Personal")}</option>
                                  <option value="project">{text("仅本机项目范围", "Local project only")}</option>
                                </select>
                              </label>
                              {editScopeKind === "project" && (
                                <label>
                                  <span>{text("本机项目 ID", "Local project ID")}</span>
                                  {knownProjects.length > 0 && (
                                    <select
                                      aria-label={text("选择已知项目", "Choose known project")}
                                      value={knownProjects.some(item => item.id === editProjectID) ? editProjectID : ""}
                                      disabled={locked}
                                      onChange={event => { if (event.target.value) setEditProjectID(event.target.value); }}
                                    >
                                      <option value="">{text("手动输入或选择项目", "Enter or select project")}</option>
                                      {knownProjects.map(project => (
                                        <option key={project.id} value={project.id}>{project.name} ({project.id})</option>
                                      ))}
                                    </select>
                                  )}
                                  <input
                                    aria-label={text("编辑本机项目 ID", "Edit local project ID")}
                                    value={editProjectID}
                                    disabled={locked}
                                    placeholder={text("输入本机项目 ID", "Enter local project ID")}
                                    onChange={event => setEditProjectID(event.target.value)}
                                  />
                                </label>
                              )}
                              <label>
                                <span>{text("模型接收方政策", "Model destination policy")}</span>
                                <select
                                  aria-label={text("编辑模型接收方政策", "Edit model destination policy")}
                                  value={editExposureMode}
                                  disabled={locked}
                                  onChange={event => setEditExposureMode(event.target.value as AgentMemoryExposureMode)}
                                >
                                  <option value="local_only">{exposureModeLabel("local_only", text)}</option>
                                  <option value="destinations">{exposureModeLabel("destinations", text)}</option>
                                  <option value="all_selected">{exposureModeLabel("all_selected", text)}</option>
                                </select>
                              </label>
                              {editExposureMode === "destinations" && (
                                <fieldset className="ra-tool-permissions">
                                  <legend>{text("允许接收此记忆的目标", "Allowed destinations for this fact")}</legend>
                                  {availableDestinations.length === 0 ? (
                                    <p>{text("暂无可用的已核对模型接收方，请重新选择政策或刷新。", "No available verified model destinations. Reselect policy or reload.")}</p>
                                  ) : (
                                    availableDestinations.map(option => {
                                      const key = destinationTupleKey(option.destination);
                                      return (
                                        <label key={key}>
                                          <input
                                            type="checkbox"
                                            checked={editDestinationKeys.includes(key)}
                                            disabled={locked}
                                            onChange={event => setEditDestinationKeys(current =>
                                              event.target.checked ? [...current, key] : current.filter(item => item !== key),
                                            )}
                                          />
                                          <span>{option.label}</span>
                                        </label>
                                      );
                                    })
                                  )}
                                </fieldset>
                              )}
                              <div className="ra-approval-actions">
                                <button
                                  type="button"
                                  className="ra-primary"
                                  disabled={locked || !editText.trim() || (editScopeKind === "project" && !editProjectID.trim()) || !editPolicyValid}
                                  onClick={() => saveEdit(entry)}
                                >
                                  {text("保存修改", "Save changes")}
                                </button>
                                <button type="button" className="ra-link" disabled={busy} onClick={() => setEditingID(null)}>
                                  {text("取消编辑", "Cancel edit")}
                                </button>
                              </div>
                            </div>
                          ) : (
                            <div>
                              <p className="ra-memory-text">{entry.text}</p>
                              <small className="ra-memory-meta">
                                {entry.scope.kind === "project"
                                  ? `${text("本机项目范围", "Local project")}: ${projectDisplayName(entry.scope.project_id)}`
                                  : text("个人范围", "Personal scope")}
                                {" · "}
                                {text("接收方政策", "Destination")}: {exposureInfo.summary}
                                {" · "}
                                {text("生成方式", "Origin")}: {formatOriginLabel(entry.origin, text)}
                                {" · "}
                                {text("来源", "Source")}: {formatSourcesSummary(entry.sources, text, entry.origin)}
                              </small>
                              {exposureInfo.hasUnavailable && (
                                <p role="status">
                                  {text(
                                    "该条目的部分模型接收方已不可用或已变更，不会自动扩大到其他模型；请编辑并重新选择可用接收方。",
                                    "One or more destinations for this fact are unavailable or changed; access will not broaden automatically. Edit to reselect an available destination.",
                                  )}
                                </p>
                              )}
                              <div className="ra-approval-actions">
                                <button type="button" className="ra-link" disabled={locked} onClick={() => startEditing(entry)}>
                                  {text("编辑", "Edit")}
                                </button>
                                <button type="button" className="ra-link" disabled={locked} onClick={() => deleteEntry(entry)}>
                                  {text("删除", "Delete")}
                                </button>
                              </div>
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ul>
                )}

                <fieldset className="ra-tool-permissions">
                  <legend>{text("添加事实", "Add fact")}</legend>
                  <p>
                    {text(
                      "可选快捷方式：日常对话无需手动添加或说“记住”，你也可以在此直接补充单条事实。",
                      "Optional shortcut: ordinary conversations do not require manual entry or a 'remember' command, but you can also add a specific fact directly here.",
                    )}
                  </p>
                  <label>
                    <span>{text("新事实内容", "New fact text")}</span>
                    <textarea
                      aria-label={text("新事实内容", "New fact text")}
                      value={newText}
                      maxLength={1000}
                      disabled={locked}
                      placeholder={text("例如：本项目的后端测试使用 pytest", "e.g. Backend tests in this project use pytest")}
                      onChange={event => setNewText(event.target.value)}
                    />
                  </label>
                  <label>
                    <span>{text("适用范围", "Scope")}</span>
                    <select
                      aria-label={text("适用范围", "Scope")}
                      value={newScopeKind}
                      disabled={locked}
                      onChange={event => setNewScopeKind(event.target.value as AgentMemoryScopeKind)}
                    >
                      <option value="personal">{text("个人范围", "Personal")}</option>
                      <option value="project">{text("仅本机项目范围", "Local project only")}</option>
                    </select>
                  </label>
                  {newScopeKind === "project" && (
                    <label>
                      <span>
                        {text("本机项目 ID", "Local project ID")}
                        <small>
                          {text(
                            "仅在该本机项目内生效，不会默许扩大为个人范围。请指定明确的项目 ID。",
                            "Applies only within this local project on this device and never silently expands to personal scope.",
                          )}
                        </small>
                      </span>
                      {knownProjects.length > 0 && (
                        <select
                          aria-label={text("选择本机项目", "Select local project")}
                          value={knownProjects.some(item => item.id === newProjectID) ? newProjectID : ""}
                          disabled={locked}
                          onChange={event => { if (event.target.value) setNewProjectID(event.target.value); }}
                        >
                          <option value="">{text("手动输入或选择项目", "Enter or select project")}</option>
                          {knownProjects.map(project => (
                            <option key={project.id} value={project.id}>{project.name} ({project.id})</option>
                          ))}
                        </select>
                      )}
                      <input
                        aria-label={text("本机项目 ID", "Local project ID")}
                        value={newProjectID}
                        disabled={locked}
                        placeholder={text("输入本机项目 ID（必填）", "Enter local project ID (required)")}
                        onChange={event => setNewProjectID(event.target.value)}
                      />
                    </label>
                  )}
                  <label>
                    <span>{text("模型接收方政策", "Model destination policy")}</span>
                    <select
                      aria-label={text("模型接收方政策", "Model destination policy")}
                      value={newExposureMode}
                      disabled={locked}
                      onChange={event => setNewExposureMode(event.target.value as AgentMemoryExposureMode)}
                    >
                      <option value="local_only">{exposureModeLabel("local_only", text)}</option>
                      <option value="destinations">{exposureModeLabel("destinations", text)}</option>
                      <option value="all_selected">{exposureModeLabel("all_selected", text)}</option>
                    </select>
                  </label>
                  {newExposureMode === "destinations" && (
                    <fieldset className="ra-tool-permissions">
                      <legend>{text("选择允许接收此记忆的目标", "Choose allowed destinations")}</legend>
                      {availableDestinations.length === 0 ? (
                        <p>{text("暂无可用的模型接收方。", "No available model destinations.")}</p>
                      ) : (
                        availableDestinations.map(option => {
                          const key = destinationTupleKey(option.destination);
                          return (
                            <label key={key}>
                              <input
                                type="checkbox"
                                checked={newDestinationKeys.includes(key)}
                                disabled={locked}
                                onChange={event => setNewDestinationKeys(current =>
                                  event.target.checked ? [...current, key] : current.filter(item => item !== key),
                                )}
                              />
                              <span>{option.label}</span>
                            </label>
                          );
                        })
                      )}
                    </fieldset>
                  )}
                  <div className="ra-approval-actions">
                    <button
                      type="button"
                      className="ra-primary"
                      disabled={locked || !newText.trim() || (newScopeKind === "project" && !newProjectID.trim()) || !newPolicyValid}
                      onClick={addEntry}
                    >
                      {text("添加事实", "Add fact")}
                    </button>
                  </div>
                </fieldset>
              </section>

              <section aria-label={text("候选记忆（待确认）", "Candidate memories (pending review)")}>
                <h3>{text("候选记忆（待确认，未启用）", "Candidate memories (pending review, inactive)")} · {pendingCandidates.length}</h3>
                <p>
                  {text(
                    "在“每条记忆先经我确认”模式下提取的事实，或自动记忆模式下存在歧义/冲突需你裁定的条目会暂存于此。待确认候选不会用于任何模型上下文，只有你明确接受后才会转为已保存事实。",
                    "Facts extracted under 'Review each memory first' or ambiguous/conflicting items under automatic memory wait here. Candidates are excluded from model context until you explicitly accept them.",
                  )}
                </p>
                {pendingCandidates.length === 0 ? (
                  <p>{text("暂无待确认的候选记忆。", "No pending memory candidates.")}</p>
                ) : (
                  <ul className="ra-memory-candidates">
                    {pendingCandidates.map(candidate => {
                      const exposureInfo = formatExposureSummary(candidate.exposure_policy, destinations, text);
                      return (
                        <li key={candidate.id} className="ra-memory-candidate">
                          <p className="ra-memory-text">{candidate.text}</p>
                          <small className="ra-memory-meta">
                            {candidate.scope.kind === "project"
                              ? `${text("本机项目范围", "Local project")}: ${projectDisplayName(candidate.scope.project_id)}`
                              : text("个人范围", "Personal scope")}
                            {" · "}
                            {text("接收方政策", "Destination")}: {exposureInfo.summary}
                            {" · "}
                            {text("生成方式", "Origin")}: {formatOriginLabel(candidate.origin, text)}
                            {" · "}
                            {text("来源", "Source")}: {formatSourcesSummary(candidate.sources, text, candidate.origin)}
                          </small>
                          <div className="ra-approval-actions">
                            <button
                              type="button"
                              className="ra-primary"
                              disabled={locked}
                              onClick={() => decideCandidate(candidate, "accept")}
                            >
                              {text("接受", "Accept")}
                            </button>
                            <button
                              type="button"
                              className="ra-link"
                              disabled={locked}
                              onClick={() => decideCandidate(candidate, "reject")}
                            >
                              {text("拒绝", "Reject")}
                            </button>
                          </div>
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>

              <section aria-label={text("清空长期记忆", "Reset long-term memory")}>
                {!confirmingReset ? (
                  <button
                    type="button"
                    className="ra-link"
                    disabled={locked}
                    onClick={() => setConfirmingReset(true)}
                  >
                    {text("清空长期记忆…", "Clear long-term memory…")}
                  </button>
                ) : (
                  <div className="ra-approval" role="group" aria-label={text("确认清空长期记忆", "Confirm clearing long-term memory")}>
                    <p>
                      {text(
                        "确定要清空本机保存的长期记忆与候选记录吗？删除长期记忆后，过往聊天记录和已经发送给远端模型的副本不会因此消失。",
                        "Clear all saved long-term memory facts and candidates on this device? Deleting long-term memory does not remove past chat history or copies already sent to remote models.",
                      )}
                    </p>
                    <div className="ra-approval-actions">
                      <button type="button" className="ra-link" disabled={busy} onClick={() => setConfirmingReset(false)}>
                        {text("取消", "Cancel")}
                      </button>
                      <button type="button" className="ra-primary" disabled={locked} onClick={confirmReset}>
                        {text("确认清空长期记忆", "Confirm clear memory")}
                      </button>
                    </div>
                  </div>
                )}
              </section>
            </>
          )}
        </section>
      )}
    </div>
  );
}
