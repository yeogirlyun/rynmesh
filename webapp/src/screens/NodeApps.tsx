import { useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useAppContext } from "../appContext";
import { useTranslation } from "react-i18next";
import {
  nodeApps,
  type Field,
  type NodeApp,
  type Preview,
  type Workspace,
  type Intent,
} from "../domain/nodeApps";
import "./nodeApps.css";
import { tr, useUILanguage } from "../uiI18n";
import { friendsApi } from "../domain/friendsClient";
type Friend = {
  peer_id: string;
  node_name: string;
  status: string;
};
const label = (value: string) =>
  tr(value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase()));
function Fields({
  fields,
  required = [],
  values,
  set,
  records,
  prefix = "",
}: {
  fields: Record<string, Field>;
  required?: string[];
  values: Record<string, unknown>;
  set: (value: Record<string, unknown>) => void;
  records: Workspace["records"];
  prefix?: string;
}) {
  return (
    <>
      {Object.entries(fields).map(([key, schema]) => {
        const name = `${prefix}${key}`;
        const change = (value: unknown) => set({ ...values, [key]: value });
        if (schema.type === "object")
          return (
            <fieldset key={key}>
              <legend>{label(key)}</legend>
              <Fields
                fields={schema.properties ?? {}}
                values={(values[key] as Record<string, unknown>) ?? {}}
                set={change}
                records={records}
                prefix={`${name}.`}
              />
            </fieldset>
          );
        if (key.endsWith("_id")) {
          const rows = records[key.slice(0, -3) + "s"] ?? [];
          return (
            <label key={key}>
              {label(key.endsWith("_id") ? key.slice(0, -3) : key)}
              <select
                aria-label={name}
                required={required.includes(key)}
                value={String(values[key] ?? "")}
                onChange={(e) => change(e.target.value)}
              >
                <option value="">{tr("Choose a record")}</option>
                {rows.map((r) => (
                  <option key={String(r.id)} value={String(r.id)}>
                    {String(r.name ?? r.title ?? r.id)}
                  </option>
                ))}
              </select>
            </label>
          );
        }
        if (schema.enum)
          return (
            <label key={key}>
              {label(key)}
              <select
                aria-label={name}
                required={required.includes(key)}
                value={String(values[key] ?? "")}
                onChange={(e) => change(e.target.value)}
              >
                <option value="">{tr("Choose")}</option>
                {schema.enum.map((value) => (
                  <option key={value}>{value}</option>
                ))}
              </select>
            </label>
          );
        if (schema.type === "boolean")
          return (
            <label key={key}>
              <input
                type="checkbox"
                aria-label={name}
                checked={Boolean(values[key])}
                onChange={(e) => change(e.target.checked)}
              />
              {label(key)}
            </label>
          );
        const value = values[key];
        if (schema.type === "array")
          return (
            <label key={key}>
              {label(key)}
              {tr("(one per line)")}
              <textarea
                aria-label={name}
                value={Array.isArray(value) ? value.join("\n") : ""}
                onChange={(e) =>
                  change(e.target.value.split("\n").filter(Boolean))
                }
              />
            </label>
          );
        return (
          <label key={key}>
            {label(key)}
            {required.includes(key) ? " *" : ""}
            <textarea
              aria-label={name}
              maxLength={schema.maxLength ?? 4000}
              required={required.includes(key)}
              value={String(value ?? "")}
              onChange={(e) =>
                change(
                  schema.type === "integer" || schema.type === "number"
                    ? Number(e.target.value)
                    : e.target.value,
                )
              }
            />
          </label>
        );
      })}
    </>
  );
}
function RecordDetails({ value }: { value: unknown }) {
  if (Array.isArray(value))
    return (
      <ul>
        {value.map((item, index) => (
          <li key={index}>
            <RecordDetails value={item} />
          </li>
        ))}
      </ul>
    );
  if (value && typeof value === "object")
    return (
      <dl>
        {Object.entries(value)
          .filter(
            ([key]) =>
              key !== "id" &&
              !key.endsWith("_id") &&
              !["created_at", "updated_at", "last_opened", "modified"].includes(
                key,
              ),
          )
          .map(([key, item]) => (
            <div key={key}>
              <dt>{label(key)}</dt>
              <dd>
                <RecordDetails value={item} />
              </dd>
            </div>
          ))}
      </dl>
    );
  return <span>{value == null ? "—" : String(value)}</span>;
}

