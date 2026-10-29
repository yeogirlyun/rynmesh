import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowDownToLine,
  Check,
  ChevronRight,
  Copy,
  Globe2,
  Moon,
  Pencil,
  Plus,
  Search,
  X,
} from "lucide-react";
import { useAppContext } from "../appContext";
import type { LLMOrderResult } from "../domain/nodeClient";
import type { WorkResult } from "../domain/types";
import {
  Arrow,
  DeviceArt,
  Empty,
  Modal,
  Note,
  PageHeading,
  ServiceArt,
  Status,
  Tabs,
} from "./components";
import { EditDevice, PairDevice, ShareServices } from "./Dialogs";
import { DeviceTopology } from "./DeviceTopology";
import { DevicesWorkspace } from "./drop/DevicesWorkspace";
import { LanguageSelect } from "./LanguageSelect";
import { nativeModelRequest, nativeBusy } from "../domain/nativeModel";
import { tr } from "../uiI18n";
import { personalHref, usePersonal, type Device, type Service } from "./model";
import { groupHomeDevices, readHomeView, writeHomeView, type HomeView } from "./homeModel";
import { DeviceHero, DeviceRail, HomeServices, HomeStats, NearbyPanel, ViewSwitch } from "./HomeParts";
import { SegTabs, ServiceDetail, ServiceTable, ServicesBand } from "./ServicesParts";

