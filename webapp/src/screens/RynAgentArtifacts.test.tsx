import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { rynAgent, type AgentTask } from "../domain/rynAgent";
import { TaskArtifacts, TurnDeliverables } from "./RynAgentArtifacts";
import RynAgentProgress from "./RynAgentProgress";
import ChatMarkdown from "./ChatMarkdown";
import { findAgentFile } from "./RynAgentFileContext";
import type { AgentFileArtifact } from "../domain/rynAgent";
import "./rynAgentText";
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

const task: AgentTask = { id: "artifact-task", title: "Report", model_id: "model", status: "failed", revision: 3,
  control_revision: 2, scope_revision: 1, created_at: 1, updated_at: 1, pending_approval: null,
  steps: 4, result: "A model-invented path: private/secret.md", error: "agent_time_limit", messages: [], events: [], limits: {} };
const artifact = { id: "op-confirmed", name: "report.md", bytes: 20, sha256: "bound-sha", created_at: "2026-10-11" };

it("opens the actual confirmed artifact for a failed task using its task-bound read endpoint", async () => {
  const read = vi.fn(); const nasRead = vi.fn(); const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/artifact-task/artifacts", () => HttpResponse.json({ artifacts: [artifact] })),
    http.get("*/api/local/agent/tasks/artifact-task/artifacts/op-confirmed", () => { read(); return HttpResponse.json({ ...artifact, text: "Actual saved report." }); }),
    http.get("*/api/local/plugins/nas/sources/:source/content", () => { nasRead(); return HttpResponse.json({ text: "MUST NOT USE OWNER NAS BYPASS" }); }));
  render(<TaskArtifacts task={task} />);
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  expect(await screen.findByText("Actual saved report.")).toBeVisible();
  expect(read).toHaveBeenCalledTimes(1); expect(nasRead).not.toHaveBeenCalled();
  expect(screen.queryByText(/private\/secret/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Download|Ask AI|Edit|Share/i })).not.toBeInTheDocument();
});

it("renders nothing for prose and saved tool outputs without confirmed artifacts", async () => {
  const listed = vi.fn();
  server.use(http.get("*/api/local/agent/tasks/artifact-task/artifacts", () => { listed(); return HttpResponse.json({ artifacts: [] }); }));
  const view = render(<TaskArtifacts task={task} />);
  await waitFor(() => expect(listed).toHaveBeenCalled());
  expect(view.container).toBeEmptyDOMElement();
});

it("clears old preview after scope change and displays denial instead of saved content", async () => {
  let revoked = false; const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/artifact-task/artifacts", () => HttpResponse.json({ artifacts: [artifact] })),
    http.get("*/api/local/agent/tasks/artifact-task/artifacts/op-confirmed", () => revoked
      ? HttpResponse.json({ detail: "agent_artifact_access_changed" }, { status: 409 })
      : HttpResponse.json({ ...artifact, text: "Previously readable content" })));
  const view = render(<TaskArtifacts task={task} />);
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  await screen.findByText("Previously readable content");
  revoked = true;
  view.rerender(<TaskArtifacts task={{ ...task, scope_revision: 2, control_revision: 3 }} />);
  expect(screen.queryByText("Previously readable content")).not.toBeInTheDocument();
  await user.click(await screen.findByRole("button", { name: "Open report.md" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("this task no longer has access");
  expect(screen.queryByText("Previously readable content")).not.toBeInTheDocument();
});

const svgV1 = '<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>';
const svgV2 = '<svg xmlns="http://www.w3.org/2000/svg"><rect width="9" height="9"/><script>alert(1)</script></svg>';
const done: AgentTask = { ...task, id: "file-task", status: "completed", error: null, result: "Saved pelican.svg; see ghost.svg too." };
const pelican = { id: "file_pelican", name: "pelican.svg", path: "pelican.svg", media_type: "image/svg+xml", preview: "image", role: "deliverable",
  status: "current", bytes: 2048, sha256: "v1", receipt_sha256: "v1", created_by_task: true, revisions: 1, goal_revisions: [1],
  mentioned_in_answer: true, render_inspected: { at: "2026-10-13", findings: 0 } };
const helper = { ...pelican, id: "file_helper", name: "notes.txt", path: "notes.txt", media_type: "text/plain", preview: "text", role: "supporting",
  sha256: "n1", mentioned_in_answer: false, render_inspected: undefined };

it("keeps computer screenshots inside collapsed operation history and excludes them from delivered files", async () => {
  const user = userEvent.setup();
  const observation = { ...pelican, id: "screen", name: "image_1.png", path: ".ryn/computer/file-task/observe/image_1.png", source: "computer_observation", operation_id: "observe" };
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [observation] })));
  const record = { ...done, events: [{ seq: 1, at: 1, type: "tool_result", call_id: "call", operation_id: "observe" }] };
  const messages = [{ role: "assistant", content: "", tool_calls: [{ id: "call", function: { name: "computer_read_window", arguments: "{}" } }] }];
  render(<TaskArtifacts task={record}><RynAgentProgress task={record} messages={messages} /></TaskArtifacts>);
  const screenLink = await screen.findByRole("button", { name: "Screen observation 1", hidden: true });
  expect(screenLink).not.toBeVisible();
  expect(screen.queryByRole("region", { name: "Result files" })).not.toBeInTheDocument();
  expect(screen.queryByText(/1 file ready/)).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Open image_1.png" })).not.toBeInTheDocument();
  await user.click(screen.getByText("View activity"));
  expect(screenLink).toBeVisible();
});