function clean(value: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(value).flatMap(([key, item]) =>
      item === "" || item === undefined
        ? []
        : typeof item === "object" && item && !Array.isArray(item)
          ? [[key, clean(item as Record<string, unknown>)]]
          : [[key, item]],
    ),
  );
}
export default function NodeApps({
  standalone = false,
}: {
  standalone?: boolean;
}) {
  useUILanguage();
  const { t } = useTranslation();
  const [params] = useSearchParams();
  const requested = params.get("app");
  const initialApp = requested && ["resume", "recruit", "desk"].includes(requested) ? requested : "resume";
  const { client } = useAppContext();
  const [friends, setFriends] = useState<Friend[]>([]);
  const [peer, setPeer] = useState("");
  const [apps, setApps] = useState<NodeApp[]>([]);
  const [selected, setSelected] = useState(initialApp);
  const [workspace, setWorkspace] = useState<Workspace | null>(null);
  const [action, setAction] = useState(initialApp === "recruit" ? "create_job" : initialApp === "desk" ? "create_ticket" : "create_resume");
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [review, setReview] = useState<Preview | null>(null);
  const [intent, setIntent] = useState<Intent | null>(null);
  const [pending, setPending] = useState<
    {
      operation_id: string;
      app: string;
      action: string;
      provider_peer: string;
    }[]
  >([]);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [grant, setGrant] = useState<{
    peer: string;
    allowed: boolean;
    preview_hash: string;
    recipient_name: string;
  } | null>(null);
  const [recipient, setRecipient] = useState("");
  const sequence = useRef(0);
  const app = apps.find((item) => item.id === selected);
  const operation = app?.operations[action];
  const currentProvider = peer || workspace?.provider_peer;
  const blocked =
    pending.some(
      (item) => item.app === selected && item.provider_peer === currentProvider,
    ) ||
    Boolean(
      intent &&
      intent.app === selected &&
      intent.provider_peer === currentProvider,
    );
  const live = client.mode === "live";
  useEffect(() => {
    if (!live) return;
    let alive = true;
    void friendsApi
      .list()
      .then((value) => {
        if (alive)
          setFriends(value.friends.filter((f) => f.status === "active"));
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [live]);
  useEffect(() => {
    const current = ++sequence.current;
    if (live)
      void nodeApps
        .pending()
        .then(setPending)
        .catch(() => {});
    const controller = new AbortController();
    setWorkspace(null);
    setReview(null);
    setResult(null);
    setError("");
    setValues({});
    if (!live) return;
    void nodeApps
      .catalog(peer, controller.signal)
      .then((value) => {
        if (sequence.current !== current) return;
        setApps(value.apps);
        if (!value.apps.some((item) => item.id === selected)) {
          const first = value.apps[0];
          if (first) choose(first.id);
          return;
        }
        return nodeApps
          .workspace(selected, peer, controller.signal)
          .then((data) => {
            if (sequence.current === current) setWorkspace(data);
          });
      })
      .catch((cause) => {
        if (!controller.signal.aborted && sequence.current === current)
          setError(cause.message);
      });
    return () => controller.abort();
  }, [peer, selected, live]);
  function choose(id: string) {
    setSelected(id);
    setAction(
      { resume: "create_resume", recruit: "create_job", desk: "create_ticket" }[
        id
      ] ?? "editor_info",
    );
    setValues({});
  }
  async function refresh() {
    const current = sequence.current;
    try {
      const value = await nodeApps.workspace(selected, peer);
      if (current === sequence.current) setWorkspace(value);
    } catch (cause) {
      setError((cause as Error).message);
    }
  }
  async function preview() {
    setBusy(true);
    setError("");
    const current = sequence.current;
    try {
      if (!workspace) return;
      const { records: _records, ...expectedContext } = workspace;
      const value = await nodeApps.preview(
        peer,
        selected,
        action,
        clean(values),
        expectedContext,
      );
      if (current === sequence.current) setReview(value);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function commit(saved: Intent) {
    setBusy(true);
    setError("");
    const current = sequence.current;
    try {
      const value = await nodeApps.invoke(saved);
      setIntent(null);
      setPending(await nodeApps.pending());
      setReview(null);
      if (current === sequence.current) {
        setResult(value.result);
        await refresh();
      }
    } catch (cause) {
      setError((cause as Error).message);
      try {
        const saved = await nodeApps.pending();
        setPending(saved);
        if (!saved.length) {
          setIntent(null);
          setReview(null);
        }
      } catch {
        /* Preserve the original request until status is known. */
      }
    } finally {
      setBusy(false);
    }
  }
  async function grantPreview(allowed: boolean) {
    setBusy(true);
    try {
      const value = await nodeApps.grantPreview(selected, recipient, allowed);
      setGrant({
        peer: recipient,
        allowed,
        preview_hash: value.preview_hash,
        recipient_name: String(value.recipient_name ?? ""),
      });
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <main className="na-page">
      <header>
        <p className="na-eyebrow">{tr("RYNMESH APPS")}</p>
        <h1>{tr("Work with people, privately")}</h1>
        <p>
          {tr(
            "Resumes, hiring and service requests on your node or an explicitly shared friend node. App usage is free. Paid work uses reviewed",
          )}{" "}
          {standalone ? (
            tr("Exchange orders on your Rynmesh node")
          ) : (
            <Link to="/exchange">{tr("Exchange orders")}</Link>
          )}
          .
        </p>
      </header>
      {!live && (
        <p role="alert">
          {tr(
            "Connect to a live node to use apps. Sample mode cannot access private workspaces.",
          )}
        </p>
      )}
      {error && <p role="alert">{error === "apps_plugin_disabled" ? t("craft.errors.plugin_disabled") : error === "apps_plugin_unavailable" ? t("craft.errors.plugin_unavailable") : error}</p>}
      {(intent || pending.length > 0) && (
        <aside>
          <strong>{tr("Saved operation awaiting confirmation")}</strong>
          <p>
            {tr(
              "Keep this exact request. A retry checks the original receipt and cannot create a second record.",
            )}
          </p>
          <button
            disabled={busy}
            onClick={() => {
              if (intent) void commit(intent);
              else if (pending[0]) {
                setBusy(true);
                void nodeApps
                  .resume(pending[0].operation_id)
                  .then(async (value) => {
                    setPending(await nodeApps.pending());
                    setResult(value.result);
                    await refresh();
                  })
                  .catch((cause) => setError(cause.message))
                  .finally(() => setBusy(false));
              }
            }}
          >
            {tr("Retry original operation")}
          </button>
        </aside>
      )}
      <div className="na-toolbar">
        <label>
          {tr("Provider")}
          <select
            aria-label={tr("Provider")}
            value={peer}
            disabled={busy || !!review || !!grant}
            onChange={(e) => setPeer(e.target.value)}
          >
            <option value="">{tr("This node")}</option>
            {friends.map((friend) => (
              <option key={friend.peer_id} value={friend.peer_id}>
                {friend.node_name || "Friend node"}
              </option>
            ))}
          </select>
        </label>
        <button disabled={busy || !workspace} onClick={() => void refresh()}>
          {tr("Refresh workspace")}
        </button>
      </div>
      <nav aria-label={tr("Apps")}>
        {apps.map((item) => (
          <button
            key={item.id}
            disabled={busy || !!review || !!grant}
            aria-pressed={selected === item.id}
            onClick={() => choose(item.id)}
          >
            {item.name}
          </button>
        ))}
      </nav>
      {live && apps.length === 0 && (
        <p>
          {tr(
            "No apps shared by this provider. Its owner can enable one app for your node.",
          )}
        </p>
      )}
      {app && (
        <>
          <h2>
            {app.runtime
              ? app.name
              : tr(
                  app.id === "resume"
                    ? "Resume"
                    : app.id === "recruit"
                      ? "Recruit"
                      : "Desk",
                )}
          </h2>
          {app.runtime ? (
            <section aria-label={tr("Craft editor details")}>
              <p>{app.description}</p>
              <p>
                {tr("Pinned version")}: {app.editor?.version}
              </p>
              <p>
                {app.runtime === "craft_web"
                  ? tr(
                      "Browser editor: install the verified build on a separate editor origin. Documents stay in the editor browser or explicit file exports; they are not synced to this workspace.",
                    )
                  : tr(
                      "Native only in this pinned release. Browser editing is unavailable; no native app is installed or launched by Rynmesh.",
                    )}
              </p>
              <p>
                {tr(
                  "Editor information is available through the reviewed operation below. Document editing tools and user launch configuration are not connected yet.",
                )}
              </p>
            </section>
          ) : (
            <p>
              {tr(
                app.id === "resume"
                  ? "Build resumes and cover letters; export reviewed text."
                  : app.id === "recruit"
                    ? "Manage job requests and candidate stages. Budgets quote Ryncoin; payments use Exchange."
                    : "Track service requests, private comments and resolutions.",
              )}
            </p>
          )}
          <div className="na-columns">
            {!app.runtime && (
              <section>
                <h3>{tr("Your workspace")}</h3>
                {Object.entries(workspace?.records ?? {})
                  .filter(([key]) => !["settings", "exports"].includes(key))
                  .map(([bucket, records]) => (
                    <div key={bucket}>
                      <h4>{label(bucket)}</h4>
                      {records.map((record) => (
                        <article key={String(record.id)}>
                          <h4>
                            {String(record.name ?? record.title ?? bucket)}
                          </h4>
                          <p>
                            {String(
                              record.target_role ??
                                record.description ??
                                record.stage ??
                                record.status ??
                                "",
                            )}
                          </p>
                          {Boolean(record.budget_ryncoin) && (
                            <p>
                              {String(record.budget_ryncoin)}{" "}
                              {tr(
                                "Ryncoin quoted \u00B7 payment handled in Exchange",
                              )}
                            </p>
                          )}
                          <button
                            onClick={() => {
                              setValues({
                                [bucket.slice(0, -1) + "_id"]: record.id,
                              });
                              setAction(
                                {
                                  resumes: "get_resume",
                                  jobs: "get_job",
                                  candidates: "get_candidate",
                                  tickets: "get_ticket",
                                }[bucket] ?? action,
                              );
                            }}
                          >
                            {tr("Open record")}
                          </button>
                        </article>
                      ))}
                    </div>
                  ))}
                {!Object.values(workspace?.records ?? {}).some(
                  (rows) => rows.length,
                ) && (
                  <p>
                    {tr("No records yet. Create your first")}{" "}
                    {app.id === "resume"
                      ? "resume"
                      : app.id === "recruit"
                        ? "job request"
                        : "service ticket"}
                    .
                  </p>
                )}
              </section>
            )}
            <section>
              <h3>
                {tr("Work on")}
                {app.name.toLowerCase()}
              </h3>
              <label>
                {tr("Operation")}
                <select
                  aria-label={tr("Operation")}
                  value={action}
                  disabled={busy || blocked || !!review || !!grant}
                  onChange={(e) => {
                    setAction(e.target.value);
                    setValues({});
                    setReview(null);
                  }}
                >
                  {Object.keys(app.operations).map((name) => (
                    <option key={name} value={name}>
                      {label(name)}
                    </option>
                  ))}
                </select>
              </label>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void preview();
                }}
              >
                {operation && (
                  <Fields
                    fields={operation.schema.properties}
                    required={operation.schema.required}
                    values={values}
                    set={setValues}
                    records={workspace?.records ?? {}}
                  />
                )}
                <button disabled={!live || busy || !workspace || blocked}>
                  {tr("Review operation")}
                </button>
              </form>
            </section>
          </div>
          {result && (
            <section>
              <h3>{tr("Confirmed result")}</h3>
              {typeof result.text === "string" ? (
                <>
                  <textarea
                    aria-label={tr("Resume export")}
                    readOnly
                    value={result.text}
                  />
                  <button
                    onClick={() => {
                      const blob = new Blob([String(result.text)], {
                        type: "text/markdown",
                      });
                      const url = URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = url;
                      a.download = "resume.md";
                      a.click();
                      URL.revokeObjectURL(url);
                    }}
                  >
                    {tr("Download resume text")}
                  </button>
                </>
              ) : (
                <RecordDetails
                  value={result.record ?? result.comment ?? result}
                />
              )}
            </section>
          )}
          {!peer && (
            <section>
              <h3>{tr("Share this app with one friend")}</h3>
              <p>
                {tr(
                  "They get their own private workspace. Your records stay separate.",
                )}
              </p>
              <select
                aria-label={tr("Share recipient")}
                value={recipient}
                disabled={busy}
                onChange={(e) => setRecipient(e.target.value)}
              >
                <option value="">{tr("Choose a friend")}</option>
                {friends.map((friend) => (
                  <option key={friend.peer_id} value={friend.peer_id}>
                    {friend.node_name}
                  </option>
                ))}
              </select>
              <button
                disabled={!recipient || busy}
                onClick={() => void grantPreview(true)}
              >
                {tr("Review sharing")}
              </button>
              <button
                disabled={!recipient || busy}
                onClick={() => void grantPreview(false)}
              >
                {tr("Review revocation")}
              </button>
            </section>
          )}
        </>
      )}
      {review && (
        <section
          role="dialog"
          aria-modal="true"
          aria-label={tr("Review app operation")}
        >
          <h2>{label(review.action)}</h2>
          <p>
            {app?.name} ·{" "}
            {peer
              ? friends.find((f) => f.peer_id === peer)?.node_name
              : "This node"}{" "}
            {tr("\u00B7 workspace revision")}
            {review.revision}
            {tr("\u00B7 free")}
          </p>
          <dl>
            {Object.entries(review.arguments).map(([key, value]) => (
              <div key={key}>
                <dt>{label(key)}</dt>
                <dd>
                  {typeof value === "object"
                    ? JSON.stringify(value)
                    : String(value)}
                </dd>
              </div>
            ))}
          </dl>
          <p>
            {tr(
              "This applies only to the reviewed provider, app and workspace state.",
            )}
          </p>
          <button
            disabled={busy}
            onClick={() => {
              const saved = {
                provider_peer: review.provider_peer,
                app: review.app,
                action: review.action,
                arguments: review.arguments,
                operation_id: crypto.randomUUID().replaceAll("-", ""),
                preview_hash: review.preview_hash,
              };
              setIntent(saved);
              void commit(saved);
            }}
          >
            {tr("Confirm operation")}
          </button>
          <button disabled={busy} onClick={() => setReview(null)}>
            {tr("Cancel")}
          </button>
        </section>
      )}
      {grant && (
        <section
          role="dialog"
          aria-modal="true"
          aria-label={tr("Review app sharing")}
        >
          <h2>
            {grant.allowed ? tr("Share") : tr("Revoke")} {app?.name}
          </h2>
          <p>
            {grant.recipient_name}
            {tr(
              "\u00B7 separate workspace \u00B7 no other friend permissions change.",
            )}
          </p>
          <button
            disabled={busy}
            onClick={async () => {
              setBusy(true);
              try {
                await nodeApps.grant(
                  selected,
                  grant.peer,
                  grant.allowed,
                  grant.preview_hash,
                );
                setGrant(null);
              } catch (cause) {
                setError((cause as Error).message);
              } finally {
                setBusy(false);
              }
            }}
          >
            {tr("Confirm sharing change")}
          </button>
          <button onClick={() => setGrant(null)}>{tr("Cancel")}</button>
        </section>
      )}
    </main>
  );
}
