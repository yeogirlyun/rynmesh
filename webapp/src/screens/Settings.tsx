import { tr, useUILanguage, uiLocale } from "../uiI18n";
import { Activity, BellRing, Cloud, Download, DownloadCloud, HardDrive, History, Network, Save, ShieldCheck, SlidersHorizontal, Sparkles, Trash2 } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useState } from "react";
import { useAppContext } from "../appContext";
import { Button, Chip, KV, LoadingPanel, PageHeader, Panel } from "../components/ui";
import type { NodeClient } from "../domain/nodeClient";
import { requestDesktopNotificationPermission, sendTestNotification } from "../domain/notifications";
import AccessPanel from "./components/AccessPanel";
import LocalModelPicker from "./components/LocalModelPicker";
import type { ActivityEvent, NodeSettings, PrivacyEraseScope, PrivacyStatus, UpdateStatus } from "../domain/types";

const sections = [
  "Identity & storage",
  "Network",
  "Trust & safety",
  "AI curator",
  "Notifications",
  "Privacy & data",
  "Ranking & publish",
  "Fetch limits",
  "Software updates",
] as const;

export default function Settings() {
  useUILanguage();
  const { client, confirm, notify, refreshShell } = useAppContext();
  const [active, setActive] = useState<(typeof sections)[number]>("Identity & storage");
  const [settings, setSettings] = useState<NodeSettings | null>(null);

  useEffect(() => {
    void client.getSettings().then(setSettings);
  }, [client]);

  if (!settings) return <LoadingPanel />;

  const update = async (patch: Partial<NodeSettings>) => {
    const next = await client.updateSettings(patch);
    setSettings(next);
    await refreshShell();
    notify("ok", tr("Settings updated through local node"));
  };

  const confirmUpdate = (patch: Partial<NodeSettings>, title: string, body: string) =>
    confirm({
      title,
      body,
      risk: "high",
      confirmLabel: tr("Apply change"),
      onConfirm: () => update(patch),
    });

  return (
    <div className="settings-layout">
      <PageHeader
        eyebrow={tr("Settings")}
        title={tr("Advanced settings")}
        context={tr("Manage this device’s connection, privacy, and service preferences.")}

      />
      <aside className="settings-rail">
        {sections.map((section) => (
          <button key={section} type="button" className={active === section ? "active" : ""} onClick={() => setActive(section)}>
            {tr(section)}
          </button>
        ))}
      </aside>
      <Panel className="settings-panel">
        {active === "Identity & storage" ? <IdentitySection settings={settings} onUpdate={update} /> : null}
        {active === "Network" ? <NetworkSection settings={settings} /> : null}
        {active === "Trust & safety" ? (
          <TrustSection settings={settings} onConfirmUpdate={confirmUpdate} />
        ) : null}
        {active === "AI curator" ? (
          <AISection settings={settings} onUpdate={update} onConfirmUpdate={confirmUpdate} />
        ) : null}
        {active === "Notifications" ? (
          <NotificationsSection settings={settings} onUpdate={update} notify={notify} />
        ) : null}
        {active === "Privacy & data" ? (
          <PrivacySection client={client} confirm={confirm} notify={notify} />
        ) : null}
        {active === "Ranking & publish" ? <RankingSection settings={settings} onUpdate={update} /> : null}
        {active === "Fetch limits" ? <FetchSection settings={settings} onUpdate={update} /> : null}
        {active === "Software updates" ? <UpdatesSection client={client} /> : null}
      </Panel>
    </div>
  );
}

