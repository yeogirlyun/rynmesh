import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import i18n, { setLanguagePreference } from "../i18n";
import en from "../locales/personal.en.json";
import zh from "../locales/personal.zh-CN.json";
import { PersonalHome, PersonalServices, PersonalDevices } from "./Pages";
import { Status } from "./components";
import { personalLabelKeys } from "./labels";

const state = vi.hoisted(() => ({ personal: {} as Record<string, unknown> }));
vi.mock("./model", async original => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => ({ notify: vi.fn(), node: { node_name: "子陵台式" } }) }));

beforeEach(() => {
  const devices = [
    { id: "self", name: "子陵台式", note: "My private note", kind: "desktop", own: true, self: true, online: true,
      network: { label: "Current network", scope: "local", route: "local" } },
    { id: "remote", name: "Office PC", note: "", kind: "desktop", own: false, online: true },
  ];
  state.personal = {
    devices, services: devices.map(device => ({ id: device.id, title: device.own ? "ChatGPT" : "Remote AI",
      deviceId: device.id, kind: "ai", online: true, description: "", href: "/chat" })),
    loading: false, error: "", demo: false, refresh: vi.fn(),
    resolveName: (id: string) => devices.find(device => device.id === id)?.name,
  };
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
});

it("keeps both language packs complete with matching interpolation variables", () => {
  expect(Object.keys(zh).sort()).toEqual(Object.keys(en).sort());
  for (const key of Object.keys(en) as (keyof typeof en)[]) {
    expect(zh[key].trim(), key).not.toBe("");
    expect(zh[key].match(/{{\w+}}/g)?.sort(), key).toEqual(en[key].match(/{{\w+}}/g)?.sort());
  }
  for (const key of Object.values(personalLabelKeys)) {
    expect(en).toHaveProperty(key);
    expect(zh).toHaveProperty(key);
  }
});

it("translates the screenshot's home content immediately while preserving names and notes", async () => {
  render(<MemoryRouter><PersonalHome /></MemoryRouter>);
  expect(screen.getByRole("button", { name: "Add device" })).toBeInTheDocument();
  await act(async () => { setLanguagePreference("zh-CN"); });
  expect(screen.getByRole("button", { name: "添加设备" })).toBeInTheDocument();
  expect(screen.getByText("当前网络")).toBeInTheDocument();
  expect(screen.getByText("你在这里 · 1 台设备")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "子陵台式 上的服务" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "开始对话" })).toBeInTheDocument();
  expect(screen.getByText("My private note")).toBeInTheDocument();
  expect(screen.getByText("ChatGPT")).toBeInTheDocument();
  expect(screen.queryByText(/My device|Open chat|Current network|From other devices/)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "编辑 子陵台式" }));
  expect(screen.getByRole("dialog", { name: "编辑设备" })).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "保存更改" })).toBeInTheDocument();
  await act(async () => { setLanguagePreference("en"); });
  expect(screen.getByRole("button", { name: "Save changes" })).toBeInTheDocument();
  expect(screen.getByText("You are here · 1 device")).toBeInTheDocument();
});

it("keeps service filters and detail tabs selected when switching languages", async () => {
  await i18n.changeLanguage("zh-CN");
  render(<MemoryRouter><PersonalServices /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "发现的设备" }));
  expect(screen.queryByRole("heading", { name: "ChatGPT" })).not.toBeInTheDocument();
  fireEvent.click(screen.getAllByRole("button", { name: "API 接入" })[0]);
  expect(screen.getByRole("link", { name: "配置 API 接入" })).toBeInTheDocument();
  await act(async () => { setLanguagePreference("en"); });
  expect(screen.getByRole("button", { name: "Discovered" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("button", { name: "API access" })).toHaveAttribute("aria-pressed", "true");
  expect(screen.getByRole("link", { name: "Configure API access" })).toBeInTheDocument();
});

it("keeps device filters and status styles independent of translated labels", async () => {
  await i18n.changeLanguage("zh-CN");
  const { container } = render(<MemoryRouter><PersonalDevices /><Status online={false} label="Failed" /></MemoryRouter>);
  fireEvent.click(screen.getByRole("button", { name: "发现的设备" }));
  expect(screen.getByRole("heading", { name: "Office PC" })).toBeInTheDocument();
  expect(container.querySelector(".pf-status.error")).toHaveTextContent("失败");
  await act(async () => { setLanguagePreference("en"); });
  expect(screen.getByRole("button", { name: "Discovered" })).toHaveAttribute("aria-pressed", "true");
  expect(container.querySelector(".pf-status.error")).toHaveTextContent("Failed");
});
