import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { nodeControlUrl } from "../domain/nodeUrl";
import type { AgentTask } from "../domain/rynAgent";
import "./RynAgentFork.css";

type Boundary = { message_count: number; token: string; role: string; preview: string };
async function request<T>(id: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(nodeControlUrl(`/agent/tasks/${encodeURIComponent(id)}/${path}`), {
    method: body === undefined ? "GET" : "POST", credentials: "include", cache: "no-store",
    headers: { "Content-Type": "application/json" }, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "agent_fork_unavailable");
  return data;
}

export function RynAgentForkSource({ task, onChoose }: { task: AgentTask; onChoose: (id: string) => void }) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  if (!task.fork) return null;
  return <section className="ra-fork"><div role="note">
    <p>{zh ? "来源：" : "Forked from: "}<button type="button" onClick={() => onChoose(task.fork!.source_task_id)}>{task.fork.source_title}</button>
      {zh ? `，第 ${task.fork.message_count} 条消息之后` : `, after message ${task.fork.message_count}`}</p>
    <p>{zh ? "会话独立，但文件共享。" : "Independent conversation, shared files."}</p>
  </div></section>;
}

export default function RynAgentFork({ task, onCreated, onChoose, showSource = true }: {
  task: AgentTask; onCreated: (task: AgentTask) => void; onChoose: (id: string) => void; showSource?: boolean;
}) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const [open, setOpen] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [choices, setChoices] = useState<Boundary[]>([]), [count, setCount] = useState(0);
  const [title, setTitle] = useState(`${task.title.slice(0, 145)}${zh ? " · 分叉" : " · Fork"}`);
  // Keep the exact request after an uncertain HTTP response. Editing the form
  // cannot accidentally create a second child until the first request resolves.
  const pendingKey = `ryn.fork.pending:${nodeControlUrl("/agent")}:${task.id}`;
  const pending = useRef<Record<string, unknown> | null>((() => {
    try { return JSON.parse(sessionStorage.getItem(pendingKey) || "null"); } catch { return null; }
  })());
  function retain(value: Record<string, unknown> | null) {
    pending.current = value;
    try { if (value) sessionStorage.setItem(pendingKey, JSON.stringify(value)); else sessionStorage.removeItem(pendingKey); } catch { /* In-memory retry still works. */ }
  }
  const warning = zh ? "会话独立，但文件共享。此操作不隔离工作区，也不撤销外部服务操作。创建后不会执行，发送新指令才会运行。" : "Independent conversation, shared files. This does not isolate the workspace or undo external services. The fork runs only after you send a new instruction.";
  async function show() {
    setOpen(true); setBusy(true); setError("");
    if (pending.current) { setTitle(String(pending.current.title)); setCount(Number(pending.current.message_count)); }
    try {
      const result = await request<{ boundaries: Boundary[] }>(task.id, "fork-boundaries");
      setChoices(result.boundaries);
      if (!pending.current) setCount(result.boundaries.at(-1)?.message_count || 0);
    } catch (err) { setError(String(err)); }
    finally { setBusy(false); }
  }
  async function create() {
    const boundary = choices.find(item => item.message_count === count);
    if (!boundary && !pending.current) return;
    if (!pending.current) retain({ client_request_id: crypto.randomUUID(), title: title.trim(), message_count: count,
      token: boundary!.token, shared_workspace_confirmed: true });
    setBusy(true); setError("");
    try {
      const next = await request<AgentTask>(task.id, "fork", pending.current);
      retain(null); setOpen(false); onCreated(next);
    } catch (err) {
      setError(String(err));
      // A definite server rejection committed no child; allow correcting inputs.
      if (err instanceof Error && err.message.startsWith("agent_") && err.message !== "agent_fork_storage_unavailable") retain(null);
    } finally { setBusy(false); }
  }
  return <section className="ra-fork">
    {showSource && <RynAgentForkSource task={task} onChoose={onChoose} />}
    <button type="button" onClick={event => { const menu = event.currentTarget.closest("details.ra-chat-menu"); if (menu instanceof HTMLDetailsElement) menu.open = false; void show(); }}>{zh ? "分叉任务" : "Fork task"}</button>
    {open && createPortal(<div className="ra-modal-backdrop"><section className="ra-modal ra-fork-dialog" role="dialog" aria-modal="true" aria-label={zh ? "分叉任务" : "Fork task"}>
      <h2>{zh ? "分叉任务" : "Fork task"}</h2>
      <p>{warning}</p>
      <p>{zh ? "只列出已保存且工具调用与结果完整配对的位置。历史操作不会重放。" : "Only saved boundaries with complete tool results are listed. Historical operations will not replay."}</p>
      <label>{zh ? "新任务标题" : "New task title"}
        <input aria-label={zh ? "新任务标题" : "New task title"} maxLength={160} value={title} disabled={busy || !!pending.current} onChange={e => setTitle(e.target.value)} />
      </label>
      <label>{zh ? "分叉位置" : "Fork position"}
        <select aria-label={zh ? "分叉位置" : "Fork position"} value={count} disabled={busy || !!pending.current} onChange={e => setCount(Number(e.target.value))}>
          {choices.map(item => <option key={item.message_count} value={item.message_count}>#{item.message_count} · {item.role} · {item.preview}</option>)}
        </select>
      </label>
      {!busy && !choices.length && <p>{zh ? "暂无完整的已保存边界。" : "No complete saved boundary is available."}</p>}
      {error.includes("reference_requires_source_task") && <p>{zh ? "历史包含绑定来源任务的资源或桌面观察引用，当前不能迁移；请在这些操作之前的位置分叉。" : "History contains task-bound resource or desktop observation references that cannot be transferred. Choose an earlier boundary."}</p>}
      {error.includes("reference_limit") && <p>{zh ? "复制的引用总量超过 40 MiB 或 256 个文件，请选择更早的位置。" : "Copied references exceed 40 MiB or 256 files. Choose an earlier boundary."}</p>}
      {error.includes("attachment_unavailable") && <p>{zh ? "附件缺失或已被修改，请恢复原附件，或选择附件之前的位置。" : "An attachment is missing or changed. Restore it or choose an earlier boundary."}</p>}
      {error && <p role="alert">{zh ? "创建未完成，请检查位置、权限及附件/结果引用后重试：" : "Creation did not complete. Check the boundary, permissions and attachment/result references, then retry: "}{error}</p>}
      <button type="button" disabled={busy || !count || !title.trim()} onClick={() => void create()}>{busy ? (zh ? "处理中…" : "Working…") : (zh ? "创建并打开分叉" : "Create and open fork")}</button>{" "}
      <button type="button" disabled={busy} onClick={() => setOpen(false)}>{zh ? "关闭" : "Close"}</button>
    </section></div>, document.body)}
  </section>;
}
