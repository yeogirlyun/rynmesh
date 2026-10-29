import { useCallback, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  CheckCircle2,
  Clock,
  HelpCircle,
  LoaderCircle,
  MinusCircle,
  RefreshCw,
  XCircle,
} from "lucide-react";
import {
  AgentComputerStatusError,
  agentComputerStatus,
  describeComputerStatusReason,
  validateAndNormalizeComputerStatus,
  type AgentComputerStatusErrorCode,
  type ComputerStatusDtoV1,
  type ComputerStatusEvidenceSource,
  type ComputerStatusExpectedPermissions,
  type ComputerStatusImageCarrier,
  type ComputerStatusModelAdapter,
  type ComputerStatusObservationStage,
  type ComputerStatusPermissionMode,
} from "../domain/agentComputerStatus";
import "./AgentComputerStatusPanel.css";

export interface AgentComputerStatusPanelProps {
  taskId?: string;
  refreshKey?: string | number;
  expectedModelId?: string | null;
  minTaskRevision?: number | null;
  minSettingsRevision?: number | null;
  expectedPermissions?: ComputerStatusExpectedPermissions;
  fetchStatus?: (options?: {
    taskId?: string;
    signal?: AbortSignal;
  }) => Promise<ComputerStatusDtoV1>;
}

type StatusBadgeTone = "neutral" | "success" | "warning" | "danger" | "info";

function useStatusText() {
  const { i18n } = useTranslation();
  const isZh = (i18n.resolvedLanguage || i18n.language || "en").toLowerCase().startsWith("zh");
  return useCallback((zh: string, en: string) => (isZh ? zh : en), [isZh]);
}

