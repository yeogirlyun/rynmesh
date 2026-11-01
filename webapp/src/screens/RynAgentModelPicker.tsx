import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react";
import { Check, ChevronDown } from "lucide-react";
import { useTranslation } from "react-i18next";
import { formatTokenCount } from "../domain/aiSources";
import { canUseAgentModel, type AgentModel } from "../domain/rynAgent";
import "./rynAgentText";
import "./RynAgentModelPicker.css";

export interface RynAgentModelPickerProps {
  models: AgentModel[];
  value: string;
  onChange: (modelID: string) => void;
  disabled?: boolean;
  /** Existing task models remain readable while their safe switch flow is separate. */
  selectionLocked?: boolean;
  onManageTaskModel?: () => void;
  /** Only pass an effort label when it describes an actual runtime setting. */
  effortLabel?: string;
  reasoningEfforts?: string[];
  effort?: string;
  onEffortChange?: (effort: string) => void;
  title?: string;
}

export default function RynAgentModelPicker({ models, value, onChange, disabled = false, selectionLocked = false, onManageTaskModel, effortLabel, reasoningEfforts = [], effort, onEffortChange, title }: RynAgentModelPickerProps) {
  const { t, i18n } = useTranslation("rynAgent");
  const text = (zh: string, en: string) => i18n.language.startsWith("zh") ? zh : en;
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [activeID, setActiveID] = useState(value);
  const search = useRef({ value: "", at: 0 });
  const selectedModel = models.find(model => model.id === value);
  const selectedName = selectedModel?.name || value || t("chooseModel");
  const effortName = (level: string) => ({ none: text("关闭", "None"), minimal: text("极低", "Minimal"), low: text("轻度", "Low"), medium: text("中度", "Medium"), high: text("重度", "High"), xhigh: text("超高", "Extra high"), max: text("最高", "Max"), ultra: text("极高", "Ultra") }[level] || level);
  const effortChoices = [...new Set(reasoningEfforts)];
  const displayEffort = effortLabel || (effort && effortChoices.includes(effort) ? effortName(effort) : undefined);
  const showEfforts = effortChoices.length > 0 && Boolean(onEffortChange);
  const sourceName = (model: AgentModel) => model.cli_source ? model.source_name || model.cli_source : t(`source_${model.source}`, { defaultValue: model.source });
  const groups = [...new Set(models.map(sourceName))].map(name => ({ name, models: models.filter(model => sourceName(model) === name) }));
  const ordered = groups.flatMap(group => group.models);
  const selectable = canUseAgentModel;
  const navigation = ordered.filter(model => selectionLocked || selectable(model));
  const activeIndex = navigation.findIndex(model => model.id === activeID);
  const status = (model: AgentModel) => !model.available ? t("notAvailable") : model.source === "peer" && model.destination_binding_supported === false ? t("modelDestinationUnavailableShort") : !canUseAgentModel(model) ? t("notSupported") : "";
  const optionID = (modelID: string) => `${id}-model-${encodeURIComponent(modelID)}`;
  const usageBadge = (model: AgentModel) => {
    const u = model.usage;
    if (!u) return "";
    if (u.today.total_tokens > 0) return `${text("今日", "Today")} ${formatTokenCount(u.today.total_tokens)}`;
    if (u.total.total_tokens > 0) return `${text("累计", "Total")} ${formatTokenCount(u.total.total_tokens)}`;
    return "";
  };

  useEffect(() => {
    if (!open) return;
    const closeOutside = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => document.removeEventListener("pointerdown", closeOutside);
  }, [open]);

  useEffect(() => { if (disabled) setOpen(false); }, [disabled]);
  useEffect(() => {
    if (open && activeID) root.current?.querySelector<HTMLElement>(`[id="${optionID(activeID)}"]`)?.scrollIntoView?.({ block: "nearest" });
  }, [activeID, open]);

  const show = (last = false) => {
    if (disabled) return;
    setActiveID(navigation.find(model => model.id === value)?.id || (last ? navigation.at(-1)?.id : navigation[0]?.id) || "");
    setOpen(true);
  };
  const choose = (model: AgentModel) => {
    if (disabled || selectionLocked || !selectable(model)) return;
    onChange(model.id);
    setOpen(false);
    trigger.current?.focus();
  };
  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>) => {
    if (event.key === "Escape") { event.preventDefault(); setOpen(false); return; }
    if (event.key === "Tab") { if (!open || !(selectionLocked ? onManageTaskModel : showEfforts) || event.shiftKey) setOpen(false); return; }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      if (!open) {
        if (event.key === "Home" || event.key === "End") { setActiveID((event.key === "Home" ? navigation[0] : navigation.at(-1))?.id || ""); setOpen(true); }
        else show(event.key === "ArrowUp");
        return;
      }
      const next = event.key === "Home" ? 0 : event.key === "End" ? navigation.length - 1 : activeIndex < 0 ? event.key === "ArrowDown" ? 0 : navigation.length - 1 : (activeIndex + (event.key === "ArrowDown" ? 1 : -1) + navigation.length) % navigation.length;
      setActiveID(navigation[next]?.id || "");
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (!open) show();
      else { const model = navigation.find(item => item.id === activeID); if (model) choose(model); }
      return;
    }
    if (event.key.length === 1 && !event.ctrlKey && !event.altKey && !event.metaKey) {
      const now = Date.now();
      search.current = { value: (now - search.current.at < 700 ? search.current.value : "") + event.key.toLocaleLowerCase(), at: now };
      const match = navigation.find(model => model.name.toLocaleLowerCase().startsWith(search.current.value));
      if (match) { setOpen(true); setActiveID(match.id); }
    }
  };

  return <div className="ra-model-picker" ref={root} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setOpen(false); }} onKeyDown={event => { if (event.key === "Escape") { setOpen(false); trigger.current?.focus(); } }}>
    <button ref={trigger} type="button" value={value} className="ra-model-trigger" role="combobox" aria-label={t("model")} aria-haspopup="listbox" aria-expanded={open} aria-controls={open ? `${id}-list` : undefined} aria-activedescendant={open && activeID ? optionID(activeID) : undefined} title={title || selectedName} disabled={disabled} onClick={() => open ? setOpen(false) : show()} onKeyDown={onKeyDown}>
      <span className="ra-model-trigger-name">{selectedName}</span>{displayEffort && <span className="ra-model-effort">{displayEffort}</span>}<ChevronDown size={13} aria-hidden="true" />
    </button>
    {open && <div className="ra-model-popover">
      <div className="ra-model-popover-title">{t("model")}</div>
      <div className="ra-model-list" id={`${id}-list`} role="listbox" aria-label={t("model")}>
        {value && !selectedModel && <div className="ra-model-option" id={optionID(value)} role="option" aria-selected="true" aria-disabled="true"><span className="ra-model-option-text"><span>{value}</span><small>{t("notAvailable")}</small></span><Check size={15} aria-hidden="true" /></div>}
        {groups.map(group => <div role="group" aria-label={group.name} key={group.name} className="ra-model-group">
          <div className="ra-model-group-label" aria-hidden="true">{group.name}</div>
          {group.models.map(model => { const note = status(model); const badge = usageBadge(model); return <div key={model.id} id={optionID(model.id)} data-model-id={model.id} role="option" aria-selected={model.id === value} aria-disabled={selectionLocked || !selectable(model)} aria-label={model.name} aria-describedby={note ? `${optionID(model.id)}-status` : undefined} className={`ra-model-option${activeID === model.id ? " active" : ""}`} title={canUseAgentModel(model) ? model.name : model.description || model.name} onPointerMove={() => setActiveID(model.id)} onMouseDown={event => event.preventDefault()} onClick={() => choose(model)}>
            <span className="ra-model-option-text"><span>{model.name}</span>{note && <small id={`${optionID(model.id)}-status`}>{note}</small>}</span>
            <span className="ra-model-option-right">{badge && <span className="ra-model-usage-pill" aria-hidden="true">{badge}</span>}{model.id === value && <Check size={15} aria-hidden="true" />}</span>
          </div>; })}
        </div>)}
        {!models.length && !value && <p className="ra-model-empty">{text("暂无可用模型", "No models available")}</p>}
      </div>
      {showEfforts && <fieldset className="ra-model-efforts" role="radiogroup" aria-label={text("思考强度", "Reasoning effort")} disabled={disabled || selectionLocked}>
        <legend>{text("思考强度", "Reasoning effort")}</legend>
        <div>{effortChoices.map(level => <label key={level} className="ra-model-effort-option" onMouseDown={event => event.preventDefault()}><input type="radio" name={`${id}-effort`} value={level} checked={effort === level} onChange={() => { onEffortChange?.(level); setOpen(false); trigger.current?.focus(); }} /><span><span>{effortName(level)}</span>{effort === level && <Check size={13} aria-hidden="true" />}</span></label>)}</div>
      </fieldset>}
      {selectionLocked && <div className="ra-model-picker-footer"><p>{text("当前对话使用此模型", "This model is used for the current chat")}</p>{onManageTaskModel && <button type="button" className="ra-model-manage" onClick={() => { setOpen(false); onManageTaskModel(); }}>{text("切换当前对话模型…", "Change this chat's model…")}</button>}</div>}
    </div>}
  </div>;
}
