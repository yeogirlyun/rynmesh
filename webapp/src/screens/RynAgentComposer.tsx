import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { useTranslation } from "react-i18next";
import { ChevronDown, File, Folder, Image, LoaderCircle, Mic, Monitor, Paperclip, Plus, Settings, ShieldAlert, ShieldCheck, X } from "lucide-react";
import type { AgentProject, AgentSettings } from "../domain/rynAgent";
import { attachmentIsImage, nativeAgentAttachments, readAgentAttachments, type AgentDraftAttachment } from "../domain/agentAttachments";
import { listenNativeFileDrop } from "../domain/dropDesktop";
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
  placeholder?: string;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  inputDisabled: boolean;
  busy: boolean;
  onChange(value: string): void;
  onSend(): void;
  onAlternateSend?(): void;
  topSlot?: ReactNode;
  onProject(): void;
  onSettings(): void;
  settingsDisabled: boolean;
  permissionMode: AgentSettings["permission_mode"] | "unknown";
  permissionLocked: boolean;
  onPermission(mode: AgentSettings["permission_mode"]): void;
  modelPicker: ReactNode;
  options: ReactNode;
  sendButton: ReactNode;
  attachments?: AgentDraftAttachment[];
  onAttachmentsChange?(attachments: AgentDraftAttachment[]): void;
  onAttachmentsBusyChange?(busy: boolean): void;
}

