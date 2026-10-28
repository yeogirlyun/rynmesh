import { useMemo, useState } from "react";
import {
  formatTokenCount,
  resetModelUsage,
  sourceRequest,
  type AIModelParameters,
  type AISource,
  type ModelTokenUsage,
} from "../domain/aiSources";
import { Modal } from "../personal/components";
import { useUILanguage } from "../uiI18n";

export function buildDailySeries(
  usages: Array<ModelTokenUsage | undefined>,
  daysCount: 7 | 30 = 7,
): Array<{ day: string; label: string; isToday: boolean; tokens: number }> {
  const anchorIso =
    usages.map(u => u?.today?.day).find((d): d is string => Boolean(d && /^\d{4}-\d{2}-\d{2}$/.test(d))) ||
    new Date().toISOString().slice(0, 10);
  const [y, m, d] = anchorIso.split("-").map(Number);
  const anchorUtc = Date.UTC(y, (m || 1) - 1, d || 1);

  const byDay = new Map<string, number>();
  for (const u of usages) {
    if (!u) continue;
    const seenForModel = new Set<string>();
    for (const item of u.daily || []) {
      if (!item?.day) continue;
      const tok = Math.max(0, Number(item.total_tokens ?? item.tokens ?? 0));
      byDay.set(item.day, (byDay.get(item.day) || 0) + tok);
      seenForModel.add(item.day);
    }
    const todayKey = u.today?.day || anchorIso;
    const todayTok = Math.max(0, Number(u.today?.total_tokens || 0));
    if (todayTok > 0 && !seenForModel.has(todayKey)) {
      byDay.set(todayKey, (byDay.get(todayKey) || 0) + todayTok);
    }
  }

  const result: Array<{ day: string; label: string; isToday: boolean; tokens: number }> = [];
  for (let i = daysCount - 1; i >= 0; i--) {
    const dt = new Date(anchorUtc - i * 86_400_000);
    const iso = dt.toISOString().slice(0, 10);
    const mm = String(dt.getUTCMonth() + 1).padStart(2, "0");
    const dd = String(dt.getUTCDate()).padStart(2, "0");
    result.push({
      day: iso,
      label: `${mm}/${dd}`,
      isToday: i === 0,
      tokens: byDay.get(iso) || 0,
    });
  }
  return result;
}

