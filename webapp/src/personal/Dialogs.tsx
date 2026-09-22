import { useState } from "react";
import { Check, Copy, Hourglass, Monitor, Plus, Share2 } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useAppContext } from "../appContext";
import {
  Connection,
  DeviceArt,
  Modal,
  Note,
  ServiceArt,
  Tabs,
} from "./components";
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
      title={device.own ? "Edit device" : "Edit local details"}
      onClose={onClose}
    >
      <div className="pf-dialog-context">
        <Monitor size={20} />
        {device.own ? "Your device" : "Shared device"}
      </div>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          const message = validateDeviceDetails(device.own, name, note);
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
          <span>{device.own ? "Device name" : "Nickname"}</span>
          <input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            required={device.own}
          />
          <small>
            {device.own
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
  const { demo, devices } = usePersonal();
  const { client, refreshShell, notify } = useAppContext();
  const [tab, setTab] = useState("My device");
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);
  const navigate = useNavigate();
  return (
    <Modal title="Add device" onClose={onClose}>
      <Tabs
        options={["My device", "Someone else’s device"]}
        value={tab}
        onChange={setTab}
      />
      <Connection
        current={devices.find((device) => device.self)}
        device={devices.find((device) => !device.self) || devices[0]}
      />
      <p className="pf-center">Open Ryn on the other device to connect it.</p>
      <div className="pf-pair-code">
        <code>{demo ? "000 000" : "— — —"}</code>
        <button
          className="pf-button"
          disabled={!demo}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText("000000");
              setCopied(true);
            } catch {
              notify("warn", "Could not copy the example code.");
            }
          }}
        >
          {copied ? <Check size={16} /> : <Copy size={16} />}Copy
        </button>
      </div>
      <Note>
        {demo
          ? "Example only — this code does not work. Pairing will require confirmation on both devices."
          : "Pairing codes are not available on this version of the node. You can discover devices on your configured network."}
      </Note>
      <div className="pf-pair-state">
        <Hourglass size={22} />
        <div>
          <strong>
            {demo ? "Confirm on both devices" : "Discover available devices"}
          </strong>
          <small>
            {demo
              ? "Preview of the pairing flow"
              : "Discovery does not grant ownership or service access."}
          </small>
        </div>
      </div>
      <footer className="pf-dialog-actions">
        <button className="pf-button" onClick={onClose}>
          Cancel
        </button>
        <button
          className="pf-button primary"
          disabled={busy}
          onClick={async () => {
            if (demo) {
              onClose();
              navigate("/devices?client=fixture");
              return;
            }
            setBusy(true);
            try {
              await client.discoverPeers();
              await refreshShell();
              onClose();
              navigate("/devices");
            } catch {
              notify(
                "danger",
                "Could not discover devices. Check your connection and try again.",
              );
            } finally {
              setBusy(false);
            }
          }}
        >
          <Plus size={16} />
          {busy ? "Discovering…" : demo ? "View devices" : "Discover devices"}
        </button>
      </footer>
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
