import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PublicPlugins from "./PublicPlugins";
import { publicPlugins, type PublicCatalog } from "../domain/publicPlugins";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ i18n: { language: "zh-CN" } }) }));
vi.mock("../domain/publicPlugins", () => ({ publicPlugins: { overview: vi.fn(), refresh: vi.fn(), action: vi.fn() } }));
const initial: PublicCatalog = { plugins: [{ id: "image-studio", name: "做图", description: "在本机生成图片", version: "1.0.0", notes: "独立插件", source: "https://github.com/yeogirlyun/rynmesh", permissions: [], installed_version: null, enabled: false, running: false, update_available: false, rollback_available: false, error: "" }], checked_at: null, error: "", source: "public", legacy_data: false };
const show = (view: "all" | "installed" | "discover" = "all") => render(<MemoryRouter><PublicPlugins view={view} /></MemoryRouter>);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(publicPlugins.overview).mockResolvedValue(structuredClone(initial));
  vi.mocked(publicPlugins.refresh).mockResolvedValue({ ...structuredClone(initial), checked_at: 1700000000 });
  vi.mocked(publicPlugins.action).mockResolvedValue(structuredClone(initial));
});
describe("Public plugin discovery and updates", () => {
  it("reads cache without downloading and explains private installation", async () => {
    show();
    expect(await screen.findByRole("heading", { name: /做图/ })).toBeInTheDocument();
    expect(publicPlugins.refresh).not.toHaveBeenCalled();
    expect(publicPlugins.action).not.toHaveBeenCalled();
    expect(screen.getByText(/Gshopper 等私有插件通过私有安装包导入/)).toBeInTheDocument();
  });
  it("refresh discovers releases without installing", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "刷新目录 / 检查更新" }));
    await waitFor(() => expect(publicPlugins.refresh).toHaveBeenCalledTimes(1));
    expect(publicPlugins.action).not.toHaveBeenCalled();
  });
  it("confirms executable plugin installation", async () => {
    show("discover");
    fireEvent.click(await screen.findByRole("button", { name: "安装" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("运行发布者的代码");
    expect(publicPlugins.action).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    await waitFor(() => expect(publicPlugins.action).toHaveBeenCalledWith("image-studio", "install"));
  });
  it("shows installed and latest versions, updates and rollback", async () => {
    const data = structuredClone(initial);
    Object.assign(data.plugins[0], { installed_version: "0.9.0", running: true, enabled: true, update_available: true, rollback_available: true });
    vi.mocked(publicPlugins.overview).mockResolvedValue(data);
    show("installed");
    expect(await screen.findByText(/已安装 0.9.0/)).toHaveTextContent("最新版本 1.0.0");
    fireEvent.click(screen.getByRole("button", { name: "更新" }));
    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    await waitFor(() => expect(publicPlugins.action).toHaveBeenCalledWith("image-studio", "update"));
  });
  it("preserves visible catalog when refresh fails", async () => {
    vi.mocked(publicPlugins.refresh).mockRejectedValue(new Error("plugin_signature_invalid"));
    show();
    await screen.findByRole("heading", { name: /做图/ });
    fireEvent.click(screen.getByRole("button", { name: "刷新目录 / 检查更新" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("发布签名校验失败");
    expect(screen.getByRole("button", { name: "安装" })).toBeInTheDocument();
  });
});