function useLinks() {
  const { demo } = usePersonal();
  return (path: string) => personalHref(path, demo);
}
export function PersonalHome() {
  const { t } = useTranslation();
  const { devices, services, loading, error, refresh, demo } = usePersonal();
  const [selectedId, setSelectedId] = useState("");
  const [view, setView] = useState<HomeView>(readHomeView);
  const [pair, setPair] = useState(false);
  const [edit, setEdit] = useState<Device | null>(null);
  const [share, setShare] = useState(false);
  const owned = devices.filter((device) => device.own);
  // Seen on the network (peer record or service advertisement) but not owned: shown here
  // so a device whose services appear in Services is never invisible on Home.
  const nearby = demo ? [] : devices.filter((device) => !device.own && !device.self);
  const selected = owned.find((device) => device.id === selectedId) || owned[0];
  const offered = services.filter((service) => service.deviceId === selected?.id);
  const groups = groupHomeDevices(owned);
  const tone = groups.find((group) => group.devices.some((device) => device.id === selected?.id))?.tone ?? "blue";
  const changeView = (next: HomeView) => {
    setView(next);
    writeHomeView(next);
  };
  const workspace = (columns: 2 | 3) => (
    <>
      <NearbyPanel nearby={nearby} services={services} />
      {selected && (
        <>
          <DeviceHero
            device={selected}
            tone={tone}
            serviceTotal={offered.length}
            serviceReady={offered.filter((service) => service.online).length}
            onEdit={() => setEdit(selected)}
            onShare={() => setShare(true)}
          />
          <HomeServices device={selected} offered={offered} loading={loading} columns={columns} />
        </>
      )}
    </>
  );
  return (
    <div className="pf-page rh-home">
      <PageHeading
        title={t("nav.home")}
        description={t("pages.homeSubtitle")}
      >
        <button className="rh-btn primary" onClick={() => setPair(true)}>
          <Plus size={19} />
          {t("personal.addDevice")}
        </button>
      </PageHeading>
      <HomeStats owned={owned} services={services} />
      {error && (
        <div className="pf-callout">
          {error}
          <button className="pf-link" onClick={() => void refresh()}>
            {t("personal.retry")}
          </button>
        </div>
      )}
      <div className="rh-head2">
        <h2>
          {t("personal.myDevices")} <span>({owned.length})</span>
        </h2>
        <ViewSwitch view={view} onChange={changeView} />
      </div>
      {view === "list" ? (
        <div className="rh-bench">
          <DeviceRail
            groups={groups}
            selectedId={selected?.id}
            loading={loading}
            onSelect={setSelectedId}
            onShowMap={() => changeView("map")}
          />
          <div className="rh-work">{workspace(2)}</div>
        </div>
      ) : (
        <>
          <div className="rh-map">
            <DeviceTopology
              devices={owned}
              selected={selected}
              loading={loading}
              demo={demo}
              hideHeading
              hideDetail
              onSelect={setSelectedId}
              onEdit={setEdit}
              onShare={() => setShare(true)}
            />
          </div>
          <div className="rh-work wide">{workspace(3)}</div>
        </>
      )}
      {pair && <PairDevice onClose={() => setPair(false)} />}{" "}
      {edit && <EditDevice device={edit} onClose={() => setEdit(null)} />}{" "}
      {share && selected && (
        <ShareServices device={selected} onClose={() => setShare(false)} />
      )}
    </div>
  );
}
export function PersonalServices() {
  const { t } = useTranslation();
  const { services, devices, resolveName, demo, loading, error, refresh } =
    usePersonal();
  const [params] = useSearchParams();
  const [filter, setFilter] = useState("All");
  const [query, setQuery] = useState("");
  const [selectedId, setSelectedId] = useState(params.get("service") || "");
  useEffect(() => {
    const target = params.get("service");
    if (target) {
      setSelectedId(target);
      setFilter("All");
      setQuery("");
    }
  }, [params]);
  const [tab, setTab] = useState<"Overview" | "API access">("Overview");
  const [share, setShare] = useState(false);
  const href = useLinks();
  // Usable services first, then known-but-offline, then those that need a connection.
  const rank = (service: Service) =>
    service.availability === "needs_connection" ? 2 : service.online ? 0 : 1;
  const isOwn = (service: Service) =>
    devices.find((device) => device.id === service.deviceId)?.own;
  const otherFilter = demo ? "Shared with me" : "Discovered";
  const counts: Record<string, number> = {
    All: services.length,
    "My devices": services.filter((service) => isOwn(service)).length,
    [otherFilter]: services.filter((service) => !isOwn(service)).length,
  };
  const visible = services.filter((service) => {
    const own = isOwn(service);
    return (
      (filter === "All" || (filter === "My devices" ? own : !own)) &&
      `${service.title} ${resolveName(service.deviceId)}`
        .toLowerCase()
        .includes(query.toLowerCase())
    );
  }).sort((a, b) => rank(a) - rank(b));
  const selected =
    visible.find((service) => service.id === selectedId) || visible[0];
  const owner = devices.find((device) => device.id === selected?.deviceId);
  return (
    <div className="pf-page rs-services">
      <PageHeading
        title={t("nav.services")}
        description={t("pages.servicesSubtitle")}
      >
        <Link className="rs-btn primary" to={href("/services/manage")}>
          <Plus size={16} aria-hidden="true" />
          {t("personal.addService")}
        </Link>
      </PageHeading>
      {services.length > 0 && <ServicesBand services={services} />}
      <div className="rs-bar">
        <SegTabs
          options={["All", "My devices", otherFilter]}
          value={filter}
          onChange={setFilter}
          counts={counts}
          label={t("personal.filter")}
        />
        <label className="rs-field">
          <Search size={16} aria-hidden="true" />
          <input
            aria-label={t("personal.searchServices")}
            placeholder={t("personal.searchServices")}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      {error && (
        <Note>
          {error}{" "}
          <button className="pf-link" onClick={() => void refresh()}>
            {t("personal.retry")}
          </button>
        </Note>
      )}
      <div className="rs-cols">
        <ServiceTable
          services={visible}
          selectedId={selected?.id}
          onSelect={setSelectedId}
          empty={
            <Empty
              title={loading ? t("personal.findingServices") : t("personal.noMatchingServices")}
            >
              {t("personal.addAServiceOnThisDeviceOrTryAnotherFilter")}
            </Empty>
          }
        />
        {selected && (
          <ServiceDetail
            service={selected}
            tab={tab}
            onTab={setTab}
            onManage={() => setShare(true)}
          />
        )}
      </div>
      <div className="rs-foot">
        <Link className="rs-link" to={href("/services/catalog")}>
          {t("personal.moreServiceTools")} <Arrow />
        </Link>
        <span>
          {demo
            ? t("personal.requestAccessBeforeUsingAServiceThatIsNotSharedWithYou")
            : t("personal.discoveredServicesAreNotProofOfOwnershipOrGrantedAccessTheProviderChecksEveryRequest")}
        </span>
      </div>
      {share && owner && (
        <ShareServices device={owner} onClose={() => setShare(false)} />
      )}
    </div>
  );
}
// The devices route is the device workspace: network map, conversations and transfers.
export function PersonalDevices() {
  return <DevicesWorkspace />;
}

