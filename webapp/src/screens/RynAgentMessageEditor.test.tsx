import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import RynAgentMessageEditor from "./RynAgentMessageEditor";
import "./rynAgentText";

it("lets the user cancel edits and send a revised previous message", async () => {
  const user = userEvent.setup(), onSend = vi.fn().mockResolvedValue(true);
  render(<RynAgentMessageEditor text="Original request" disabled={false} onSend={onSend}><p>Original request</p></RynAgentMessageEditor>);
  await user.click(screen.getByRole("button", { name: "Edit message" }));
  await user.clear(screen.getByRole("textbox"));
  await user.type(screen.getByRole("textbox"), "Discarded revision");
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Escape" });
  expect(onSend).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: "Edit message" }));
  expect(screen.getByRole("textbox")).toHaveValue("Original request");
  await user.clear(screen.getByRole("textbox"));
  await user.type(screen.getByRole("textbox"), "Revised request");
  await user.click(screen.getByRole("button", { name: "Save and send" }));
  expect(onSend).toHaveBeenCalledExactlyOnceWith("Revised request");
  expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
});

it("keeps edited text after the server rejects the change", async () => {
  const user = userEvent.setup(), onSend = vi.fn().mockResolvedValue(false);
  render(<RynAgentMessageEditor text="Original" disabled={false} onSend={onSend}>Original</RynAgentMessageEditor>);
  await user.click(screen.getByRole("button", { name: "Edit message" }));
  await user.type(screen.getByRole("textbox"), " revised");
  fireEvent.keyDown(screen.getByRole("textbox"), { key: "Enter", ctrlKey: true });
  expect(onSend).toHaveBeenCalledExactlyOnceWith("Original revised");
  expect(await screen.findByRole("textbox")).toHaveValue("Original revised");
});
