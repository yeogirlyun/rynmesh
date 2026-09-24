import { useTranslation } from "react-i18next";
import { useEffect, useId, useRef, type ReactNode } from "react";
import {
  AudioLines,
  ChevronRight,
  FileText,
  Film,
  Info,
  Network,
  Sparkles,
  X,
} from "lucide-react";
import { personalLabelKeys } from "./labels";
import type { Device, Service } from "./model";

// Shared vector artwork stays crisp at every density and in both appearances.
export function DeviceArt({
  kind = "desktop",
  dark = false,
  small = false,
}: {
  kind?: Device["kind"];
  dark?: boolean;
  small?: boolean;
}) {
  const id = useId().replaceAll(":", "");
  return (
    <svg
      className={`pf-device-art${small ? " small" : ""}`}
      viewBox="0 0 112 88"
      fill="none"
      aria-hidden="true"
    >
      <defs>
        <linearGradient
          id={`${id}s`}
          x1="23"
          y1="7"
          x2="85"
          y2="61"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor={dark ? "#617da3" : "#e1f3ff"} />
          <stop offset=".5" stopColor={dark ? "#2b496c" : "#86b4df"} />
          <stop offset="1" stopColor={dark ? "#142539" : "#477daf"} />
        </linearGradient>
        <linearGradient id={`${id}m`} x1="0" y1="0" x2="0" y2="1">
          <stop stopColor="#e7edf3" />
          <stop offset=".48" stopColor="#9ba6b1" />
          <stop offset=".65" stopColor="#edf1f5" />
          <stop offset="1" stopColor="#8e9ca9" />
        </linearGradient>
        <linearGradient id={`${id}b`}>
          <stop stopColor="#8995a1" />
          <stop offset=".45" stopColor="#f3f5f8" />
          <stop offset="1" stopColor="#919da8" />
        </linearGradient>
      </defs>
      <ellipse cx="56" cy="80" rx="36" ry="3" fill="#455772" opacity=".10" />
      {kind === "desktop" ? (
        <>
          <path d="M48 65h16l3 11H45l3-11Z" fill={`url(#${id}m)`} />
          <rect
            x="39"
            y="75"
            width="34"
            height="3"
            rx="1.5"
            fill={`url(#${id}b)`}
          />
          <rect
            x="15"
            y="10"
            width="82"
            height="56"
            rx="3"
            fill={`url(#${id}m)`}
          />
          <rect x="15" y="10" width="82" height="49" rx="3" fill="#29333d" />
          <path d="M18 13h76v43H18z" fill={`url(#${id}s)`} />
          <path
            d="M18 14c30 0 38 34 76 32v10H18V14Z"
            fill="#2c689c"
            opacity=".3"
          />
          <path
            d="M18 14c23-2 49 30 76 32-24 3-55-26-76-24v-8Z"
            fill="#fff"
            opacity=".3"
          />
          <circle cx="56" cy="62" r=".8" fill="#707e8c" />
        </>
      ) : (
        <>
          <rect x="22" y="17" width="68" height="48" rx="3" fill="#44515e" />
          <rect
            x="24"
            y="19"
            width="64"
            height="43"
            rx="1"
            fill={`url(#${id}s)`}
          />
          <path
            d="M24 20c24 0 36 30 64 30v12H24V20Z"
            fill="#286b9b"
            opacity=".3"
          />
          <path
            d="M24 21c25 1 40 29 64 29-24 1-39-22-64-23v-6Z"
            fill="#fff"
            opacity=".25"
          />
          <path
            d="m22 65-13 9c-1 2 2 3 6 3h82c4 0 7-1 6-3l-13-9H22Z"
            fill={`url(#${id}m)`}
          />
          <path d="m25 66-6 5h74l-6-5H25Z" fill="#7f8b96" />
          <path d="M45 72h22l2 2H43l2-2Z" fill="#bfc8d0" />
        </>
      )}
    </svg>
  );
}
export function ServiceArt({
  kind,
  small = false,
  brand,
}: {
  kind: Service["kind"];
  small?: boolean;
  brand?: string;
}) {
  if (kind === "ai" && brand) return (
    <span className={`pf-service-art branded${small ? " small" : ""}`} aria-hidden="true">
      <span className={`pf-provider-logo ${brand}`} />
    </span>
  );
  const Icon = {
    ai: Sparkles,
    document: FileText,
    audio: AudioLines,
    network: Network,
    video: Film,
  }[kind];
  return (
    <span
      className={`pf-service-art ${kind}${small ? " small" : ""}`}
      aria-hidden="true"
    >
      {kind === "ai" ? (
        <svg
          width={small ? 27 : 37}
          height={small ? 27 : 37}
          viewBox="0 0 40 40"
          fill="currentColor"
        >
          <path d="M15 9c1.8 10.1 4 12.3 14 14-10 1.8-12.2 4-14 14C13.2 27 11 24.8 1 23c10-1.7 12.2-3.9 14-14Z" />
          <path
            d="M29 1c1 6.1 2.6 7.8 9 9-6.4 1.2-8 2.8-9 9-1.1-6.2-2.8-7.8-9-9 6.2-1.2 7.9-2.9 9-9Z"
            opacity=".76"
          />
        </svg>
      ) : (
        <Icon strokeWidth={1.5} size={small ? 22 : 30} />
      )}
    </span>
  );
}
export function Status({
  online,
  self,
  label,
}: {
  online: boolean | null;
  self?: boolean;
  label?: string;
}) {
  const { t } = useTranslation();
  return (
    <span
      className={`pf-status ${label === "Failed" ? "error" : label === "Processing" ? "processing" : self ? "local" : online === true ? "online" : "offline"}`}
    >
      <i />
      {(label && (personalLabelKeys[label] ? t(`personal.${personalLabelKeys[label]}`) : label)) ||
        (self
          ? t("personal.thisDevice")
          : online === true
            ? t("personal.online")
            : online === false
              ? t("personal.offline")
              : t("personal.unknown"))}
    </span>
  );
}
export function PageHeading({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children?: ReactNode;
}) {
  return (
    <header className="pf-heading">
      <div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      {children}
    </header>
  );
}
export function Note({ children }: { children: ReactNode }) {
  return (
    <p className="pf-note">
      <Info size={16} />
      <span>{children}</span>
    </p>
  );
}
export function Empty({
  title,
  children,
}: {
  title: string;
  children?: ReactNode;
}) {
  return (
    <div className="pf-empty">
      <Network size={28} strokeWidth={1.3} />
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
export function Tabs<T extends string>({
  options,
  value,
  onChange,
}: {
  options: readonly T[];
  value: T;
  onChange: (value: T) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="pf-tabs" aria-label={t("personal.filter")}>
      {options.map((option) => (
        <button
          key={personalLabelKeys[option] ? t(`personal.${personalLabelKeys[option]}`) : option}
          type="button"
          aria-pressed={value === option}
          className={value === option ? "selected" : ""}
          onClick={() => onChange(option)}
        >
          {personalLabelKeys[option] ? t(`personal.${personalLabelKeys[option]}`) : option}
        </button>
      ))}
    </div>
  );
}
export function Connection({
  device,
  current,
}: {
  device: Device;
  current?: Device;
}) {
  const { t } = useTranslation();
  return (
    <div className="pf-connection">
      <div>
        <DeviceArt kind={current?.kind || "laptop"} />
        <span>{t("personal.thisDevice")}</span>
        <small>{current?.name || t("personal.laptop")}</small>
      </div>
      <span className="pf-connection-line">
        <i className={device.online ? "connected" : ""} />
      </span>
      <div>
        <DeviceArt kind={device.kind} />
        <span>{device.name}</span>
      </div>
    </div>
  );
}
export function Modal({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const dialog = useRef<HTMLDialogElement>(null);
  const id = useId();
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement as HTMLElement | null;
    element?.showModal();
    return () => {
      element?.close();
      previous?.focus();
    };
  }, []);
  return (
    <dialog
      ref={dialog}
      className="pf-dialog"
      aria-labelledby={id}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
      onClick={(event) => {
        if (event.target === event.currentTarget) {
          const rect = event.currentTarget.getBoundingClientRect();
          if (
            event.clientX < rect.left ||
            event.clientX > rect.right ||
            event.clientY < rect.top ||
            event.clientY > rect.bottom
          )
            onClose();
        }
      }}
    >
      <div className="pf-dialog-heading">
        <h2 id={id}>{title}</h2>
        <button
          className="pf-icon-button"
          onClick={onClose}
          aria-label={t("personal.closeDialog")}
        >
          <X size={20} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Arrow() {
  return <ChevronRight size={18} />;
}
