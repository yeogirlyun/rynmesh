import { useTranslation } from "react-i18next";
import { useState } from "react";
import { Info, Share2, ShieldCheck } from "lucide-react";
import { Link } from "react-router-dom";
import { personalLabelKeys } from "./labels";
import { SpaceControls } from "./Space";
import { useAppContext } from "../appContext";
import { Modal } from "./components";
import { usePersonal, validateDeviceDetails, type Device, type Service } from "./model";
import { ADD_DEVICE_PATH, DeviceGlyph, DeviceLine, PathIcon, Pill, ServiceTile, Tile, deviceHue, initialsOf } from "./drop/FamilyParts";

// Device dialogs in the devices-family style (personal/drop/family.css, `dialog.rf-dlg`). Behaviour is unchanged.

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
      className="rf rf-dlg rf-dlg-edit rf-dlg-hue rf-h-indigo"
    >
      <Tile hue="indigo" size="lg" className="rf-mark">{(px) => <DeviceGlyph kind={kind} size={px} />}</Tile>
      <p className="rf-sub">
        {device.own ? t("personal.yourDevice") : t("personal.sharedDevice")}
        {device.self ? t("personal.thisDeviceSuffix") : ""}
      </p>
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
        <div className="rf-body">
          <label className="rf-field">
            <span>{device.self ? t("personal.deviceName") : t("personal.nickname")}<small>{name.length}/32</small></span>
            <input
              className="rf-inp rf-inp-l"
              autoFocus
              value={name}
              maxLength={32}
              onChange={(event) => setName(event.target.value)}
              required={device.self}
            />
            <small className="rf-hint">
              {device.self
                ? t("personal.visibleToDevicesConnectedToYours")
                : t("personal.onlyChangesTheNameShownToYou")}{" "}
              {t("personal.upTo32Characters")}
            </small>
          </label>
          <label className="rf-field">
            <span>{t("personal.privateNoteLabel")}<small>{note.length}/200</small></span>
            <textarea
              className="rf-inp rf-inp-m"
              rows={3}
              maxLength={200}
              value={note}
              onChange={(event) => setNote(event.target.value)}
            />
            <small className="rf-hint">{t("personal.onlyYouCanSeeThisNoteUpTo200Characters")}</small>
          </label>
          <fieldset>
            <legend className="rf-legend">{t("personal.deviceIcon")}</legend>
            <div className="rf-icon-grid">
              {(["desktop", "laptop"] as const).map((option) => (
                <label className="rf-opt rf-opt-icon" key={option}>
                  <input type="radio" name="device-kind" checked={kind === option} onChange={() => setKind(option)} />
                  <span className="rf-chipicon"><DeviceLine kind={option} size={20} /></span>
                  <b>{t(`personal.${option}`)}</b>
                </label>
              ))}
            </div>
          </fieldset>
          {error && (
            <p className="rf-err" role="alert">
              {personalLabelKeys[error] ? t(`personal.${personalLabelKeys[error]}`) : error}
            </p>
          )}
          <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>
            {device.own
              ? t("personal.renamingDoesNotChangeTheDeviceID")
              : t("personal.theOwnersDeviceNameWillNotChange")}
            {demo ? t("personal.changesApplyToThisPreview") : ""}
          </span></p>
        </div>
        <footer className="rf-foot rf-foot-end">
          <button type="button" className="rf-btn" onClick={onClose}>
            {t("personal.cancel")}
          </button>
          <button className="rf-btn rf-btn-p" disabled={busy}>
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
    <Modal title={t("personal.addDevice")} onClose={onClose} className="rf rf-dlg rf-dlg-add">
      <span className="rf-mark"><PathIcon d={ADD_DEVICE_PATH} size={21} /></span>
      <p className="rf-sub">{t("personal.bringYourComputersTogetherNoAccountRequired")}</p>
      <SpaceControls compact />
      <footer className="rf-foot">
        <small className="rf-foot-note">{t("personal.devicesKeepTheirIdentityWhenTheirIPAddressChanges")}</small>
        <button type="button" className="rf-btn" onClick={onClose}>{t("personal.done")}</button>
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
  const { demo } = usePersonal();
  return demo ? (
    <PreviewShareServices device={device} onClose={onClose} />
  ) : (
    <LiveShareServices device={device} onClose={onClose} />
  );
}
const SHARE_CLASS = "rf rf-dlg rf-dlg-share";
function ShareMark() {
  return <span className="rf-mark"><Share2 size={19} strokeWidth={1.8} aria-hidden="true" /></span>;
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
    <Modal title={t("personal.shareAIService")} onClose={onClose} className={SHARE_CLASS}>
      <ShareMark />
      <p className="rf-sub">{device.name}</p>
      {device.self ? (
        <>
          <div className="rf-body">
            <label className="rf-field">
              <span>{t("personal.allowAccessFrom")}</span>
              <select
                className="rf-inp rf-inp-l"
                value={access}
                onChange={(e) => setAccess(e.target.value as "local" | "space")}
              >
                <option value="local">{t("personal.thisDeviceOnly")}</option>
                <option value="space" disabled={space?.membership !== "active"}>
                  {t("personal.mySpaceDevices")}
                </option>
              </select>
            </label>
            <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>
              {t("personal.appliesToAITasksOnThisComputerFilesDesktopAccessAndSeparatelyIssuedAPIKeysAreNotChanged")}
            </span></p>
            {space?.membership !== "active" && (
              <Link className="rf-lnk" to="/settings/space">
                {t("personal.createOrJoinAPersonalSpaceFirst")}
              </Link>
            )}
            {error && (
              <p role="alert" className="rf-err">
                {personalLabelKeys[error] ? t(`personal.${personalLabelKeys[error]}`) : error}
              </p>
            )}
          </div>
          <footer className="rf-foot rf-foot-end">
            <button className="rf-btn" onClick={onClose}>
              {t("personal.cancel")}
            </button>
            <button
              className="rf-btn rf-btn-p"
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
        <div className="rf-body">
          <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>
            {t("personal.openRynOnThisComputerToChangeItsServicePermissionsMembershipDoesNotGrantRemoteAdministration")}
          </span></p>
        </div>
      )}
    </Modal>
  );
}
const serviceTone = (service: Service) => (service.online ? "ok" : "off") as "ok" | "off";
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
  const others = devices.filter((item) => !item.own);
  return (
    <Modal title={t("personal.shareServices")} onClose={onClose} className={SHARE_CLASS}>
      <ShareMark />
      <p className="rf-sub">{device.name} {t("personal.ownerOnly")}</p>
      <div className="rf-body">
        <fieldset className="rf-sec">
          <legend className="rf-step"><b>1</b><span className="rf-h3">{t("personal.chooseServices")}</span></legend>
          <div className="rf-pick-list">
            {available.map((service) => (
              <label className="rf-opt rf-opt-row" key={service.id}>
                <input
                  type="checkbox"
                  checked={selectedServices.includes(service.id)}
                  onChange={() =>
                    choose(service.id, selectedServices, setSelectedServices)
                  }
                />
                <ServiceTile kind={service.kind} />
                <span className="rf-opt-stack"><b>{service.title}</b><small>{t("personal.providedByThisDevice")}</small></span>
                <Pill tone={serviceTone(service)} small>{service.online ? t("personal.available") : t("personal.offline")}</Pill>
              </label>
            ))}
          </div>
          {!available.length && <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>{t("personal.noServicesOnThisDeviceYet")}</span></p>}
        </fieldset>
        <fieldset className="rf-sec">
          <legend className="rf-step"><b>2</b><span className="rf-h3">{t("personal.allowTheseDevices")}</span></legend>
          <div className="rf-pick-grid">
            {others.map((item) => (
              <label className={`rf-opt rf-opt-row rf-h-${deviceHue(item.id, { online: item.online })}`} key={item.id}>
                <input
                  type="checkbox"
                  checked={selectedDevices.includes(item.id)}
                  onChange={() =>
                    choose(item.id, selectedDevices, setSelectedDevices)
                  }
                />
                <span className="rf-avatar" aria-hidden="true">{initialsOf(item.name)}</span>
                <span className="rf-opt-stack"><b className="rf-ell">{item.name}</b><small className="rf-ell">{t("personal.sharedDevice")}</small></span>
              </label>
            ))}
          </div>
        </fieldset>
        <div className="rf-stack">
          <p className="rf-callout rf-callout-brand"><ShieldCheck size={15} aria-hidden="true" /><span>
            {t("personal.onlySelectedServicesWillBeSharedThisDoesNotGrantDesktopOrFullComputerControl")}
          </span></p>
          <p className="rf-callout"><Info size={15} aria-hidden="true" /><span>
            {demo
              ? t("personal.previewOnlySavingHereDoesNotGrantRealAccess")
              : t("personal.perdeviceServiceAccessIsNotSupportedByThisNodeVersionNoAccessWillBeChanged")}
          </span></p>
        </div>
      </div>
      <footer className="rf-foot">
        <small className="rf-foot-note">{t("personal.serviceCountShare", { count: selectedServices.length })} · {t("personal.countDevices", { count: selectedDevices.length })}</small>
        <button className="rf-btn" onClick={onClose}>
          {t("personal.cancel")}
        </button>
        <button
          className="rf-btn rf-btn-p"
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
