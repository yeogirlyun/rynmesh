import { useState } from "react";
import { Monitor, Share2 } from "lucide-react";
import { Link } from "react-router-dom";
import { SpaceControls } from "./Space";
import { useAppContext } from "../appContext";
import { DeviceArt, Modal, Note, ServiceArt } from "./components";
import { usePersonal, validateDeviceDetails, type Device } from "./model";

export function EditDevice({
  device,
  onClose,
}: {
  device: Device;
  onClose: () => void;
}) {
  const { saveDevice, demo } = usePersonal();
  const [name, setName] = useState(device.name);
  const [note, setNote] = useState(device.note);
  const [kind, setKind] = useState(device.kind);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal
      title={device.self ? "Edit device" : "Edit local details"}
      onClose={onClose}
    >
      <div className="pf-dialog-context">
        <Monitor size={20} />
        {device.own ? "Your device" : "Shared device"}
      </div>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          const message = validateDeviceDetails(
            Boolean(device.self),
            name,
            note,
          );
          if (message) {
            setError(message);
            return;
          }
          setBusy(true);
          try {
            await saveDevice(device, name, note, kind);
            onClose();
          } catch (error) {
            setError(
              error instanceof Error
                ? error.message
                : "Could not save changes.",
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="pf-field">
          <span>{device.self ? "Device name" : "Nickname"}</span>
          <input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            required={device.self}
          />
          <small>
            {device.self
              ? "Visible to devices connected to yours."
              : "Only changes the name shown to you."}{" "}
            Up to 32 characters.
          </small>
        </label>
        <label className="pf-field">
          <span>Private note</span>
          <textarea
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <small>Only you can see this note. Up to 200 characters.</small>
        </label>
        <label className="pf-field">
          <span>Device icon</span>
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value as Device["kind"])}
          >
            <option value="desktop">Desktop</option>
            <option value="laptop">Laptop</option>
          </select>
        </label>
        {error && (
          <p className="pf-error" role="alert">
            {error}
          </p>
        )}
        <Note>
          {device.own
            ? "Renaming does not change the device ID."
            : "The owner’s device name will not change."}
          {demo ? " Changes apply to this preview." : ""}
        </Note>
        <footer className="pf-dialog-actions">
          <button type="button" className="pf-button" onClick={onClose}>
            Cancel
          </button>
          <button className="pf-button primary" disabled={busy}>
            {busy ? "Saving…" : "Save changes"}
          </button>
        </footer>
      </form>
    </Modal>
  );
}
export function PairDevice({ onClose }: { onClose: () => void }) {
  return (
    <Modal title="Add device" onClose={onClose}>
      <SpaceControls compact />
    </Modal>
  );
}
export function ShareServices({
  device,
  onClose,
}: {
  device: Device;
  onClose: () => void;
}) {
  const { demo } = usePersonal();
  return demo ? (
    <PreviewShareServices device={device} onClose={onClose} />
  ) : (
    <LiveShareServices device={device} onClose={onClose} />
  );
}
function LiveShareServices({
  device,
  onClose,
}: {
  device: Device;
  onClose: () => void;
}) {
  const { space, spaceAction } = usePersonal();
  const [access, setAccess] = useState(space?.ai_access || "local");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title="Share AI service" onClose={onClose}>
      <p className="pf-muted">{device.name}</p>
      {device.self ? (
        <>
          <label className="pf-field">
            <span>Allow access from</span>
            <select
              value={access}
              onChange={(e) => setAccess(e.target.value as "local" | "space")}
            >
              <option value="local">This device only</option>
              <option value="space" disabled={space?.membership !== "active"}>
                My space devices
              </option>
            </select>
          </label>
          <Note>
            Applies to AI tasks on this computer. Files, desktop access and
            separately issued API keys are not changed.
          </Note>
          {space?.membership !== "active" && (
            <Link className="pf-link" to="/settings/space">
              Create or join a personal space first →
            </Link>
          )}
          {error && (
            <p role="alert" className="pf-error">
              {error}
            </p>
          )}
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={onClose}>
              Cancel
            </button>
            <button
              className="pf-button primary"
              disabled={busy || !space}
              onClick={async () => {
                setBusy(true);
                try {
                  await spaceAction("policy", { access });
                  onClose();
                } catch (e) {
                  setError(
                    e instanceof Error ? e.message : "Could not save access.",
                  );
                } finally {
                  setBusy(false);
                }
              }}
            >
              Save access
            </button>
          </footer>
        </>
      ) : (
        <Note>
          Open Ryn on this computer to change its service permissions.
          Membership does not grant remote administration.
        </Note>
      )}
    </Modal>
  );
}
function PreviewShareServices({
  device,
  onClose,
}: {
  device: Device;
  onClose: () => void;
}) {
  const { services, devices, demo } = usePersonal();
  const { notify } = useAppContext();
  const [selectedServices, setSelectedServices] = useState<string[]>([]);
  const [selectedDevices, setSelectedDevices] = useState<string[]>([]);
  const choose = (
    value: string,
    current: string[],
    update: (items: string[]) => void,
  ) =>
    update(
      current.includes(value)
        ? current.filter((item) => item !== value)
        : [...current, value],
    );
  const available = services.filter(
    (service) => service.deviceId === device.id,
  );
  return (
    <Modal title="Share services" onClose={onClose}>
      <p className="pf-muted">{device.name} · Owner only</p>
      <h3>Choose services</h3>
      <div className="pf-choice-grid">
        {available.map((service) => (
          <label
            className={selectedServices.includes(service.id) ? "selected" : ""}
            key={service.id}
          >
            <ServiceArt kind={service.kind} small />
            <span>{service.title}</span>
            <input
              type="checkbox"
              checked={selectedServices.includes(service.id)}
              onChange={() =>
                choose(service.id, selectedServices, setSelectedServices)
              }
            />
          </label>
        ))}
      </div>
      {!available.length && <Note>No services on this device yet.</Note>}
      <h3>Allow these devices</h3>
      <div className="pf-choice-grid">
        {devices
          .filter((item) => !item.own)
          .map((item) => (
            <label
              className={selectedDevices.includes(item.id) ? "selected" : ""}
              key={item.id}
            >
              <DeviceArt small kind={item.kind} />
              <span>{item.name}</span>
              <input
                type="checkbox"
                checked={selectedDevices.includes(item.id)}
                onChange={() =>
                  choose(item.id, selectedDevices, setSelectedDevices)
                }
              />
            </label>
          ))}
      </div>
      <Note>
        {demo
          ? "Preview only. Saving here does not grant real access."
          : "Per-device service access is not supported by this node version. No access will be changed."}
      </Note>
      <div className="pf-callout">
        <Share2 size={18} />
        Only selected services will be shared. This does not grant desktop or
        full computer control.
      </div>
      <footer className="pf-dialog-actions">
        <button className="pf-button" onClick={onClose}>
          Cancel
        </button>
        <button
          className="pf-button primary"
          disabled={
            !demo || !selectedServices.length || !selectedDevices.length
          }
          onClick={() => {
            notify(
              "info",
              "Preview reviewed. No real service permissions were changed.",
            );
            onClose();
          }}
        >
          Save access
        </button>
      </footer>
    </Modal>
  );
}