it("opens common Office files through the task-bound, versioned local preview endpoint", async () => {
  const user = userEvent.setup(); const requested: string[] = []; const nas = vi.fn();
  const files = [{ ...helper, name: "销售.xlsx", id: "file_workbook", path: "销售.xlsx", preview: "download", role: "deliverable", sha256: "sheet-v1" },
    { ...helper, name: "报告.docx", id: "file_document", path: "报告.docx", preview: "download", role: "deliverable", sha256: "doc-v1" }];
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files })),
    http.get("*/api/local/agent/tasks/file-task/files/:file/preview", ({ request, params }) => {
      requested.push(request.url);
      return HttpResponse.json(params.file === "file_workbook" ? { kind: "spreadsheet", truncated: false, sha256: "sheet-v1", sheets: [
        { name: "季度汇总", rows: [{ index: 1, cells: [{ column: 1, value: "季度销售额" }] }], columns: 1, total_rows: 1, total_columns: 1, truncated: false }] }
        : { kind: "document", truncated: false, sha256: "doc-v1", blocks: [{ type: "paragraph", style: "", runs: [{ text: "已保存的销售报告", bold: false, italic: false }] }] });
    }), http.get("*/api/local/plugins/nas/sources/:source/content", () => { nas(); return HttpResponse.json({ text: "unbound" }); }));
  render(<TaskArtifacts task={done} />);
  await user.click(await screen.findByRole("button", { name: "Open 销售.xlsx" }));
  expect(await screen.findByRole("tab", { name: "季度汇总" })).toBeVisible();
  expect(screen.getByRole("cell", { name: "季度销售额" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Open 报告.docx" }));
  expect(await screen.findByText("已保存的销售报告")).toBeVisible();
  expect(requested[0]).toMatch(/files\/file_workbook\/preview\?v=sheet-v1$/);
  expect(requested[1]).toMatch(/files\/file_document\/preview\?v=doc-v1$/);
  expect(nas).not.toHaveBeenCalled();
});

function blobURLs() {
  const blobs: Blob[] = [];
  vi.stubGlobal("URL", Object.assign(class extends URL {}, {
    createObjectURL: (blob: Blob) => { blobs.push(blob); return `blob:artifact-${blobs.length}`; }, revokeObjectURL: vi.fn() }));
  return blobs;
}

it("presents receipt-backed result files and opens an SVG only as an inert image", async () => {
  const blobs = blobURLs(); const requested: string[] = []; const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [pelican, helper], unbacked_mentions: ["ghost.svg"] })),
    http.get("*/api/local/agent/tasks/file-task/files/:id", ({ request }) => { requested.push(request.url); return new HttpResponse(svgV2, { headers: { "Content-Type": "text/html" } }); }));
  const { container } = render(<TaskArtifacts task={done} />);
  const section = await screen.findByRole("region", { name: "Result files" });
  await user.click(screen.getByLabelText("Open with · pelican.svg"));
  expect(await screen.findByText("Ryn checked how this version renders")).toBeInTheDocument();
  expect(section).toHaveTextContent("Other files from this task (1)");
  expect(section).not.toHaveTextContent("ghost.svg");
  const tab = screen.getByRole("link", { name: "Open pelican.svg in a new tab" });
  expect(tab.getAttribute("href")).toMatch(/\/agent\/tasks\/file-task\/files\/file_pelican\?v=v1$/);
  expect(tab).toHaveAttribute("target", "_blank"); expect(tab.getAttribute("rel")).toContain("noopener");
  await user.click(screen.getByRole("button", { name: "Open pelican.svg" }));
  const image = await screen.findByRole("img", { name: "pelican.svg" });
  expect(image).toHaveAttribute("src", "blob:artifact-1");
  // The listing's type wins over a sniffed or served one, and the markup is never put into the page.
  expect(blobs[0].type).toBe("image/svg+xml"); expect(await blobs[0].text()).toBe(svgV2);
  expect(requested[0]).toMatch(/files\/file_pelican\?v=v1$/);
  expect(container.querySelector("iframe, object, embed, script")).toBeNull();
  expect(container.querySelector(`rect[width="9"]`)).toBeNull();
});

