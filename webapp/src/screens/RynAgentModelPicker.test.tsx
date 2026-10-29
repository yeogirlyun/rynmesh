import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import type { AgentModel } from "../domain/rynAgent";
import RynAgentModelPicker from "./RynAgentModelPicker";

const models: AgentModel[] = [
  { id: "alpha", name: "Alpha", source: "local_api", source_name: "Personal API", available: true, agent_supported: true },
  { id: "offline", name: "Offline", source: "local_api", source_name: "Personal API", available: false, agent_supported: true },
  { id: "beta", name: "Beta", source: "local_service", source_name: "Local CLI", cli_source: "codex", available: true, agent_supported: true },
  { id: "probe", name: "Configured CLI", source: "local_service", source_name: "Local CLI", cli_source: "codex", cli_model: "configured-cli", available: true, agent_supported: true, tool_support: "unknown" },
];

it("keeps the model trigger compact and opens grouped choices with current selection and availability", async () => {
  const user = userEvent.setup();
  const changed = vi.fn();
  render(<RynAgentModelPicker models={models} value="alpha" onChange={changed} effortLabel="Light" />);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  expect(trigger).toHaveTextContent("AlphaLight");
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.click(trigger);
  const list = screen.getByRole("listbox", { name: "Model" });
  expect(within(list).getByRole("group", { name: "My source" })).toBeVisible();
  expect(within(list).getByRole("group", { name: "Local CLI" })).toBeVisible();
  expect(within(list).getByRole("option", { name: "Alpha" })).toHaveAttribute("aria-selected", "true");
  expect(within(list).getByRole("option", { name: "Offline" })).toHaveAccessibleDescription("Unavailable");
  await user.click(within(list).getByRole("option", { name: "Offline" }));
  expect(changed).not.toHaveBeenCalled();
  expect(list).toBeVisible();
  await user.click(within(list).getByRole("option", { name: "Beta" }));
  expect(changed).toHaveBeenCalledWith("beta");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("supports keyboard selection, skips unavailable models and closes with Escape", async () => {
  const user = userEvent.setup();
  const changed = vi.fn();
  render(<RynAgentModelPicker models={models} value="alpha" onChange={changed} />);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  trigger.focus();
  await user.keyboard("{ArrowDown}{ArrowDown}");
  expect(trigger).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: "Beta" }).id);
  await user.keyboard("{Enter}");
  expect(changed).toHaveBeenCalledWith("beta");
  await user.keyboard("{End}");
  expect(trigger).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: "Configured CLI" }).id);
  await user.keyboard("{Home}");
  expect(trigger).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: "Alpha" }).id);
  await user.keyboard("Beta");
  expect(trigger).toHaveAttribute("aria-activedescendant", screen.getByRole("option", { name: "Beta" }).id);
  await user.keyboard("{Escape}");
  expect(trigger).toHaveAttribute("aria-expanded", "false");
  expect(trigger).toHaveFocus();
});

it.each(["unknown", "unsupported"] as const)("selects a configured CLI with %s diagnostic evidence without a verification row", async tool_support => {
  const user = userEvent.setup();
  const changed = vi.fn();
  render(<RynAgentModelPicker models={models.map(model => model.id === "probe" ? { ...model, tool_support } : model)} value="alpha" onChange={changed} />);
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  const option = screen.getByRole("option", { name: "Configured CLI" });
  expect(option).toHaveAttribute("aria-disabled", "false");
  expect(option).not.toHaveAttribute("aria-describedby");
  expect(option.querySelector("small")).toBeNull();
  expect(screen.queryByText(/Needs verification/)).not.toBeInTheDocument();
  await user.click(option);
  expect(changed).toHaveBeenCalledWith("probe");
});

it("keeps unsupported CLI bridges and unverifiable shared destinations unselectable with explicit reasons", async () => {
  const user = userEvent.setup();
  const changed = vi.fn();
  render(<RynAgentModelPicker models={[
    { ...models[3], agent_supported: false },
    { ...models[0], id: "peer", name: "Shared model", source: "peer", destination_binding_supported: false },
  ]} value="" onChange={changed} />);
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  const unsupported = screen.getByRole("option", { name: "Configured CLI" });
  const destination = screen.getByRole("option", { name: "Shared model" });
  expect(unsupported).toHaveAttribute("aria-disabled", "true");
  expect(unsupported).toHaveAccessibleDescription("Assistant tools unsupported");
  expect(destination).toHaveAttribute("aria-disabled", "true");
  expect(destination).toHaveAccessibleDescription("Destination unavailable");
  await user.click(unsupported);
  await user.click(destination);
  expect(changed).not.toHaveBeenCalled();
});

it("keeps a locked task model readable and routes switching through its management action", async () => {
  const user = userEvent.setup();
  const changed = vi.fn();
  const manage = vi.fn();
  render(<RynAgentModelPicker models={models} value="alpha" onChange={changed} selectionLocked onManageTaskModel={manage} />);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  await user.click(trigger);
  const option = screen.getByRole("option", { name: "Beta" });
  expect(option).toHaveAttribute("aria-disabled", "true");
  await user.click(option);
  expect(changed).not.toHaveBeenCalled();
  expect(screen.getByRole("listbox")).toBeVisible();
  await user.tab();
  expect(screen.getByRole("button", { name: "Change this chat's model…" })).toHaveFocus();
  await user.keyboard("{Enter}");
  expect(manage).toHaveBeenCalledOnce();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});

