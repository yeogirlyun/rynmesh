import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom";
import { expect, it, vi } from "vitest";
import ts from "typescript";
import i18n from "./i18n";
import { tr } from "./uiI18n";
import en from "./locales/ui.en.json";
import zh from "./locales/ui.zh-CN.json";
import { makeFixtureNodeClient } from "./domain/fixtureNodeClient";
import type { AppOutletContext } from "./appContext";
import { requestDesktopNotificationPermission } from "./domain/notifications";
import ServicesCatalog from "./screens/ServicesCatalog";
import Explore from "./screens/Explore";
import Settings from "./screens/Settings";

const sources = import.meta.glob<string>(["./**/*.tsx", "./**/*.ts", "!./**/*.test.*", "!./test/**"], {
  query: "?raw", import: "default", eager: true,
});

it("has complete bilingual messages and matching interpolation values", () => {
  expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
  for (const key of Object.keys(en) as (keyof typeof en)[]) {
    expect(zh[key].trim(), key).not.toBe("");
    expect(zh[key].match(/{{\w+}}/g)?.sort(), key).toEqual(en[key].match(/{{\w+}}/g)?.sort());
  }
});

it("keeps application UI copy in language packs and protocol attributes untranslated", () => {
  // Product names, protocol acronyms, commands, paths and example IDs are literal.
  const literals = new Set([
    "ChatGPT", "Ryn", "R", "NAS", "CLI ·", "Ollama", "Token", "ms",
    "https://example.com/v1", "qwen", "20260518__casefile__example-tt-deep-veo",
    "ollama.com/download", "ollama pull gemma3:4b", "ollama pull", "ANTHROPIC_API_KEY",
    "~/.rynmesh/control_token", "net.egress", "https://nas.example.com:5006", "Work/Reports", "ryn-invite-v1:…",
  ]);
  const violations: string[] = [];
  for (const [file, source] of Object.entries(sources)) {
    const ast = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, file.endsWith("tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    function visit(node: ts.Node) {
      const location = `${file}:${ast.getLineAndCharacterOfPosition(node.getStart(ast)).line + 1}`;
      if (ts.isCallExpression(node) && node.expression.getText(ast) === "tr" && node.arguments[0] && ts.isStringLiteral(node.arguments[0])) {
        const key = node.arguments[0].text;
        if (!(key in en) || !(key in zh)) violations.push(`${location} missing translation: ${key}`);
      }
      let copy: string | undefined;
      if (ts.isJsxText(node)) copy = node.text.replace(/\s+/g, " ").trim();
      if (ts.isJsxAttribute(node) && node.initializer) {
        if (["title", "label", "aria-label", "placeholder", "description", "body", "context", "eyebrow", "alt", "caption", "message"].includes(node.name.getText(ast)) && ts.isStringLiteral(node.initializer)) copy = node.initializer.text;
        if (["key", "kind", "id", "status", "risk", "outcome"].includes(node.name.getText(ast)) && /^\{tr\(/.test(node.initializer.getText(ast))) violations.push(`${location} translated protocol attribute`);
      }
      if (copy && /[A-Za-z\p{Script=Han}]/u.test(copy) && !literals.has(copy)) violations.push(`${location} hardcoded copy: ${copy}`);
      ts.forEachChild(node, visit);
    }
    visit(ast);
  }
  expect(violations).toEqual([]);
});

function renderPage(page: React.ReactNode) {
  const client = makeFixtureNodeClient();
  const context: AppOutletContext = {
    client,
    node: { node_name: "My unchanged device", peer_id: "peer:test", daemon_running: true, registry: "connected", peer_count: 0, local_items: 0, fetched_items: 0, pending_recs: 0, version: "test", uptime_seconds: 1 },
    registry: { status: "connected", url: "" }, peers: [],
    refreshShell: vi.fn(async () => undefined), confirm: vi.fn(), notify: vi.fn(),
  };
  render(<MemoryRouter><Routes><Route element={<Outlet context={context} />}><Route index element={page} /></Route></Routes></MemoryRouter>);
  return client;
}

it("updates catalog descriptions, actions, prices and counts immediately in both directions", async () => {
  renderPage(<ServicesCatalog />);
  await screen.findByRole("heading", { name: "Browse services" });
  await act(async () => { await i18n.changeLanguage("zh-CN"); });
  expect(screen.getByRole("heading", { name: tr("Browse services") })).toBeInTheDocument();
  expect(screen.getByText(tr("Talk to an AI model running on a connected device."))).toBeInTheDocument();
  expect(screen.getByText("3 项服务")).toBeInTheDocument();
  expect(screen.queryByText(/credits|No provider online|Workflow|Connection/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "AI" }));
  expect(screen.getByText("1 项服务")).toBeInTheDocument();
  await act(async () => { await i18n.changeLanguage("en"); });
  expect(screen.getByText("1 service")).toBeInTheDocument();
  expect(screen.getByText("Talk to an AI model running on a connected device.")).toBeInTheDocument();
});

it("keeps Chinese content filters as stable API values across language changes", async () => {
  await i18n.changeLanguage("zh-CN");
  const client = renderPage(<Explore />);
  const listContent = vi.spyOn(client, "listContent");
  const rank = await screen.findByRole("combobox", { name: tr("Rank") });
  fireEvent.change(rank, { target: { value: "newest" } });
  await waitFor(() => expect(listContent).toHaveBeenLastCalledWith(expect.objectContaining({ rank: "newest" })));
  const kind = await screen.findByRole("combobox", { name: tr("Kind") });
  fireEvent.change(kind, { target: { value: "document" } });
  await waitFor(() => expect(listContent).toHaveBeenLastCalledWith(expect.objectContaining({ rank: "newest", kind: "document" })));
  await screen.findByRole("combobox", { name: tr("Kind") });
  await act(async () => { await i18n.changeLanguage("en"); });
  expect(screen.getByRole("combobox", { name: "Kind" })).toHaveValue("document");
  expect(screen.getByRole("combobox", { name: "Rank" })).toHaveValue("newest");
});

it("localizes every advanced settings section without changing saved options", async () => {
  await i18n.changeLanguage("zh-CN");
  const client = renderPage(<Settings />);
  const update = vi.spyOn(client, "updateSettings");
  for (const section of ["Identity & storage", "Network", "Trust & safety", "AI curator", "Notifications", "Privacy & data", "Ranking & publish", "Fetch limits", "Software updates"]) {
    expect(await screen.findByRole("button", { name: tr(section) })).toBeInTheDocument();
  }
  fireEvent.click(screen.getByRole("button", { name: "排序与发布" }));
  fireEvent.change(screen.getAllByRole("combobox")[0], { target: { value: "newest" } });
  await waitFor(() => expect(update).toHaveBeenCalledWith({ rank_default: "newest" }));
  await waitFor(() => expect(screen.getAllByRole("combobox")[0]).toHaveValue("newest"));
  await act(async () => { await i18n.changeLanguage("en"); });
  expect(screen.getByRole("heading", { name: "Ranking & publish" })).toBeInTheDocument();
  expect(screen.getAllByRole("combobox")[0]).toHaveValue("newest");
});

it("checks the browser Notification API by its unchanged property name in Chinese", async () => {
  await i18n.changeLanguage("zh-CN");
  vi.stubGlobal("Notification", { permission: "granted" });
  try {
    expect(await requestDesktopNotificationPermission()).toBe("granted");
  } finally { vi.unstubAllGlobals(); }
});