it("links confirmed absolute paths, inline code and filenames to the same preview while preserving external links", async () => {
  const user = userEvent.setup();
  const file = { ...helper, role: "deliverable", absolute_path: "C:\\Users\\ziling\\.rynmesh\\agent\\files\\验收 记录.md", name: "验收 记录.md", path: "验收 记录.md" };
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [file] })),
    http.get("*/api/local/agent/tasks/file-task/files/:id", () => new HttpResponse("Verified acceptance record")));
  const view = render(<TaskArtifacts task={done}><ChatMarkdown fileLinks>{`已完成。 [查看验收记录](<${file.absolute_path}>)\n\n文件：\`${file.absolute_path}\`\n\n验收 记录.md。 [官网](https://example.com/验收%20记录.md) [不存在](C:/ghost.md)`}</ChatMarkdown></TaskArtifacts>);
  const link = await screen.findByRole("link", { name: "查看验收记录" });
  expect(link).toHaveAttribute("title", file.absolute_path);
  expect(view.container.querySelectorAll("a.ra-file-link")).toHaveLength(3);
  expect(screen.getByRole("link", { name: "官网" })).toHaveAttribute("href", "https://example.com/%E9%AA%8C%E6%94%B6%20%E8%AE%B0%E5%BD%95.md");
  expect(screen.queryByRole("link", { name: "不存在" })).not.toBeInTheDocument();
  await user.click(link);
  expect(await screen.findByText("Verified acceptance record")).toBeVisible();
});

it("shows saved files for an unfinished task and uses the bound id for desktop open/reveal", async () => {
  const user = userEvent.setup(); const actions: unknown[] = [];
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [pelican] })),
    http.post("*/api/local/agent/tasks/file-task/files/file_pelican/actions", async ({ request }) => { actions.push(await request.json()); return HttpResponse.json({ opened: true }); }));
  render(<TaskArtifacts task={{ ...done, status: "failed", result: "Saved pelican.svg, remaining work failed." }} />);
  expect(await screen.findByText("1 file available · task unfinished")).toBeVisible();
  await user.click(screen.getByLabelText("Open with · pelican.svg"));
  await user.click(screen.getByRole("button", { name: "Default application" }));
  await waitFor(() => expect(actions).toEqual([{ action: "open" }]));
  await user.click(screen.getByLabelText("Open with · pelican.svg"));
  await user.click(screen.getByRole("button", { name: "Show in folder" }));
  await waitFor(() => expect(actions).toEqual([{ action: "open" }, { action: "reveal" }]));
});

