import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { expect, it, vi } from "vitest";
import RynAgentContextMenu from "./RynAgentContextMenu";

it("portals outside a clipped sidebar and navigates enabled actions with the keyboard", async () => {
  const selected = vi.fn(), close = vi.fn(), user = userEvent.setup();
  const view = render(<div style={{ overflow: "hidden" }}><RynAgentContextMenu anchor={{ x: 20, y: 30 }} label="Task actions" onClose={close} items={[
    { id: "rename", label: "Rename", onSelect: selected },
    { id: "archive", label: "Archive", disabled: true, description: "Pause this task first", onSelect: selected },
    { id: "delete", label: "Delete", danger: true, onSelect: selected },
  ]} /></div>);
  const menu = screen.getByRole("menu", { name: "Task actions" });
  expect(menu.parentElement).toBe(document.body);
  expect(view.container).not.toContainElement(menu);
  expect(screen.getByRole("menuitem", { name: "Rename" })).toHaveFocus();
  expect(screen.getByRole("menuitem", { name: /Archive/ })).toBeDisabled();
  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
  await user.keyboard("{ArrowDown}");
  expect(screen.getByRole("menuitem", { name: "Rename" })).toHaveFocus();
  await user.keyboard("{End}");
  expect(screen.getByRole("menuitem", { name: "Delete" })).toHaveFocus();
  await user.keyboard("{Home}{ArrowUp}{Enter}");
  expect(selected).toHaveBeenCalledOnce();
  expect(close).toHaveBeenCalledOnce();
});

it("dismisses with Escape and returns focus to its trigger", async () => {
  function Harness() {
    const [open, setOpen] = useState(false);
    return <><button onClick={() => setOpen(true)}>Open actions</button>{open && <RynAgentContextMenu anchor={{ x: 0, y: 0 }} label="Actions" onClose={() => setOpen(false)} items={[{ id: "rename", label: "Rename", onSelect: () => undefined }]} />}</>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole("button", { name: "Open actions" }));
  expect(screen.getByRole("menuitem", { name: "Rename" })).toHaveFocus();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Open actions" })).toHaveFocus();
});

it("dismisses on an outside pointer or sidebar scroll without running an action", () => {
  const close = vi.fn(), selected = vi.fn();
  render(<><button>Outside</button><RynAgentContextMenu anchor={{ x: 10, y: 10 }} label="Actions" onClose={close} items={[{ id: "rename", label: "Rename", onSelect: selected }]} /></>);
  fireEvent.pointerDown(screen.getByRole("menuitem", { name: "Rename" }));
  expect(close).not.toHaveBeenCalled();
  fireEvent.pointerDown(screen.getByRole("button", { name: "Outside" }));
  expect(close).toHaveBeenCalledOnce();
  fireEvent.scroll(document);
  expect(close).toHaveBeenCalledTimes(2);
  expect(selected).not.toHaveBeenCalled();
});

it("clamps its position to the viewport and repositions after resize", async () => {
  const bounds = vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({ width: 200, height: 120 } as DOMRect);
  try {
    render(<RynAgentContextMenu anchor={{ x: window.innerWidth + 100, y: window.innerHeight + 100 }} label="Actions" onClose={vi.fn()} items={[{ id: "rename", label: "Rename", onSelect: vi.fn() }]} />);
    const menu = screen.getByRole("menu");
    expect(menu).toHaveStyle({ left: `${window.innerWidth - 208}px`, top: `${window.innerHeight - 128}px` });
    bounds.mockReturnValue({ width: 250, height: 160 } as DOMRect);
    fireEvent(window, new Event("resize"));
    await waitFor(() => expect(menu).toHaveStyle({ left: `${window.innerWidth - 258}px`, top: `${window.innerHeight - 168}px` }));
  } finally { bounds.mockRestore(); }
});
