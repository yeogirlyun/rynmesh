import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ArrowDownToLine,
  Check,
  ChevronRight,
  Copy,
  ExternalLink,
  Globe2,
  Link2,
  Moon,
  Pencil,
  Plus,
  Search,
  Share2,
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
import { LanguageSelect } from "./LanguageSelect";
import { nativeModelRequest, nativeBusy } from "../domain/nativeModel";
import { tr } from "../uiI18n";
import { personalHref, usePersonal, type Device, type Service } from "./model";

function useLinks() {
  const { demo } = usePersonal();
  return (path: string) => personalHref(path, demo);
}
function serviceUrl(service: Service) {
  if (service.href) return service.href;
  const params = new URLSearchParams({
    peer: service.record?.peer_id || service.deviceId,
    service: service.record?.service.package_id || "",
  });
  return `/services/private-ai/chat?${params}`;
}
function ServiceRow({
  service,
  onSelect,
  selected,
  compact = false,
}: {
  service: Service;
  onSelect?: () => void;
  selected?: boolean;
  compact?: boolean;
}) {
  const { t } = useTranslation();
  const { resolveName, devices, demo } = usePersonal();
  const href = useLinks();
  const [preview, setPreview] = useState(false);
  const navigate = useNavigate();
  return (
    <article
      className={`pf-service-row${selected ? " selected" : ""}${compact ? " compact" : ""}`}
    >
      <button
        className="pf-service-select"
        onClick={
          onSelect ||
          (() =>
            service.preview
              ? setPreview(true)
              : navigate(href(serviceUrl(service))))
        }
      >
        <ServiceArt kind={service.kind} brand={service.brand} />
        <div>
          <h3 className="pf-service-title">{service.title}{service.accessLabel && <span className="pf-access-label">{service.accessLabel}</span>}</h3>
          <p>
            {resolveName(service.deviceId)} ·{" "}
            {devices.find((device) => device.id === service.deviceId)?.own
              ? t("personal.myDevice")
              : demo
                ? t("personal.sharedWithMe")
                : t("personal.discoveredDevice")}
          </p>
          {!compact && <small>{service.description}</small>}
        </div>
      </button>
      {!compact && (
        <Status
          online={service.online}
          label={service.online ? "Ready" : "Offline"}
        />
      )}
      <button
        className={`pf-button${service.kind === "ai" ? " primary" : ""}`}
        disabled={!service.online}
        onClick={() =>
          service.preview
            ? setPreview(true)
            : navigate(href(serviceUrl(service)))
        }
      >
        {service.kind === "ai" ? t("personal.openChat") : t("personal.open")}
      </button>
      {onSelect && (
        <button
          className="pf-icon-button"
          aria-label={t("personal.viewNameDetails", { name: service.title })}
          onClick={onSelect}
        >
          <Arrow />
        </button>
      )}
      {preview && (
        <Modal title={service.title} onClose={() => setPreview(false)}>
          <ServiceArt kind={service.kind} />
          <p>
            {t("personal.thisServiceIsShownAsSampleDataInTheDesignPreviewItIsNotInstalledOrRunning")}
          </p>
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={() => setPreview(false)}>
              {t("personal.close")}
            </button>
          </footer>
        </Modal>
      )}
    </article>
  );
}
export function PersonalHome() {
  const { t } = useTranslation();
  const { devices, services, loading, error, refresh, demo } = usePersonal();
  const [selectedId, setSelectedId] = useState("");
  const [pair, setPair] = useState(false);
  const [edit, setEdit] = useState<Device | null>(null);
  const [share, setShare] = useState(false);
  const href = useLinks();
  const owned = devices.filter((device) => device.own);
  const selected = owned.find((device) => device.id === selectedId) || owned[0];
  const offered = services.filter(
    (service) => service.deviceId === selected?.id,
  );
  return (
    <div className="pf-page">
      <PageHeading
        title={t("nav.home")}
        description={t("pages.homeSubtitle")}
      >
        <button className="pf-button dark" onClick={() => setPair(true)}>
          <Plus size={19} />
          {t("personal.addDevice")}
        </button>
      </PageHeading>
      {error && (
        <div className="pf-callout">
          {error}
          <button className="pf-link" onClick={() => void refresh()}>
            {t("personal.retry")}
          </button>
        </div>
      )}
      <DeviceTopology
        devices={owned}
        selected={selected}
        loading={loading}
        demo={demo}
        onSelect={setSelectedId}
        onEdit={setEdit}
        onShare={() => setShare(true)}
      />
      {selected && (
        <div className="pf-home-services">
          <section>
            <div className="pf-section-heading">
              <div>
                <h2>{t("personal.servicesOnName", { name: selected.name })}</h2>
                <p>{t("personal.fromYourName", { name: selected.name })}</p>
              </div>
            </div>
            <div className="pf-panel pf-service-stack">
              {offered.map((service) => (
                <ServiceRow service={service} compact key={service.id} />
              ))}
              {!offered.length && (
                <Empty
                  title={
                    loading ? t("personal.findingServices") : t("personal.noServicesOnThisDevice")
                  }
                >
                  {loading ? (
                    t("personal.checkingYourConnectedDevices")
                  ) : (
                    <Link to={href("/services/manage")}>
                      {t("personal.setUpAServiceOnThisDevice")}
                    </Link>
                  )}
                </Empty>
              )}
              <Link className="pf-browse-row" to={href("/services")}>
                <Share2 size={29} strokeWidth={1.4} />
                <div>
                  <h3>{t("personal.fromOtherDevices")}</h3>
                  <small>{t("personal.browseServicesFromYourOtherDevices")}</small>
                </div>
                <span>
                  {t("personal.browseServices")} <Arrow />
                </span>
              </Link>
            </div>
          </section>
        </div>
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
  const [tab, setTab] = useState("Overview");
  const [share, setShare] = useState(false);
  const href = useLinks();
  const visible = services.filter((service) => {
    const own = devices.find((device) => device.id === service.deviceId)?.own;
    return (
      (filter === "All" || (filter === "My devices" ? own : !own)) &&
      `${service.title} ${resolveName(service.deviceId)}`
        .toLowerCase()
        .includes(query.toLowerCase())
    );
  });
  const selected =
    visible.find((service) => service.id === selectedId) || visible[0];
  const owner = devices.find((device) => device.id === selected?.deviceId);
  return (
    <div className="pf-page">
      <PageHeading
        title={t("nav.services")}
        description={t("pages.servicesSubtitle")}
      >
        <Link className="pf-button primary" to={href("/services/manage")}>
          <Plus size={19} />
          {t("personal.addService")}
        </Link>
      </PageHeading>
      <div className="pf-filter-row">
        <Tabs
          options={[
            "All",
            "My devices",
            demo ? "Shared with me" : "Discovered",
          ]}
          value={filter}
          onChange={setFilter}
        />
        <label className="pf-search">
          <Search size={18} />
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
      <div className="pf-service-layout">
        <div className="pf-service-list">
          {visible.map((service) => (
            <ServiceRow
              key={service.id}
              service={service}
              selected={selected?.id === service.id}
              onSelect={() => setSelectedId(service.id)}
            />
          ))}
          {!visible.length && (
            <Empty
              title={loading ? t("personal.findingServices") : t("personal.noMatchingServices")}
            >
              {t("personal.addAServiceOnThisDeviceOrTryAnotherFilter")}
            </Empty>
          )}
        </div>
        {selected && (
          <aside className="pf-panel pf-service-detail">
            <div className="pf-detail-lockup">
              <ServiceArt kind={selected.kind} brand={selected.brand} />
              <div>
                <h2>{selected.title}</h2>
                <p>{t("personal.onName", { name: resolveName(selected.deviceId) })}</p>
              </div>
            </div>
            <Tabs
              options={["Overview", "API access"]}
              value={tab}
              onChange={setTab}
            />
            {tab === "Overview" ? (
              <>
                <p className="pf-description">
                  {selected.description}
                  {selected.accessLabel && <><br />{t("personal.accessMethodMethod", { method: selected.accessLabel })}</>}
                </p>
                <h3>{t("personal.access")}</h3>
                <p>
                  {demo
                    ? owner?.own
                      ? t("personal.onlyMyDevices")
                      : t("personal.sharedWithMe")
                    : t("personal.controlledByTheServiceProvider")}
                </p>
                <div className="pf-button-row">
                  <button
                    className="pf-button"
                    onClick={() => setTab("API access")}
                  >
                    <Link2 size={15} />
                    {t("personal.apiAccess")}
                  </button>
                  {owner?.own && (
                    <button
                      className="pf-button"
                      onClick={() => setShare(true)}
                    >
                      <Share2 size={15} />
                      {t("personal.manageAccess")}
                    </button>
                  )}
                </div>
                <hr />
                <h3>{t("personal.manageOnName", { name: resolveName(selected.deviceId) })}</h3>
                <p>{t("personal.serviceSetupIsManagedOnTheDeviceRunningIt")}</p>
                <Link
                  className="pf-link"
                  to={href(
                    `/devices?device=${encodeURIComponent(selected.deviceId)}`,
                  )}
                >
                  {t("personal.viewDevice")} <ExternalLink size={14} />
                </Link>
              </>
            ) : (
              <>
                <p className="pf-description">
                  {t("personal.connectYourAppsAndAgentsThroughTheLocalInferenceAPI")}
                </p>
                {selected.kind === "ai" ? (
                  <Link
                    className="pf-button"
                    to={href("/services/api")}
                  >
                    <Link2 size={16} />
                    {t("personal.configureAPIAccess")}
                  </Link>
                ) : (
                  <Note>
                    {selected.preview
                      ? t("personal.thisSampleServiceDoesNotProvideAWorkingAPI")
                      : t("personal.apiDetailsAreProvidedByTheServiceOwner")}
                  </Note>
                )}
              </>
            )}
          </aside>
        )}
      </div>
      <Link
        className="pf-link"
        style={{ marginTop: 22 }}
        to={href("/services/catalog")}
      >
        {t("personal.moreServiceTools")} <Arrow />
      </Link>
      <Note>
        {demo
          ? t("personal.requestAccessBeforeUsingAServiceThatIsNotSharedWithYou")
          : t("personal.discoveredServicesAreNotProofOfOwnershipOrGrantedAccessTheProviderChecksEveryRequest")}
      </Note>
      {share && owner && (
        <ShareServices device={owner} onClose={() => setShare(false)} />
      )}
    </div>
  );
}
export function PersonalDevices() {
  const { t } = useTranslation();
  const { devices, services, demo } = usePersonal();
  const { notify } = useAppContext();
  const [params] = useSearchParams();
  const requested = devices.find(
    (device) => device.id === params.get("device"),
  );
  const [filter, setFilter] = useState(
    requested && !requested.own
      ? demo
        ? "Shared with me"
        : "Discovered"
      : "My devices",
  );
  const [id, setId] = useState(requested?.id || "");
  const requestedId = requested?.id;
  const requestedOwn = requested?.own;
  useEffect(() => {
    if (requestedId) {
      setId(requestedId);
      setFilter(
        requestedOwn ? "My devices" : demo ? "Shared with me" : "Discovered",
      );
    }
  }, [requestedId, requestedOwn, demo]);
  const [pair, setPair] = useState(false);
  const [edit, setEdit] = useState(false);
  const [share, setShare] = useState(false);
  const href = useLinks();
  const visible = devices.filter((device) =>
    filter === "My devices" ? device.own : !device.own,
  );
  const selected = visible.find((device) => device.id === id) || visible[0];
  const offered = services.filter(
    (service) => service.deviceId === selected?.id,
  );
  return (
    <div className="pf-page">
      <PageHeading
        title={t("nav.devices")}
        description={t("pages.devicesSubtitle")}
      >
        <button className="pf-button dark" onClick={() => setPair(true)}>
          <Plus size={19} />
          {t("personal.addDevice")}
        </button>
      </PageHeading>
      <Tabs
        options={["My devices", demo ? "Shared with me" : "Discovered"]}
        value={filter}
        onChange={setFilter}
      />
      <div className="pf-devices-layout">
        <div className="pf-device-list">
          {visible.map((device) => (
            <button
              key={device.id}
              className={`pf-device-list-item${selected?.id === device.id ? " selected" : ""}`}
              onClick={() => setId(device.id)}
            >
              <DeviceArt kind={device.kind} small />
              <div>
                <div>
                  <strong>{device.name}</strong>
                  <Status online={device.online} self={device.self} />
                </div>
                <small>{device.own ? t("personal.yourDevice") : t("personal.remoteDevice")}</small>
                <small className="pf-ellipsis">
                  {t("personal.privateNote")} {device.note || t("personal.notSet")}
                </small>
              </div>
              <Arrow />
            </button>
          ))}
          {!visible.length && (
            <Empty title={t("personal.noDevicesHereYet")}>
              {t("personal.addADeviceToGetStarted")}
            </Empty>
          )}
        </div>
        {selected && (
          <div>
            <section className="pf-panel pf-device-detail">
              <div className="pf-detail-lockup">
                <DeviceArt kind={selected.kind} />
                <div>
                  <div className="pf-inline">
                    <h2>{selected.name}</h2>
                    <Status online={selected.online} self={selected.self} />
                  </div>
                  <p>{selected.own ? t("personal.yourDevice") : t("personal.remoteDevice")}</p>
                  <div className="pf-device-id">
                    {t("personal.deviceID")}{" "}
                    <code title={selected.id}>
                      {selected.id.length > 20
                        ? `${selected.id.slice(0, 9)}…${selected.id.slice(-5)}`
                        : selected.id}
                    </code>
                    <button
                      className="pf-icon-button"
                      aria-label={t("personal.copyDeviceID")}
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(selected.id);
                          notify("ok", t("personal.deviceIDCopied"));
                        } catch {
                          notify("warn", t("personal.couldNotCopyTheDeviceID"));
                        }
                      }}
                    >
                      <Copy size={14} />
                    </button>
                  </div>
                </div>
              </div>
              <dl className="pf-properties">
                <div>
                  <dt>{selected.own ? t("personal.deviceName") : t("personal.nickname")}</dt>
                  <dd>{selected.name}</dd>
                </div>
                <div>
                  <dt>{t("personal.privateNoteLabel")}</dt>
                  <dd>{selected.note || t("personal.notSet")}</dd>
                </div>
              </dl>
              <button className="pf-button" onClick={() => setEdit(true)}>
                <Pencil size={15} />
                {t("personal.editDetails")}
              </button>
              <Note>
                {t("personal.onlyYouCanSeePrivateNotes")}
                {!selected.own
                  ? t("personal.theOwnersDeviceNameStaysUnchanged")
                  : ""}
              </Note>
            </section>
            <div className="pf-section-heading">
              <h2>{t("personal.access")}</h2>
            </div>
            <div className="pf-panel pf-access-list">
              {offered.map((service) => (
                <div className="pf-access-row" key={service.id}>
                  <ServiceArt kind={service.kind} brand={service.brand} small />
                  <div>
                    <h3>{service.title}</h3>
                    <small>
                      {selected.own
                        ? t("personal.providedByThisDevice")
                        : demo
                          ? t("personal.sharedWithMe")
                          : t("personal.advertisedByThisDevice")}
                    </small>
                  </div>
                  <Status
                    online={service.online}
                    label={
                      service.online
                        ? demo
                          ? "Access granted"
                          : "Available"
                        : "Offline"
                    }
                  />
                </div>
              ))}
              {!offered.length && (
                <Note>{t("personal.noServicesAdvertisedOnThisDevice")}</Note>
              )}
              <Link className="pf-browse-row" to={href("/services")}>
                {t("personal.viewServices")} <Arrow />
              </Link>
            </div>
            <div className="pf-device-actions">
              {selected.own ? (
                <button className="pf-button" onClick={() => setShare(true)}>
                  <Share2 size={16} />
                  {t("personal.shareServices")}
                </button>
              ) : (
                <Note>{t("personal.serviceAccessIsManagedByTheDeviceOwner")}</Note>
              )}
              <Link className="pf-link" to={href("/peers")}>
                {t("personal.connectionDetails")} <Arrow />
              </Link>
            </div>
          </div>
        )}
      </div>
      {pair && <PairDevice onClose={() => setPair(false)} />}{" "}
      {edit && selected && (
        <EditDevice device={selected} onClose={() => setEdit(false)} />
      )}{" "}
      {share && selected && (
        <ShareServices device={selected} onClose={() => setShare(false)} />
      )}
    </div>
  );
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
