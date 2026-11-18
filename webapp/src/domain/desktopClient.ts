import { invoke } from "@tauri-apps/api/core";
export interface DesktopPreferences {
  background: boolean;
  startup: boolean;
  startup_supported: boolean;
  awake_supported?: boolean;
  silent_start: boolean;
  auto_recover: boolean;
  keep_awake: boolean;
  close_notice_seen: boolean;
}
export interface DesktopStatus {
  node_online: boolean;
  configured: boolean;
  sharing: boolean;
  active_tasks: number;
  agent_tasks?: number;
  agent_active_runs?: number;
  agent_waiting?: number;
  agent_task_titles?: string[];
  setup_active: boolean;
  recovery_attempts: number;
  recovery: string;
  awake_active: boolean;
  last_error: string | null;
}
export const getDesktopPreferences = () => invoke<DesktopPreferences>("get_desktop_preferences");
export const setDesktopPreferences = (preferences: DesktopPreferences) => invoke<DesktopPreferences>("set_desktop_preferences", { preferences });
export const getDesktopStatus = async (): Promise<DesktopStatus> => {
  const status = await invoke<DesktopStatus>("desktop_status");
  return {
    ...status,
    agent_active_runs: status.agent_active_runs ?? 0,
    agent_waiting: status.agent_waiting ?? 0,
    agent_task_titles: status.agent_task_titles ?? [],
  };
};
export type DesktopAction = "pause" | "resume" | "restart" | "logs" | "diagnostics" | "quit";
export const runDesktopAction = (action: DesktopAction) => invoke<string>("desktop_action", { action });
