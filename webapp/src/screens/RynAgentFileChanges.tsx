import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { agentFileChanges, type ChangeReview, type UndoPreview, type UndoResult } from "../domain/agentFileChanges";
import "./RynAgentFileChanges.css";

export default function RynAgentFileChanges({ taskId, revision }: { taskId: string; revision: number }) {
  const { i18n } = useTranslation();
  const zh = i18n.language.startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const reason = (code: string) => ({
    file_changed: text("文件已被用户或其他任务再次修改，保留当前内容。", "The file changed after this task; current content is preserved."),
    intervening_changes: text("本任务多次修改之间存在其他修改，无法整段安全撤销。", "Other edits occurred between this task's writes; combined undo is unsafe."),
    interrupted_file_operation: text("文件操作中断，修改归属未确认。", "File operation interrupted; ownership is unconfirmed."),
    snapshot_missing: text("快照缺失或损坏。", "Snapshot is missing or damaged."),
  }[code] || code);
  const [open, setOpen] = useState(false);
  const [review, setReview] = useState<ChangeReview | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [preview, setPreview] = useState<{ data: UndoPreview; ids?: string[]; requestId: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [result, setResult] = useState<UndoResult | null>(null);
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    agentFileChanges.review(taskId).then(data => { if (!disposed) setReview(data); })
      .catch(err => { if (!disposed) setError(String(err.message)); });
    return () => { disposed = true; };
  }, [taskId, revision, open]);
  async function load() { setReview(await agentFileChanges.review(taskId)); }
  async function prepare(ids?: string[]) {
    setBusy(true); setError(""); setResult(null); setPreview(null);
    try { setPreview({ data: await agentFileChanges.preview(taskId, ids), ids, requestId: crypto.randomUUID() }); }
    catch (err) { setError(String((err as Error).message)); }
    finally { setBusy(false); }
  }
  async function undo() {
    if (!preview) return;
    setBusy(true); setError("");
    try {
      const value = await agentFileChanges.undo(taskId, preview.data.token, preview.requestId, preview.ids);
      setResult(value); setPreview(null); setSelected([]); await load();
    } catch (err) {
      // Keep the same idempotency key for a lost-response retry; read durable progress.
      setError(`${text("结果未确认，请刷新检查操作记录；重试使用同一操作号。", "Result unconfirmed. Refresh operation history; retry uses the same request ID.")} ${(err as Error).message}`);
      await load().catch(() => undefined);
    } finally { setBusy(false); }
  }
  const labels: Record<string, string> = { created: text("新建", "Created"), modified: text("修改", "Modified"), deleted: text("删除", "Deleted") };
  const resultView = (value: UndoResult) => <div className="ra-file-change-result" role="status">
    <strong>{value.status === "completed" ? text("撤销完成", "Undo completed") : value.status === "conflict" ? text("冲突：未执行撤销", "Conflict: undo not applied") : text("撤销中断，请检查逐文件结果", "Undo interrupted: inspect each file result")}</strong>
    {value.completed.length > 0 && <p>{text("已恢复：", "Restored: ")}{value.completed.join(", ")}</p>}
    {value.remaining?.map(item => <p key={item.path}>{item.path} — {item.state}</p>)}
    {value.conflicts?.map(item => <p key={item.path}>{item.path} — {reason(item.reason)}</p>)}
  </div>;
  return <section className="ra-file-changes" aria-label={text("任务文件修改", "Task file changes")}>
    <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{text("查看文件修改与撤销", "Review file changes and undo")}</button>
    {open && <>
      <p>{text("只撤销本任务文件工具的修改；会话历史和外部服务操作保持原样。重命名两端一起撤销。", "Undo this task's file-tool changes. Conversation history and external service actions remain. Rename endpoints are restored together.")}</p>
      <p className="ra-file-change-muted">{text("快照限额：单文件 2 MiB、每任务 8 MiB / 64 路径 / 256 次操作，总计 256 MiB。超限在修改前拒绝。", "Snapshot limits: 2 MiB per file, 8 MiB / 64 paths / 256 operations per task, 256 MiB total. Over-limit writes are rejected before modification.")}</p>
      <button disabled={busy} onClick={() => { setError(""); load().catch(err => setError(err.message)); }}>{text("刷新修改列表", "Refresh changes")}</button>
      {error && <p role="alert">{error}</p>}
      {!review && !error && <p>{text("读取快照中…", "Loading snapshots…")}</p>}
      {review?.task_busy && <p>{text("请先暂停任务，待工具和后台进程停止后再撤销。", "Pause the task and stop its tools/background processes before undo.")}</p>}
      {review?.files.map(file => <article key={file.path}>
        <div className="ra-file-change-heading"><label><input type="checkbox" checked={selected.includes(file.id)} disabled={busy || !file.can_undo}
          onChange={ev => { setPreview(null); setSelected(ev.target.checked ? [...new Set([...selected, file.id])] : selected.filter(id => id !== file.id)); }} />
          <strong>{file.path}</strong></label><span>{labels[file.status] || file.status}{file.rename_group ? text(" · 重命名关联", " · Linked rename") : ""}</span></div>
        <small>{file.problem ? text("文件操作记录（状态待核实）", "File operation record (unconfirmed)") : text("确认文件工具修改", "Confirmed file-tool change")} · {file.before_bytes ?? "?"} → {file.after_bytes ?? "?"} B · {file.media_type}</small>
        {(file.conflict || file.problem) && <p role="alert">{text("不可安全撤销：", "Cannot safely undo: ")}{reason(file.conflict || file.problem || "")}</p>}
        {file.binary ? <p>{text("二进制文件：不提供文本 diff", "Binary file: no text diff")}</p> : <details><summary>{text("查看 diff", "View diff")}</summary><pre tabIndex={0}>{file.diff || text("无文本差异", "No text difference")}</pre>{file.diff_truncated && <p>{text("diff 预览已截断；撤销使用完整快照。", "Diff preview truncated; undo uses complete snapshots.")}</p>}</details>}
        <button disabled={busy || !file.can_undo || review.task_busy} onClick={() => prepare([file.id])}>{text("预览撤销此文件", "Preview undo for this file")}</button>
      </article>)}
      {review && !review.files.length && <p>{text("没有待撤销的已确认文件修改。", "No confirmed file changes to undo.")}</p>}
      {!!review?.files.length && <div className="ra-file-change-actions">
        <button disabled={busy || !selected.length || review.task_busy} onClick={() => prepare(selected)}>{text("预览撤销所选", "Preview undo selected")}</button>
        <button disabled={busy || review.task_busy} onClick={() => prepare()}>{text("预览撤销本任务全部文件", "Preview undo all task files")}</button>
      </div>}
      {preview && <section className="ra-file-change-preview" aria-label={text("撤销预览", "Undo preview")}>
        <h4>{text("撤销预览", "Undo preview")}</h4>
        {preview.data.items.map(item => <p key={item.path}>{item.path} — {item.before.sha256 === null ? text("删除本任务新建文件", "Remove task-created file") : text("恢复修改前快照", "Restore original snapshot")} ({item.after.bytes} → {item.before.bytes} B)</p>)}
        {preview.data.conflicts.map(item => <p role="alert" key={item.path}>{text("冲突，整批未执行：", "Conflict, entire batch blocked: ")}{item.path} — {reason(item.reason)}</p>)}
        <button disabled={busy || !preview.data.can_undo} onClick={undo}>{text("确认撤销这些文件", "Confirm undo these files")}</button>
        <button disabled={busy} onClick={() => setPreview(null)}>{text("取消", "Cancel")}</button>
      </section>}
      {result && resultView(result)}
      {!!review?.undo_operations?.length && <details><summary>{text("持久化撤销记录", "Saved undo history")}</summary>{review.undo_operations.map(value => <div key={value.id}>{resultView(value)}</div>)}</details>}
      {!!review?.observed?.length && <details><summary>{text("命令期间观察到的变化（归属未确认，不可撤销）", "Changes observed during commands (ownership unconfirmed, cannot undo)")}</summary>{review.observed.map((file, i) => <p key={i}>{file.path} — {file.status}</p>)}</details>}
    </>}
  </section>;
}
