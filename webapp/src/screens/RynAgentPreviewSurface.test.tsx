import { useCallback, useState } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import RynAgentPreviewSurface, { AgentPreviewHostContext } from "./RynAgentPreviewSurface";

it("docks outside the transcript, closes on Escape and restores the file trigger", async () => {
  const opened = vi.fn();
  function Harness() {
    const [host, setHost] = useState<HTMLDivElement | null>(null);
    const [open, setOpen] = useState(false);
    const onOpen = useCallback(() => opened(), []);
    return <AgentPreviewHostContext.Provider value={{ host, hidden: false, onOpen }}>
      <div data-testid="transcript"><button onClick={() => setOpen(true)}>Open file</button>
        {open && <RynAgentPreviewSurface label="Report" onClose={() => setOpen(false)}><button onClick={() => setOpen(false)}>Close preview</button><p>Report contents</p></RynAgentPreviewSurface>}
      </div><div ref={setHost} data-testid="host" />
    </AgentPreviewHostContext.Provider>;
  }
  const user = userEvent.setup(); render(<Harness />);
  const trigger = screen.getByRole("button", {name: "Open file"});
  await user.click(trigger);
  const preview = screen.getByRole("region", {name: "Report"});
  expect(screen.getByTestId("host")).toContainElement(preview);
  expect(screen.getByTestId("transcript")).not.toContainElement(preview);
  expect(preview).toHaveFocus();
  expect(opened).toHaveBeenCalledTimes(1);
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByText("Report contents")).not.toBeInTheDocument());
  expect(trigger).toHaveFocus();
});