export default function RynAgentComposer(props: Props) {
  const { t, i18n } = useTranslation("rynAgent");
  const options = useRef<HTMLDetailsElement>(null);
  const permissions = useRef<HTMLDetailsElement>(null);
  const recognition = useRef<Recognition | null>(null);
  const shell = useRef<HTMLDivElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const readingFiles = useRef(false);
  const mounted = useRef(true);
  const latest = useRef(props);
  latest.current = props;
  const [listening, setListening] = useState(false);
  const [voiceError, setVoiceError] = useState(false);
  const [attachmentError, setAttachmentError] = useState("");
  const [preparingFiles, setPreparingFiles] = useState(false);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const attachments = props.attachments || [];
  const Speech = speechConstructor();
  useLayoutEffect(() => {
    const textarea = props.inputRef.current;
    if (!textarea) return;
    textarea.style.height = "0px";
    textarea.style.height = `${Math.max(66, Math.min(220, textarea.scrollHeight))}px`;
    textarea.style.overflowY = textarea.scrollHeight > 220 ? "auto" : "hidden";
  }, [props.value, props.inputRef]);
  useEffect(() => {
    mounted.current = true;
    const closeMenus = (event: PointerEvent) => {
      for (const menu of [options.current, permissions.current]) if (menu?.open && !menu.contains(event.target as Node)) menu.open = false;
    };
    document.addEventListener("pointerdown", closeMenus);
    return () => {
      mounted.current = false;
      document.removeEventListener("pointerdown", closeMenus);
      if (recognition.current) {
        recognition.current.onend = null; recognition.current.onresult = null; recognition.current.onerror = null;
        recognition.current.stop();
      }
    };
  }, []);
  const addFiles = async (files: File[]) => {
    if (!files.length || latest.current.inputDisabled || !latest.current.onAttachmentsChange || readingFiles.current) return;
    const target = latest.current;
    readingFiles.current = true;
    setAttachmentError(""); setPreparingFiles(true); target.onAttachmentsBusyChange?.(true);
    try {
      const added = await readAgentAttachments(files, target.attachments || []);
      if (mounted.current) target.onAttachmentsChange?.([...(target.attachments || []), ...added]);
    } catch (error) {
      if (mounted.current) setAttachmentError(error instanceof Error && error.message === "attachmentLimit" ? "attachmentLimit" : "attachmentReadError");
    } finally {
      readingFiles.current = false;
      if (mounted.current) { setPreparingFiles(false); target.onAttachmentsBusyChange?.(false); }
    }
  };
  useEffect(() => {
    if (!props.onAttachmentsChange) return;
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listenNativeFileDrop(event => {
      if (disposed || latest.current.inputDisabled) return;
      if (event.type === "leave") { setDraggingFiles(false); return; }
      const bounds = shell.current?.getBoundingClientRect();
      const inside = bounds && event.x >= bounds.left && event.x <= bounds.right && event.y >= bounds.top && event.y <= bounds.bottom;
      setDraggingFiles(Boolean(inside && event.type === "over"));
      if (!inside || event.type !== "drop" || readingFiles.current) return;
      try {
        const current = latest.current.attachments || [];
        const added = nativeAgentAttachments(event.paths, current);
        latest.current.onAttachmentsChange?.([...current, ...added]); setAttachmentError("");
      } catch { setAttachmentError("attachmentLimit"); }
    }).then(cleanup => { if (disposed) cleanup(); else unlisten = cleanup; }).catch(() => undefined);
    return () => { disposed = true; unlisten?.(); };
  }, [Boolean(props.onAttachmentsChange)]);
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
  return <div ref={shell} className={`ra-composer-shell${draggingFiles ? " ra-composer-dragging" : ""}`} onDragOver={event => {
    if (!props.onAttachmentsChange || !Array.from(event.dataTransfer.types).includes("Files")) return;
    event.preventDefault(); event.dataTransfer.dropEffect = props.inputDisabled ? "none" : "copy";
    if (!props.inputDisabled) setDraggingFiles(true);
  }} onDragLeave={event => { if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDraggingFiles(false); }} onDrop={event => {
    if (!props.onAttachmentsChange || !Array.from(event.dataTransfer.types).includes("Files")) return;
    event.preventDefault(); setDraggingFiles(false); void addFiles(Array.from(event.dataTransfer.files));
  }}>
    <div className="ra-composer-context">
      <button type="button" className="ra-context-project" title={props.project?.path || props.workspace || t("defaultOutputFolder")} onClick={props.onProject}><Folder size={14} /><span>{props.project?.path || props.workspace || t("defaultOutputFolder")}</span></button>
      <span className="ra-context-device"><Monitor size={15} />{t("thisComputer")}</span>
      <button type="button" className="ra-context-settings" aria-label={t("conversationSettings")} title={t("settings")} disabled={props.settingsDisabled} onClick={props.onSettings}><Settings size={14} /></button>
    </div>
    {props.topSlot}
    <div className="ra-composer">
      {props.onAttachmentsChange && <input ref={picker} className="ra-attachment-picker" type="file" multiple tabIndex={-1} aria-label={t("addAttachments")} disabled={props.inputDisabled || preparingFiles} onChange={event => { void addFiles(Array.from(event.target.files || [])); event.target.value = ""; }} />}
      {attachments.length > 0 && <div className="ra-composer-attachments" aria-label={t("attachments")}>
        {attachments.map(file => <div className="ra-composer-attachment" key={file.id} title={file.name}>
          {file.preview ? <img src={file.preview} alt="" /> : attachmentIsImage(file) ? <Image size={21} /> : <File size={21} />}
          <span>{file.name}</span><button type="button" aria-label={t("removeAttachment", { name: file.name })} disabled={props.inputDisabled || preparingFiles} onClick={() => props.onAttachmentsChange?.(attachments.filter(item => item.id !== file.id))}><X size={13} /></button>
        </div>)}
      </div>}
      <textarea ref={props.inputRef} rows={1} aria-label={t("input")} placeholder={props.placeholder || t("placeholder")} value={props.value} disabled={props.inputDisabled} onChange={event => props.onChange(event.target.value)} onPaste={event => {
        const files = Array.from(event.clipboardData.files);
        if (props.onAttachmentsChange && files.length && !props.inputDisabled) { event.preventDefault(); void addFiles(files); }
      }} onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          if (preparingFiles) return;
          if ((event.metaKey || event.ctrlKey) && props.onAlternateSend) props.onAlternateSend();
          else props.onSend();
        }
      }} />
      <div className="ra-composer-controls">
        <div className="ra-composer-left">
          <details ref={options} className="ra-composer-options" onKeyDown={escape}><summary aria-label={t("composerOptions")} title={t("composerOptions")}><Plus size={19} /></summary><div className="ra-composer-options-panel">{props.onAttachmentsChange && <button type="button" className="ra-attach-files" disabled={props.inputDisabled || preparingFiles} onClick={() => { picker.current?.click(); if (options.current) options.current.open = false; }}><Paperclip size={15} />{t("addAttachments")}</button>}{props.options}</div></details>
          <details ref={permissions} className="ra-composer-permissions" onKeyDown={escape}><summary aria-label={t("mode")} title={t("mode")} className={modeLabel === "fullAccess" ? "ra-full-access" : ""}>{modeLabel === "fullAccess" ? <ShieldAlert size={13} /> : <ShieldCheck size={13} />}<span>{t(modeLabel)}</span><ChevronDown size={11} /></summary><div className="ra-permissions-menu" role="menu" aria-label={t("mode")}>
            {(Object.keys(menuKeys) as Array<AgentSettings["permission_mode"]>).map(mode => <button type="button" key={mode} role="menuitemradio" aria-checked={props.permissionMode === mode} disabled={props.busy || props.permissionLocked} onClick={() => { props.onPermission(mode); if (permissions.current) permissions.current.open = false; }}>{t(menuKeys[mode])}</button>)}
            {props.permissionLocked && <small>{t("taskPermissionFixed")}</small>}
          </div></details>
        </div>
        <div className="ra-composer-right">{props.modelPicker}<button type="button" className={`ra-voice${listening ? " active" : ""}`} aria-label={t(listening ? "stopVoiceInput" : "voiceInput")} aria-pressed={listening} title={t(Speech ? listening ? "stopVoiceInput" : "voiceInput" : "voiceUnavailable")} disabled={!Speech || props.inputDisabled || props.busy} onClick={toggleVoice}><Mic size={17} /></button>{props.sendButton}</div>
      </div>
      {voiceError && <p className="ra-voice-error" role="status">{t("voiceInputError")}</p>}
      {preparingFiles && <p className="ra-attachment-status" role="status"><LoaderCircle size={13} className="ra-spinner" />{t("preparingAttachments")}</p>}
      {attachmentError && <p className="ra-attachment-error" role="alert">{t(attachmentError)}</p>}
      {draggingFiles && <div className="ra-composer-drop-hint"><Paperclip size={24} /><span>{t("dropAttachments")}</span></div>}
    </div>
  </div>;
}