it("closes on outside interaction and disables interaction while busy", async () => {
  const user = userEvent.setup();
  const view = render(<><RynAgentModelPicker models={models} value="alpha" onChange={vi.fn()} /><button>Outside</button></>);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  await user.click(trigger);
  await user.click(screen.getByRole("button", { name: "Outside" }));
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  await user.click(trigger);
  view.rerender(<RynAgentModelPicker models={models} value="alpha" onChange={vi.fn()} disabled />);
  expect(trigger).toBeDisabled();
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
});

it("shows a retained model that disappeared from the catalog and a useful empty state", async () => {
  const user = userEvent.setup();
  const view = render(<RynAgentModelPicker models={[]} value="retained-model" onChange={vi.fn()} />);
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  const missing = screen.getByRole("option");
  expect(missing).toHaveTextContent("retained-modelUnavailable");
  expect(missing).toHaveAttribute("aria-disabled", "true");
  view.rerender(<RynAgentModelPicker models={[]} value="" onChange={vi.fn()} />);
  expect(screen.getByText("No models available")).toBeVisible();
});

it("offers only the model's supported reasoning efforts and applies a real selection", async () => {
  const user = userEvent.setup();
  const changeEffort = vi.fn();
  render(<RynAgentModelPicker models={models} value="beta" onChange={vi.fn()} reasoningEfforts={["low", "high"]} effort="low" onEffortChange={changeEffort} />);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  expect(trigger).toHaveValue("beta");
  expect(trigger).toHaveTextContent("BetaLow");
  await user.click(trigger);
  const efforts = screen.getByRole("radiogroup", { name: "Reasoning effort" });
  expect(within(efforts).getAllByRole("radio")).toHaveLength(2);
  expect(within(efforts).getByRole("radio", { name: "Low" })).toBeChecked();
  expect(within(efforts).queryByRole("radio", { name: "Medium" })).not.toBeInTheDocument();
  await user.tab();
  expect(within(efforts).getByRole("radio", { name: "Low" })).toHaveFocus();
  await user.keyboard("{ArrowRight}");
  expect(changeEffort).toHaveBeenCalledWith("high");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("applies reasoning effort when its visible label text is clicked and restores trigger focus", async () => {
  const user = userEvent.setup();
  const changeEffort = vi.fn();
  const view = render(<RynAgentModelPicker models={models} value="beta" onChange={vi.fn()} reasoningEfforts={["low", "high"]} effort="low" onEffortChange={changeEffort} />);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  await user.click(trigger);
  const highText = within(screen.getByRole("radiogroup", { name: "Reasoning effort" })).getByText("High");
  expect(highText.tagName).toBe("SPAN");
  expect(highText).toBeVisible();
  await user.click(highText);
  expect(changeEffort).toHaveBeenCalledExactlyOnceWith("high");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  view.rerender(<RynAgentModelPicker models={models} value="beta" onChange={vi.fn()} reasoningEfforts={["low", "high"]} effort="high" onEffortChange={changeEffort} />);
  expect(trigger).toHaveTextContent("BetaHigh");
  await user.click(trigger);
  expect(screen.getByRole("radio", { name: "High" })).toBeChecked();
  expect(screen.getByRole("radio", { name: "Low" })).not.toBeChecked();
});

it("applies a keyboard Space selection on the effort radio and restores trigger focus", async () => {
  const user = userEvent.setup();
  const changeEffort = vi.fn();
  render(<RynAgentModelPicker models={models} value="beta" onChange={vi.fn()} reasoningEfforts={["low", "high"]} effort="low" onEffortChange={changeEffort} />);
  const trigger = screen.getByRole("combobox", { name: "Model" });
  await user.click(trigger);
  const high = screen.getByRole("radio", { name: "High" });
  high.focus();
  await user.keyboard(" ");
  expect(changeEffort).toHaveBeenCalledExactlyOnceWith("high");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
});

it("retains every reasoning level reported by the selected model", async () => {
  const user = userEvent.setup(), changeEffort = vi.fn();
  render(<RynAgentModelPicker models={models} value="beta" onChange={vi.fn()} reasoningEfforts={["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]} effort="high" onEffortChange={changeEffort} />);
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  const choices = within(screen.getByRole("radiogroup", { name: "Reasoning effort" }));
  expect(choices.getAllByRole("radio").map(radio => (radio as HTMLInputElement).value)).toEqual(["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);
  await user.click(choices.getByRole("radio", { name: "Ultra" }));
  expect(changeEffort).toHaveBeenCalledExactlyOnceWith("ultra");
});

it("omits reasoning controls without model capability and prevents effort changes for a locked task", async () => {
  const user = userEvent.setup();
  const changeEffort = vi.fn();
  const view = render(<RynAgentModelPicker models={models} value="alpha" onChange={vi.fn()} effort="low" onEffortChange={changeEffort} />);
  await user.click(screen.getByRole("combobox", { name: "Model" }));
  expect(screen.queryByRole("radiogroup")).not.toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Model" })).not.toHaveTextContent("Low");
  view.rerender(<RynAgentModelPicker models={models} value="beta" onChange={vi.fn()} reasoningEfforts={["low", "high"]} effort="low" onEffortChange={changeEffort} selectionLocked />);
  const high = screen.getByRole("radio", { name: "High" });
  expect(high).toBeDisabled();
  await user.click(high);
  expect(changeEffort).not.toHaveBeenCalled();
});
