import i18n from "../i18n";
import { beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";
import type { AppOutletContext } from "../appContext";
import { makeFixtureNodeClient } from "../domain/fixtureNodeClient";
import type { InferenceAccess } from "../domain/nodeClient";
import { APIAccessPage, AgentSharingPage, ModelMappingPage } from "./AIWorkspacePages";

const target = { id: "peer/home/codex-cli", rynmesh: { source: "peer", model_alias: "Codex CLI", adapter: "codex_cli", max_output_tokens: 512 } };
const initial: InferenceAccess = { base_url: "http://127.0.0.1:8791/v1", keys: [], models: [target], targets: [target], aliases: {} };

function renderPage(page: "api" | "mapping" | "sharing") {
  const client = makeFixtureNodeClient();
  const access = structuredClone(initial);
  client.getInferenceAccess = vi.fn(async () => structuredClone(access));
  client.setInferenceModelAlias = vi.fn(async (name, id) => { access.aliases[name] = id; return { name, target: id }; });
  client.createInferenceKey = vi.fn(async () => ({ id: "key-1", key: "ryn_secret_for_test", name: "IDE", output_token_limit: 100 }));
  client.getCLIServices = vi.fn(async () => ({ personal_space_required: true, personal_space_ready: true, services: [
    { kind: "codex_cli" as const, title: "Codex CLI", installed: true, configured: true, publication_enabled: false, online: true, api_text_only: true, service_id: "codex-cli" },
  ] }));
  client.setCLISharing = vi.fn(async () => ({ publication_enabled: true }));
  const context: AppOutletContext = {
    client, node: { node_name: "Laptop", peer_id: "peer:laptop", daemon_running: true, registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 60 },
    registry: { status: "connected", url: "" }, peers: [], refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
  render(<MemoryRouter><Routes><Route element={<Outlet context={context} />}><Route index element={page === "api" ? <APIAccessPage /> : page === "mapping" ? <ModelMappingPage /> : <AgentSharingPage />} /></Route></Routes></MemoryRouter>);
  return { client, user: userEvent.setup() };
}

describe("AI workspace companion pages", () => {
  it("creates a project key and shows it only in the current page session", async () => {
    const { client, user } = renderPage("api");
    await screen.findByRole("heading", { name: "本机 API 地址" });
    await user.clear(screen.getByRole("textbox", { name: "项目名称" }));
    await user.type(screen.getByRole("textbox", { name: "项目名称" }), "IDE");
    await user.click(screen.getByRole("button", { name: "创建密钥" }));
    await waitFor(() => expect(client.createInferenceKey).toHaveBeenCalledWith("IDE", 100000));
    expect(screen.getByText("ryn_secret_for_test")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "我已保存，隐藏密钥" }));
    expect(screen.queryByText("ryn_secret_for_test")).not.toBeInTheDocument();
  });

  it("maps a stable name to the selected remote CLI target", async () => {
    const { client, user } = renderPage("mapping");
    await screen.findByRole("heading", { name: "创建或修改映射" });
    await user.type(screen.getByRole("textbox", { name: "对外模型名称" }), "home-codex");
    await user.click(screen.getByRole("button", { name: "保存映射" }));
    await waitFor(() => expect(client.setInferenceModelAlias).toHaveBeenCalledWith("home-codex", target.id));
    expect(screen.getByText("home-codex")).toBeInTheDocument();
  });

  it("turns on sharing for a configured CLI", async () => {
    const { client, user } = renderPage("sharing");
    await screen.findByRole("heading", { name: "Codex CLI" });
    await user.click(screen.getByRole("button", { name: "分享给我的节点" }));
    await waitFor(() => expect(client.setCLISharing).toHaveBeenCalledWith("codex_cli", true));
  });
});

beforeEach(async () => { await i18n.changeLanguage("zh-CN"); });