it("does not resolve an ambiguous filename or a remote URL to a local artifact", () => {
  const files = [{ ...helper, path: "a/notes.txt" }, { ...helper, id: "b", path: "b/notes.txt" }] as AgentFileArtifact[];
  expect(findAgentFile(files, "notes.txt")).toBeUndefined();
  expect(findAgentFile(files, "https://example.com/a/notes.txt")).toBeUndefined();
  expect(findAgentFile(files, "a/notes.txt:12")?.path).toBe("a/notes.txt");
});

it("follows a revised file to its new version instead of showing stale content", async () => {
  const blobs = blobURLs(); let version = "v1"; const requested: string[] = []; const user = userEvent.setup();
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [{ ...pelican, sha256: version, revisions: version === "v1" ? 1 : 2 }] })),
    http.get("*/api/local/agent/tasks/file-task/files/:id", ({ request }) => { requested.push(new URL(request.url).search); return new HttpResponse(new URL(request.url).search === "?v=v1" ? svgV1 : svgV2); }));
  const view = render(<TaskArtifacts task={done} />);
  await user.click(await screen.findByRole("button", { name: "Open pelican.svg" }));
  await screen.findByRole("img", { name: "pelican.svg" });
  expect(await blobs[0].text()).toBe(svgV1);
  version = "v2";
  view.rerender(<TaskArtifacts task={{ ...done, revision: 9, result: "Updated pelican.svg.", events: [{ seq: 5, at: 5, type: "tool_result", tool: "ryn_file_edit", result: {} }] }} />);
  await waitFor(() => expect(requested).toEqual(["?v=v1", "?v=v2"]));
  await waitFor(() => expect(screen.getByRole("img", { name: "pelican.svg" })).toHaveAttribute("src", "blob:artifact-2"));
  expect(await blobs[1].text()).toBe(svgV2);
  expect(screen.getByRole("region", { name: "Result files" })).toHaveTextContent("2 versions");
});

it("recovers a transient failure and keeps collecting files after the model turn has ended", async () => {
  let calls = 0;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => {
    calls++;
    if (calls === 1) return HttpResponse.json({ detail: "temporarily_unavailable" }, { status: 503 });
    return HttpResponse.json({ artifacts: [], files: calls === 2 ? [] : [pelican], pending_files: calls === 2 });
  }));
  render(<TaskArtifacts task={done} />);
  expect(await screen.findByRole("button", { name: "Open pelican.svg" }, { timeout: 5000 })).toBeVisible();
  expect(calls).toBe(3);
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("keeps confirmed file links during a node outage and restores the listing automatically", async () => {
  let online = true;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => online
    ? HttpResponse.json({ artifacts: [artifact] })
    : HttpResponse.json({ detail: "agent_unavailable" }, { status: 503 })));
  const view = render(<TaskArtifacts task={done} />);
  await screen.findByRole("button", { name: "Open report.md" });
  online = false;
  view.rerender(<TaskArtifacts task={{ ...done, file_artifact_revision: 1 }} />);
  await screen.findByText("Could not load created files.");
  expect(screen.getByRole("button", { name: "Open report.md" })).toBeVisible();
  online = true;
  await waitFor(() => expect(screen.queryByText("Could not load created files.")).not.toBeInTheDocument(), { timeout: 3000 });
  expect(screen.getByRole("button", { name: "Open report.md" })).toBeVisible();
});

it("backs off unchanged active file listings and refreshes immediately on a file revision", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const listing = vi.spyOn(rynAgent, "artifacts").mockResolvedValue({ artifacts: [], files: [] });
  const view = render(<TaskArtifacts task={{ ...done, status: "running" }} />);
  try {
    await act(async () => {});
    expect(listing).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(4999));
    expect(listing).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(listing).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(9999));
    expect(listing).toHaveBeenCalledTimes(2);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(listing).toHaveBeenCalledTimes(3);
    view.rerender(<TaskArtifacts task={{ ...done, status: "running", file_artifact_revision: 1 }} />);
    await act(async () => {});
    expect(listing).toHaveBeenCalledTimes(4);
  } finally { view.unmount(); }
});

