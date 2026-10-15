import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, Folder, Mic, Monitor, Plus, Settings, ShieldAlert, ShieldCheck } from "lucide-react";
import type { AgentProject, AgentSettings } from "../domain/rynAgent";
import "./RynAgentComposer.css";

type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean;
  start(): void; stop(): void;
  onresult: ((event: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onend: (() => void) | null; onerror: (() => void) | null;
};
type RecognitionConstructor = new () => Recognition;
function speechConstructor(): RecognitionConstructor | undefined {
  const browser = window as unknown as { SpeechRecognition?: RecognitionConstructor; webkitSpeechRecognition?: RecognitionConstructor };
  return browser.SpeechRecognition || browser.webkitSpeechRecognition;
}

interface Props {
  project?: AgentProject;
  workspace?: string;
  value: string;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  inputDisabled: boolean;
  busy: boolean;
  onChange(value: string): void;
  onSend(): void;
  onProject(): void;
  onSettings(): void;
  settingsDisabled: boolean;
  permissionMode: AgentSettings["permission_mode"] | "unknown";
  permissionLocked: boolean;
  onPermission(mode: AgentSettings["permission_mode"]): void;
  modelPicker: ReactNode;
  options: ReactNode;
  sendButton: ReactNode;
}

export default function RynAgentComposer(props: Props) {
  const { t, i18n } = useTranslation("rynAgent");
  const options = useRef<HTMLDetailsElement>(null);
  const permissions = useRef<HTMLDetailsElement>(null);
  const recognition = useRef<Recognition | null>(null);
  const latest = useRef(props);
  latest.current = props;
  const [listening, setListening] = useState(false);
  const [voiceError, setVoiceError] = useState(false);
  const Speech = speechConstructor();
  useLayoutEffect(() => {
    const textarea = props.inputRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.max(66, Math.min(220, textarea.scrollHeight))}px`;
    textarea.style.overflowY = textarea.scrollHeight > 220 ? "auto" : "hidden";
  }, [props.value, props.inputRef]);
  useEffect(() => {
    const closeMenus = (event: PointerEvent) => {
      for (const menu of [options.current, permissions.current]) if (menu?.open && !menu.contains(event.target as Node)) menu.open = false;
    };
    document.addEventListener("pointerdown", closeMenus);
    return () => {
      document.removeEventListener("pointerdown", closeMenus);
      if (recognition.current) {
        recognition.current.onend = null; recognition.current.onresult = null; recognition.current.onerror = null;
        recognition.current.stop();
      }
    };
  }, []);
  const toggleVoice = () => {
    if (!Speech || props.inputDisabled || props.busy) return;
    if (listening) { recognition.current?.stop(); return; }
    const next = new Speech(); recognition.current = next;
    next.lang = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en-US";
    next.continuous = true; next.interimResults = false;
    next.onresult = event => {
      const text = Array.from(event.results).slice(event.resultIndex).filter(result => result.isFinal).map(result => result[0].transcript).join("");
      if (text) latest.current.onChange(`${latest.current.value}${latest.current.value ? " " : ""}${text}`);
    };
    next.onend = () => setListening(false);
    next.onerror = () => { setListening(false); setVoiceError(true); };
    setVoiceError(false);
    try { next.start(); setListening(true); } catch { setVoiceError(true); }
  };
  const modeLabel = props.permissionMode === "read_only" ? "readOnly" : props.permissionMode === "confirm_changes" ? "confirmChanges" : props.permissionMode === "unknown" ? "unknownMode" : "fullAccess";
  const menuKeys = { workspace: "workspaceMode", read_only: "readOnly", confirm_changes: "confirmChanges", automatic: "automatic" } as const;
  const escape = (event: React.KeyboardEvent<HTMLDetailsElement>) => {
    if (event.key === "Escape") { event.currentTarget.open = false; event.currentTarget.querySelector<HTMLElement>("summary")?.focus(); }
  };
  return <div className="ra-composer-shell">
    <div className="ra-composer-context">
      <button type="button" className="ra-context-project" title={props.project?.path || props.workspace || t("defaultOutputFolder")} onClick={props.onProject}><Folder size={14} /><span>{props.project?.path || props.workspace || t("defaultOutputFolder")}</span></button>
      <span className="ra-context-device"><Monitor size={15} />{t("thisComputer")}</span>
      <button type="button" className="ra-context-settings" aria-label={t("conversationSettings")} title={t("settings")} disabled={props.settingsDisabled} onClick={props.onSettings}><Settings size={14} /></button>
    </div>
    <div className="ra-composer">
      <textarea ref={props.inputRef} rows={1} aria-label={t("input")} placeholder={t("placeholder")} value={props.value} disabled={props.inputDisabled} onChange={event => props.onChange(event.target.value)} onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); props.onSend(); }
      }} />
      <div className="ra-composer-controls">
        <div className="ra-composer-left">
          <details ref={options} className="ra-composer-options" onKeyDown={escape}><summary aria-label={t("composerOptions")} title={t("composerOptions")} onClick={event => { if (props.busy) event.preventDefault(); }}><Plus size={19} /></summary><div className="ra-composer-options-panel">{props.options}</div></details>
          <details ref={permissions} className="ra-composer-permissions" onKeyDown={escape}><summary aria-label={t("mode")} title={t("mode")} className={modeLabel === "fullAccess" ? "ra-full-access" : ""}>{modeLabel === "fullAccess" ? <ShieldAlert size={13} /> : <ShieldCheck size={13} />}<span>{t(modeLabel)}</span><ChevronDown size={11} /></summary><div className="ra-permissions-menu" role="menu" aria-label={t("mode")}>
            {(Object.keys(menuKeys) as Array<AgentSettings["permission_mode"]>).map(mode => <button type="button" key={mode} role="menuitemradio" aria-checked={props.permissionMode === mode} disabled={props.busy || props.permissionLocked} onClick={() => { props.onPermission(mode); if (permissions.current) permissions.current.open = false; }}>{t(menuKeys[mode])}</button>)}
            {props.permissionLocked && <small>{t("taskPermissionFixed")}</small>}
          </div></details>
        </div>
        <div className="ra-composer-right">{props.modelPicker}<button type="button" className={`ra-voice${listening ? " active" : ""}`} aria-label={t(listening ? "stopVoiceInput" : "voiceInput")} aria-pressed={listening} title={t(Speech ? listening ? "stopVoiceInput" : "voiceInput" : "voiceUnavailable")} disabled={!Speech || props.inputDisabled || props.busy} onClick={toggleVoice}><Mic size={17} /></button>{props.sendButton}</div>
      </div>
      {voiceError && <p className="ra-voice-error" role="status">{t("voiceInputError")}</p>}
    </div>
  </div>;
}