function StatusBadge({
  tone,
  label,
  testId,
}: {
  tone: StatusBadgeTone;
  label: string;
  testId?: string;
}) {
  const toneClass =
    tone === "success"
      ? "acs-badge-success"
      : tone === "warning"
        ? "acs-badge-warning"
        : tone === "danger"
          ? "acs-badge-danger"
          : tone === "info"
            ? "acs-badge-info"
            : "";

  const Icon =
    tone === "success"
      ? CheckCircle2
      : tone === "warning"
        ? AlertTriangle
        : tone === "danger"
          ? XCircle
          : tone === "info"
            ? Clock
            : HelpCircle;

  return (
    <span className={`acs-badge ${toneClass}`.trim()} data-testid={testId}>
      <Icon size={13} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}

function formatAdapterLabel(
  adapter: ComputerStatusModelAdapter | null,
  tx: (zh: string, en: string) => string,
): string {
  switch (adapter) {
    case "codex_cli":
      return "Codex CLI";
    case "antigravity_cli":
      return "Antigravity CLI";
    case "openai_chat":
      return "OpenAI Chat";
    case "openai_responses":
      return "OpenAI Responses";
    case "anthropic_messages":
      return "Anthropic Messages";
    case "other":
      return tx("其他通道", "Other adapter");
    case "unknown":
    case null:
    default:
      return tx("适配器未知", "Unknown adapter");
  }
}

function formatCarrierLabel(
  carrier: ComputerStatusImageCarrier,
  tx: (zh: string, en: string) => string,
): string {
  switch (carrier) {
    case "codex_turn_input":
      return tx("Codex 回合输入", "Codex turn input");
    case "api_image_blocks":
      return tx("API 图像块", "API image blocks");
    case "none":
      return tx("无图像通道", "None");
    case "unknown":
    default:
      return tx("未知", "Unknown");
  }
}

function formatPermissionModeLabel(
  mode: ComputerStatusPermissionMode | null,
  tx: (zh: string, en: string) => string,
): string {
  switch (mode) {
    case "workspace":
      return tx("工作区读写 (workspace)", "Workspace");
    case "read_only":
      return tx("只读 (read_only)", "Read-only");
    case "confirm_changes":
      return tx("变更需确认 (confirm_changes)", "Confirm changes");
    case "automatic":
      return tx("自动执行 (automatic)", "Automatic");
    case "unknown":
    case null:
    default:
      return tx("未知", "Unknown");
  }
}

function formatEvidenceSourceLabel(
  source: ComputerStatusEvidenceSource,
  tx: (zh: string, en: string) => string,
): string {
  switch (source) {
    case "extension_store":
      return tx("扩展配置库", "Extension store");
    case "agent_store":
      return tx("Agent 配置库", "Agent store");
    case "native_probe_cache":
      return tx("原生握手缓存", "Native verification cache");
    case "live_session":
      return tx("实时会话", "Live session");
    case "model_route_contract":
      return tx("模型通道契约", "Model route contract");
    case "model_image_probe":
      return tx("模型像素验证", "Model pixel verification");
    case "runtime_receipt":
      return tx("运行时回执", "Runtime receipt");
    case "client_render_receipt":
      return tx("客户端渲染回执", "Client render receipt");
    case "none":
    default:
      return tx("无来源记录", "No recorded source");
  }
}

function describeFetchError(
  code: AgentComputerStatusErrorCode,
  tx: (zh: string, en: string) => string,
): string {
  switch (code) {
    case "endpoint_not_found":
    case "agent_computer_status_unavailable":
    case "network_error":
      return tx(
        "电脑控制状态接口尚未提供，暂无法确认",
        "Computer status is unavailable; readiness cannot be confirmed",
      );
    case "agent_task_not_found":
    case "agent_task_deleted":
      return tx(
        "未找到所请求的任务状态，暂无法确认",
        "Requested task status was not found; readiness cannot be confirmed",
      );
    case "agent_invalid_task_id":
    case "agent_invalid_request":
    case "invalid_response":
      return tx(
        "状态响应无效或上下文不匹配，暂无法确认",
        "Status response is invalid or context mismatched; readiness cannot be confirmed",
      );
    case "unauthorized":
      return tx(
        "当前会话无权读取电脑控制状态，暂无法确认",
        "Unauthorized to read computer status; readiness cannot be confirmed",
      );
    case "status_unavailable":
    default:
      return tx(
        "状态暂不可用，暂无法确认",
        "Status unavailable; readiness cannot be confirmed",
      );
  }
}

interface ScopedStatusState {
  taskKey: string | null;
  contextKey: string;
  requestKey: string;
  snapshot: ComputerStatusDtoV1 | null;
  lastSuccessAt: string | null;
  isStale: boolean;
  errorCode: AgentComputerStatusErrorCode | null;
}

export function AgentComputerStatusPanel({
  taskId,
  refreshKey,
  expectedModelId,
  minTaskRevision,
  minSettingsRevision,
  expectedPermissions,
  fetchStatus = agentComputerStatus.getStatus,
}: AgentComputerStatusPanelProps) {
  const tx = useStatusText();
  const headingId = useId();

  // Normalize requested task context: undefined means settings context (null).
  const requestedTaskId: string | null = taskId === undefined ? null : taskId;
  const normalizedExpectedModelId: string | null | undefined =
    expectedModelId === undefined
      ? undefined
      : expectedModelId === null
        ? null
        : typeof expectedModelId === "string" && expectedModelId.trim().length > 0
          ? expectedModelId.trim()
          : null;

  const normalizedMinTaskRevision: number | undefined =
    typeof minTaskRevision === "number" &&
    Number.isInteger(minTaskRevision) &&
    minTaskRevision >= 0
      ? minTaskRevision
      : undefined;
  const normalizedMinSettingsRevision: number | undefined =
    typeof minSettingsRevision === "number" &&
    Number.isInteger(minSettingsRevision) &&
    minSettingsRevision >= 0
      ? minSettingsRevision
      : undefined;

  const expectedAgentEnabled = expectedPermissions?.agentEnabled;
  const expectedGlobalDesktopAccess = expectedPermissions?.globalDesktopAccess;
  const expectedTaskDesktopAccess = expectedPermissions?.taskDesktopAccess;
  const expectedTaskLocalExecution = expectedPermissions?.taskLocalExecution;
  const expectedGlobalPermissionMode = expectedPermissions?.globalPermissionMode;
  const expectedTaskPermissionMode = expectedPermissions?.taskPermissionMode;

  const contextKey = JSON.stringify([
    requestedTaskId,
    refreshKey ?? null,
    normalizedExpectedModelId === undefined ? "__undef__" : normalizedExpectedModelId,
    normalizedMinTaskRevision ?? null,
    normalizedMinSettingsRevision ?? null,
    expectedAgentEnabled ?? null,
    expectedGlobalDesktopAccess ?? null,
    expectedTaskDesktopAccess ?? null,
    expectedTaskLocalExecution ?? null,
    expectedGlobalPermissionMode ?? null,
    expectedTaskPermissionMode ?? null,
  ]);

  const [manualRefreshCount, setManualRefreshCount] = useState<number>(0);
  const requestKey = `${contextKey}#${manualRefreshCount}`;

  const [scopedState, setScopedState] = useState<ScopedStatusState>({
    taskKey: requestedTaskId,
    contextKey,
    requestKey,
    snapshot: null,
    lastSuccessAt: null,
    isStale: false,
    errorCode: null,
  });
  const [loading, setLoading] = useState<boolean>(true);

  const requestGenRef = useRef<number>(0);
  const abortControllerRef = useRef<AbortController | null>(null);
  const currentTaskKeyRef = useRef<string | null>(requestedTaskId);
  currentTaskKeyRef.current = requestedTaskId;
  const currentRequestKeyRef = useRef<string>(requestKey);
  currentRequestKeyRef.current = requestKey;

  // Immediately scope visible state to the current task + selection/refresh generation at render time.
  const isSameTaskScope = scopedState.taskKey === requestedTaskId;
  const isCurrentRequestScope =
    isSameTaskScope &&
    scopedState.contextKey === contextKey &&
    scopedState.requestKey === requestKey;

  const snapshot = isCurrentRequestScope ? scopedState.snapshot : null;
  const lastSuccessAt = isSameTaskScope ? scopedState.lastSuccessAt : null;
  const isStale = isSameTaskScope
    ? isCurrentRequestScope
      ? scopedState.isStale
      : scopedState.lastSuccessAt !== null
    : false;
  const errorCode = isCurrentRequestScope ? scopedState.errorCode : null;
  const effectiveLoading = loading || !isCurrentRequestScope;

  useEffect(() => {
    let mounted = true;
    const capturedTaskId = requestedTaskId;
    const capturedContextKey = contextKey;
    const capturedRequestKey = requestKey;
    const capturedExpectedModelId = normalizedExpectedModelId;
    const capturedMinTaskRevision = normalizedMinTaskRevision;
    const capturedMinSettingsRevision = normalizedMinSettingsRevision;
    const capturedExpectedPermissions: ComputerStatusExpectedPermissions | undefined =
      expectedAgentEnabled !== undefined ||
      expectedGlobalDesktopAccess !== undefined ||
      expectedTaskDesktopAccess !== undefined ||
      expectedTaskLocalExecution !== undefined ||
      expectedGlobalPermissionMode !== undefined ||
      expectedTaskPermissionMode !== undefined
        ? {
            agentEnabled: expectedAgentEnabled,
            globalDesktopAccess: expectedGlobalDesktopAccess,
            taskDesktopAccess: expectedTaskDesktopAccess,
            taskLocalExecution: expectedTaskLocalExecution,
            globalPermissionMode: expectedGlobalPermissionMode,
            taskPermissionMode: expectedTaskPermissionMode,
          }
        : undefined;
    const reqId = ++requestGenRef.current;

    if (abortControllerRef.current) {
      abortControllerRef.current.abort();
    }
    const controller = new AbortController();
    abortControllerRef.current = controller;

    setLoading(true);
    // Clear current snapshot immediately when task or refresh/selection scope changes.
    setScopedState(prev => {
      if (prev.taskKey !== capturedTaskId) {
        return {
          taskKey: capturedTaskId,
          contextKey: capturedContextKey,
          requestKey: capturedRequestKey,
          snapshot: null,
          lastSuccessAt: null,
          isStale: false,
          errorCode: null,
        };
      }
      if (prev.requestKey !== capturedRequestKey) {
        return {
          taskKey: capturedTaskId,
          contextKey: capturedContextKey,
          requestKey: capturedRequestKey,
          snapshot: null,
          lastSuccessAt: prev.lastSuccessAt,
          isStale: prev.lastSuccessAt !== null,
          errorCode: null,
        };
      }
      return prev;
    });

    const runFetch = async () => {
      try {
        const rawOrNormalized = await fetchStatus({
          ...(capturedTaskId !== null ? { taskId: capturedTaskId } : {}),
          signal: controller.signal,
        });

        // Re-validate to guarantee task_id, model_id, revision bounds, permission state, and schema v1 invariants
        const validated = validateAndNormalizeComputerStatus(rawOrNormalized, {
          expectedTaskId: capturedTaskId,
          expectedModelId: capturedExpectedModelId,
          minTaskRevision: capturedMinTaskRevision,
          minSettingsRevision: capturedMinSettingsRevision,
          expectedPermissions: capturedExpectedPermissions,
        });

        if (
          !mounted ||
          reqId !== requestGenRef.current ||
          currentTaskKeyRef.current !== capturedTaskId ||
          currentRequestKeyRef.current !== capturedRequestKey
        ) {
          return;
        }

        setScopedState({
          taskKey: capturedTaskId,
          contextKey: capturedContextKey,
          requestKey: capturedRequestKey,
          snapshot: validated,
          lastSuccessAt: validated.generated_at,
          isStale: false,
          errorCode: null,
        });
      } catch (error) {
        if (
          !mounted ||
          reqId !== requestGenRef.current ||
          currentTaskKeyRef.current !== capturedTaskId ||
          currentRequestKeyRef.current !== capturedRequestKey
        ) {
          return;
        }
        if (
          (error instanceof DOMException && error.name === "AbortError") ||
          (error instanceof Error && error.name === "AbortError")
        ) {
          return;
        }

        const mappedCode: AgentComputerStatusErrorCode =
          error instanceof AgentComputerStatusError ? error.code : "status_unavailable";

        // Failed refresh clears current ready state; retains prior lastSuccessAt only as explicitly stale.
        setScopedState(prev => {
          const priorSuccess = prev.taskKey === capturedTaskId ? prev.lastSuccessAt : null;
          return {
            taskKey: capturedTaskId,
            contextKey: capturedContextKey,
            requestKey: capturedRequestKey,
            snapshot: null,
            lastSuccessAt: priorSuccess,
            isStale: priorSuccess !== null,
            errorCode: mappedCode,
          };
        });
      } finally {
        if (
          mounted &&
          reqId === requestGenRef.current &&
          currentTaskKeyRef.current === capturedTaskId &&
          currentRequestKeyRef.current === capturedRequestKey
        ) {
          setLoading(false);
        }
      }
    };

    void runFetch();

    return () => {
      mounted = false;
      controller.abort();
    };
  }, [
    requestedTaskId,
    contextKey,
    requestKey,
    normalizedExpectedModelId,
    normalizedMinTaskRevision,
    normalizedMinSettingsRevision,
    expectedAgentEnabled,
    expectedGlobalDesktopAccess,
    expectedTaskDesktopAccess,
    expectedTaskLocalExecution,
    expectedGlobalPermissionMode,
    expectedTaskPermissionMode,
    fetchStatus,
  ]);

  const handleManualRefresh = () => {
    setManualRefreshCount(count => count + 1);
  };

  // 1. Plugin row computation
  const pluginBadge = (): { tone: StatusBadgeTone; label: string } => {
    if (!snapshot || snapshot.plugin.enabled === null) {
      return { tone: "neutral", label: tx("未知", "Unknown") };
    }
    if (snapshot.plugin.enabled === true) {
      return { tone: "success", label: tx("已启用", "Enabled") };
    }
    return { tone: "warning", label: tx("已停用", "Disabled") };
  };

  // 2. Native engine row computation
  const nativeBadge = (): { tone: StatusBadgeTone; label: string } => {
    if (!snapshot) {
      return { tone: "neutral", label: tx("未知", "Unknown") };
    }
    switch (snapshot.native.status) {
      case "ready":
        return { tone: "success", label: tx("已就绪", "Ready") };
      case "unverified":
        return { tone: "warning", label: tx("未验证", "Unverified") };
      case "missing":
        return { tone: "danger", label: tx("缺失", "Missing") };
      case "incompatible":
        return { tone: "danger", label: tx("协议不兼容", "Incompatible") };
      case "error":
        return { tone: "danger", label: tx("错误", "Error") };
      case "unknown":
      default:
        return { tone: "neutral", label: tx("未知", "Unknown") };
    }
  };

  // 3. Permissions badges computation
  const triStatePermBadge = (
    value: boolean | null | undefined,
    prefixZh: string,
    prefixEn: string,
  ): { tone: StatusBadgeTone; label: string } => {
    if (!snapshot || value === null || value === undefined) {
      return {
        tone: "neutral",
        label: tx(`${prefixZh}：未知`, `${prefixEn}: Unknown`),
      };
    }
    if (value === true) {
      return {
        tone: "success",
        label: tx(`${prefixZh}：已开启`, `${prefixEn}: Granted`),
      };
    }
    return {
      tone: "warning",
      label: tx(`${prefixZh}：未开启`, `${prefixEn}: Denied`),
    };
  };

  // 4. Task session row computation
  const sessionBadge = (): { tone: StatusBadgeTone; label: string } => {
    if (requestedTaskId === null) {
      return { tone: "neutral", label: tx("未选择任务", "No task selected") };
    }
    if (!snapshot) {
      return { tone: "neutral", label: tx("未知", "Unknown") };
    }
    switch (snapshot.session.state) {
      case "connected":
        return { tone: "info", label: tx("已连接", "Connected") };
      case "starting":
        return { tone: "info", label: tx("正在启动", "Starting") };
      case "waiting_approval":
        return { tone: "warning", label: tx("等待确认", "Waiting approval") };
      case "busy":
        return { tone: "info", label: tx("运行中", "Busy") };
      case "none":
        return { tone: "neutral", label: tx("未启动", "Not started") };
      case "cancelled":
        return { tone: "neutral", label: tx("已取消", "Cancelled") };
      case "lost":
        return { tone: "danger", label: tx("连接已断开", "Connection lost") };
      case "not_applicable":
        return { tone: "neutral", label: tx("未选择任务", "No task selected") };
      case "unknown":
      default:
        return { tone: "neutral", label: tx("未知", "Unknown") };
    }
  };

  // 5. Model image capability badge computation
  const modelImageBadge = (): { tone: StatusBadgeTone; label: string } => {
    if (!snapshot) {
      return { tone: "neutral", label: tx("未知", "Unknown") };
    }
    switch (snapshot.model.image_input.status) {
      case "unsupported":
        return { tone: "warning", label: tx("不支持图像输入", "Image input unsupported") };
      case "carrier_available":
        return {
          tone: "warning",
          label: tx("可传图，待验证", "Image carrier available; verification pending"),
        };
      case "verified":
        return { tone: "success", label: tx("图像输入已验证", "Image input verified") };
      case "unknown":
      default:
        return { tone: "neutral", label: tx("未知", "Unknown") };
    }
  };

  // 6. Observation stage badge helper
  const renderStageCard = (
    stageKey: "saved" | "displayed" | "submitted" | "semantic_observed",
    titleZh: string,
    titleEn: string,
    stage: ComputerStatusObservationStage,
    matchesCurrentSelection: boolean,
  ) => {
    let tone: StatusBadgeTone = "neutral";
    let stateLabel = tx("未知", "Unknown");

    if (stage.state === "recorded") {
      if (stageKey === "semantic_observed") {
        tone = matchesCurrentSelection ? "success" : "warning";
        stateLabel = tx("模型已返回观察", "Model observation returned");
      } else if (stageKey === "submitted") {
        tone = "info";
        stateLabel = tx("已提交（待观察确认）", "Submitted (not proof of observation)");
      } else {
        tone = "success";
        stateLabel = tx("已记录", "Recorded");
      }
    } else if (stage.state === "not_recorded") {
      tone = "neutral";
      stateLabel = tx("未记录", "Not recorded");
    } else if (stage.state === "failed") {
      tone = "danger";
      stateLabel = tx("失败", "Failed");
    } else {
      tone = "neutral";
      stateLabel = tx("未知", "Unknown");
    }

    const StageIcon =
      tone === "success"
        ? CheckCircle2
        : tone === "warning"
          ? AlertTriangle
          : tone === "danger"
            ? XCircle
            : tone === "info"
              ? Clock
              : stage.state === "not_recorded"
                ? MinusCircle
                : HelpCircle;

    return (
      <div
        key={stageKey}
        className="acs-stage-card"
        data-testid={`computer-status-stage-${stageKey}`}
      >
        <span className="acs-stage-title">{tx(titleZh, titleEn)}</span>
        <div className="acs-badge-group">
          <span
            className={`acs-badge ${
              tone === "success"
                ? "acs-badge-success"
                : tone === "warning"
                  ? "acs-badge-warning"
                  : tone === "danger"
                    ? "acs-badge-danger"
                    : tone === "info"
                      ? "acs-badge-info"
                      : ""
            }`.trim()}
          >
            <StageIcon size={12} aria-hidden="true" />
            <span>{stateLabel}</span>
          </span>
        </div>
        {stage.evidence.observed_at ? (
          <span className="acs-stage-time">{stage.evidence.observed_at}</span>
        ) : null}
      </div>
    );
  };

  const pBadge = pluginBadge();
  const nBadge = nativeBadge();
  const sBadge = sessionBadge();
  const mBadge = modelImageBadge();

  const pluginReason = describeComputerStatusReason(snapshot?.plugin.reason_code, tx);
  const nativeReason = describeComputerStatusReason(snapshot?.native.reason_code, tx);
  const permissionsReason = describeComputerStatusReason(snapshot?.permissions.reason_code, tx);
  const sessionReason = describeComputerStatusReason(snapshot?.session.reason_code, tx);
  const modelReason = describeComputerStatusReason(snapshot?.model.image_input.reason_code, tx);

  const modelNameLabel = snapshot
    ? snapshot.model.display_name ?? snapshot.model.id ?? tx("未选择模型", "No model selected")
    : tx("未选择模型", "No model selected");

  return (
    <section
      className="acs-panel"
      aria-labelledby={headingId}
      data-testid="agent-computer-status-panel"
    >
      <div className="acs-header">
        <div className="acs-title-group">
          <h3 id={headingId}>{tx("电脑控制状态与证据", "Computer Use status & evidence")}</h3>
          <p className="acs-subtitle">
            {tx(
              "以下各分项独立反映只读快照与缓存验证回执，不合并为单一就绪状态。",
              "Each item independently reports read-only snapshots and cached verification receipts without combining into a single ready badge.",
            )}
          </p>
        </div>

        <div className="acs-header-actions">
          <span className="acs-live-status" aria-live="polite">
            {effectiveLoading ? (
              <>
                <LoaderCircle size={13} className="acs-spin" aria-hidden="true" />
                <span>{tx("正在刷新状态…", "Refreshing status…")}</span>
              </>
            ) : lastSuccessAt ? (
              <span className={`acs-sync-meta ${isStale ? "acs-sync-meta-stale" : ""}`.trim()}>
                <Clock size={12} aria-hidden="true" />
                <span>
                  {isStale
                    ? tx(
                        `上次成功响应（已过期）：${lastSuccessAt}`,
                        `Last successful response (stale): ${lastSuccessAt}`,
                      )
                    : tx(
                        `上次成功响应：${lastSuccessAt}`,
                        `Last successful response: ${lastSuccessAt}`,
                      )}
                </span>
              </span>
            ) : null}
          </span>

          <button
            type="button"
            className="ae-btn ae-btn-sm"
            aria-busy={effectiveLoading}
            onClick={handleManualRefresh}
          >
            <RefreshCw size={13} className={effectiveLoading ? "acs-spin" : ""} aria-hidden="true" />
            <span>{tx("刷新状态", "Refresh status")}</span>
          </button>
        </div>
      </div>

      {errorCode ? (
        <div
          className="acs-notice acs-notice-warning"
          role="status"
          data-testid="computer-status-unavailable-notice"
        >
          <AlertTriangle size={15} aria-hidden="true" />
          <div>
            <div>{describeFetchError(errorCode, tx)}</div>
            {isStale && lastSuccessAt ? (
              <div>
                {tx(
                  "已清除当前就绪状态；先前的检查结果已标记为过期。",
                  "Current ready state has been cleared; previous status is marked stale.",
                )}
              </div>
            ) : null}
          </div>
        </div>
      ) : null}

      <dl className="acs-dl">
        {/* 1. Plugin */}
        <div className="acs-row" data-testid="computer-status-row-plugin">
          <dt className="acs-dt">{tx("插件", "Plugin")}</dt>
          <dd className="acs-dd">
            <div className="acs-badge-group">
              <StatusBadge tone={pBadge.tone} label={pBadge.label} />
              {snapshot ? (
                <span className="acs-badge">
                  {snapshot.plugin.installed === true
                    ? tx("已安装", "Installed")
                    : snapshot.plugin.installed === false
                      ? tx("未安装", "Not installed")
                      : tx("安装状态未知", "Install state unknown")}
                </span>
              ) : null}
            </div>
            {snapshot ? (
              <div className="acs-meta-list">
                {snapshot.plugin.revision !== null ? (
                  <span>{tx(`修订 #${snapshot.plugin.revision}`, `Rev #${snapshot.plugin.revision}`)}</span>
                ) : null}
                <span>
                  {tx(
                    `来源：${formatEvidenceSourceLabel(snapshot.plugin.evidence.source, tx)}`,
                    `Source: ${formatEvidenceSourceLabel(snapshot.plugin.evidence.source, tx)}`,
                  )}
                </span>
                {snapshot.plugin.evidence.observed_at ? (
                  <span>{snapshot.plugin.evidence.observed_at}</span>
                ) : null}
              </div>
            ) : null}
            {pluginReason ? <div className="acs-reason">{pluginReason}</div> : null}
          </dd>
        </div>

        {/* 2. Native engine */}
        <div className="acs-row" data-testid="computer-status-row-native">
          <dt className="acs-dt">{tx("原生引擎", "Native engine")}</dt>
          <dd className="acs-dd">
            <div className="acs-badge-group">
              <StatusBadge tone={nBadge.tone} label={nBadge.label} />
              {snapshot ? (
                <span className="acs-badge">
                  {snapshot.native.executable_presence === "present"
                    ? tx("可执行文件：已存在", "Executable: Present")
                    : snapshot.native.executable_presence === "missing"
                      ? tx("可执行文件：缺失", "Executable: Missing")
                      : tx("可执行文件：未知", "Executable: Unknown")}
                </span>
              ) : null}
            </div>
            <div className="acs-meta-list">
              <span>
                {snapshot
                  ? snapshot.native.platform === "windows"
                    ? tx("平台：Windows", "Platform: Windows")
                    : snapshot.native.platform === "unsupported"
                      ? tx("平台：不支持", "Platform: Unsupported")
                      : tx("平台：未知", "Platform: Unknown")
                  : tx("平台：未知", "Platform: Unknown")}
              </span>
              <span>
                {tx("版本：", "Version: ")}
                <code>{snapshot?.native.version ?? tx("未知", "Unknown")}</code>
              </span>
              <span>
                {tx("协议：", "Protocol: ")}
                <code>{snapshot?.native.protocol_version ?? tx("未知", "Unknown")}</code>
              </span>
              <span>
                {snapshot?.native.evidence.observed_at
                  ? tx(
                      `验证时间：${snapshot.native.evidence.observed_at}`,
                      `Verified at: ${snapshot.native.evidence.observed_at}`,
                    )
                  : tx("验证时间：无记录", "Verified at: Not recorded")}
              </span>
            </div>
            {nativeReason ? <div className="acs-reason">{nativeReason}</div> : null}
          </dd>
        </div>

        {/* 3. Permissions */}
        <div className="acs-row" data-testid="computer-status-row-permissions">
          <dt className="acs-dt">{tx("权限", "Permissions")}</dt>
          <dd className="acs-dd">
            <div className="acs-badge-group">
              <StatusBadge
                {...triStatePermBadge(snapshot?.permissions.agent_enabled, "Agent", "Agent")}
              />
              <StatusBadge
                {...triStatePermBadge(
                  snapshot?.permissions.global_desktop_access,
                  "全局桌面权限",
                  "Global desktop",
                )}
              />
              {requestedTaskId !== null ? (
                <>
                  <StatusBadge
                    {...triStatePermBadge(
                      snapshot?.permissions.task_local_execution,
                      "任务本地执行",
                      "Task local execution",
                    )}
                  />
                  <StatusBadge
                    {...triStatePermBadge(
                      snapshot?.permissions.task_desktop_access,
                      "任务桌面权限",
                      "Task desktop",
                    )}
                  />
                </>
              ) : null}
            </div>
            {snapshot ? (
              <div className="acs-meta-list">
                {snapshot.permissions.global_permission_mode ? (
                  <span>
                    {tx(
                      `全局模式：${formatPermissionModeLabel(snapshot.permissions.global_permission_mode, tx)}`,
                      `Global mode: ${formatPermissionModeLabel(snapshot.permissions.global_permission_mode, tx)}`,
                    )}
                  </span>
                ) : null}
                {requestedTaskId !== null && snapshot.permissions.task_permission_mode ? (
                  <span>
                    {tx(
                      `任务模式：${formatPermissionModeLabel(snapshot.permissions.task_permission_mode, tx)}`,
                      `Task mode: ${formatPermissionModeLabel(snapshot.permissions.task_permission_mode, tx)}`,
                    )}
                  </span>
                ) : null}
                {requestedTaskId !== null && snapshot.permissions.task_scope === "not_requested" ? (
                  <span>{tx("任务范围：未请求", "Task scope: Not requested")}</span>
                ) : null}
              </div>
            ) : null}
            {permissionsReason ? <div className="acs-reason">{permissionsReason}</div> : null}
          </dd>
        </div>

        {/* 4. Task session */}
        <div className="acs-row" data-testid="computer-status-row-session">
          <dt className="acs-dt">{tx("任务会话", "Task session")}</dt>
          <dd className="acs-dd">
            <div className="acs-badge-group">
              <StatusBadge tone={sBadge.tone} label={sBadge.label} />
              {requestedTaskId === null && !snapshot ? (
                <StatusBadge tone="neutral" label={tx("未知", "Unknown")} />
              ) : null}
            </div>
            {requestedTaskId !== null && snapshot ? (
              <div className="acs-meta-list">
                {snapshot.session.generation !== null ? (
                  <span>
                    {tx(
                      `会话代次 #${snapshot.session.generation}`,
                      `Generation #${snapshot.session.generation}`,
                    )}
                  </span>
                ) : null}
                {snapshot.session.state === "connected" ? (
                  <span>
                    {tx(
                      "会话连接不代表原生引擎或模型图像能力已就绪",
                      "Session connection does not imply native engine or model vision readiness",
                    )}
                  </span>
                ) : null}
                {snapshot.session.evidence.observed_at ? (
                  <span>{snapshot.session.evidence.observed_at}</span>
                ) : null}
              </div>
            ) : null}
            {sessionReason ? <div className="acs-reason">{sessionReason}</div> : null}
          </dd>
        </div>

        {/* 5. Selected model & image capability */}
        <div className="acs-row" data-testid="computer-status-row-model">
          <dt className="acs-dt">{tx("当前模型", "Selected model")}</dt>
          <dd className="acs-dd">
            <div className="acs-badge-group">
              <span className="acs-badge">
                <code>{modelNameLabel}</code>
              </span>
              <StatusBadge tone={mBadge.tone} label={mBadge.label} />
            </div>
            {snapshot ? (
              <div className="acs-meta-list">
                <span>
                  {tx(
                    `适配器：${formatAdapterLabel(snapshot.model.adapter, tx)}`,
                    `Adapter: ${formatAdapterLabel(snapshot.model.adapter, tx)}`,
                  )}
                </span>
                <span>
                  {snapshot.model.availability === "available"
                    ? tx("可用性：可用", "Availability: Available")
                    : snapshot.model.availability === "unavailable"
                      ? tx("可用性：不可用", "Availability: Unavailable")
                      : tx("可用性：未知", "Availability: Unknown")}
                </span>
                <span>
                  {tx(
                    `图像通道：${formatCarrierLabel(snapshot.model.image_input.carrier, tx)}`,
                    `Carrier: ${formatCarrierLabel(snapshot.model.image_input.carrier, tx)}`,
                  )}
                </span>
                {snapshot.model.image_input.evidence.observed_at ? (
                  <span>{snapshot.model.image_input.evidence.observed_at}</span>
                ) : null}
              </div>
            ) : null}
            {modelReason ? <div className="acs-reason">{modelReason}</div> : null}
          </dd>
        </div>

        {/* 6. Latest image evidence */}
        <div className="acs-row" data-testid="computer-status-row-observation">
          <dt className="acs-dt">{tx("最近图像证据", "Latest image evidence")}</dt>
          <dd className="acs-dd">
            {!snapshot || !snapshot.latest_observation ? (
              <div className="acs-badge-group">
                {!snapshot ? <StatusBadge tone="neutral" label={tx("未知", "Unknown")} /> : null}
                <span className="acs-badge">
                  <MinusCircle size={13} aria-hidden="true" />
                  <span>{tx("尚无观察记录", "No observation recorded")}</span>
                </span>
              </div>
            ) : (
              <>
                <div className="acs-badge-group">
                  <span className="acs-badge">
                    <code>{snapshot.latest_observation.id}</code>
                  </span>
                  {snapshot.latest_observation.width && snapshot.latest_observation.height ? (
                    <span className="acs-badge">
                      {`${snapshot.latest_observation.width}×${snapshot.latest_observation.height}`}
                    </span>
                  ) : null}
                  {!snapshot.latest_observation.matches_current_selection ? (
                    <StatusBadge
                      tone="warning"
                      label={tx("来自先前模型", "Earlier model")}
                      testId="computer-status-observation-earlier-model"
                    />
                  ) : null}
                </div>

                <div className="acs-stage-grid">
                  {renderStageCard(
                    "saved",
                    "1. 图像已保存",
                    "1. Image saved",
                    snapshot.latest_observation.stages.saved,
                    snapshot.latest_observation.matches_current_selection,
                  )}
                  {renderStageCard(
                    "displayed",
                    "2. 客户端已显示",
                    "2. Client displayed",
                    snapshot.latest_observation.stages.displayed,
                    snapshot.latest_observation.matches_current_selection,
                  )}
                  {renderStageCard(
                    "submitted",
                    "3. 已提交至通道",
                    "3. Submitted to carrier",
                    snapshot.latest_observation.stages.submitted,
                    snapshot.latest_observation.matches_current_selection,
                  )}
                  {renderStageCard(
                    "semantic_observed",
                    "4. 语义观察回执",
                    "4. Semantic observation",
                    snapshot.latest_observation.stages.semantic_observed,
                    snapshot.latest_observation.matches_current_selection,
                  )}
                </div>
              </>
            )}
          </dd>
        </div>
      </dl>
    </section>
  );
}

export default AgentComputerStatusPanel;