it("continues collecting pending shell files after the turn ends and stops once they arrive", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  const listing = vi.spyOn(rynAgent, "artifacts")
    .mockResolvedValueOnce({ artifacts: [], files: [], pending_files: true })
    .mockResolvedValue({ artifacts: [], files: [pelican as AgentFileArtifact], pending_files: false });
  const view = render(<TaskArtifacts task={done} />);
  try {
    await act(async () => {});
    await act(() => vi.advanceTimersByTimeAsync(1499));
    expect(listing).toHaveBeenCalledTimes(1);
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(screen.getByRole("button", { name: "Open pelican.svg" })).toBeVisible();
    await act(() => vi.advanceTimersByTimeAsync(30000));
    expect(listing).toHaveBeenCalledTimes(2);
  } finally { view.unmount(); }
});

it("recovers file delivery across ten independent task mounts without a manual refresh", async () => {
  const calls = new Map<string, number>();
  server.use(http.get("*/api/local/agent/tasks/:task/artifacts", ({ params }) => {
    const id = String(params.task); const count = (calls.get(id) || 0) + 1; calls.set(id, count);
    if (count === 1) return new HttpResponse(null, { status: 503 });
    return HttpResponse.json({ artifacts: [], files: [{ ...pelican, name: `${id}.svg` }] });
  }));
  for (let index = 0; index < 10; index++) {
    const id = `repeated-${index}`;
    const view = render(<TaskArtifacts task={{ ...done, id }} />);
    expect(await screen.findByRole("button", { name: `Open ${id}.svg` }, { timeout: 3000 })).toBeVisible();
    view.unmount();
    expect(calls.get(id)).toBe(2);
  }
}, 10000);

it("refreshes an ended task when a background artifact revision changes without tool events", async () => {
  let ready = false;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: ready ? [pelican] : [] })));
  const view = render(<TaskArtifacts task={{ ...done, file_artifact_revision: 0 }} />);
  await waitFor(() => expect(view.container).toBeEmptyDOMElement());
  ready = true;
  view.rerender(<TaskArtifacts task={{ ...done, file_artifact_revision: 1 }} />);
  expect(await screen.findByRole("button", { name: "Open pelican.svg" })).toBeVisible();
});

it("recovers a file changed between listing and preview without a manual refresh", async () => {
  const user = userEvent.setup(); let version = "v1"; const requested: string[] = [];
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [{ ...helper, role: "deliverable", sha256: version }] })),
    http.get("*/api/local/agent/tasks/file-task/files/:id", ({ request }) => {
      requested.push(new URL(request.url).search);
      if (version === "v1") { version = "v2"; return HttpResponse.json({ detail: "agent_artifact_content_changed" }, { status: 409 }); }
      return new HttpResponse("Current version after the write race");
    }));
  render(<TaskArtifacts task={done} />);
  await user.click(await screen.findByRole("button", { name: "Open notes.txt" }));
  expect(await screen.findByText("Current version after the write race")).toBeVisible();
  expect(requested).toEqual(["?v=v1", "?v=v2"]);
});

it("ignores an older task's delayed artifact response after switching tasks", async () => {
  let release: () => void = () => {}; let firstStarted = false; let firstReturned = false;
  const delayed = new Promise<void>(resolve => { release = resolve; });
  server.use(http.get("*/api/local/agent/tasks/older/artifacts", async () => {
    firstStarted = true; await delayed; firstReturned = true;
    return HttpResponse.json({ artifacts: [], files: [{ ...pelican, name: "older.svg" }] });
  }), http.get("*/api/local/agent/tasks/newer/artifacts", () => HttpResponse.json({ artifacts: [], files: [{ ...pelican, name: "newer.svg" }] })));
  const view = render(<TaskArtifacts task={{ ...done, id: "older" }} />);
  await waitFor(() => expect(firstStarted).toBe(true));
  view.rerender(<TaskArtifacts task={{ ...done, id: "newer" }} />);
  expect(await screen.findByRole("button", { name: "Open newer.svg" })).toBeVisible();
  release();
  await waitFor(() => expect(firstReturned).toBe(true));
  expect(screen.queryByRole("button", { name: "Open older.svg" })).not.toBeInTheDocument();
});

