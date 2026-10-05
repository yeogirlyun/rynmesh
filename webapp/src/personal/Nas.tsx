import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import {
  Folder,
  FileText,
  HardDrive,
  Plus,
  RefreshCw,
  ArrowUp,
  Download,
  Sparkles,
  Search,
  Upload,
  FolderPlus,
  ChevronRight,
  MoreHorizontal,
  ShieldCheck,
  Info,
  X,
  ArrowUpDown,
  File,
} from "lucide-react";
import { useAppContext } from "../appContext";
import { Modal, PageHeading } from "./components";
import { usePersonal } from "./model";
import {
  nasChanged,
  nasJson,
  nasRequest,
  setNasHandoff,
  useNasStatus,
  type NasEntry,
  type NasSource,
  type NasStatus,
} from "../domain/nas";
import "./nas.css";
import { NasPreview, NasThumbnail, isNasImage, isNasText } from "./NasPreview";

function fileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}
function modifiedTime(entry: NasEntry) {
  if (!entry.modified) return 0;
  const value = new Date(
    typeof entry.modified === "number" ? entry.modified * 1000 : entry.modified,
  ).getTime();
  return Number.isFinite(value) ? value : 0;
}
function modifiedLabel(entry: NasEntry, language: string) {
  const time = modifiedTime(entry);
  return time
    ? new Intl.DateTimeFormat(language, {
        day: "2-digit",
        month: "short",
        year: "numeric",
      }).format(time)
    : "—";
}
function fileKind(entry: NasEntry, t: TFunction) {
  if (entry.directory) return t("personal.folder");
  const extension = entry.name.includes(".")
    ? entry.name.split(".").pop()?.toLowerCase()
    : "";
  if (
    ["txt", "md", "csv", "json", "log", "yaml", "yml"].includes(extension || "")
  )
    return t("personal.textDocument");
  return extension ? t("personal.extensionFile", { extension: extension.toUpperCase() }) : t("personal.file");
}

