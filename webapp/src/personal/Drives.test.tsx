import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { DrivesPage } from "./Drives";
import { clearNasMemory, type NasStatus } from "../domain/nas";
import i18n from "../i18n";
import { openDriveBrowser } from "../domain/driveBrowser";
import { isTauriDesktop } from "../domain/nodeUrl";

vi.mock("../domain/driveBrowser", () => ({ openDriveBrowser: vi.fn() }));
vi.mock("../domain/nodeUrl", async importOriginal => ({ ...await importOriginal<typeof import("../domain/nodeUrl")>(), isTauriDesktop: vi.fn(() => false) }));

vi.mock("./model", () => ({ usePersonal: () => ({ demo: false }) }));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), confirm: vi.fn() }) }));
let state: NasStatus;
beforeEach(() => {
  clearNasMemory();
  vi.mocked(openDriveBrowser).mockResolvedValue(undefined);
  vi.mocked(isTauriDesktop).mockReturnValue(false);
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  state = { enabled: true, sources: [], systems: [], max_file_bytes: 67108864, cloud_providers: [
    { id: "baidu", configured: true, client_id: "existing-app" },
    { id: "google", configured: false, client_id: "" },
    { id: "onedrive", configured: false, client_id: "" },
  ] };
  server.use(http.put("*/api/local/plugins/nas/browser-location", () => HttpResponse.json({ ok: true })), http.get("*/api/local/plugins/nas", () => HttpResponse.json(state)));
});
function view() { return render(<MemoryRouter><DrivesPage /></MemoryRouter>); }

it("configures OneDrive without a secret and displays Microsoft's device code", async () => {
  const saved: unknown[] = [];
  server.use(
    http.put("*/api/local/plugins/nas/cloud/apps/onedrive", async ({ request }) => { saved.push(await request.json()); return HttpResponse.json({ configured: true }); }),
    http.post("*/api/local/plugins/nas/cloud/auth/onedrive", async ({ request }) => { saved.push(await request.json()); return HttpResponse.json({ id: "ms-auth", url: "https://microsoft.com/devicelogin", user_code: "MICROSOFT" }); }),
    http.delete("*/api/local/plugins/nas/cloud/auth-status/ms-auth", () => HttpResponse.json({ ok: true })),
  );
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "Add drive" }))[0]);
  fireEvent.click(screen.getByRole("button", { name: "OneDrive" }));
  expect(screen.queryByLabelText(/Client Secret/)).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open developer console" })).toHaveAttribute("href", "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade");
  fireEvent.change(screen.getByLabelText("Application (client) ID"), { target: { value: "ms-client-id" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and authorize" }));
  expect(await screen.findByText("MICROSOFT")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Open official authorization page" })).toHaveAttribute("href", "https://microsoft.com/devicelogin");
  expect(saved).toEqual([{ client_id: "ms-client-id", client_secret: "" }, { name: "OneDrive", writable: false, allow_ai: false, language: "en" }]);
});

it("switches the open configuration form and instructions to Chinese", async () => {
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "Add drive" }))[0]);
  fireEvent.click(screen.getByRole("button", { name: "Google Drive" }));
  await i18n.changeLanguage("zh-CN");
  expect(await screen.findByLabelText("客户端 ID（Client ID）")).toBeInTheDocument();
  expect(screen.getByLabelText("客户端密钥（Client Secret）")).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存并开始授权" })).toBeInTheDocument();
  expect(screen.getByText("应用配置 · 操作步骤")).toBeInTheDocument();
});

it("saves Google app locally and exposes real authorization without a fake connected card", async () => {
  const saved: unknown[] = [];
  server.use(
    http.put("*/api/local/plugins/nas/cloud/apps/google", async ({ request }) => { saved.push(await request.json()); return HttpResponse.json({ configured: true }); }),
    http.post("*/api/local/plugins/nas/cloud/auth/google", async ({ request }) => { saved.push(await request.json()); return HttpResponse.json({ id: "auth", url: "https://accounts.google.com/o/oauth2/v2/auth?state=test" }); }),
    http.delete("*/api/local/plugins/nas/cloud/auth-status/auth", () => HttpResponse.json({ ok: true })),
  );
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "Add drive" }))[0]);
  expect(screen.getByRole("button", { name: "OneDrive" })).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Google Drive" }));
  fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "desktop-id" } });
  fireEvent.change(screen.getByLabelText("Client Secret"), { target: { value: "local-secret" } });
  fireEvent.click(screen.getByRole("button", { name: "Save and authorize" }));
  expect(await screen.findByRole("link", { name: "Open official authorization page" })).toHaveAttribute("href", "https://accounts.google.com/o/oauth2/v2/auth?state=test");
  expect(saved).toEqual([{ client_id: "desktop-id", client_secret: "local-secret" }, { name: "Google Drive", writable: false, allow_ai: false, language: "en" }]);
  expect(screen.queryByText("local-secret")).not.toBeInTheDocument();
  expect(screen.getByRole("status")).toHaveTextContent("Waiting for authorization");
});

it("uses saved Baidu configuration and cancels its pending device authorization on close", async () => {
  vi.mocked(isTauriDesktop).mockReturnValue(true);
  let cancelled = false;
  server.use(
    http.post("*/api/local/plugins/nas/cloud/auth/baidu", () => HttpResponse.json({ id: "baidu-auth", url: "https://openapi.baidu.com/device", user_code: "ABCD" })),
    http.delete("*/api/local/plugins/nas/cloud/auth-status/baidu-auth", () => { cancelled = true; return HttpResponse.json({ ok: true }); }),
  );
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "Add drive" }))[0]);
  expect(screen.queryByLabelText("Secret Key")).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Save and authorize" }));
  expect(await screen.findByText("ABCD")).toBeInTheDocument();
  await waitFor(() => expect(openDriveBrowser).toHaveBeenCalledWith("https://openapi.baidu.com/device"));
  fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
  await waitFor(() => expect(cancelled).toBe(true));
});

it("opens the console and shows next steps, with a copyable URL on failure", async () => {
  vi.mocked(openDriveBrowser).mockRejectedValueOnce(new Error("no handler"));
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "Add drive" }))[0]);
  fireEvent.click(screen.getByRole("button", { name: "Google Drive" }));
  fireEvent.click(screen.getByRole("link", { name: "Open developer console" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not open the browser");
  expect(screen.getByLabelText("Developer console URL (click to select and copy)")).toHaveValue("https://console.cloud.google.com/apis/library/drive.googleapis.com");
  fireEvent.click(screen.getByRole("link", { name: "Open developer console" }));
  expect(await screen.findByRole("status")).toHaveTextContent("Follow the steps above");
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

it("keeps a failed login retryable and does not invent files", async () => {
  server.use(http.post("*/api/local/plugins/nas/cloud/auth/baidu", () => HttpResponse.json({ detail: "drive_authorization_failed" }, { status: 400 })));
  view();
  fireEvent.click((await screen.findAllByRole("button", { name: "Add drive" }))[0]);
  fireEvent.click(screen.getByRole("button", { name: "Save and authorize" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Authorization failed");
  expect(screen.getByRole("button", { name: "Save and authorize" })).toBeEnabled();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});
