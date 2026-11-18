import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import PublicPlugins, { usePublicCatalog } from "./PublicPlugins";
import { publicPlugins, type PublicCatalog } from "../domain/publicPlugins";

vi.mock("react-i18next", () => ({ useTranslation: () => ({ i18n: { language: "zh-CN" } }) }));
vi.mock("../domain/publicPlugins", () => ({ publicPlugins: { overview: vi.fn(), refresh: vi.fn(), action: vi.fn() } }));
const initial: PublicCatalog = { plugins: [{ id: "image-studio", name: "做图", description: "在本机生成图片", version: "1.0.0", notes: "独立插件", source: "https://github.com/yeogirlyun/rynmesh", permissions: [], installed_version: null, enabled: false, running: false, update_available: false, rollback_available: false, error: "" }], checked_at: null, error: "", source: "public", legacy_data: false };
function Catalog() {
  return <PublicPlugins catalog={usePublicCatalog()} />;
}
const show = () => render(<MemoryRouter><Catalog /></MemoryRouter>);
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(publicPlugins.overview).mockResolvedValue(structuredClone(initial));
  vi.mocked(publicPlugins.refresh).mockResolvedValue({ ...structuredClone(initial), checked_at: 1700000000 });
  vi.mocked(publicPlugins.action).mockResolvedValue(structuredClone(initial));
});
describe("Public plugin discovery and updates", () => {
  it("reads the signed public catalog cache without downloading", async () => {
    show();
    expect(await screen.findByRole("heading", { name: /做图/ })).toBeInTheDocument();
    expect(publicPlugins.refresh).not.toHaveBeenCalled();
    expect(publicPlugins.action).not.toHaveBeenCalled();
    expect(screen.getByText(/安装前校验签名/)).toBeInTheDocument();
  });
  it("refresh discovers releases without installing", async () => {
    show();
    await screen.findByRole("heading", { name: "做图" });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    await waitFor(() => expect(publicPlugins.refresh).toHaveBeenCalledTimes(1));
    expect(publicPlugins.action).not.toHaveBeenCalled();
    expect(await screen.findByText(/上次检查/)).toBeInTheDocument();
  });
  it("confirms executable plugin installation", async () => {
    show();
    fireEvent.click(await screen.findByRole("button", { name: "安装到本机" }));
    expect(screen.getByRole("dialog")).toHaveTextContent("运行发布者的代码");
    expect(publicPlugins.action).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    await waitFor(() => expect(publicPlugins.action).toHaveBeenCalledWith("image-studio", "install"));
  });
  it("shows installed and latest versions and moves maintenance actions to details", async () => {
    const data = structuredClone(initial);
    Object.assign(data.plugins[0], { installed_version: "0.9.0", running: true, enabled: true, update_available: true, rollback_available: true });
    vi.mocked(publicPlugins.overview).mockResolvedValue(data);
    show();
    expect(await screen.findByText(/已安装 0.9.0/)).toHaveTextContent("最新版本 1.0.0");
    expect(screen.getByRole("link", { name: "打开" })).toHaveAttribute("href", "/apps/app/public/image-studio");
    expect(screen.queryByRole("button", { name: /卸载|停用|回退/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "更新到 1.0.0" }));
    fireEvent.click(screen.getByRole("button", { name: "确认" }));
    await waitFor(() => expect(publicPlugins.action).toHaveBeenCalledWith("image-studio", "update"));
  });
  it("shows an open link for an installed app with no update", async () => {
    const data = structuredClone(initial);
    Object.assign(data.plugins[0], { installed_version: "1.0.0", enabled: false, running: false });
    vi.mocked(publicPlugins.overview).mockResolvedValue(data);
    show();
    expect(await screen.findByText("已安装 1.0.0 · 已是最新")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "打开" })).toHaveAttribute("href", "/apps/app/public/image-studio");
    expect(screen.queryByRole("button", { name: /安装到本机|更新到/ })).not.toBeInTheDocument();
  });
  it("preserves visible catalog when refresh fails", async () => {
    vi.mocked(publicPlugins.refresh).mockRejectedValue(new Error("plugin_signature_invalid"));
    show();
    await screen.findByRole("heading", { name: /做图/ });
    fireEvent.click(screen.getByRole("button", { name: "刷新" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("发布签名校验失败");
    expect(screen.getByRole("button", { name: "安装到本机" })).toBeInTheDocument();
  });
});
