import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentFork, { RynAgentForkSource } from "./RynAgentFork";
import { agentErrorHelp, KNOWN_AGENT_ERROR_CODES } from "./rynAgentErrors";

const locale = vi.hoisted(() => ({ language: "en" }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ i18n: locale }) }));
beforeEach(() => { locale.language = "en"; sessionStorage.clear(); });
afterEach(cleanup);

const task = { id: "parent", title: "Original", status: "completed", messages: [], fork: {
  source_task_id: "source", source_title: "Source", message_count: 12,
  shared_workspace: true, awaiting_instruction: true, compacted: true, omitted_messages: 8,
} } as unknown as AgentTask;

it.each([true, false])("S02-I29 provides fork-specific guidance in both languages (zh=%s)", zh => {
  expect(KNOWN_AGENT_ERROR_CODES).toContain("agent_fork_too_large");
  const help = agentErrorHelp("agent_fork_too_large", zh);
  expect(help.text).toBe(zh ? "这段对话太长，无法分叉；请从靠后的位置分叉" :
    "This conversation is too long to fork. Choose a later point to fork.");
  expect(help.actions.length).toBeGreaterThan(0);
});

it.each(["zh", "en"])("S02-I29 labels a compacted fork on its real source panel (%s)", language => {
  locale.language = language;
  render(<RynAgentForkSource task={task} onChoose={vi.fn()} />);
  expect(screen.getByText(language === "zh" ? "早期内容已压成摘要" : "Earlier content has been summarized.")).toBeTruthy();
});

it("S02-I29 does not label an ordinary fork as summarized", () => {
  render(<RynAgentForkSource task={{ ...task, fork: { ...task.fork!, compacted: false } } as AgentTask} onChoose={vi.fn()} />);
  expect(screen.queryByText("Earlier content has been summarized.")).toBeNull();
});

it("S02-I29 shows the new guidance on the actual fork creation failure", async () => {
  locale.language = "zh";
  server.use(http.get("*/api/local/agent/tasks/parent/fork-boundaries", () => HttpResponse.json({
    boundaries: [{ message_count: 12, token: "prefix", role: "assistant", preview: "Answer" }],
  })));
  server.use(http.post("*/api/local/agent/tasks/parent/fork", () =>
    HttpResponse.json({ detail: "agent_fork_too_large" }, { status: 409 })));
  const user = userEvent.setup();
  render(<RynAgentFork task={task} onChoose={vi.fn()} onCreated={vi.fn()} />);
  await user.click(screen.getByRole("button", { name: "分叉任务" }));
  await screen.findByRole("option", { name: /Answer/ });
  await user.click(screen.getByRole("button", { name: "创建并打开分叉" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("这段对话太长，无法分叉；请从靠后的位置分叉");
});
