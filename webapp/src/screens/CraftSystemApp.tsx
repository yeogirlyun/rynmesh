import { useCallback, useEffect, useRef, useState } from "react";
import { MonitorCheck } from "lucide-react";
import { systemApps, type CraftPlugin, type SystemApp } from "../domain/craftPlugins";

type Props = {
  app: CraftPlugin;
  /** Translates a key of the craft namespace. */
  text: (key: string, options?: Record<string, unknown>) => string;
};

const message = (error: unknown) => error instanceof Error ? error.message : String(error);

/**
 * Whether the editor really is on this computer (Start menu or Applications, "Open with"), with a
 * retry when that failed and, at the user's request, the default-app choice. The desktop app
 * reports what exists, never what an installed plugin should have.
 */
export default function CraftSystemApp({ app, text }: Props) {
  const [info, setInfo] = useState<SystemApp | null>(null);
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState("");
  const [choice, setChoice] = useState<"" | "busy" | "done" | "failed">("");
  const integrated = app.web && app.installed && app.enabled;
  const mounted = useRef(true);
  useEffect(() => () => { mounted.current = false; }, []);
  const load = useCallback(async () => {
    const value = await systemApps.info(app.id).catch(() => null);
    if (mounted.current) setInfo(value);
  }, [app.id]);
  useEffect(() => {
    setInfo(null); setAdding(false); setAddError(""); setChoice("");
    if (integrated) void load();
  }, [integrated, load]);
  // A sync is running: look again until it has reported.
  useEffect(() => {
    if (info?.state !== "pending") return;
    const timer = window.setTimeout(() => void load(), 1000);
    return () => window.clearTimeout(timer);
  }, [info, load]);
  if (!info) return null;
  const place = text(`systemApp.where.${info.place}`);
  const types = info.documents.join(" ");
  const add = async () => {
    setAdding(true); setAddError("");
    try { await systemApps.sync(); }
    catch (error) { if (mounted.current) setAddError(message(error)); }
    await load();
    if (mounted.current) setAdding(false);
  };
  const choose = async () => {
    setChoice("busy");
    try { await systemApps.chooseDefault(app.id); setChoice("done"); }
    catch { setChoice("failed"); }
  };
  const error = info.error || addError;
  return <section className="craft-system-app" aria-label={text("systemApp.title")} aria-busy={info.state === "pending" || adding}>
    <h3><MonitorCheck size={16} aria-hidden="true" />{text("systemApp.title")}</h3>
    {info.state === "ready" ? <>
      <p>{text(`systemApp.${info.place}`, { name: info.name })}</p>
      {info.documents.length > 0 && <>
        <p>{text("systemApp.openWith", { name: info.name, types })}</p>
        <button type="button" className="craft-button" disabled={choice === "busy"} onClick={() => void choose()}>{text(`systemApp.${info.default_choice}`)}</button>
        {choice === "done" && <p role="status">{text(info.default_choice === "settings" ? "systemApp.settingsOpened" : "systemApp.defaultSet", { name: info.name, types })}</p>}
        {choice === "failed" && <p role="alert">{text("systemApp.failed")}</p>}
      </>}
    </> : info.state === "pending" ? <p role="status">{text("systemApp.pending", { name: info.name, place })}</p> : <>
      <p role={info.state === "failed" ? "alert" : undefined}>{text(info.state === "failed" ? "systemApp.notAdded" : "systemApp.missing", { name: info.name, place })}</p>
      {error && <p className="craft-system-app-error">{text("systemApp.error", { error })}</p>}
      <button type="button" className="craft-button" disabled={adding} onClick={() => void add()}>
        {text(adding ? "systemApp.adding" : info.state === "failed" ? "systemApp.retry" : "systemApp.add", { place })}
      </button>
    </>}
  </section>;
}
