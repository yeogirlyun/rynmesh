import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import WorkspaceSelect from "./WorkspaceSelect";

it("supports keyboard choice, Escape, outside click and a disabled control", async () => {
  const user = userEvent.setup();
  const onChange = vi.fn();
  const props = { caption: "模型", label: "模型", value: "a", options: [{ value: "a", label: "Model A" }, { value: "b", label: "Model B" }], onChange };
  const { rerender } = render(<><WorkspaceSelect {...props} /><button>Outside</button></>);
  const trigger = screen.getByRole("button", { name: "模型" });
  trigger.focus();
  await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
  expect(onChange).toHaveBeenCalledWith("b");
  expect(trigger).toHaveFocus();
  await user.click(trigger);
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  expect(trigger).toHaveFocus();
  await user.click(trigger);
  await user.click(screen.getByText("Outside"));
  expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  rerender(<WorkspaceSelect {...props} disabled />);
  expect(screen.getByRole("button", { name: "模型" })).toBeDisabled();
});
