import { useTranslation } from "react-i18next";
import { useState } from "react";
import { Monitor, Share2 } from "lucide-react";
import { Link } from "react-router-dom";
import { personalLabelKeys } from "./labels";
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
  const { t } = useTranslation();
  const { saveDevice, demo } = usePersonal();
  const [name, setName] = useState(device.name);
  const [note, setNote] = useState(device.note);
  const [kind, setKind] = useState(device.kind);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal
      title={device.self ? t("personal.editDevice") : t("personal.editLocalDetails")}
      onClose={onClose}
    >
      <div className="pf-dialog-context">
        <Monitor size={20} />
        {device.own ? t("personal.yourDevice") : t("personal.sharedDevice")}
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
                : t("personal.couldNotSaveChanges"),
            );
          } finally {
            setBusy(false);
          }
        }}
      >
        <label className="pf-field">
          <span>{device.self ? t("personal.deviceName") : t("personal.nickname")}</span>
          <input
            autoFocus
            value={name}
            onChange={(event) => setName(event.target.value)}
            required={device.self}
          />
          <small>
            {device.self
              ? t("personal.visibleToDevicesConnectedToYours")
              : t("personal.onlyChangesTheNameShownToYou")}{" "}
            {t("personal.upTo32Characters")}
          </small>
        </label>
        <label className="pf-field">
          <span>{t("personal.privateNoteLabel")}</span>
          <textarea
            rows={3}
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <small>{t("personal.onlyYouCanSeeThisNoteUpTo200Characters")}</small>
        </label>
        <label className="pf-field">
          <span>{t("personal.deviceIcon")}</span>
          <select
            value={kind}
            onChange={(event) => setKind(event.target.value as Device["kind"])}
          >
            <option value="desktop">{t("personal.desktop")}</option>
            <option value="laptop">{t("personal.laptop")}</option>
          </select>
        </label>
        {error && (
          <p className="pf-error" role="alert">
            {personalLabelKeys[error] ? t(`personal.${personalLabelKeys[error]}`) : error}
          </p>
        )}
        <Note>
          {device.own
            ? t("personal.renamingDoesNotChangeTheDeviceID")
            : t("personal.theOwnersDeviceNameWillNotChange")}
          {demo ? t("personal.changesApplyToThisPreview") : ""}
        </Note>
        <footer className="pf-dialog-actions">
          <button type="button" className="pf-button" onClick={onClose}>
            {t("personal.cancel")}
          </button>
          <button className="pf-button primary" disabled={busy}>
            {busy ? t("personal.saving") : t("personal.saveChanges")}
          </button>
        </footer>
      </form>
    </Modal>
  );
}
export function PairDevice({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  return (
    <Modal title={t("personal.addDevice")} onClose={onClose}>
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
  const { t } = useTranslation();
  const { space, spaceAction } = usePersonal();
  const [access, setAccess] = useState(space?.ai_access || "local");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  return (
    <Modal title={t("personal.shareAIService")} onClose={onClose}>
      <p className="pf-muted">{device.name}</p>
      {device.self ? (
        <>
          <label className="pf-field">
            <span>{t("personal.allowAccessFrom")}</span>
            <select
              value={access}
              onChange={(e) => setAccess(e.target.value as "local" | "space")}
            >
              <option value="local">{t("personal.thisDeviceOnly")}</option>
              <option value="space" disabled={space?.membership !== "active"}>
                {t("personal.mySpaceDevices")}
              </option>
            </select>
          </label>
          <Note>
            {t("personal.appliesToAITasksOnThisComputerFilesDesktopAccessAndSeparatelyIssuedAPIKeysAreNotChanged")}
          </Note>
          {space?.membership !== "active" && (
            <Link className="pf-link" to="/settings/space">
              {t("personal.createOrJoinAPersonalSpaceFirst")}
            </Link>
          )}
          {error && (
            <p role="alert" className="pf-error">
              {personalLabelKeys[error] ? t(`personal.${personalLabelKeys[error]}`) : error}
            </p>
          )}
          <footer className="pf-dialog-actions">
            <button className="pf-button" onClick={onClose}>
              {t("personal.cancel")}
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
                    e instanceof Error ? e.message : t("personal.couldNotSaveAccess"),
                  );
                } finally {
                  setBusy(false);
                }
              }}
            >
              {t("personal.saveAccess")}
            </button>
          </footer>
        </>
      ) : (
        <Note>
          {t("personal.openRynOnThisComputerToChangeItsServicePermissionsMembershipDoesNotGrantRemoteAdministration")}
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
  const { t } = useTranslation();
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
    <Modal title={t("personal.shareServices")} onClose={onClose}>
      <p className="pf-muted">{device.name} {t("personal.ownerOnly")}</p>
      <h3>{t("personal.chooseServices")}</h3>
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
      {!available.length && <Note>{t("personal.noServicesOnThisDeviceYet")}</Note>}
      <h3>{t("personal.allowTheseDevices")}</h3>
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
          ? t("personal.previewOnlySavingHereDoesNotGrantRealAccess")
          : t("personal.perdeviceServiceAccessIsNotSupportedByThisNodeVersionNoAccessWillBeChanged")}
      </Note>
      <div className="pf-callout">
        <Share2 size={18} />
        {t("personal.onlySelectedServicesWillBeSharedThisDoesNotGrantDesktopOrFullComputerControl")}
      </div>
      <footer className="pf-dialog-actions">
        <button className="pf-button" onClick={onClose}>
          {t("personal.cancel")}
        </button>
        <button
          className="pf-button primary"
          disabled={
            !demo || !selectedServices.length || !selectedDevices.length
          }
          onClick={() => {
            notify(
              "info",
              t("personal.previewReviewedNoRealServicePermissionsWereChanged"),
            );
            onClose();
          }}
        >
          {t("personal.saveAccess")}
        </button>
      </footer>
    </Modal>
  );
}
