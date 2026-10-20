import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import type { ComputerStatusExpectedPermissions } from "../domain/agentComputerStatus";
import {
  AgentComputerStatusPanel,
  type AgentComputerStatusPanelProps,
} from "./AgentComputerStatusPanel";

export interface AgentTaskComputerStatusTaskLike {
  id?: string | null;
  revision?: number | null;
  model_id?: string | null;
  model_destination?: {
    catalog_model_id?: string | null;
    provider_peer_id?: string | null;
    source_id?: string | null;
    destination_revision?: string | null;
  } | null;
  control_revision?: number | null;
  scope_revision?: number | null;
  goal?: {
    revision?: number | null;
  } | null;
  permission_mode?: string | null;
  local_execution?: {
    workspace_root?: string | null;
    desktop_access?: boolean | null;
  } | null;
  file_artifact_revision?: number | null;
}

export interface AgentTaskComputerStatusSettingsLike {
  enabled?: boolean | null;
  desktop_access?: boolean | null;
  permission_mode?: string | null;
}

export interface AgentTaskComputerStatusProps {
  task?: AgentTaskComputerStatusTaskLike | null;
  settings?: AgentTaskComputerStatusSettingsLike | null;
  fetchStatus?: AgentComputerStatusPanelProps["fetchStatus"];
}

export function buildTaskComputerStatusRefreshKey(
  task?: AgentTaskComputerStatusTaskLike | null,
  settings?: AgentTaskComputerStatusSettingsLike | null,
): string {
  const taskId = typeof task?.id === "string" ? task.id.trim() : "";
  if (!taskId) {
    return "";
  }

  return JSON.stringify([
    taskId,
    task?.model_id === undefined ? null : task.model_id,
    task?.model_destination?.catalog_model_id ?? null,
    task?.model_destination?.provider_peer_id ?? null,
    task?.model_destination?.source_id ?? null,
    task?.model_destination?.destination_revision ?? null,
    task?.control_revision ?? null,
    task?.scope_revision ?? null,
    task?.goal?.revision ?? null,
    task?.permission_mode ?? null,
    task?.local_execution?.workspace_root ?? null,
    task?.local_execution?.desktop_access ?? null,
    task?.file_artifact_revision ?? null,
    settings?.enabled ?? null,
    settings?.desktop_access ?? null,
    settings?.permission_mode ?? null,
  ]);
}

export function AgentTaskComputerStatus({
  task,
  settings,
  fetchStatus,
}: AgentTaskComputerStatusProps) {
  const { i18n } = useTranslation();
  const isZh = (i18n.resolvedLanguage || i18n.language || "en")
    .toLowerCase()
    .startsWith("zh");
  const tx = useCallback((zh: string, en: string) => (isZh ? zh : en), [isZh]);

  const taskId = typeof task?.id === "string" ? task.id.trim() : "";
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!taskId) {
      setOpen(false);
    }
  }, [taskId]);

  const expectedModelId: string | null | undefined =
    task?.model_id === undefined
      ? undefined
      : task.model_id === null
        ? null
        : typeof task.model_id === "string" && task.model_id.trim().length > 0
          ? task.model_id.trim()
          : null;

  const expectedPermissions = useMemo<ComputerStatusExpectedPermissions>(
    () => ({
      agentEnabled:
        typeof settings?.enabled === "boolean" ? settings.enabled : undefined,
      globalDesktopAccess:
        typeof settings?.desktop_access === "boolean"
          ? settings.desktop_access
          : undefined,
      taskDesktopAccess:
        typeof task?.local_execution?.desktop_access === "boolean"
          ? task.local_execution.desktop_access
          : undefined,
      globalPermissionMode:
        typeof settings?.permission_mode === "string" &&
        settings.permission_mode.trim().length > 0
          ? settings.permission_mode.trim()
          : undefined,
      taskPermissionMode:
        typeof task?.permission_mode === "string" &&
        task.permission_mode.trim().length > 0
          ? task.permission_mode.trim()
          : undefined,
    }),
    [
      settings?.enabled,
      settings?.desktop_access,
      settings?.permission_mode,
      task?.local_execution?.desktop_access,
      task?.permission_mode,
    ],
  );

  const refreshKey = useMemo(
    () =>
      buildTaskComputerStatusRefreshKey(
        {
          id: taskId,
          model_id: task?.model_id,
          model_destination: task?.model_destination,
          control_revision: task?.control_revision,
          scope_revision: task?.scope_revision,
          goal: task?.goal,
          permission_mode: task?.permission_mode,
          local_execution: task?.local_execution,
          file_artifact_revision: task?.file_artifact_revision,
        },
        {
          enabled: settings?.enabled,
          desktop_access: settings?.desktop_access,
          permission_mode: settings?.permission_mode,
        },
      ),
    [
      taskId,
      task?.model_id,
      task?.model_destination?.catalog_model_id,
      task?.model_destination?.provider_peer_id,
      task?.model_destination?.source_id,
      task?.model_destination?.destination_revision,
      task?.control_revision,
      task?.scope_revision,
      task?.goal?.revision,
      task?.permission_mode,
      task?.local_execution?.workspace_root,
      task?.local_execution?.desktop_access,
      task?.file_artifact_revision,
      settings?.enabled,
      settings?.desktop_access,
      settings?.permission_mode,
    ],
  );

  const normalizedTaskRevision =
    typeof task?.revision === "number" &&
    Number.isInteger(task.revision) &&
    task.revision >= 0
      ? task.revision
      : undefined;

  // Capture the caller's known task.revision lower bound at each relevant refreshKey/taskId boundary,
  // holding it steady across unrelated streaming/polling ticks so volatile revisions never cause GET storms.
  const capturedScopeRef = useRef<{
    taskId: string;
    refreshKey: string;
    minTaskRevision: number | undefined;
  }>({
    taskId: "",
    refreshKey: "",
    minTaskRevision: undefined,
  });

  if (
    capturedScopeRef.current.taskId !== taskId ||
    capturedScopeRef.current.refreshKey !== refreshKey
  ) {
    capturedScopeRef.current = {
      taskId,
      refreshKey,
      minTaskRevision: normalizedTaskRevision,
    };
  }

  const minTaskRevision = capturedScopeRef.current.minTaskRevision;

  if (!taskId) {
    return null;
  }

  return (
    <details
      className="ra-task-context ra-task-computer-status"
      data-testid="agent-task-computer-status"
      onToggle={(event) => {
        setOpen(event.currentTarget.open);
      }}
    >
      <summary>{tx("电脑控制状态", "Computer control status")}</summary>
      {open && (
        <AgentComputerStatusPanel
          key={taskId}
          taskId={taskId}
          refreshKey={refreshKey}
          expectedModelId={expectedModelId}
          minTaskRevision={minTaskRevision}
          expectedPermissions={expectedPermissions}
          fetchStatus={fetchStatus}
        />
      )}
    </details>
  );
}

export default AgentTaskComputerStatus;
