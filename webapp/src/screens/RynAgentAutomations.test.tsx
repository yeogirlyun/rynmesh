import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { rynAgent, type AgentAutomation } from "../domain/rynAgent";
import { AutomationDialog } from "./RynAgentAutomations";

describe("AutomationDialog schedule preservation", () => {
  afterEach(() => vi.restoreAllMocks());

  const initial: Partial<AgentAutomation> = {
    id: "weekly-task",
    name: "Weekly review",
    prompt: "Review the workspace",
    status: "ACTIVE",
    repeat: "weekly",
    time: "09:00",
    weekday: "MO",
    weekdays: ["MO", "WE"],
    desktop_access: false,
  };

  function openDialog() {
    const update = vi.spyOn(rynAgent, "updateAutomation").mockResolvedValue({
      automation: initial as AgentAutomation,
    });
    render(<AutomationDialog open initial={initial} zh={false} onClose={vi.fn()} onSaved={vi.fn()} />);
    return update;
  }

  it("does not rewrite the schedule or enable desktop access when renaming a task", async () => {
    const update = openDialog();
    fireEvent.change(screen.getByPlaceholderText("Task title (e.g. Send WeChat message)"), { target: { value: "New review title" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("weekly-task", {
      name: "New review title",
      prompt: "Review the workspace",
      status: "ACTIVE",
      desktop_access: false,
    }));
  });

  it("keeps all weekly days when changing the execution time", async () => {
    const update = openDialog();
    fireEvent.change(document.querySelector('input[type="time"]')!, { target: { value: "10:30" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("weekly-task", expect.objectContaining({
      repeat: "weekly",
      time: "10:30",
      weekday: "MO",
      weekdays: ["MO", "WE"],
      desktop_access: false,
    })));
  });

  it("uses the explicitly selected single weekday when changing the day", async () => {
    const update = openDialog();
    fireEvent.change(screen.getAllByRole("combobox")[1], { target: { value: "FR" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][1]).toMatchObject({ repeat: "weekly", weekday: "FR" });
    expect(update.mock.calls[0][1]).not.toHaveProperty("weekdays");
  });
});