it("keeps a temporarily locked file visible and automatically rechecks it", async () => {
  let calls = 0;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [
    { ...pelican, status: ++calls === 1 ? "unavailable" : "unverified" }] })));
  render(<TaskArtifacts task={done} />);
  expect(await screen.findByText("Temporarily unreadable · retrying automatically")).toBeVisible();
  await waitFor(() => expect(screen.getByRole("button", { name: "Open pelican.svg" })).toBeEnabled(), { timeout: 3000 });
  expect(calls).toBe(2);
});

it("keeps native open/reveal available for a file above the preview limit", async () => {
  const user = userEvent.setup(); const actions: unknown[] = [];
  vi.stubGlobal("__TAURI_INTERNALS__", {});
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [{ ...pelican, status: "too_large" }] })),
    http.post("*/api/local/agent/tasks/file-task/files/file_pelican/actions", async ({ request }) => { actions.push(await request.json()); return HttpResponse.json({ opened: true }); }));
  render(<TaskArtifacts task={done} />);
  expect(await screen.findByRole("button", { name: "Open pelican.svg" })).toBeDisabled();
  await user.click(screen.getByLabelText("Open with · pelican.svg"));
  expect(screen.getByRole("button", { name: "Show in folder" })).toBeEnabled();
  await user.click(screen.getByRole("button", { name: "Default application" }));
  await waitFor(() => expect(actions).toEqual([{ action: "open" }]));
});


it("does not render a ready-files card for unmentioned shell command scratch files and keeps refresh errors in the same section", async () => {
  const scratch1 = {
    ...helper, id: "file_brief", name: "skills_brief.txt", path: "skills_brief.txt",
    source: "command", attribution: "observed_during_command", role: "supporting", mentioned_in_answer: false,
  } as AgentFileArtifact;
  const scratch2 = {
    ...helper, id: "file_list", name: "skills_list.json", path: "skills_list.json",
    source: "command", attribution: "observed_during_command", role: "supporting", mentioned_in_answer: false,
  } as AgentFileArtifact;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [scratch1, scratch2] })));
  const view = render(<TaskArtifacts task={{ ...done, status: "running", result: "" }} />);
  await waitFor(() => expect(screen.queryByText(/files? ready/i)).not.toBeInTheDocument());
  expect(view.container.querySelector("section.ra-files")).toBeNull();

  // When a real deliverable exists and a subsequent refresh fails, only one section is rendered.
  let fail = false;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => fail
    ? HttpResponse.json({ detail: "agent_unavailable" }, { status: 503 })
    : HttpResponse.json({ artifacts: [], files: [pelican, scratch1] })));
  view.rerender(<TaskArtifacts task={{ ...done, file_artifact_revision: 1 }} />);
  expect(await screen.findByRole("button", { name: "Open pelican.svg" })).toBeVisible();
  fail = true;
  view.rerender(<TaskArtifacts task={{ ...done, file_artifact_revision: 2 }} />);
  expect(await screen.findByText("Could not load created files.")).toBeVisible();
  expect(view.container.querySelectorAll("section.ra-files")).toHaveLength(1);
});

it("hides helper scripts and places earlier turn deliverables inline at their turn", async () => {
  const turn0Pdf = {
    ...pelican, id: "file_pdf0", name: "Payoneer_fixed.pdf", path: "Payoneer_fixed.pdf",
    media_type: "application/pdf", preview: "download", role: "deliverable",
    source: "command", attribution: "observed_during_command", turn_index: 0,
  } as AgentFileArtifact;
  const helperPy = {
    ...helper, id: "file_py", name: "geom_probe2.py", path: "geom_probe2.py",
    role: "supporting", turn_index: 0,
  } as AgentFileArtifact;
  const helperJson = {
    ...helper, id: "file_json", name: "verify_payoneer.json", path: "verify_payoneer.json",
    role: "supporting", source: "command", attribution: "observed_during_command", turn_index: 0,
  } as AgentFileArtifact;
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () =>
    HttpResponse.json({ artifacts: [], files: [turn0Pdf, helperPy, helperJson] })));
  const view = render(
    <TaskArtifacts task={done} maxTurnIndex={1}>
      <div data-testid="turn-0">
        <p>Turn 0 response</p>
        <TurnDeliverables turnIndex={0} />
      </div>
      <div data-testid="turn-1">
        <p>Turn 1 response</p>
      </div>
    </TaskArtifacts>,
  );
  const pdfBtn = await screen.findByRole("button", { name: "Open Payoneer_fixed.pdf" });
  expect(view.getByTestId("turn-0")).toContainElement(pdfBtn);
  expect(screen.queryByText(/geom_probe2\.py|verify_payoneer\.json/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Observed during command|命令运行期间发现/)).not.toBeInTheDocument();
  expect(view.container.querySelectorAll("section.ra-files")).toHaveLength(1);
});