type Task = {
  id: string;
  title: string;
  deviceId: string;
  kind: Service["kind"];
  status: string;
  input?: string;
  result?: string;
  llm?: boolean;
  incoming?: boolean;
  sample?: boolean;
  titleKey?: string;
  localModel?: boolean;
};
const SAMPLE_TASKS: Task[] = [
  {
    id: "sample-transcription",
    title: "Meeting transcription",
    titleKey: "personal.meetingTranscription",
    deviceId: "preview-alex",
    kind: "audio",
    status: "running",
    input: "meeting.wav",
    sample: true,
  },
  {
    id: "sample-document",
    title: "Convert proposal",
    titleKey: "personal.convertProposal",
    deviceId: "peer:fixture-llm-provider",
    kind: "document",
    status: "completed",
    input: "proposal.docx",
    result: "Sample document conversion result. This is design preview data.",
    sample: true,
  },
  {
    id: "sample-failed",
    title: "Transcribe interview",
    titleKey: "personal.transcribeInterview",
    deviceId: "preview-studio",
    kind: "audio",
    status: "failed",
    input: "interview.mp3",
    sample: true,
  },
];
function taskState(state: string) {
  if (["succeeded", "completed"].includes(state)) return "Completed";
  if (["failed", "timed_out", "cancelled", "rejected"].includes(state))
    return state === "cancelled" ? "Cancelled" : "Failed";
  return "Processing";
}
export function PersonalTasks() {
  const { t } = useTranslation();
  const { client, node, notify } = useAppContext();
  const { demo, devices, resolveName } = usePersonal();
  const [tasks, setTasks] = useState<Task[]>(demo ? SAMPLE_TASKS : []);
  const [direction, setDirection] = useState("Requested by me");
  const [filter, setFilter] = useState("All");
  const [selectedId, setSelectedId] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let active = true;
    const update = async () => {
      const results = await Promise.allSettled([
        client.listLLMOrders(),
        client.listWorkResults(),
        client.listLLMProviderOrders(),
          demo ? Promise.resolve(null) : nativeModelRequest().catch(() => null),
      ]);
      if (!active) return;
      const llm = results[0].status === "fulfilled" ? results[0].value : [];
      const work = results[1].status === "fulfilled" ? results[1].value : [];
      const native = results[3].status === "fulfilled" ? results[3].value : null;
      const provider =
        results[2].status === "fulfilled" ? results[2].value : [];
      setError(
        results.some((result) => result.status === "rejected")
          ? t("personal.someTasksCouldNotBeLoadedRetryingAutomatically")
          : "",
      );
      setTasks([
        ...(demo ? SAMPLE_TASKS : []),
        ...(native && (nativeBusy(native) || native.state === "error") ? [{
          id: "local-model", title: `${tr("本地模型")} · ${native.name}`,
          deviceId: node.peer_id, kind: "ai" as const,
          status: native.state === "error" ? "failed" : "running", localModel: true,
        }] : []),
        ...llm.map(
          (order: LLMOrderResult): Task => ({
            id: order.task_id,
            title: order.model_alias
              ? `${t("personal.aiChat")} · ${order.model_alias}`
              : t("personal.aiChat"),
            deviceId: order.provider_peer_id || "",
            kind: "ai",
            status: order.state,
            result: order.output,
            llm: true,
          }),
        ),
        ...provider.map(
          (order: LLMOrderResult): Task => ({
            id: `provider-${order.task_id}`,
            title: order.model_alias
              ? `${t("personal.aiChat")} · ${order.model_alias}`
              : t("personal.aiChat"),
            deviceId: node.peer_id,
            kind: "ai",
            status: order.state,
            incoming: true,
          }),
        ),
        ...work.map(
          (order: WorkResult): Task => ({
            id: order.work_order_id,
            title: order.message || t("personal.serviceTask"),
            deviceId: order.provider_peer_id,
            kind: "network",
            status: order.status,
            incoming: order.provider_peer_id === node.peer_id,
          }),
        ),
      ]);
    };
    void update();
    const timer = window.setInterval(() => void update(), 5000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [client, demo, node.peer_id, t]);
  const visible = tasks.filter(
    (task) =>
      (direction === "Running on this device"
        ? task.incoming
        : !task.incoming) &&
      (filter === "All" ||
        (filter === "In progress"
          ? taskState(task.status) === "Processing"
          : taskState(task.status) === filter)),
  );
  const selected = visible.find((task) => task.id === selectedId);
  const download = (task: Task) => {
    const url = URL.createObjectURL(
      new Blob([task.result || ""], { type: "text/plain;charset=utf-8" }),
    );
    const a = document.createElement("a");
    a.href = url;
    a.download = `${task.sample ? "sample-" : ""}${task.id.replace(/[^a-zA-Z0-9_-]/g, "_")}.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };
  return (
    <div className="pf-page">
      <PageHeading
        title={t("nav.tasks")}
        description={t("pages.tasksSubtitle")}
      />
      <div className={`pf-tasks-layout${selected ? " with-detail" : ""}`}>
        <section>
          <Tabs
            options={["Requested by me", "Running on this device"]}
            value={direction}
            onChange={setDirection}
          />
          <div className="pf-task-filters">
            <Tabs
              options={["All", "In progress", "Completed", "Failed"]}
              value={filter}
              onChange={setFilter}
            />
          </div>
          {error && <Note>{error}</Note>}
          <div className="pf-table-wrap">
            <table className="pf-task-table">
              <thead>
                <tr>
                  <th>{t("personal.task")}</th>
                  <th>{t("personal.runsOn")}</th>
                  <th>{t("personal.status")}</th>
                  <th>{t("personal.action")}</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((task) => (
                  <tr
                    key={task.id}
                    className={selected?.id === task.id ? "selected" : ""}
                  >
                    <td>
                      <div className="pf-inline">
                        <ServiceArt kind={task.kind} small />
                        <div>
                          <strong>{task.titleKey ? t(task.titleKey) : task.title}</strong>
                          <small>{task.input || task.id}</small>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className="pf-inline">
                        <DeviceArt
                          small
                          kind={
                            devices.find(
                              (device) => device.id === task.deviceId,
                            )?.kind
                          }
                        />
                        <span>
                          {task.deviceId
                            ? resolveName(task.deviceId)
                            : t("personal.notReported")}
                        </span>
                      </div>
                    </td>
                    <td>
                      <Status
                        online={taskState(task.status) === "Completed"}
                        label={taskState(task.status)}
                      />
                    </td>
                    <td>
                      <button
                        className="pf-button"
                        onClick={() => task.localModel ? window.location.assign("/services/sources?model=local") : setSelectedId(task.id)}
                      >
                        {t("personal.view")} <ChevronRight size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!visible.length && (
              <Empty title={t("personal.noTasksYet")}>
                {t("personal.tasksWillAppearHereWhenYouUseAService")}
              </Empty>
            )}
          </div>
          <Note>
            {t("personal.ifATaskFailsDueToAConnectionIssueYouCanTryItAgainLater")}
          </Note>
        </section>
        {selected && (
          <aside className="pf-panel pf-task-detail">
            <div className="pf-section-heading">
              <h2>{selected.titleKey ? t(selected.titleKey) : selected.title}</h2>
              <button
                className="pf-icon-button"
                aria-label={t("personal.closeTaskDetails")}
                onClick={() => setSelectedId("")}
              >
                <X size={18} />
              </button>
            </div>
            <p>{t("personal.runsOn")}</p>
            <div className="pf-inline">
              <DeviceArt small />
              <strong>
                {selected.deviceId
                  ? resolveName(selected.deviceId)
                  : t("personal.notReportedByThisTask")}
              </strong>
            </div>
            <p>{t("personal.service")}</p>
            <div className="pf-inline">
              <ServiceArt kind={selected.kind} />
              <strong>
                {selected.kind === "ai"
                  ? t("personal.aiChat")
                  : selected.kind === "audio"
                    ? t("personal.transcription")
                    : selected.kind === "document"
                      ? t("personal.documentConverter")
                      : t("personal.serviceTask")}
              </strong>
            </div>
            <p>{t("personal.status")}</p>
            <Status
              online={taskState(selected.status) === "Completed"}
              label={taskState(selected.status)}
            />
            {selected.sample && (
              <ol className="pf-timeline">
                {["Submitted", "Accepted", "Processing", "Result ready"].map(
                  (step, index) => (
                    <li
                      key={t(`personal.${({ Submitted: "submitted", Accepted: "accepted", Processing: "processing", "Result ready": "resultReady" } as Record<string, string>)[step]}`)}
                      className={
                        index <
                        (taskState(selected.status) === "Completed" ? 4 : 2)
                          ? "done"
                          : ""
                      }
                    >
                      <span>
                        {index < 2 ||
                        taskState(selected.status) === "Completed" ? (
                          <Check size={12} />
                        ) : (
                          ""
                        )}
                      </span>
                      {t(`personal.${({ Submitted: "submitted", Accepted: "accepted", Processing: "processing", "Result ready": "resultReady" } as Record<string, string>)[step]}`)}
                    </li>
                  ),
                )}
              </ol>
            )}
            {selected.input && (
              <>
                <p>{t("personal.inputFile")}</p>
                <strong>{selected.input}</strong>
              </>
            )}
            {selected.result && taskState(selected.status) === "Completed" && (
              <button
                className="pf-button full"
                onClick={() => download(selected)}
              >
                <ArrowDownToLine size={16} />
                {t("personal.downloadResult")}
              </button>
            )}
            {selected.llm && taskState(selected.status) === "Processing" && (
              <button
                className="pf-button full"
                disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  try {
                    const result = await client.cancelLLMOrder(selected.id);
                    setTasks((current) =>
                      current.map((task) =>
                        task.id === selected.id
                          ? { ...task, status: result.state }
                          : task,
                      ),
                    );
                    notify(
                      "info",
                      result.state === "cancelled"
                        ? t("personal.taskCancelled")
                        : t("personal.cancellationRequestedWaitingForTheProcessingDevice"),
                    );
                  } catch {
                    notify("danger", t("personal.couldNotCancelTheTask"));
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? t("personal.requestingCancellation") : t("personal.cancelTask")}
              </button>
            )}
            {selected.sample && <Note>{t("personal.sampleTaskNoWorkIsRunning")}</Note>}
          </aside>
        )}
      </div>
    </div>
  );
}
export function PersonalSettings() {
  const { t } = useTranslation();
  const { appearance, setAppearance, demo } = usePersonal();
  const { node, registry, refreshShell, notify } = useAppContext();
  const href = useLinks();
  const [diagnostics, setDiagnostics] = useState(false);
  return (
    <div className="pf-page pf-settings">
      <PageHeading
        title={t("nav.settings")}
        description={t("settings.subtitle")}
      />
      <section className="pf-settings-general">
        <h2 className="pf-settings-group-label">{t("settings.general")}</h2>
        <div className="pf-settings-control-row">
          <Globe2 size={21} strokeWidth={1.7} aria-hidden="true" />
          <div className="pf-settings-control-copy">
            <h3>{t("settings.language")}</h3>
            <p>{t("settings.languageHelp")}</p>
          </div>
          <LanguageSelect />
        </div>
        <div className="pf-settings-control-row">
          <Moon size={21} strokeWidth={1.7} aria-hidden="true" />
          <div className="pf-settings-control-copy">
            <h3>{t("settings.appearance")}</h3>
            <p>{t("settings.appearanceHelp")}</p>
          </div>
          <div className="pf-appearance-inline">
          {(["light", "dark", "system"] as const).map((theme) => (
            <button
              type="button"
              aria-pressed={appearance === theme}
              className={appearance === theme ? "selected" : ""}
              key={theme}
              onClick={() => setAppearance(theme)}
            >
              {t(`settings.${theme}`)}
            </button>
          ))}
          </div>
        </div>
        <div className="pf-settings-control-row">
          <div className="pf-settings-control-copy no-icon">
            <h3>{t("nav.personalSpace")}</h3>
            <p>{t("settings.personalSpaceHelp")}</p>
          </div>
          <Link className="pf-button" to={personalHref("/settings/space", demo)}>{t("settings.manageSpace")} <ChevronRight size={16} /></Link>
        </div>
      </section>
      <section className="pf-setting-row"><div><h2>{t("nav.desktop")}</h2><p>{t("settings.desktopHelp")}</p></div><Link className="pf-button" to={href("/settings/desktop")}>{t("settings.desktopSettings")} <ChevronRight size={16} /></Link></section>
      <section className="pf-setting-row"><div><h2>{t("nav.agentExtensions")}</h2><p>{t("settings.agentExtensionsHelp")}</p></div><Link className="pf-button" to={href("/settings/extensions")}>{t("settings.manageAgentExtensions")} <ChevronRight size={16} /></Link></section>
      <section>
        <div className="pf-setting-row">
          <div>
            <h2>{t("settings.connection")}</h2>
            <Status
              online={registry.status === "connected"}
              label={t("settings.status", { status: t(registry.status === "connected" ? "settings.connected" : "settings.disconnected") })}
            />
            <p>{t("settings.connectionHelp")}</p>
          </div>
          <button className="pf-button" onClick={() => setDiagnostics(true)}>
            {t("settings.diagnostics")}
          </button>
        </div>
      </section>
      <section>
        <h2>{t("settings.dataPrivacy")}</h2>
        <Link className="pf-setting-row" to={href("/devices")}>
          <div>
            <h3>{t("settings.privateNotes")}</h3>
            <p>{t("settings.privateNotesHelp")}</p>
          </div>
          <Arrow />
        </Link>
        <div className="pf-setting-row">
          <div>
            <h3>{t("settings.localHistory")}</h3>
            <p>{t("settings.localHistoryHelp")}</p>
          </div>
          <Link className="pf-button" to={href("/settings/advanced")}>
            {t("settings.manage")}
          </Link>
        </div>
      </section>
      <Link className="pf-link" to={href("/settings/advanced")}>
        {t("nav.advancedSettings")} <Arrow />
      </Link>
      {diagnostics && (
        <Modal
          title={t("settings.diagnostics")}
          onClose={() => setDiagnostics(false)}
        >
          <dl className="pf-properties">
            <div>
              <dt>{t("settings.thisDevice")}</dt>
              <dd>{node.node_name}</dd>
            </div>
            <div>
              <dt>{t("settings.localService")}</dt>
              <dd>{t(node.daemon_running ? "settings.running" : "settings.stopped")}</dd>
            </div>
            <div>
              <dt>{t("settings.discoveryConnection")}</dt>
              <dd>{registry.status}</dd>
            </div>
            <div>
              <dt>{t("settings.knownDevices")}</dt>
              <dd>{node.peer_count}</dd>
            </div>
          </dl>
          <footer className="pf-dialog-actions">
            <button
              className="pf-button primary"
              onClick={async () => {
                await refreshShell();
                notify("info", t("settings.checkComplete"));
              }}
            >
              {t("settings.checkAgain")}
            </button>
          </footer>
        </Modal>
      )}
    </div>
  );
}
