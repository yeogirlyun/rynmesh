import { nodeControlUrl } from "./nodeUrl";
import { tr } from "../uiI18n";
export type Field = {
  type?: string;
  enum?: string[];
  properties?: Record<string, Field>;
  items?: Field;
  maxLength?: number;
};
export type Operation = {
  effect: "read" | "write";
  schema: { properties: Record<string, Field>; required: string[] };
};
export type NodeApp = {
  id: string;
  name: string;
  description: string;
  operations: Record<string, Operation>;
};
export type WorkspaceContext = {
  app: string;
  provider_peer: string;
  consumer_peer: string;
  access_generation: string;
  revision: number;
  state_hash: string;
};
export type Workspace = WorkspaceContext & {
  app: string;
  provider_peer: string;
  consumer_peer: string;
  revision: number;
  records: Record<string, Record<string, unknown>[]>;
};
export type Preview = {
  app: string;
  action: string;
  arguments: Record<string, unknown>;
  provider_peer: string;
  consumer_peer: string;
  revision: number;
  effect: string;
  preview_hash: string;
  price_ryncoin: string;
};
export type Intent = {
  provider_peer: string;
  app: string;
  action: string;
  arguments: Record<string, unknown>;
  operation_id: string;
  preview_hash: string;
};
async function request<T>(
  path: string,
  body?: object,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch(nodeControlUrl(`/node-apps${path}`), {
    method: body ? "POST" : "GET",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}),
    signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(
      error.detail === "apps_review_changed"
        ? tr(
            "This workspace or sharing permission changed. Refresh and review again.",
          )
        : error.detail === "apps_response_unconfirmed"
          ? tr(
              "Response unconfirmed. Keep the saved operation and check its receipt before trying again.",
            )
          : error.detail === "apps_access_denied"
            ? tr("This provider has not shared this app with your node.")
            : tr(
                "This operation could not be confirmed. Refresh the workspace before making another change.",
              ),
    );
  }
  return response.json();
}
export const nodeApps = {
  pending: () =>
    request<
      {
        operation_id: string;
        app: string;
        action: string;
        provider_peer: string;
      }[]
    >("/pending"),
  resume: (operation_id: string) =>
    request<{ result: Record<string, unknown> }>("/resume", { operation_id }),
  catalog: (peer: string, signal?: AbortSignal) =>
    request<{ provider_peer: string; apps: NodeApp[] }>(
      `?provider_peer=${encodeURIComponent(peer)}`,
      undefined,
      signal,
    ),
  workspace: (app: string, peer: string, signal?: AbortSignal) =>
    request<Workspace>(
      `/${encodeURIComponent(app)}/workspace?provider_peer=${encodeURIComponent(peer)}`,
      undefined,
      signal,
    ),
  preview: (
    provider_peer: string,
    app: string,
    action: string,
    args: Record<string, unknown>,
    expected_context: WorkspaceContext,
  ) =>
    request<Preview>("/preview", {
      provider_peer,
      app,
      action,
      arguments: args,
      expected_context,
    }),
  invoke: (intent: Intent) =>
    request<{
      revision: number;
      result: Record<string, unknown>;
      operation_id: string;
    }>("/invoke", intent),
  grantPreview: (app: string, consumer_peer: string, allowed: boolean) =>
    request<Record<string, unknown> & { preview_hash: string }>(
      "/grants/preview",
      { app, consumer_peer, allowed },
    ),
  grant: (
    app: string,
    consumer_peer: string,
    allowed: boolean,
    preview_hash: string,
  ) => request("/grants", { app, consumer_peer, allowed, preview_hash }),
};
