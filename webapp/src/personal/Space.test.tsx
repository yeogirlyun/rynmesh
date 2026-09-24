import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { setLanguagePreference } from "../i18n";
import { MemoryRouter } from "react-router-dom";
import { fixtureSpace } from "../domain/space";
import { SpaceControls } from "./Space";

const state = vi.hoisted(() => ({
  personal: {} as Record<string, unknown>,
  app: {} as Record<string, unknown>,
}));
vi.mock("./model", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  usePersonal: () => state.personal,
}));
vi.mock("../appContext", () => ({ useAppContext: () => state.app }));
const action = vi.fn();
beforeEach(() => {
  action.mockReset().mockResolvedValue(fixtureSpace());
  state.personal = {
    space: fixtureSpace(),
    spaceAction: action,
    demo: false,
    refresh: vi.fn(),
    devices: [],
  };
  state.app = {
    node: { node_name: "My laptop" },
    client: { spaceBackup: vi.fn() },
  };
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
});
function view() {
  return render(
    <MemoryRouter>
      <SpaceControls />
    </MemoryRouter>,
  );
}

describe("personal space controls", () => {
  it("keeps a pending join visible without offering duplicate create or join forms", async () => {
    state.personal.space = {
      ...fixtureSpace(), space: null, membership: "none", members: [],
      pending: [{ id: "pending-join", action: "join" }],
      last_error: "Could not reach the coordination service. Pending requests will retry.",
    };
    view();
    await act(async () => { setLanguagePreference("zh-CN"); });
    expect(screen.getByRole("alert")).toHaveTextContent("无法连接协调服务");
    expect(screen.getByRole("status")).toHaveTextContent("正在等待协调设备");
    expect(screen.queryByRole("button", { name: "创建空间" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "加入空间" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消等待" })).toBeEnabled();
    expect(action).not.toHaveBeenCalled();
  });
  it("switches invitation and membership labels without changing the invitation", async () => {
    state.personal.space = { ...fixtureSpace(), invitation: "ryn-invite-v1:example" };
    view();
    expect(screen.getByRole("button", { name: "Create invitation" })).toBeInTheDocument();
    await act(async () => { setLanguagePreference("zh-CN"); });
    expect(screen.getByRole("button", { name: "创建邀请" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "邀请有效期" })).toHaveValue("24");
    expect(screen.getByText("协调设备 · 管理设备 · 成员身份已验证")).toBeInTheDocument();
    expect(screen.getByDisplayValue("ryn-invite-v1:example")).toBeInTheDocument();
    expect(screen.queryByText("Add another computer")).not.toBeInTheDocument();
    await act(async () => { setLanguagePreference("en"); });
    expect(screen.getByRole("button", { name: "Copy invitation" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Invitation expires after" })).toHaveValue("24");
  });
  it("joins with only an invitation, using the current device name automatically", async () => {
    state.personal.space = {
      ...fixtureSpace(),
      space: null,
      members: [],
      membership: "none",
      can_manage: false,
    };
    view();
    expect(screen.queryByRole("textbox", { name: "Device name" })).not.toBeInTheDocument();
    expect(screen.getByText("Joining as My laptop. You can rename this device later.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Join space" })).toBeDisabled();
    fireEvent.change(
      screen.getByRole("textbox", { name: "Invitation" }),
      { target: { value: "ryn-invite-v1:example" } },
    );
    fireEvent.click(screen.getByRole("button", { name: "Join space" }));
    await waitFor(() =>
      expect(action).toHaveBeenCalledWith("join", {
        invitation: "ryn-invite-v1:example",
        name: "My laptop",
      }),
    );
    expect(screen.queryByText("Membership verified")).not.toBeInTheDocument();
  });
  it("requires a concrete confirmation before removing a member", async () => {
    view();
    fireEvent.click(
      screen.getAllByRole("button", { name: "Remove" })[0],
    );
    expect(
      screen.getByRole("dialog", { name: "Remove Home PC?" }),
    ).toBeInTheDocument();
    expect(action).not.toHaveBeenCalled();
    fireEvent.click(
      screen.getByRole("button", { name: "Confirm" }),
    );
    await waitFor(() =>
      expect(action).toHaveBeenCalledWith("remove", {
        peer_id: "peer:fixture-llm-provider",
      }),
    );
  });
  it("does not expose management actions on an ordinary member", () => {
    state.personal.space = {
      ...fixtureSpace(),
      can_manage: false,
      coordinator: false,
    };
    view();
    expect(
      screen.queryByRole("button", { name: "Create invitation" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Remove" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "AI access" })).toBeEnabled();
  });
  it("prevents live mutations in preview mode", () => {
    state.personal.demo = true;
    view();
    expect(
      screen.getByRole("button", { name: "Create invitation" }),
    ).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "AI access" })).toBeDisabled();
    expect(action).not.toHaveBeenCalled();
  });
});
