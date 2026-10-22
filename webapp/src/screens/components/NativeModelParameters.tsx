import { useState } from "react";
import { nativeModelRequest, type NativeModelStatus } from "../../domain/nativeModel";
import { useUILanguage } from "../../uiI18n";

type Configuration = NonNullable<NativeModelStatus["model_settings"]>["configuration"];
type Field = keyof Configuration;
const draftOf = (value: Configuration): Record<Field, string> => ({
  max_output_tokens: String(value.max_output_tokens), max_concurrent: String(value.max_concurrent),
  requests_per_day: value.requests_per_day === null ? "" : String(value.requests_per_day),
});

export default function NativeModelParameters({ status, busy, onBusy, onUpdate }: {
  status: NativeModelStatus; busy: boolean; onBusy: (value: boolean) => void; onUpdate: (value: NativeModelStatus) => void;
}) {
  const zh = useUILanguage().startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const [snapshot, setSnapshot] = useState(status);
  const [draft, setDraft] = useState(() => status.model_settings ? draftOf(status.model_settings.configuration) : null);
  const [edited, setEdited] = useState<Field[]>([]);
  const [uncertain, setUncertain] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const settings = snapshot.model_settings;
  if (!settings || !draft) return <p>{text("当前后端未提供本机模型参数。", "This backend does not expose native model parameters.")}</p>;
  const changed = snapshot.id !== status.id || settings.rule_revision !== status.model_settings?.rule_revision;
  const stale = uncertain || changed;
  const fields: Array<[Field, string, number, number]> = [
    ["max_output_tokens", text("最大输出 Token", "Maximum output tokens"), 1, Math.min(settings.context_window || 393216, 393216)],
    ["max_concurrent", text("并发请求上限", "Concurrent request limit"), 1, 2500],
    ["requests_per_day", text("每日请求上限", "Daily request limit"), 1, 1000000],
  ];
  const adopt = (next: NativeModelStatus) => {
    if (next.id !== snapshot.id || !next.installed || !next.model_settings) throw new Error("model_changed");
    setSnapshot(next); setDraft(draftOf(next.model_settings.configuration)); setEdited([]); setUncertain(false); onUpdate(next);
  };
  const reload = async () => {
    if (busy) return;
    onBusy(true); setError(""); setSaved(false);
    try { adopt(await nativeModelRequest()); }
    catch { setUncertain(true); setError(text("无法读取当前模型参数。如果模型已更换，请关闭后重新打开。", "Could not read current parameters. If the model changed, close and reopen this panel.")); }
    finally { onBusy(false); }
  };
  const save = async () => {
    if (busy || stale || !edited.length) return;
    setError(""); setSaved(false);
    const patch: Partial<Configuration> = {};
    for (const [name, , min, max] of fields) {
      if (!edited.includes(name)) continue;
      const raw = draft[name].trim();
      if (name === "requests_per_day" && !raw) { patch.requests_per_day = null; continue; }
      const value = Number(raw);
      if (!raw || !Number.isInteger(value) || value < min || value > max) {
        setError(text("请核对数值范围，只有每日请求上限可以留空。", "Check numeric ranges. Only the daily request limit may be blank.")); return;
      }
      patch[name] = value;
    }
    onBusy(true);
    try {
      adopt(await nativeModelRequest("model-config", { expected_rule_revision: settings.rule_revision, configuration: patch }, "PUT"));
      setSaved(true);
    } catch (reason) {
      setUncertain(true);
      setError((reason as Error).message === "native_model_rule_revision_conflict"
        ? text("模型参数已变化，请重新读取后核对。旧编辑不会自动覆盖新参数。", "Model parameters changed. Reload and review them; old edits will not overwrite new parameters.")
        : text("保存未确认，请重新读取实际参数后再编辑。", "Save was not confirmed. Reload actual parameters before editing again."));
    } finally { onBusy(false); }
  };
  return <section className="native-model-parameters" aria-label={text("本机模型参数", "Native model parameters")}>
    <h4>{text("此模型参数", "Parameters for this model")}</h4>
    <p>{text("应用于本机调用与设备分享。不会启动模型或开启分享。", "Applies to local calls and device sharing. Saving does not start the model or enable sharing.")}</p>
    {settings.context_window && <p>{text("模型上下文窗口", "Model context window")}: {settings.context_window.toLocaleString()}</p>}
    <div className="native-model-parameter-fields">{fields.map(([name, label, min, max]) => <label key={name}>{label}
      <input type="number" aria-label={label} min={min} max={max} value={draft[name]} disabled={busy || stale}
        placeholder={name === "requests_per_day" ? text("不限", "Unlimited") : undefined}
        onChange={event => { setDraft(current => current && ({ ...current, [name]: event.target.value })); setEdited(current => current.includes(name) ? current : [...current, name]); setSaved(false); }} />
      <small>{text("当前生效", "Currently effective")}: {settings.configuration[name] ?? text("不限", "Unlimited")}</small>
    </label>)}</div>
    <p>{text("每日上限按 UTC 日统计实际派发次数，失败或取消也计数，重启保留。留空为不限；不是 Token 或费用额度。", "Daily limits count actual dispatch attempts per UTC day, including failed or cancelled calls, and survive restarts. Blank means unlimited. This is not a token or cost limit.")}</p>
    {(error || changed) && <p role="alert">{error || text("模型配置已变化，请重新读取。", "Model configuration changed. Reload before editing.")}</p>}
    {saved && <p role="status">{text("参数已保存。", "Parameters saved.")}</p>}
    <div className="native-model-parameter-actions"><button className="ais-secondary" disabled={busy} onClick={() => void reload()}>{text("重新读取", "Reload")}</button>
      <button className="ais-primary" disabled={busy || stale || !edited.length} onClick={() => void save()}>{text("保存此模型参数", "Save this model's parameters")}</button></div>
  </section>;
}
