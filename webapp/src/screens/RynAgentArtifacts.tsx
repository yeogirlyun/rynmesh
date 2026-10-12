import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { rynAgent, type AgentArtifact, type AgentTask } from "../domain/rynAgent";
import { NasPreview } from "../personal/NasPreview";
import "../personal/nas.css";

export function TaskArtifacts({ task }: { task: AgentTask }) {
  const { t } = useTranslation("rynAgent");
  const [items, setItems] = useState<AgentArtifact[]>([]);
  const [chosen, setChosen] = useState<AgentArtifact | null>(null);
  const [error, setError] = useState(false);
  const [reload, setReload] = useState(0);
  const writes = task.events.filter(e => e.type === "tool_result" && e.tool === "ryn_create_text_file").length;
  useEffect(() => {
    const controller = new AbortController(); setChosen(null); setItems([]); setError(false);
    void rynAgent.artifacts(task.id, controller.signal).then(value => {
      if (!controller.signal.aborted) setItems(value.artifacts);
    }).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [task.id, task.control_revision, task.scope_revision, writes, reload]);
  const readText = useCallback(async (signal: AbortSignal) => {
    if (!chosen) throw new Error(t("artifactReadError"));
    try { return (await rynAgent.artifact(task.id, chosen.id, signal)).text; }
    catch { throw new Error(t("artifactReadError")); }
  }, [task.id, chosen, t]);
  if (!items.length && !error) return null;
  return <section className="ra-result ra-artifacts" aria-label={t("artifacts")}>
    <strong>{t("artifacts")}</strong><p>{t("artifactsHelp")}</p>
    {error && <p role="alert">{t("artifactListError")} <button onClick={() => setReload(n => n + 1)}>{t("retry")}</button></p>}
    {items.map(item => <button key={item.id} onClick={() => setChosen(item)}>{t("openArtifact", { name: item.name })}</button>)}
    {chosen && <NasPreview key={`${task.id}/${task.control_revision}/${task.scope_revision}/${chosen.id}`} source={{ id: task.id, name: t("artifacts"), allow_ai: false }}
      entry={{ name: chosen.name, path: chosen.name, directory: false, size: chosen.bytes, modified: chosen.created_at || "" }}
      onClose={() => setChosen(null)} download={() => {}} askAi={() => {}} busy={false} readText={readText} readOnly />}
  </section>;
}
