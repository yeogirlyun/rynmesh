import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentAutomation } from "../domain/rynAgent";
import { AutomationsPanel } from "./RynAgentAutomations";

afterEach(cleanup);

function automation(filesError?: string): AgentAutomation & { files_error?: string } {
  return {
    id: "unit-automation", name: "Unit schedule", prompt: "Observe", status: "PAUSED",
    kind: "cron", repeat: "daily", time: "09:00", weekdays: [], next_run_at: null,
    rrule: "FREQ=DAILY;BYHOUR=9;BYMINUTE=0", weekday: "MO", interval_hours: 1,
    schedule_label: "Daily 09:00", schedule_label_zh: "每天 09:00", schedule_label_en: "Daily 09:00",
    desktop_access: false, workspace_root: "", target_task_id: "", execution_environment: "local",
    next_run_at_iso: "", last_run_at: null, last_run_at_iso: "", last_run_status: "",
    last_run_summary: "", last_task_id: "", run_count: 0, memory_path: "", created_at: "", updated_at: "",
    files_error: filesError,
  };
}

it("S10-I18 displays the configuration failure on the actual schedule row", () => {
  render(<AutomationsPanel automations={[automation("agent_automation_write_failed")]} zh onRefresh={vi.fn()} />);
  expect(screen.getByText("配置文件写入失败：当前以应用内数据为准")).toBeTruthy();
});

it("S10-I18 keeps clean schedule rows free of a file failure warning", () => {
  render(<AutomationsPanel automations={[automation()]} zh onRefresh={vi.fn()} />);
  expect(screen.queryByText("配置文件写入失败：当前以应用内数据为准")).toBeNull();
});