export function PluginsPage() {
  const { t, i18n } = useTranslation();
  const { demo } = usePersonal();
  const { status, error } = useNasStatus(demo);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState("");
  const { confirm } = useAppContext();
  const toggle = async () => {
    setBusy(true);
    setFailure("");
    try {
      await nasJson("", { enabled: !status?.enabled }, "PUT");
      setNasHandoff(null);
      nasChanged();
    } catch (e) {
      setFailure((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="screen-stack">
      <PageHeading
        title={t("personal.plugins")}
        description={t("personal.chooseTheCapabilitiesAvailableOnThisDevice")}
      />
      {(error || failure) && <p role="alert">{failure || error}</p>}
      <section className="nas-plugin-card">
        <HardDrive size={36} />
        <div>
          <h2>NAS</h2>
          <p>
            {t("personal.connectYourNASFoldersBrowseFilesUploadDocumentsAndUseSelectedTextWithAIChat")}
          </p>
          <p>
            {t("personal.connectionsAreManagedByThisDeviceDisablingKeepsTheirConfigurationAndHidesNAS")}
          </p>
          <small>
            {demo
              ? t("personal.pluginSetupIsAvailableOnALiveDevice")
              : status?.enabled
                ? t("personal.enabled")
                : t("personal.disabled")}
          </small>
        </div>
        <button
          className="pf-button primary"
          disabled={!status || busy || demo}
          onClick={() =>
            status?.enabled
              ? confirm({
                  risk: "medium",
                  title: t("personal.disableNAS"),
                  body: t("personal.waitForAnyCurrentFileOperationToFinishThenStopNASAccessOnThisDeviceSavedConnectionsWillBeRetained"),
                  confirmLabel: t("personal.disable"),
                  onConfirm: toggle,
                })
              : void toggle()
          }
        >
          {busy ? t("personal.saving") : status?.enabled ? t("personal.disable") : t("personal.enable")}
        </button>
      </section>
      {status?.enabled && (
        <Link className="pf-button" to="/nas">
          {t("personal.openNAS")}
        </Link>
      )}
    </div>
  );
}

export function NasPage() {
  const { t, i18n } = useTranslation();
  const { demo } = usePersonal();
  const { status, error } = useNasStatus(demo);
  if (error) return <p role="alert">{error}</p>;
  if (!status) return <p>{t("personal.loadingPlugin")}</p>;
  if (!status.enabled)
    return (
      <div className="screen-stack">
        <PageHeading
          title={t("personal.nasIsDisabled")}
          description={t("personal.enableTheNASPluginToConnectYourStorage")}
        />
        <Link className="pf-button" to="/settings/plugins">
          {t("personal.managePlugins")}
        </Link>
      </div>
    );
  return <NasBrowser status={status} />;
}

function NasBrowser({ status }: { status: NasStatus }) {
  const { t, i18n } = useTranslation();
  const { notify, confirm } = useAppContext();
  const navigate = useNavigate();
  const [selected, setSelected] = useState(status.sources[0]?.id || "");
  const source = status.sources.find((s) => s.id === selected);
  const [path, setPath] = useState("");
  const [entries, setEntries] = useState<NasEntry[]>([]);
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("name");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [folder, setFolder] = useState<string | null>(null);
  const [preview, setPreview] = useState<NasEntry | null>(null);
  const [revision, setRevision] = useState(0);
  const upload = useRef<HTMLInputElement>(null);
  const generation = useRef(0);
  useEffect(() => {
    if (!status.sources.some((s) => s.id === selected)) {
      setSelected(status.sources[0]?.id || "");
      setPath("");
    }
  }, [status, selected]);
  useEffect(() => {
    const id = ++generation.current;
    setEntries([]);
    setError("");
    setPreview(null);
    if (!source) return;
    setBusy(true);
    void nasJson<{ entries: NasEntry[] }>(
      `/sources/${source.id}/files?path=${encodeURIComponent(path)}`,
    )
      .then((data) => {
        if (id === generation.current) setEntries(data.entries);
      })
      .catch((e) => {
        if (id === generation.current) setError(e.message);
      })
      .finally(() => {
        if (id === generation.current) setBusy(false);
      });
    return () => {
      generation.current++;
    };
  }, [source?.id, path, revision]);
  async function action(fn: () => Promise<void>) {
    setBusy(true);
    setError("");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function content(entry: NasEntry, mode: "preview" | "download" | "ai") {
    if (!source) return;
    if (mode === "preview") {
      setPreview(entry);
      return;
    }
    await action(async () => {
      const response = await nasRequest(
        `/sources/${source.id}/content?path=${encodeURIComponent(entry.path)}&mode=${mode}`,
      );
      if (mode === "download") {
        const url = URL.createObjectURL(await response.blob());
        const a = document.createElement("a");
        a.href = url;
        a.download = entry.name;
        a.click();
        window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        notify("ok", t("personal.fileDownloaded"));
      } else {
        const value = await response.json();
        {
          setNasHandoff({
            sourceId: source.id,
            sourceName: source.name,
            path: entry.path,
            text: value.text,
            writable: source.writable,
          });
          navigate("/services/private-ai/chat");
        }
      }
    });
  }
  const visibleEntries = entries
    .filter((entry) => entry.name.toLowerCase().includes(query.toLowerCase()))
    .sort((a, b) => {
      if (a.directory !== b.directory) return a.directory ? -1 : 1;
      if (sort === "modified")
        return (
          modifiedTime(b) - modifiedTime(a) || a.name.localeCompare(b.name)
        );
      if (sort === "size")
        return b.size - a.size || a.name.localeCompare(b.name);
      return a.name.localeCompare(b.name, undefined, { numeric: true });
    });
  const navigateFolder = (next: string) => {
    setPath(next);
    setQuery("");
  };
  const removeConnection = () => {
    if (!source) return;
    confirm({
      risk: "medium",
      title: t("personal.removeNASConnection"),
      body: t("personal.removeNameFromRynFilesOnTheNASStayInPlace", { name: source.name }),
      confirmLabel: t("personal.remove"),
      onConfirm: async () => {
        await action(async () => {
          await nasRequest(`/sources/${source.id}`, { method: "DELETE" });
          setNasHandoff(null);
          nasChanged();
        });
      },
    });
  };
  return (
    <div className="nas-page">
      <header className="nas-heading">
        <div>
          <h1>NAS</h1>
          <p>{t("personal.browseFilesAcrossYourConnectedStorage")}</p>
        </div>
        <button className="pf-button nas-add" onClick={() => setAdding(true)}>
          <Plus size={16} />
          {t("personal.addNAS")}
        </button>
      </header>
      {!status.sources.length ? (
        <section className="nas-empty nas-first-connection">
          <span className="nas-empty-icon">
            <HardDrive size={30} strokeWidth={1.4} />
          </span>
          <h2>{t("personal.yourStorageConnected")}</h2>
          <p>
            {t("personal.bringFoldersFromFeiniuSynologyAndOtherNASSystemsIntoRyn")}
          </p>
          <button className="pf-button primary" onClick={() => setAdding(true)}>
            <Plus size={16} />
            {t("personal.addNAS")}
          </button>
        </section>
      ) : (
        <>
          <nav className="nas-sources" aria-label={t("personal.nasConnections")}>
            {status.sources.map((s) => (
              <button
                key={s.id}
                disabled={busy}
                aria-pressed={selected === s.id}
                className={`nas-source ${selected === s.id ? "active" : ""}`}
                onClick={() => {
                  setSelected(s.id);
                  navigateFolder("");
                }}
              >
                <span className="nas-source-icon">
                  <HardDrive size={21} strokeWidth={1.5} />
                </span>
                <span className="nas-source-label">
                  <strong>{s.name}</strong>
                  <small>
                    {status.systems.find((t) => t.id === s.system)?.name}
                  </small>
                </span>
                <span className="nas-source-access">
                  {s.writable ? t("personal.readWrite") : t("personal.readOnly")}
                </span>
              </button>
            ))}
          </nav>
          {source && (
            <section
              className="nas-workspace"
              aria-label={t("personal.nameFiles", { name: source.name })}
            >
              <div className="nas-location-bar">
                <div className="nas-location">
                  <button
                    className="nas-icon-button"
                    disabled={!path || busy}
                    aria-label={t("personal.up")}
                    title={t("personal.upOneFolder")}
                    onClick={() =>
                      navigateFolder(path.split("/").slice(0, -1).join("/"))
                    }
                  >
                    <ArrowUp size={17} />
                  </button>
                  <nav className="nas-breadcrumbs" aria-label={t("personal.folderPath")}>
                    <button
                      disabled={busy}
                      onClick={() => navigateFolder("")}
                      aria-current={!path ? "location" : undefined}
                    >
                      <HardDrive size={14} />
                      {t("personal.allFiles")}
                    </button>
                    {path
                      .split("/")
                      .filter(Boolean)
                      .map((part, index, parts) => (
                        <span key={parts.slice(0, index + 1).join("/")}>
                          <ChevronRight size={13} />
                          <button
                            title={part}
                            disabled={busy}
                            aria-current={
                              index === parts.length - 1
                                ? "location"
                                : undefined
                            }
                            onClick={() =>
                              navigateFolder(
                                parts.slice(0, index + 1).join("/"),
                              )
                            }
                          >
                            {part}
                          </button>
                        </span>
                      ))}
                  </nav>
                </div>
                {source.writable && (
                  <div className="nas-write-actions">
                    <button
                      className="pf-button nas-folder-button"
                      disabled={busy}
                      onClick={() => setFolder("")}
                    >
                      <FolderPlus size={16} />
                      <span>{t("personal.newFolder")}</span>
                    </button>
                    <button
                      className="pf-button primary"
                      disabled={busy}
                      onClick={() => upload.current?.click()}
                    >
                      <Upload size={16} />
                      {t("personal.uploadFile")}
                    </button>
                  </div>
                )}
              </div>
              <div className="nas-filter-bar">
                <label className="nas-search">
                  <Search size={17} />
                  <input
                    aria-label={t("personal.filterThisFolder")}
                    placeholder={t("personal.searchThisFolder")}
                    value={query}
                    onChange={(e) => setQuery(e.target.value)}
                  />
                  {query && (
                    <button
                      className="nas-icon-button"
                      aria-label={t("personal.clearSearch")}
                      onClick={() => setQuery("")}
                    >
                      <X size={14} />
                    </button>
                  )}
                </label>
                <label className="nas-sort">
                  <ArrowUpDown size={14} />
                  <select
                    aria-label={t("personal.sortFiles")}
                    value={sort}
                    onChange={(e) => setSort(e.target.value)}
                  >
                    <option value="name">{t("personal.name")}</option>
                    <option value="modified">{t("personal.lastModified")}</option>
                    <option value="size">{t("personal.size")}</option>
                  </select>
                </label>
                <div className="nas-workspace-tools">
                  <span
                    className={`nas-connection-status ${error ? "attention" : ""}`}
                  >
                    <i />
                    {busy ? t("personal.working") : error ? t("personal.needsAttention") : t("personal.ready")}
                  </span>
                  <button
                    className="nas-icon-button"
                    disabled={busy}
                    onClick={() => setRevision((r) => r + 1)}
                    aria-label={t("personal.refresh")}
                    title={t("personal.refresh")}
                  >
                    <RefreshCw
                      size={17}
                      className={busy ? "nas-spinning" : ""}
                    />
                  </button>
                  <details className="nas-connection-menu">
                    <summary
                      aria-label={t("personal.connectionDetails")}
                      title={t("personal.connectionDetails")}
                    >
                      <MoreHorizontal size={20} />
                    </summary>
                    <div className="nas-menu-panel">
                      <span className="nas-eyebrow">{t("personal.connection")}</span>
                      <strong>{source.name}</strong>
                      <dl>
                        <div>
                          <dt>{t("personal.system")}</dt>
                          <dd>
                            {
                              status.systems.find((s) => s.id === source.system)
                                ?.name
                            }
                          </dd>
                        </div>
                        <div>
                          <dt>{t("personal.protocol")}</dt>
                          <dd>{source.protocol.toUpperCase()}</dd>
                        </div>
                        <div>
                          <dt>{t("personal.access")}</dt>
                          <dd>
                            {source.writable ? t("personal.readWrite") : t("personal.readOnly")}
                          </dd>
                        </div>
                        <div>
                          <dt>{t("personal.aiAccess")}</dt>
                          <dd>
                            {source.allow_ai
                              ? t("personal.selectedFilesOnly")
                              : t("personal.disabled")}
                          </dd>
                        </div>
                      </dl>
                      <p>
                        {t("personal.connectedThroughThisDeviceFilesStayOnYourNAS")}
                      </p>
                      <button
                        className="nas-remove"
                        disabled={busy}
                        onClick={removeConnection}
                      >
                        {t("personal.removeConnection")}
                      </button>
                    </div>
                  </details>
                </div>
              </div>
              <input
                ref={upload}
                aria-label={t("personal.uploadFile")}
                type="file"
                hidden
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (!file) return;
                  void action(async () => {
                    if (file.size > status.max_file_bytes)
                      throw new Error(
                        t("personal.filesMustBe64MiBOrSmallerInThisRelease"),
                      );
                    await nasRequest(
                      `/sources/${source.id}/content?path=${encodeURIComponent((path ? path + "/" : "") + file.name)}`,
                      { method: "PUT", body: file },
                    );
                    notify("ok", t("personal.fileSavedToNAS"));
                    setRevision((r) => r + 1);
                  });
                }}
              />
              {error && (
                <div role="alert" className="nas-error">
                  <Info size={16} />
                  {error}
                </div>
              )}
              <div
                className={`nas-browser-body ${preview ? "has-preview" : ""}`}
              >
                <div className="nas-file-area" aria-busy={busy}>
                  <table className="nas-table" aria-label={t("personal.nasFiles")}>
                    <thead>
                      <tr>
                        <th scope="col">{t("personal.name")}</th>
                        <th scope="col" className="nas-type-column">
                          {t("personal.type")}
                        </th>
                        <th scope="col" className="nas-date-column">
                          {t("personal.modified")}
                        </th>
                        <th scope="col" className="nas-size-column">
                          {t("personal.size")}
                        </th>
                        <th scope="col" className="nas-actions-column">
                          <span className="nas-sr-only">{t("personal.actions")}</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {visibleEntries.map((entry) => (
                        <tr
                          key={entry.path}
                          className={
                            preview?.path === entry.path ? "selected" : ""
                          }
                        >
                          <td>
                            <button
                              className="nas-file-name"
                              disabled={busy}
                              onClick={() =>
                                entry.directory
                                  ? navigateFolder(entry.path)
                                  : void content(entry, "preview")
                              }
                            >
                              {isNasImage(entry) ? (
                                <NasThumbnail
                                  key={`${source.id}:${entry.path}:${revision}`}
                                  source={source.id}
                                  entry={entry}
                                />
                              ) : (
                                <span
                                  className={`nas-file-icon ${entry.directory ? "folder" : isNasText(entry) ? "text" : ""}`}
                                >
                                  {entry.directory ? (
                                    <Folder size={22} strokeWidth={1.5} />
                                  ) : isNasText(entry) ? (
                                    <FileText size={20} strokeWidth={1.5} />
                                  ) : (
                                    <File size={20} strokeWidth={1.5} />
                                  )}
                                </span>
                              )}
                              <span title={entry.name}>{entry.name}</span>
                            </button>
                          </td>
                          <td className="nas-type-column">{fileKind(entry, t)}</td>
                          <td className="nas-date-column">
                            {modifiedLabel(entry, i18n.language)}
                          </td>
                          <td className="nas-size-column">
                            {entry.directory ? "—" : fileSize(entry.size)}
                          </td>
                          <td className="nas-actions-column">
                            <div className="nas-file-actions">
                              {entry.directory ? (
                                <ChevronRight
                                  size={16}
                                  className="nas-folder-arrow"
                                />
                              ) : (
                                <>
                                  <button
                                    className="nas-icon-button"
                                    disabled={busy}
                                    aria-label={t("personal.downloadName", { name: entry.name })}
                                    title={t("personal.downloadFile")}
                                    onClick={() =>
                                      void content(entry, "download")
                                    }
                                  >
                                    <Download size={16} />
                                  </button>
                                  {source.allow_ai && isNasText(entry) && (
                                    <button
                                      className="nas-ai-button"
                                      disabled={busy}
                                      onClick={() => void content(entry, "ai")}
                                    >
                                      <Sparkles size={14} />
                                      {t("personal.askAI")}
                                    </button>
                                  )}
                                </>
                              )}
                            </div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {busy && !entries.length && (
                    <div className="nas-loading" role="status">
                      <RefreshCw size={20} className="nas-spinning" />
                      <span>{t("personal.openingYourFiles")}</span>
                    </div>
                  )}
                  {!busy && !error && visibleEntries.length === 0 && (
                    <div className="nas-empty">
                      <span className="nas-empty-icon">
                        {query ? (
                          <Search size={26} strokeWidth={1.4} />
                        ) : (
                          <Folder size={28} strokeWidth={1.4} />
                        )}
                      </span>
                      <h3>{query ? t("personal.noMatchingFiles") : t("personal.aFreshFolder")}</h3>
                      <p>
                        {query
                          ? t("personal.tryAnotherNameOrClearYourSearch")
                          : source.writable
                            ? t("personal.uploadAFileToMakeYourselfAtHome")
                            : t("personal.filesAddedToThisFolderWillAppearHere")}
                      </p>
                      {query && (
                        <button
                          className="pf-button"
                          onClick={() => setQuery("")}
                        >
                          {t("personal.clearSearch")}
                        </button>
                      )}
                    </div>
                  )}
                </div>
                {preview && (
                  <NasPreview
                    key={`${source.id}:${preview.path}:${revision}`}
                    source={source}
                    entry={preview}
                    onClose={() => setPreview(null)}
                    download={() => void content(preview, "download")}
                    askAi={() => void content(preview, "ai")}
                    busy={busy}
                  />
                )}
              </div>
              <footer className="nas-footer">
                <span>
                  {busy
                    ? t("personal.updating")
                    : t(query ? "personal.filteredItemsCount" : "personal.itemsCount", { count: visibleEntries.length, total: entries.length })}
                </span>
                <div>
                  <ShieldCheck size={14} />
                  <span>{t("personal.storedOnYourNAS")}</span>
                  <details className="nas-limits">
                    <summary aria-label={t("personal.fileLimits")} title={t("personal.fileLimits")}>
                      <Info size={14} />
                    </summary>
                    <p>
                      {t("personal.uploadAndDownloadUpTo64MiBPerFileTextPreviewAndAIInputUpTo256KiBJPEGPNGAndWebPPreviewsUpTo20MiB40MegapixelsExistingFilesAreNeverOverwritten")}
                    </p>
                  </details>
                </div>
              </footer>
            </section>
          )}
        </>
      )}
      {adding && (
        <AddNas
          status={status}
          onClose={() => setAdding(false)}
          onAdded={(id) => {
            setSelected(id);
            navigateFolder("");
            nasChanged();
            setAdding(false);
          }}
        />
      )}
      {folder !== null && source && (
        <Modal title={t("personal.newFolder")} onClose={() => setFolder(null)}>
          <form
            className="nas-form"
            onSubmit={(e) => {
              e.preventDefault();
              void action(async () => {
                await nasJson(`/sources/${source.id}/folders`, {
                  path: (path ? path + "/" : "") + folder,
                });
                setFolder(null);
                setRevision((r) => r + 1);
              });
            }}
          >
            <p>{t("personal.createAFolderInPath", { path: path || source.share || source.name })}</p>
            <label>
              {t("personal.folderName")}
              <input
                required
                autoFocus
                value={folder}
                onChange={(e) => setFolder(e.target.value)}
              />
            </label>
            {error && (
              <p role="alert" className="nas-error">
                {error}
              </p>
            )}
            <footer className="pf-dialog-actions">
              <button
                type="button"
                className="pf-button"
                onClick={() => setFolder(null)}
              >
                {t("personal.cancel")}
              </button>
              <button className="pf-button primary" disabled={busy}>
                <FolderPlus size={16} />
                {t("personal.createFolder")}
              </button>
            </footer>
          </form>
        </Modal>
      )}
    </div>
  );
}

function AddNas({
  status,
  onClose,
  onAdded,
}: {
  status: NasStatus;
  onClose: () => void;
  onAdded: (id: string) => void;
}) {
  const { t, i18n } = useTranslation();
  const [system, setSystem] = useState("fnos");
  const [protocol, setProtocol] = useState("smb");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const supported = status.systems.find((s) => s.id === system)?.protocols || [
    "smb",
  ];
  return (
    <Modal title={t("personal.addNAS")} onClose={onClose}>
      <form
        className="nas-form"
        onSubmit={(e) => {
          e.preventDefault();
          const values = Object.fromEntries(new FormData(e.currentTarget));
          setBusy(true);
          setError("");
          void nasJson<NasSource>("/sources", {
            ...values,
            system,
            protocol,
            writable: values.writable === "on",
            allow_ai: values.allow_ai === "on",
          })
            .then((s) => onAdded(s.id))
            .catch((e) => setError(e.message))
            .finally(() => setBusy(false));
        }}
      >
        <label>
          {t("personal.nasSystem")}
          <select
            value={system}
            onChange={(e) => {
              setSystem(e.target.value);
              setProtocol("smb");
            }}
          >
            {status.systems.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          {t("personal.connectionName")}
          <input name="name" required maxLength={80} placeholder={t("personal.homeNAS")} />
        </label>
        <label>
          {t("personal.fileProtocol")}
          <select
            value={protocol}
            onChange={(e) => setProtocol(e.target.value)}
          >
            {supported.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </label>
        {protocol === "smb" ? (
          <>
            <label>
              {t("personal.host")}
              <input name="host" required placeholder="192.168.1.20" />
            </label>
            <div className="nas-form-pair">
              <label>
                {t("personal.sharedFolder")}
                <input name="share" required placeholder={t("personal.documents")} />
              </label>
              <label>
                {t("personal.port")}
                <input
                  name="port"
                  type="number"
                  min="1"
                  max="65535"
                  defaultValue="445"
                  required
                />
              </label>
            </div>
          </>
        ) : (
          <label>
            {t("personal.webdavURL")}
            <input
              name="url"
              type="url"
              required
              placeholder="https://nas.example.com:5006"
            />
          </label>
        )}
        <label>
          {t("personal.subfolderOptional")}
          <input name="root" placeholder="Work/Reports" />
        </label>
        <div className="nas-form-pair">
          <label>
            {t("personal.username")}
            <input name="username" autoComplete="off" required />
          </label>
          <label>
            {t("personal.password")}
            <input
              name="password"
              type="password"
              autoComplete="new-password"
              required
            />
          </label>
        </div>
        <label className="nas-check">
          <input name="writable" type="checkbox" />
          {t("personal.allowUploadingAndCreatingFolders")}
        </label>
        <label className="nas-check">
          <input name="allow_ai" type="checkbox" />
          {t("personal.allowSelectedTextFilesToBeOpenedInAIChat")}
        </label>
        <p>
          {t("personal.enableFileSharingOnYourNASFirstUseAnAccountRestrictedToTheFoldersYouWantToConnectRynChecksAccessBeforeSaving")}
        </p>
        {error && (
          <p role="alert" className="nas-error">
            {error}
          </p>
        )}
        <footer className="pf-dialog-actions">
          <button
            type="button"
            className="pf-button"
            disabled={busy}
            onClick={onClose}
          >
            {t("personal.cancel")}
          </button>
          <button className="pf-button primary" disabled={busy}>
            {busy ? t("personal.testingConnection") : t("personal.testAndAdd")}
          </button>
        </footer>
      </form>
    </Modal>
  );
}
