import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentConnection from "./RynAgentConnection";

afterEach(cleanup);
it("S02-I10 T5 shows the continued interruption notice on task detail", async () => {
  await i18next.changeLanguage("zh-CN");
  const task = { id: "i10", status: "queued", error: null, events: [{ seq: 1, at: 1, type: "interrupted", continued: true, reason: "node_restarted" }] } as unknown as AgentTask;
  render(<RynAgentConnection task={task} modelName="model" busy={false} enabled onAction={vi.fn()} />);
  expect(screen.getByText(/已自动续跑/)).toBeInTheDocument();
});