it("waits for both turn completion and background file production before presenting current files", async () => {
  let pending = true;
  const loaded = vi.fn();
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => {
    loaded();
    return HttpResponse.json({ artifacts: [artifact], files: [pelican], pending_files: pending });
  }));
  const view = render(<TaskArtifacts task={{ ...done, status: "running", result: "" }} />);
  await waitFor(() => expect(loaded).toHaveBeenCalled());
  expect(screen.queryByRole("button", { name: "Open pelican.svg" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Open report.md" })).not.toBeInTheDocument();
  view.rerender(<TaskArtifacts task={done} />);
  await waitFor(() => expect(loaded).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole("button", { name: "Open pelican.svg" })).not.toBeInTheDocument();
  pending = false;
  view.rerender(<TaskArtifacts task={{ ...done, file_artifact_revision: 1 }} />);
  expect(await screen.findByRole("button", { name: "Open pelican.svg" })).toBeVisible();
  expect(await screen.findByRole("button", { name: "Open report.md" })).toBeVisible();
  view.rerender(<TaskArtifacts task={{ ...done, run_state: "running", file_artifact_revision: 1 }} />);
  expect(screen.queryByRole("button", { name: "Open pelican.svg" })).not.toBeInTheDocument();
});

it("keeps script implementation in collapsed activity without claiming a tool request succeeded", async () => {
  const user = userEvent.setup();
  const messages = [{ role: "assistant", content: "", tool_calls: [{ id: "write", function: {
    name: "ryn_file_write", arguments: JSON.stringify({ path: "redact_emails.py", content: "print('redact')" }),
  } }] }];
  render(<RynAgentProgress task={{ ...done, status: "running", events: [] }} messages={messages} />);
  const script = screen.getByText("redact_emails.py");
  expect(script).not.toBeVisible();
  expect(screen.queryByText("Edited")).not.toBeInTheDocument();
  expect(screen.queryByText("+1")).not.toBeInTheDocument();
  await user.click(screen.getByText("View activity"));
  expect(script).toBeVisible();
});


it("exports the absolute local path through native drag and keeps the floating menu above a low card", async () => {
  const user = userEvent.setup();
  const invoke = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("__TAURI_INTERNALS__", { invoke });
  const local = { ...pelican, absolute_path: "C:\\工作 文件\\report, final.svg" };
  server.use(http.get("*/api/local/agent/tasks/file-task/artifacts", () => HttpResponse.json({ artifacts: [], files: [local] })));
  render(<TaskArtifacts task={done} />);
  const name = await screen.findByRole("button", { name: "Open pelican.svg" });
  expect(name.closest(".ra-file-card")).toHaveAttribute("draggable", "true");
  fireEvent.dragStart(name);
  await waitFor(() => expect(invoke).toHaveBeenCalledWith("drag_local_file", { path: local.absolute_path }, undefined));
  const anchor = screen.getByLabelText("Open with · pelican.svg").parentElement!;
  vi.spyOn(anchor, "getBoundingClientRect").mockReturnValue({ top: 660, bottom: 690, right: 600, left: 500, width: 100, height: 30, x: 500, y: 660, toJSON() {} });
  await user.click(screen.getByLabelText("Open with · pelican.svg"));
  const options = (await screen.findByRole("button", { name: "Default application" })).parentElement!;
  expect(options.parentElement).toBe(document.body);
  expect(parseFloat(options.style.top)).toBeLessThan(660);
  await user.keyboard("{Escape}");
  await waitFor(() => expect(screen.queryByRole("button", { name: "Default application" })).not.toBeInTheDocument());
});