function PrivacySection({
  client,
  confirm,
  notify,
}: {
  client: NodeClient;
  confirm: ReturnType<typeof useAppContext>["confirm"];
  notify: (tone: "ok" | "warn" | "danger", text: string) => void;
}) {
  useUILanguage();
  const [status, setStatus] = useState<PrivacyStatus | null>(null);
  const [events, setEvents] = useState<ActivityEvent[]>([]);

  const reload = async () => {
    const [nextStatus, nextEvents] = await Promise.all([
      client.getPrivacyStatus(),
      client.getActivity(),
    ]);
    setStatus(nextStatus);
    setEvents(nextEvents);
  };

  useEffect(() => {
    void reload();
  }, [client]);

  const download = async () => {
    const payload = await client.exportPersonalData();
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `ryn-personal-data-${new Date().toISOString().slice(0, 10)}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
    notify("ok", tr("Personal data export created locally"));
  };

  const erase = (scopes: PrivacyEraseScope[], title: string, body: string) => {
    confirm({
      title,
      body,
      risk: "high",
      confirmLabel: tr("Erase local data"),
      onConfirm: async () => {
        await client.erasePersonalData(scopes);
        await reload();
        notify("ok", tr("Selected personal data erased from this node"));
      },
    });
  };

  return (
    <Section title={tr("Privacy & local data")} icon={<ShieldCheck size={22} />}>
      <div className="privacy-local-callout">
        <HardDrive size={18} />
        <span>
          <b>{tr("Your profile, history, cached reading, and audit trail stay on this node.")}</b>
          <small>{tr("Public content discovery contacts the listed source sites. AI metadata leaves the device only when you explicitly enable cloud access.")}</small>
        </span>
      </div>
      {status ? (
        <KV rows={[
          { label: tr("Storage root"), value: status.storage_root },
          { label: tr("Reading history"), value: tr("{{count}} items", { count: status.reading_history_items }) },
          { label: tr("Recommendation learning"), value: tr("{{feedback}} feedback · {{signals}} signals", { feedback: status.feedback_items, signals: status.learned_signals }) },
          { label: tr("Discovery cache"), value: tr("{{v0}} items · {{v1}} reader pages", { v0: status.cached_discovery_items, v1: status.reader_cache_files }) },
          { label: tr("Assistant audit"), value: tr("{{count}} events", { count: status.audit_events }) },
          { label: tr("Cloud AI"), value: tr(status.cloud_ai_enabled ? "enabled" : "disabled") },
        ]} />
      ) : <p className="muted">{tr("Inspecting local data…")}</p>}
      <div className="button-row">
        <Button icon={Download} variant="primary" onClick={() => void download()}>
          {tr("Export my data")}
        </Button>
        <Button icon={History} onClick={() => erase(["history"], tr("Clear reading history?"), tr("This erases opened items, bookmarks, playback position, and reading progress from this node."))}>
          {tr("Clear history")}
        </Button>
        <Button icon={Trash2} onClick={() => erase(["profile"], tr("Reset recommendation learning?"), tr("This erases your direction, topic and platform choices, and all more/less/hide feedback."))}>
          {tr("Reset learning")}
        </Button>
        <Button icon={Trash2} onClick={() => erase(["cache"], tr("Clear downloaded discovery cache?"), tr("This erases feed results and locally extracted reader pages. Default sources remain and will refill the feed on the next discovery cycle."))}>
          {tr("Clear cache")}
        </Button>
        <Button icon={Trash2} variant="danger" onClick={() => erase(["audit"], tr("Clear assistant audit?"), tr("This permanently erases the local record of assistant activity."))}>
          {tr("Clear audit")}
        </Button>
      </div>
      <div className="privacy-audit">
        <div className="privacy-audit-head"><Activity size={16} /><b>{tr("Recent assistant activity")}</b></div>
        {events.slice(0, 8).map((event) => (
          <div className="privacy-audit-row" key={event.id ?? `${event.t}-${event.text}`}>
            <span>{event.text}</span>
            <small>{event.t ? new Date(event.t).toLocaleString(uiLocale()) : ""}</small>
          </div>
        ))}
        {!events.length ? <p className="muted">{tr("No assistant activity recorded yet.")}</p> : null}
      </div>
    </Section>
  );
}

function NotificationsSection({
  settings,
  onUpdate,
  notify,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
  notify: (tone: "ok" | "warn" | "danger", text: string) => void;
}) {
  useUILanguage();
  const enable = async (enabled: boolean) => {
    if (enabled) {
      const permission = await requestDesktopNotificationPermission();
      if (permission !== "granted") {
        notify("warn", tr("Desktop notification permission was not granted"));
        await onUpdate({ notifications_enabled: false });
        return;
      }
    }
    await onUpdate({ notifications_enabled: enabled });
  };

  return (
    <Section title={tr("Notifications")} icon={<BellRing size={22} />}>
      <label className="toggle-row">
        <input
          type="checkbox"
          checked={settings.notifications_enabled}
          onChange={(event) => void enable(event.target.checked)}
        />
        {tr("Notify me when new recommendations are ready")}
      </label>
      <div className="setting-row">
        <span>
          <b>{tr("Frequency")}</b>
          <small>{tr("The in-app badge always updates immediately")}</small>
        </span>
        <select
          value={settings.notification_frequency}
          onChange={(event) => void onUpdate({
            notification_frequency: event.target.value as NodeSettings["notification_frequency"],
          })}
        >
          <option value="immediate">{tr("Immediate")}</option>
          <option value="hourly">{tr("At most hourly")}</option>
          <option value="daily">{tr("At most daily")}</option>
        </select>
      </div>
      <div className="setting-row">
        <span>
          <b>{tr("Quiet hours")}</b>
          <small>{tr("Local time, start inclusive and end exclusive")}</small>
        </span>
        <div className="setting-edit">
          <input
            type="number"
            min="0"
            max="23"
            value={settings.notification_quiet_start}
            onChange={(event) => void onUpdate({ notification_quiet_start: Number(event.target.value) })}
            aria-label={tr("Quiet hours start")}
          />
          <span>{tr("to")}</span>
          <input
            type="number"
            min="0"
            max="23"
            value={settings.notification_quiet_end}
            onChange={(event) => void onUpdate({ notification_quiet_end: Number(event.target.value) })}
            aria-label={tr("Quiet hours end")}
          />
        </div>
      </div>
      <Button
        icon={BellRing}
        onClick={async () => {
          const sent = await sendTestNotification(() => window.location.assign("/digest"));
          notify(sent ? "ok" : "warn", sent ? tr("Test notification sent") : tr("Notification permission is unavailable"));
        }}
      >
        {tr("Send test notification")}
      </Button>
    </Section>
  );
}

function IdentitySection({
  settings,
  onUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
}) {
  useUILanguage();
  return (
    <Section title={tr("Identity & storage")} icon={<HardDrive size={22} />}>
      <SettingInput label={tr("Device name")} value={settings.node_name} onSave={(value) => onUpdate({ node_name: value })} />
      <ReadOnly label={tr("Storage")} value={settings.node_storage} />
      <ReadOnly label={tr("Trusted roots")} value={settings.trusted_roots.join(", ")} />
    </Section>
  );
}

function NetworkSection({
  settings,
}: {
  settings: NodeSettings;
}) {
  useUILanguage();
  return (
    <Section title={tr("Network")} icon={<Network size={22} />}>
      <div className="alert-callout">
        {tr("Desktop networking is configured automatically at startup. Advanced operators can override RYNMESH_* environment variables before launch.")}
      </div>
      <ReadOnly label={tr("Mode")} value={settings.desktop_managed ? tr("automatic desktop") : tr("operator managed")} />
      <ReadOnly label={tr("Network")} value={settings.network_id ?? "rynmesh-main"} />
      <ReadOnly label={tr("Registry URL")} value={settings.registry_url} />
      <ReadOnly label={tr("Peer HTTP host")} value={settings.peer_http_host} />
      <ReadOnly label={tr("Public endpoint")} value={settings.public_endpoint} />
      <ReadOnly label={tr("Peer HTTP port")} value={String(settings.peer_http_port)} />
    </Section>
  );
}

function TrustSection({
  settings,
  onConfirmUpdate,
}: {
  settings: NodeSettings;
  onConfirmUpdate: (patch: Partial<NodeSettings>, title: string, body: string) => void;
}) {
  useUILanguage();
  return (
    <Section title={tr("Trust & safety")} icon={<ShieldCheck size={22} />}>
      <div className="setting-row">
        <span>
          <b>{tr("Safety policy")}</b>
          <small>{tr("High-risk local policy change")}</small>
        </span>
        <div className="segmented">
          {(["permissive", "standard", "strict"] as const).map((policy) => (
            <button
              key={policy}
              type="button"
              className={settings.safety_policy === policy ? "active" : ""}
              onClick={() =>
                onConfirmUpdate(
                  { safety_policy: policy },
                  tr("Change safety policy?"),
                  tr("This affects what the local node will fetch, propagate, and publish."),
                )
              }
            >
              {tr(policy)}
            </button>
          ))}
        </div>
      </div>
      <ReadOnly label={tr("Trusted root peer IDs")} value={settings.trusted_roots.join(", ")} />
      <AccessPanel />
    </Section>
  );
}

function AISection({
  settings,
  onUpdate,
  onConfirmUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
  onConfirmUpdate: (patch: Partial<NodeSettings>, title: string, body: string) => void;
}) {
  useUILanguage();
  return (
    <Section title={tr("AI curator")} icon={<Sparkles size={22} />}>
      <div className="setting-row">
        <span>
          <b>{tr("Provider")}</b>
          <small>{tr("Local model is the privacy-preserving default")}</small>
        </span>
        <div className="segmented">
          <button type="button" className={settings.ai_provider === "local" ? "active" : ""} onClick={() => void onUpdate({ ai_provider: "local", cloud_access: false })}> {tr("local")} </button>
          <button
            type="button"
            className={settings.ai_provider === "cloud" ? "active" : ""}
            onClick={() =>
              onConfirmUpdate(
                { ai_provider: "cloud", cloud_access: true },
                tr("Enable cloud model access?"),
                tr("Bounded metadata can leave the machine. Full local files still require separate confirmation."),
              )
            }
          > {tr("cloud")} </button>
        </div>
      </div>
      <div className="setting-row setting-row-stacked">
        <span>
          <b>{tr("Local model")}</b>
          <small>{tr("Runs on this machine — nothing leaves it")}</small>
        </span>
      </div>
      <LocalModelPicker />
      <ReadOnly label={tr("Cloud access")} value={tr(settings.cloud_access ? "enabled" : "disabled")} icon={<Cloud size={14} />} />
    </Section>
  );
}

function RankingSection({
  settings,
  onUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
}) {
  useUILanguage();
  return (
    <Section title={tr("Ranking & publish")} icon={<SlidersHorizontal size={22} />}>
      <div className="setting-row">
        <span>
          <b>{tr("Default rank")}</b>
          <small>{tr("Used by Explore and recommendations")}</small>
        </span>
        <select value={settings.rank_default} onChange={(event) => void onUpdate({ rank_default: event.target.value as NodeSettings["rank_default"] })}>
          {["weight", "newest", "trusted", "ai", "novelty"].map((rank) => (
            <option key={rank} value={rank}>{tr(rank)}</option>
          ))}
        </select>
      </div>
      <div className="setting-row">
        <span>
          <b>{tr("Publish visibility")}</b>
          <small>{tr("Default for new publish drafts")}</small>
        </span>
        <select value={settings.publish_visibility} onChange={(event) => void onUpdate({ publish_visibility: event.target.value as NodeSettings["publish_visibility"] })}>
          {["network", "trusted", "local"].map((visibility) => (
            <option key={visibility} value={visibility}>{tr(visibility)}</option>
          ))}
        </select>
      </div>
    </Section>
  );
}

function FetchSection({
  settings,
  onUpdate,
}: {
  settings: NodeSettings;
  onUpdate: (patch: Partial<NodeSettings>) => Promise<void>;
}) {
  useUILanguage();
  const pct = Math.round((settings.fetch_used_mb / settings.fetch_budget_mb) * 100);
  return (
    <Section title={tr("Fetch limits")} icon={<HardDrive size={22} />}>
      <KV
        rows={[
          { label: tr("Daily budget"), value: `${settings.fetch_budget_mb} MB` },
          { label: tr("Used today"), value: `${settings.fetch_used_mb} MB` },
          { label: tr("Timeout"), value: `${settings.fetch_timeout_s}s` },
        ]}
      />
      <div className={`budget-bar${pct > 80 ? " budget-warn" : ""}`}>
        <span style={{ width: `${Math.min(100, pct)}%` }} />
      </div>
      <SettingInput
        label={tr("Fetch timeout seconds")}
        value={String(settings.fetch_timeout_s)}
        onSave={(value) => onUpdate({ fetch_timeout_s: Number(value) || settings.fetch_timeout_s })}
      />
    </Section>
  );
}

function UpdatesSection({ client }: { client: NodeClient }) {
  useUILanguage();
  const [upd, setUpd] = useState<UpdateStatus | null>(null);

  useEffect(() => {
    let alive = true;
    void client
      .updatesStatus()
      .then((s) => {
        if (alive) setUpd(s);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [client]);

  return (
    <Section title={tr("Software updates")} icon={<DownloadCloud size={22} />}>
      <p className="muted">
        {tr("Current version:")} <span className="mono">{upd?.currentVersion ?? "…"}</span>
      </p>
      {upd?.manualInstallRequired ? (
        <p className="muted">{tr("Install a newer Ryn desktop package to update this app.")}</p>
      ) : <label className="toggle-row">
        <input
          type="checkbox"
          checked={upd?.autoUpdate ?? true}
          onChange={async (event) => {
            await client.setAutoUpdate(event.target.checked);
            setUpd(await client.updatesCheck());
          }}
        />
        {tr("Automatically install updates")}
      </label>}
      {!upd?.manualInstallRequired && upd?.availableVersion ? (
        <div className="update-banner">
          <span>{tr("Version {{version}} available.", { version: upd.availableVersion })}</span>
          <Button onClick={() => void client.updatesApply()}>{tr("Update now")}</Button>
        </div>
      ) : null}
      {upd?.lastError ? <p className="service-status error">⚠ {upd.lastError}</p> : null}
    </Section>
  );
}

function Section({ title, icon, children }: { title: string; icon: ReactNode; children: ReactNode }) {
  useUILanguage();
  return (
    <div className="settings-section">
      <div className="section-title">
        {icon}
        <div>
          <span className="eyebrow">{tr("Settings")}</span>
          <h2>{title}</h2>
        </div>
      </div>
      {children}
    </div>
  );
}

function ReadOnly({ label, value, icon }: { label: string; value: string; icon?: ReactNode }) {
  useUILanguage();
  return (
    <div className="setting-row">
      <span>
        <b>{label}</b>
        <small>{tr("Read only")}</small>
      </span>
      <code>
        {icon}
        {value}
      </code>
    </div>
  );
}

function SettingInput({
  label,
  value,
  onSave,
}: {
  label: string;
  value: string;
  onSave: (value: string) => Promise<void>;
}) {
  useUILanguage();
  const [current, setCurrent] = useState(value);
  return (
    <div className="setting-row">
      <span>
        <b>{label}</b>
        <small>{tr("Saved on this device")}</small>
      </span>
      <div className="setting-edit">
        <input value={current} onChange={(event) => setCurrent(event.target.value)} />
        <Button icon={Save} onClick={() => void onSave(current)}>
          {tr("Save")}
        </Button>
      </div>
    </div>
  );
}
