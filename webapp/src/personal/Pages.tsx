import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import {
  ArrowDownToLine,
  Check,
  ChevronRight,
  Copy,
  ExternalLink,
  Link2,
  Moon,
  Pencil,
  Plus,
  Search,
  Share2,
  Sun,
  X,
} from "lucide-react";
import { useAppContext } from "../appContext";
import type { LLMOrderResult } from "../domain/nodeClient";
import type { WorkResult } from "../domain/types";
import { isTauriDesktop } from "../domain/nodeUrl";
import { getDesktopPreferences, setDesktopPreferences, type DesktopPreferences } from "../domain/desktopClient";
import {
  Arrow,
  Connection,
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
        <ServiceArt kind={service.kind} />
        <div>
          <h3>{service.title}</h3>
          <p>
            {resolveName(service.deviceId)} ·{" "}
            {devices.find((device) => device.id === service.deviceId)?.own
              ? "My device"
              : demo
                ? "Shared with me"
                : "Discovered device"}
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
        {service.kind === "ai" ? "Open chat" : "Open"}
      </button>
      {onSelect && (
        <button
          className="pf-icon-button"
          aria-label={`View ${service.title} details`}
          onClick={onSelect}
        >
          <Arrow />
        </button>
      )}
      {preview && (
        <Modal title={service.title} onClose={() => setPreview(false)}>
          <ServiceArt kind={service.kind} />
          <p>
            This service is shown as sample data in the design preview. It is
            not installed or running.
          </p>
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={() => setPreview(false)}>
              Close
            </button>
          </footer>
        </Modal>
      )}
    </article>
  );
}
export function PersonalHome() {
  const { devices, services, loading, error, refresh } = usePersonal();
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
        title="Home"
        description="Use your devices. Share what they can do."
      >
        <button className="pf-button dark" onClick={() => setPair(true)}>
          <Plus size={19} />
          Add device
        </button>
      </PageHeading>
      {error && (
        <div className="pf-callout">
          {error}
          <button className="pf-link" onClick={() => void refresh()}>
            Retry
          </button>
        </div>
      )}
      <div className="pf-section-heading">
        <h2>
          My devices <span>({owned.length})</span>
        </h2>
      </div>
      <div className="pf-device-grid">
        {owned.map((device) => (
          <article
            className={`pf-device-card${selected?.id === device.id ? " selected" : ""}`}
            key={device.id}
          >
            <button
              className="pf-device-select"
              onClick={() => setSelectedId(device.id)}
              aria-pressed={selected?.id === device.id}
            >
              <div className="pf-device-card-top">
                <DeviceArt kind={device.kind} />
                <Status online={device.online} self={device.self} />
              </div>
              <h3>{device.name}</h3>
              <p>
                {device.hardware ? `${device.hardware} · ` : ""}
                {services.filter((service) => service.deviceId === device.id)
                  .length || ""}
                {services.some((service) => service.deviceId === device.id)
                  ? " services"
                  : device.self
                    ? "Current device"
                    : ""}
              </p>
              <small>{device.note || "Add a private note"}</small>
              <ChevronRight className="pf-card-arrow" size={19} />
            </button>
            <button
              className="pf-device-edit pf-icon-button"
              aria-label={`Edit ${device.name}`}
              onClick={() => setEdit(device)}
            >
              <Pencil size={15} />
            </button>
          </article>
        ))}
      </div>
      {selected && (
        <div className="pf-home-bottom">
          <section>
            <div className="pf-section-heading">
              <div>
                <h2>Services on {selected.name}</h2>
                <p>From your {selected.name}</p>
              </div>
            </div>
            <div className="pf-panel pf-service-stack">
              {offered.map((service) => (
                <ServiceRow service={service} compact key={service.id} />
              ))}
              {!offered.length && (
                <Empty
                  title={
                    loading ? "Finding services…" : "No services on this device"
                  }
                >
                  {loading ? (
                    "Checking your connected devices."
                  ) : (
                    <Link to={href("/services/manage")}>
                      Set up a service on this device
                    </Link>
                  )}
                </Empty>
              )}
              <Link className="pf-browse-row" to={href("/services")}>
                <Share2 size={29} strokeWidth={1.4} />
                <div>
                  <h3>From other devices</h3>
                  <small>Browse services from your other devices</small>
                </div>
                <span>
                  Browse services <Arrow />
                </span>
              </Link>
            </div>
          </section>
          <aside className="pf-panel pf-connection-panel">
            <div className="pf-section-heading">
              <h2>{selected.name}</h2>
              <Status online={selected.online} />
            </div>
            <Connection
              device={selected}
              current={devices.find((device) => device.self)}
            />
            <dl className="pf-properties">
              <div>
                <dt>Connection</dt>
                <dd>
                  {selected.self
                    ? "This device"
                    : selected.online
                      ? "Online"
                      : "Offline"}
                </dd>
              </div>
              <div>
                <dt>Access</dt>
                <dd>{selected.self ? "Local device" : "My device"}</dd>
              </div>
            </dl>
            <button className="pf-button full" onClick={() => setShare(true)}>
              <Share2 size={17} />
              Share service
            </button>
          </aside>
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
        title="Services"
        description="Use capabilities from your devices and devices shared with you."
      >
        <Link className="pf-button primary" to={href("/services/manage")}>
          <Plus size={19} />
          Add service
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
            aria-label="Search services"
            placeholder="Search services"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
      </div>
      {error && (
        <Note>
          {error}{" "}
          <button className="pf-link" onClick={() => void refresh()}>
            Retry
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
              title={loading ? "Finding services…" : "No matching services"}
            >
              Add a service on this device or try another filter.
            </Empty>
          )}
        </div>
        {selected && (
          <aside className="pf-panel pf-service-detail">
            <div className="pf-detail-lockup">
              <ServiceArt kind={selected.kind} />
              <div>
                <h2>{selected.title}</h2>
                <p>On {resolveName(selected.deviceId)}</p>
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
                  {selected.kind === "ai"
                    ? `Chat with the model running on ${resolveName(selected.deviceId)}.`
                    : selected.description}
                </p>
                <h3>Access</h3>
                <p>
                  {demo
                    ? owner?.own
                      ? "Only my devices"
                      : "Shared with me"
                    : "Controlled by the service provider"}
                </p>
                <div className="pf-button-row">
                  <button
                    className="pf-button"
                    onClick={() => setTab("API access")}
                  >
                    <Link2 size={15} />
                    API access
                  </button>
                  {owner?.own && (
                    <button
                      className="pf-button"
                      onClick={() => setShare(true)}
                    >
                      <Share2 size={15} />
                      Manage access
                    </button>
                  )}
                </div>
                <hr />
                <h3>Manage on {resolveName(selected.deviceId)}</h3>
                <p>Service setup is managed on the device running it.</p>
                <Link
                  className="pf-link"
                  to={href(
                    `/devices?device=${encodeURIComponent(selected.deviceId)}`,
                  )}
                >
                  View device <ExternalLink size={14} />
                </Link>
              </>
            ) : (
              <>
                <p className="pf-description">
                  Connect your apps and agents through the local inference API.
                </p>
                {selected.kind === "ai" ? (
                  <Link
                    className="pf-button"
                    to={href("/services/api")}
                  >
                    <Link2 size={16} />
                    Configure API access
                  </Link>
                ) : (
                  <Note>
                    {selected.preview
                      ? "This sample service does not provide a working API."
                      : "API details are provided by the service owner."}
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
        More service tools <Arrow />
      </Link>
      <Note>
        {demo
          ? "Request access before using a service that is not shared with you."
          : "Discovered services are not proof of ownership or granted access. The provider checks every request."}
      </Note>
      {share && owner && (
        <ShareServices device={owner} onClose={() => setShare(false)} />
      )}
    </div>
  );
}
export function PersonalDevices() {
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
        title="Devices"
        description="Manage your devices and devices shared with you."
      >
        <button className="pf-button dark" onClick={() => setPair(true)}>
          <Plus size={19} />
          Add device
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
                <small>{device.own ? "Your device" : "Remote device"}</small>
                <small className="pf-ellipsis">
                  Private note: {device.note || "Not set"}
                </small>
              </div>
              <Arrow />
            </button>
          ))}
          {!visible.length && (
            <Empty title="No devices here yet">
              Add a device to get started.
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
                  <p>{selected.own ? "Your device" : "Remote device"}</p>
                  <div className="pf-device-id">
                    Device ID:{" "}
                    <code title={selected.id}>
                      {selected.id.length > 20
                        ? `${selected.id.slice(0, 9)}…${selected.id.slice(-5)}`
                        : selected.id}
                    </code>
                    <button
                      className="pf-icon-button"
                      aria-label="Copy device ID"
                      onClick={async () => {
                        try {
                          await navigator.clipboard.writeText(selected.id);
                          notify("ok", "Device ID copied");
                        } catch {
                          notify("warn", "Could not copy the device ID.");
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
                  <dt>{selected.own ? "Device name" : "Nickname"}</dt>
                  <dd>{selected.name}</dd>
                </div>
                <div>
                  <dt>Private note</dt>
                  <dd>{selected.note || "Not set"}</dd>
                </div>
              </dl>
              <button className="pf-button" onClick={() => setEdit(true)}>
                <Pencil size={15} />
                Edit details
              </button>
              <Note>
                Only you can see private notes.
                {!selected.own
                  ? " The owner’s device name stays unchanged."
                  : ""}
              </Note>
            </section>
            <div className="pf-section-heading">
              <h2>Access</h2>
            </div>
            <div className="pf-panel pf-access-list">
              {offered.map((service) => (
                <div className="pf-access-row" key={service.id}>
                  <ServiceArt kind={service.kind} small />
                  <div>
                    <h3>{service.title}</h3>
                    <small>
                      {selected.own
                        ? "Provided by this device"
                        : demo
                          ? "Shared with me"
                          : "Advertised by this device"}
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
                <Note>No services advertised on this device.</Note>
              )}
              <Link className="pf-browse-row" to={href("/services")}>
                View services <Arrow />
              </Link>
            </div>
            <div className="pf-device-actions">
              {selected.own ? (
                <button className="pf-button" onClick={() => setShare(true)}>
                  <Share2 size={16} />
                  Share services
                </button>
              ) : (
                <Note>Service access is managed by the device owner.</Note>
              )}
              <Link className="pf-link" to={href("/peers")}>
                Connection details <Arrow />
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
};
const SAMPLE_TASKS: Task[] = [
  {
    id: "sample-transcription",
    title: "Meeting transcription",
    deviceId: "preview-alex",
    kind: "audio",
    status: "running",
    input: "meeting.wav",
    sample: true,
  },
  {
    id: "sample-document",
    title: "Convert proposal",
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
      ]);
      if (!active) return;
      const llm = results[0].status === "fulfilled" ? results[0].value : [];
      const work = results[1].status === "fulfilled" ? results[1].value : [];
      const provider =
        results[2].status === "fulfilled" ? results[2].value : [];
      setError(
        results.some((result) => result.status === "rejected")
          ? "Some tasks could not be loaded. Retrying automatically."
          : "",
      );
      setTasks([
        ...(demo ? SAMPLE_TASKS : []),
        ...llm.map(
          (order: LLMOrderResult): Task => ({
            id: order.task_id,
            title: order.model_alias
              ? `AI chat · ${order.model_alias}`
              : "AI chat",
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
              ? `AI chat · ${order.model_alias}`
              : "AI chat",
            deviceId: node.peer_id,
            kind: "ai",
            status: order.state,
            incoming: true,
          }),
        ),
        ...work.map(
          (order: WorkResult): Task => ({
            id: order.work_order_id,
            title: order.message || "Service task",
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
  }, [client, demo, node.peer_id]);
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
        title="Tasks"
        description="Track work running across your devices."
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
                  <th>Task</th>
                  <th>Runs on</th>
                  <th>Status</th>
                  <th>Action</th>
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
                          <strong>{task.title}</strong>
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
                            : "Not reported"}
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
                        onClick={() => setSelectedId(task.id)}
                      >
                        View <ChevronRight size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            {!visible.length && (
              <Empty title="No tasks yet">
                Tasks will appear here when you use a service.
              </Empty>
            )}
          </div>
          <Note>
            If a task fails due to a connection issue, you can try it again
            later.
          </Note>
        </section>
        {selected && (
          <aside className="pf-panel pf-task-detail">
            <div className="pf-section-heading">
              <h2>{selected.title}</h2>
              <button
                className="pf-icon-button"
                aria-label="Close task details"
                onClick={() => setSelectedId("")}
              >
                <X size={18} />
              </button>
            </div>
            <p>Runs on</p>
            <div className="pf-inline">
              <DeviceArt small />
              <strong>
                {selected.deviceId
                  ? resolveName(selected.deviceId)
                  : "Not reported by this task"}
              </strong>
            </div>
            <p>Service</p>
            <div className="pf-inline">
              <ServiceArt kind={selected.kind} />
              <strong>
                {selected.kind === "ai"
                  ? "AI chat"
                  : selected.kind === "audio"
                    ? "Transcription"
                    : selected.kind === "document"
                      ? "Document converter"
                      : "Service task"}
              </strong>
            </div>
            <p>Status</p>
            <Status
              online={taskState(selected.status) === "Completed"}
              label={taskState(selected.status)}
            />
            {selected.sample && (
              <ol className="pf-timeline">
                {["Submitted", "Accepted", "Processing", "Result ready"].map(
                  (step, index) => (
                    <li
                      key={step}
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
                      {step}
                    </li>
                  ),
                )}
              </ol>
            )}
            {selected.input && (
              <>
                <p>Input file</p>
                <strong>{selected.input}</strong>
              </>
            )}
            {selected.result && taskState(selected.status) === "Completed" && (
              <button
                className="pf-button full"
                onClick={() => download(selected)}
              >
                <ArrowDownToLine size={16} />
                Download result
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
                        ? "Task cancelled"
                        : "Cancellation requested. Waiting for the processing device.",
                    );
                  } catch {
                    notify("danger", "Could not cancel the task.");
                  } finally {
                    setBusy(false);
                  }
                }}
              >
                {busy ? "Requesting cancellation…" : "Cancel task"}
              </button>
            )}
            {selected.sample && <Note>Sample task. No work is running.</Note>}
          </aside>
        )}
      </div>
    </div>
  );
}
export function PersonalSettings() {
  const { appearance, setAppearance, demo } = usePersonal();
  const { node, registry, refreshShell, notify } = useAppContext();
  const href = useLinks();
  const [diagnostics, setDiagnostics] = useState(false);
  const [startup, setStartup] = useState(false);
  const [background, setBackground] = useState(true);
  const [desktopPrefs, setDesktopPrefs] = useState<DesktopPreferences | null>(null);
  const [savingPrefs, setSavingPrefs] = useState(false);
  useEffect(() => {
    if (demo || !isTauriDesktop()) return;
    void getDesktopPreferences().then((prefs) => {
      setDesktopPrefs(prefs); setStartup(prefs.startup); setBackground(prefs.background);
    }).catch(() => notify("danger", "Desktop preferences could not be loaded."));
  }, [demo, notify]);
  async function updateDesktopPrefs(nextBackground: boolean, nextStartup: boolean) {
    if (demo) { setBackground(nextBackground); setStartup(nextStartup); return; }
    setSavingPrefs(true);
    try {
      const prefs = await setDesktopPreferences(nextBackground, nextStartup);
      setDesktopPrefs(prefs); setStartup(prefs.startup); setBackground(prefs.background);
    } catch (error) { notify("danger", error instanceof Error ? error.message : String(error)); }
    finally { setSavingPrefs(false); }
  }
  return (
    <div className="pf-page pf-settings">
      <PageHeading
        title="Settings"
        description="These preferences apply to this device."
      />
      <section className="pf-setting-row"><div><h2>Personal space</h2><p>Add your computers, manage membership and share AI.</p></div><Link className="pf-button" to={personalHref("/settings/space", demo)}>Manage space <ChevronRight size={16} /></Link></section>
      <section>
        <h2>Appearance</h2>
        <p>Choose the app’s appearance.</p>
        <div className="pf-appearance">
          {(["light", "dark", "system"] as const).map((theme) => (
            <button
              aria-pressed={appearance === theme}
              className={appearance === theme ? "selected" : ""}
              key={theme}
              onClick={() => setAppearance(theme)}
            >
              {theme === "system" ? (
                <span className="pf-system-art">
                  <Sun size={27} />
                  <Moon size={27} />
                </span>
              ) : (
                <DeviceArt dark={theme === "dark"} />
              )}
              <strong>{theme[0].toUpperCase() + theme.slice(1)}</strong>
              <span className="pf-radio">{appearance === theme && <i />}</span>
            </button>
          ))}
        </div>
      </section>
      <section>
        <h2>Startup &amp; background</h2>
        <div className="pf-setting-row">
          <div>
            <h3>Launch at startup</h3>
            <p>
              {demo
                ? "Start Ryn when you sign in. Preview preference."
                : desktopPrefs?.startup_supported ? "Start Ryn when you sign in to Windows." : "Available in the Windows desktop app."}
            </p>
          </div>
          <button
            type="button"
            className={`pf-switch${startup ? " on" : ""}`}
            role="switch"
            aria-checked={startup}
            aria-label="Launch at startup"
            disabled={savingPrefs || (!demo && !desktopPrefs?.startup_supported)}
            onClick={() => void updateDesktopPrefs(background, !startup)}
          >
            <span />
          </button>
        </div>
        <div className="pf-setting-row">
          <div>
            <h3>Keep running when the window closes</h3>
            <p>
              {demo
                ? "Services stay available while this device is online. Preview preference."
                : node.desktop_managed
                  ? "Keep services available from the system tray."
                  : "Background behavior is managed by the Ryn desktop app."}
            </p>
          </div>
          <button
            type="button"
            className={`pf-switch${(demo || desktopPrefs ? background : node.desktop_managed) ? " on" : ""}`}
            role="switch"
            aria-checked={demo || desktopPrefs ? background : Boolean(node.desktop_managed)}
            aria-label="Keep running when the window closes"
            disabled={savingPrefs || (!demo && !desktopPrefs)}
            onClick={() => void updateDesktopPrefs(!background, startup)}
          >
            <span />
          </button>
        </div>
      </section>
      <section>
        <div className="pf-setting-row">
          <div>
            <h2>Connection</h2>
            <Status
              online={registry.status === "connected"}
              label={`Status: ${registry.status === "connected" ? "Connected" : "Disconnected"}`}
            />
            <p>Show connection problems and retry options.</p>
          </div>
          <button className="pf-button" onClick={() => setDiagnostics(true)}>
            Connection diagnostics
          </button>
        </div>
      </section>
      <section>
        <h2>Data &amp; privacy</h2>
        <Link className="pf-setting-row" to={href("/devices")}>
          <div>
            <h3>Private notes</h3>
            <p>Visible only to you.</p>
          </div>
          <Arrow />
        </Link>
        <div className="pf-setting-row">
          <div>
            <h3>Local history</h3>
            <p>Manage records stored on this device.</p>
          </div>
          <Link className="pf-button" to={href("/settings/advanced")}>
            Manage
          </Link>
        </div>
      </section>
      <Link className="pf-link" to={href("/settings/advanced")}>
        Advanced settings <Arrow />
      </Link>
      {diagnostics && (
        <Modal
          title="Connection diagnostics"
          onClose={() => setDiagnostics(false)}
        >
          <dl className="pf-properties">
            <div>
              <dt>This device</dt>
              <dd>{node.node_name}</dd>
            </div>
            <div>
              <dt>Local service</dt>
              <dd>{node.daemon_running ? "Running" : "Stopped"}</dd>
            </div>
            <div>
              <dt>Discovery connection</dt>
              <dd>{registry.status}</dd>
            </div>
            <div>
              <dt>Known devices</dt>
              <dd>{node.peer_count}</dd>
            </div>
          </dl>
          <footer className="pf-dialog-actions">
            <button
              className="pf-button primary"
              onClick={async () => {
                await refreshShell();
                notify("info", "Connection check complete");
              }}
            >
              Check again
            </button>
          </footer>
        </Modal>
      )}
    </div>
  );
}
