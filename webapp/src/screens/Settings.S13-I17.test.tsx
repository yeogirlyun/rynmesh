import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import { PrivacySection } from "./Settings";
import { conversationCleanup } from "../domain/conversationCleanup";
import ConversationCleanupPanel from "./components/ConversationCleanupPanel";

const { confirm } = vi.hoisted(() => ({ confirm: vi.fn() }));
vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm }) }));
afterEach(() => { vi.restoreAllMocks(); confirm.mockReset(); });

it("T1 confirms each Agent scope and preserves credential/plugin disclosure", async () => {
  const client = makeFixtureNodeClient();
  const erase = vi.spyOn(client, "erasePersonalData");
  render(<PrivacySection client={client} confirm={confirm} notify={vi.fn()} revision={0} />);
  expect(screen.getByText(/不会删除模型来源凭据和插件/)).toBeInTheDocument();
  const user = userEvent.setup();
  for (const [name, scope] of [["清除 Agent 历史", "agent_history"], ["清除 Agent 记忆", "agent_memory"]]) {
    await user.click(screen.getByRole("button", { name }));
    expect(confirm.mock.lastCall![0].risk).toBe("high");
    expect(erase).toHaveBeenCalledTimes(scope === "agent_history" ? 0 : 1);
    await act(() => confirm.mock.lastCall![0].onConfirm());
    expect(erase).toHaveBeenLastCalledWith([scope]);
  }
});

it("T2 displays active answers skipped by real cleanup response", async () => {
  vi.spyOn(conversationCleanup, "jobs").mockResolvedValue([{
    id: "job", done: ["source", "replica", "backups", "search"], pending: ["orders"], cancelled: false,
    local_copies_complete: false, browser_cleanup_required: true, remote_confirmed: false, identities: 1,
    skipped_active: ["active"],
  }]);
  render(<ConversationCleanupPanel />);
  expect(await screen.findByText("有 1 个进行中的问答稍后再清")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: /Continue cleanup/ })).toBeInTheDocument();
});
