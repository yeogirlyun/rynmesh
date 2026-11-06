import { afterEach, beforeEach, expect, test } from "vitest";
import { agentTaskSummary, clearAgentRecords, flushAgentRecords, loadAgentRecords, PERSISTED_AGENT_TASKS, removeAgentTask, saveAgentTask, saveAgentTaskList } from "./agentTaskStore";
import { agentTaskCache, agentTaskListCache, forgetAgentTask, hydrateAgentRecords, rememberAgentTask, rememberAgentTaskList, resetAgentWorkspaceMemory } from "./agentWorkspaceCache";
import type { AgentTask } from "./rynAgent";

const flag = globalThis as { __rynAgentRecordsDisabled?: boolean };

function task(id: string, revision = 1, text = `secret conversation ${id}`): AgentTask {
  return {
    id, title: `Task ${id}`, model_id: "model", status: "completed", revision,
    created_at: 1, updated_at: revision, pending_approval: null, steps: 1, result: "", error: null,
    messages: [{ role: "user", content: text }], events: [], limits: {},
  } as unknown as AgentTask;
}

async function rawRecords(): Promise<Array<{ id: string; ciphertext: string }>> {
  const database = await new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open("ryn-agent-task-cache");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const rows = await new Promise<Array<{ id: string; ciphertext: string }>>((resolve, reject) => {
    const request = database.transaction("records", "readonly").objectStore("records").getAll();
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  database.close();
  return rows;
}

beforeEach(async () => {
  flag.__rynAgentRecordsDisabled = false;
  await clearAgentRecords();
});

afterEach(async () => {
  await clearAgentRecords();
  flag.__rynAgentRecordsDisabled = true;
});

test("keeps the task list and opened conversations encrypted on the device", async () => {
  saveAgentTaskList("node-a", [task("one")]);
  saveAgentTask("node-a", task("one", 2, "quarterly payroll numbers"));
  await flushAgentRecords();

  const rows = await rawRecords();
  expect(rows).toHaveLength(2);
  for (const row of rows) {
    expect(row.ciphertext).not.toContain("payroll");
    expect(atob(row.ciphertext)).not.toContain("payroll");
  }
  const loaded = await loadAgentRecords("node-a");
  expect(loaded.tasks?.map(item => item.id)).toEqual(["one"]);
  // The sidebar copy never carries the transcript.
  expect(loaded.tasks?.[0]).not.toHaveProperty("messages");
  expect(loaded.details[0].messages[0].content).toBe("quarterly payroll numbers");
  expect(await loadAgentRecords("node-b")).toEqual({ tasks: null, details: [] });
});

test("writes a frequently updated task once, with its newest value", async () => {
  for (let revision = 1; revision <= 5; revision += 1) saveAgentTask("node-a", task("live", revision));
  await flushAgentRecords();
  const loaded = await loadAgentRecords("node-a");
  expect(loaded.details.map(item => item.revision)).toEqual([5]);
});

test("keeps only the most recently opened conversations", async () => {
  for (let index = 0; index < PERSISTED_AGENT_TASKS + 3; index += 1) {
    saveAgentTask("node-a", task(`t${index}`));
    await flushAgentRecords();
  }
  const loaded = await loadAgentRecords("node-a");
  expect(loaded.details).toHaveLength(PERSISTED_AGENT_TASKS);
  expect(loaded.details[0].id).toBe(`t${PERSISTED_AGENT_TASKS + 2}`);
  expect(loaded.details.some(item => item.id === "t0")).toBe(false);
});

test("a removed task is not written back by an earlier queued save", async () => {
  saveAgentTask("node-a", task("gone"));
  await flushAgentRecords();
  saveAgentTask("node-a", task("gone", 2));
  removeAgentTask("node-a", "gone");
  await flushAgentRecords();
  expect((await loadAgentRecords("node-a")).details).toEqual([]);
});

test("persists nothing while the device copy is switched off", async () => {
  flag.__rynAgentRecordsDisabled = true;
  saveAgentTask("node-a", task("off"));
  await flushAgentRecords();
  flag.__rynAgentRecordsDisabled = false;
  expect((await loadAgentRecords("node-a")).details).toEqual([]);
});

test("restores the saved list and conversations after a restart without replacing newer ones", async () => {
  rememberAgentTaskList([task("a", 3), task("b", 1)]);
  rememberAgentTask(task("a", 3, "restored text"));
  rememberAgentTask(task("b", 1));
  await flushAgentRecords();

  resetAgentWorkspaceMemory();
  // This run already refreshed "b" from the node.
  agentTaskCache().set("b", task("b", 4, "fresh from node"));
  await hydrateAgentRecords();

  expect(agentTaskListCache()?.map(item => item.id)).toEqual(["a", "b"]);
  expect(agentTaskCache().get("a")?.messages[0].content).toBe("restored text");
  expect(agentTaskCache().get("b")?.messages[0].content).toBe("fresh from node");

  forgetAgentTask("a");
  expect(agentTaskListCache()?.map(item => item.id)).toEqual(["b"]);
  await flushAgentRecords();
  resetAgentWorkspaceMemory();
  await hydrateAgentRecords();
  expect(agentTaskCache().has("a")).toBe(false);
  resetAgentWorkspaceMemory();
});

test("summaries drop only transcript fields", () => {
  const summary = agentTaskSummary(task("x"));
  expect(summary).toMatchObject({ id: "x", title: "Task x", revision: 1 });
  for (const field of ["messages", "events", "limits", "streaming", "pending_user_messages"]) expect(summary).not.toHaveProperty(field);
});