export function ModelTokenUsageSummaryCard({
  usage,
  busy,
  onReset,
}: {
  usage?: ModelTokenUsage;
  busy?: boolean;
  onReset?: (scope: "today" | "all") => Promise<void>;
}) {
  const zh = useUILanguage().startsWith("zh");
  const text = (cn: string, en: string) => (zh ? cn : en);
  const series = useMemo(() => (usage ? buildDailySeries([usage], 7) : []), [usage]);
  if (!usage) return null;
  const todayTokens = usage.today?.total_tokens || 0;
  const totalTokens = usage.total?.total_tokens || 0;
  const sevenDayTokens = series.reduce((sum, item) => sum + item.tokens, 0);
  const maxBar = Math.max(1, ...series.map(item => item.tokens));

  return (
    <section className="mp-codex-usage-card mp-codex-usage-compact" aria-label={text("Token 每日用量", "Daily token usage")}>
      <div className="mp-codex-stats">
        <div className="mp-codex-stat">
          <span>{text("今日用量", "Today")}</span>
          <strong>{formatTokenCount(todayTokens)}</strong>
        </div>
        <div className="mp-codex-stat">
          <span>{text("近 7 天", "Last 7 days")}</span>
          <strong>{formatTokenCount(sevenDayTokens)}</strong>
        </div>
        <div className="mp-codex-stat">
          <span>{text("累计总计", "Total")}</span>
          <strong>{formatTokenCount(totalTokens)}</strong>
        </div>
        {onReset && (todayTokens > 0 || totalTokens > 0) && (
          <button type="button" className="mp-link-btn mp-codex-reset" disabled={busy} onClick={() => void onReset("today")}>
            {text("清零今日", "Reset today")}
          </button>
        )}
      </div>
      <div className="mp-codex-bars" role="img" aria-label={text("近 7 天每日用量", "Last 7 days daily token usage")}>
        {series.map(item => {
          const heightPct = item.tokens > 0 ? Math.max(10, Math.round((item.tokens / maxBar) * 100)) : 3;
          return (
            <div key={item.day} className={`mp-codex-bar-col${item.isToday ? " is-today" : ""}`} title={`${item.day}: ${item.tokens.toLocaleString()} tokens`}>
              <small className="mp-codex-bar-val">{item.tokens > 0 ? formatTokenCount(item.tokens) : "0"}</small>
              <div className="mp-codex-bar-track">
                <div className="mp-codex-bar-fill" style={{ height: `${heightPct}%` }} />
              </div>
              <span className="mp-codex-bar-day">{item.isToday ? text("今天", "Today") : item.label}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

type NumericParameterField = "context_window" | "max_output_tokens" | "max_concurrent" | "requests_per_day";

export default function ModelPoolSettings({ source, modelID, onClose, onSaved }: { source: AISource; modelID: string; onClose: () => void; onSaved: () => Promise<unknown> }) {
  const zh = useUILanguage().startsWith("zh");
  const text = (cn: string, en: string) => zh ? cn : en;
  const [settings, setSettings] = useState(source.model_settings?.[modelID]);
  const [inheritedSource, setInheritedSource] = useState(source);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [changedFields, setChangedFields] = useState<Array<keyof AIModelParameters>>([]);
  const [draft, setDraft] = useState<Record<keyof AIModelParameters, string>>(() => {
    const overrides = source.model_settings?.[modelID]?.overrides || {};
    return {
      context_window: overrides.context_window?.toString() || "",
      max_output_tokens: overrides.max_output_tokens?.toString() || "",
      max_concurrent: overrides.max_concurrent?.toString() || "",
      requests_per_day: overrides.requests_per_day?.toString() || "",
      tokens_per_day: overrides.tokens_per_day?.toString() || "",
      shared_tokens_per_day: overrides.shared_tokens_per_day?.toString() || "",
      request_defaults: overrides.request_defaults === undefined ? "" : JSON.stringify(overrides.request_defaults, null, 2),
    };
  });
  if (!settings) return null;
  const usage = settings.usage || inheritedSource.model_usage?.[modelID];
  const fields: Array<[NumericParameterField, string, number, number]> = [
    ["context_window", text("上下文窗口", "Context window"), 256, 2000000],
    ["max_output_tokens", text("最大输出 Token", "Maximum output tokens"), 1, 393216],
    ["max_concurrent", text("并发请求上限", "Concurrent request limit"), 1, 2500],
    ["requests_per_day", text("每日请求上限", "Daily request limit"), 1, 1000000],
  ];
  const resetButtonLabel = (name: NumericParameterField) => {
    if (name === "requests_per_day") return text("取消每日上限", "Remove daily limit");
    return text("恢复继承", "Inherit source");
  };
  const edit = (name: keyof AIModelParameters, value: string) => {
    setDraft(current => ({ ...current, [name]: value }));
    setChangedFields(current => current.includes(name) ? current : [...current, name]);
  };
  const reset = (name: keyof AIModelParameters) => edit(name, "");
  const handleResetUsage = async (scope: "today" | "all") => {
    if (busy || !usage?.usage_key) return;
    setBusy(true);
    setError("");
    try {
      const res = await resetModelUsage(usage.usage_key, scope);
      setSettings(current => current ? { ...current, usage: res.usage } : current);
      await onSaved();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : text("重置用量失败。", "Could not reset usage."));
    } finally {
      setBusy(false);
    }
  };
  const save = async () => {
    if (busy || conflict) return;
    setError("");
    const configuration: Record<string, unknown> = {};
    try {
      for (const [name, , min, max] of fields) {
        const value = (draft[name] || "").trim();
        if (value && (!Number.isInteger(Number(value)) || Number(value) < min || Number(value) > max)) throw new Error(text("请核对数值范围。", "Check numeric ranges."));
        configuration[name] = value ? Number(value) : null;
      }
      configuration.request_defaults = draft.request_defaults.trim() ? JSON.parse(draft.request_defaults) : null;
      const inheritedContext = inheritedSource.context_window_mode === "auto"
        ? inheritedSource.model_metadata?.[modelID]?.context_window || settings.effective.context_window || 32768 : inheritedSource.context_window;
      const context = Number(configuration.context_window || inheritedContext);
      const output = Number(configuration.max_output_tokens || inheritedSource.max_output_tokens);
      if (output > context) throw new Error(text("最大输出不能超过上下文窗口。", "Maximum output cannot exceed the context window."));
    } catch (reason) { setError(reason instanceof Error ? reason.message : text("参数格式无效。", "Invalid parameter format.")); return; }
    setBusy(true);
    try {
      const patch = Object.fromEntries(changedFields.map(name => [name, configuration[name]]));
      const next = await sourceRequest<AISource>(`/${encodeURIComponent(source.id)}/models/config`, { model_id: modelID, expected_rule_revision: settings.rule_revision, configuration: patch }, "PUT");
      if (next.model_settings?.[modelID]) setSettings(next.model_settings[modelID]);
      await onSaved(); onClose();
    } catch (reason) {
      const message = reason instanceof Error ? reason.message : "";
      if (message === "model_rule_revision_conflict") { setConflict(true); setError(text("该模型配置已变化，请重新读取并核对。不会自动把旧编辑覆盖到新版本。", "This model's configuration changed. Reload and review it; the old edit will not overwrite the new version automatically.")); }
      else { setConflict(true); setError(text("保存未确认，请重新读取实际配置后再编辑。", "Save was not confirmed. Reload actual configuration before editing again.")); }
    } finally { setBusy(false); }
  };
  const reload = async () => {
    if (busy) return;
    setBusy(true); setError("");
    try {
      const value = await sourceRequest<{ sources: AISource[] }>();
      const currentSource = value.sources.find(row => row.id === source.id);
      const next = currentSource?.model_settings?.[modelID];
      if (!next) throw new Error("missing_model_settings");
      setInheritedSource(currentSource!);
      setSettings(next); setConflict(false);
      setChangedFields([]);
      setDraft({
        context_window: next.overrides.context_window?.toString() || "",
        max_output_tokens: next.overrides.max_output_tokens?.toString() || "",
        max_concurrent: next.overrides.max_concurrent?.toString() || "",
        requests_per_day: next.overrides.requests_per_day?.toString() || "",
        tokens_per_day: next.overrides.tokens_per_day?.toString() || "",
        shared_tokens_per_day: next.overrides.shared_tokens_per_day?.toString() || "",
        request_defaults: next.overrides.request_defaults === undefined ? "" : JSON.stringify(next.overrides.request_defaults, null, 2),
      });
    } catch { setConflict(true); setError(text("无法读取当前模型配置。", "Could not read current model configuration.")); }
    finally { setBusy(false); }
  };
  const probe = async (kind: "connection" | "model_exists" | "tools") => {
    if (busy || conflict) return;
    setBusy(true); setError("");
    let probeFailed = false;
    try {
      await sourceRequest(`/${encodeURIComponent(source.id)}/models/test`, { model_id: modelID, probe: kind });
    } catch { probeFailed = true; }
    try {
      const value = await sourceRequest<{ sources: AISource[] }>();
      const currentSource = value.sources.find(row => row.id === source.id);
      const latest = currentSource?.model_settings?.[modelID];
      if (!latest) throw new Error("missing_model_settings");
      setSettings(latest); setInheritedSource(currentSource!);
      await onSaved();
      if (latest.rule_revision !== settings.rule_revision) {
        setConflict(true);
        setError(text("探测期间模型配置已变化，请重新读取并核对编辑内容。", "Model configuration changed during the probe. Reload and review your edits."));
      } else if (probeFailed) setError(text("探测未通过，请核对最新证据；不会根据配置推断连接正常。", "The probe did not pass. Review current evidence; configuration alone does not establish connectivity."));
    } catch { setError(text("无法读取探测后的最新证据，当前显示为先前记录，不能据此判断连接正常。", "Could not reload probe evidence. Displayed evidence is the previous record and does not establish current connectivity.")); }
    finally { setBusy(false); }
  };
  const renderField = ([name, label, min, max]: [NumericParameterField, string, number, number]) => {
    const effectiveValue = settings.effective[name] ?? null;
    const isInherited = settings.inherited_fields.includes(name) || settings.overrides[name] === undefined;
    return (
      <label className="ais-field mp-compact-field" key={name}>
        {label}
        <input type="number" aria-label={label} min={min} max={max} value={draft[name] || ""} disabled={busy || conflict} onChange={event => edit(name, event.target.value)} placeholder={effectiveValue === null ? text("不限", "Unlimited") : String(effectiveValue)} />
        <span className="mp-field-foot">
          <small>{text("当前生效", "Currently effective")}: {effectiveValue === null ? text("不限", "Unlimited") : String(effectiveValue)} · {isInherited ? text("继承来源", "Inherited from source") : text("此模型覆盖", "Model override")}</small>
          <button className="mp-link-btn" type="button" disabled={busy || conflict} onClick={() => reset(name)}>{resetButtonLabel(name)}</button>
        </span>
      </label>
    );
  };
  return <Modal title={`${text("此模型参数", "Parameters for this model")} · ${modelID}`} onClose={() => { if (!busy) onClose(); }}><div className="ais-modal mp-parameters">
    <p className="mp-modal-sub">{source.name} · {modelID}</p>
    <ModelTokenUsageSummaryCard usage={usage} busy={busy} onReset={usage?.usage_key ? handleResetUsage : undefined} />
    {settings.context_window_basis && <p className="ais-muted">{settings.context_window_known === false
      ? text("模型未声明实际窗口，当前为保守回退值；可手动覆盖。", "The model did not declare its window. A conservative fallback applies; you can set an override.")
      : settings.context_window_basis === "provider_model_metadata"
        ? text("窗口来自该来源的模型列表；手动覆盖优先。", "The window comes from this source's model listing; manual overrides take priority.")
        : text("当前使用手动配置的模型窗口。", "The configured model window applies.")}</p>}
    <div className="mp-param-grid">
      {fields.map(f => renderField(f))}
    </div>
    <label className="ais-field mp-compact-field">
      {text("请求参数 JSON", "Request parameters JSON")}
      <textarea rows={2} aria-label={text("请求参数 JSON", "Request parameters JSON")} value={draft.request_defaults} disabled={busy || conflict} onChange={event => edit("request_defaults", event.target.value)} placeholder={JSON.stringify(settings.effective.request_defaults)} />
      <span className="mp-field-foot">
        <small>{text("支持 temperature、top_p、thinking、reasoning_effort", "Supports temperature, top_p, thinking and reasoning_effort")}</small>
        <button className="mp-link-btn" type="button" disabled={busy || conflict} onClick={() => reset("request_defaults")}>{text("恢复来源请求参数", "Inherit source request parameters")}</button>
      </span>
    </label>
    <details className="mp-evidence-details"><summary>{text("当前请求参数及证据", "Effective request parameters and evidence")}</summary><pre>{JSON.stringify({ request_defaults: settings.effective.request_defaults, evidence: settings.evidence }, null, 2)}</pre></details>
    <div className="mp-probes"><button className="ais-secondary" disabled={busy || conflict} onClick={() => void probe("connection")}>{text("探测连接", "Probe connection")}</button><button className="ais-secondary" disabled={busy || conflict} onClick={() => void probe("model_exists")}>{text("核对模型列表", "Check model listing")}</button><button className="ais-secondary" disabled={busy || conflict} onClick={() => void probe("tools")}>{text("诊断工具调用", "Diagnose tool calls")}</button></div>
    {error && <p role="alert">{error}</p>}
    <div className="ais-modal-footer"><button className="ais-secondary" disabled={busy} onClick={() => void reload()}>{text("重新读取", "Reload")}</button><button className="ais-primary" disabled={busy || conflict} onClick={() => void save()}>{text("保存此模型参数", "Save this model's parameters")}</button></div>
  </div></Modal>;
}
