import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { friendsApi } from "../domain/friendsClient";
import i18n from "../i18n";
import Friends from "./Friends";

vi.mock("../appContext", () => ({ useAppContext: () => ({ confirm: vi.fn() }) }));
vi.mock("qrcode", () => ({ default: { toDataURL: vi.fn().mockResolvedValue("data:image/png;base64,AA==") } }));
vi.mock("./components/FriendAI", () => ({ default: () => null }));

describe("S13-I15 Friends unencrypted local storage warning", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("zh");
    vi.spyOn(friendsApi, "invitationContext").mockResolvedValue({
      endpoint: "http://192.168.1.2:8791",
      address_category: "LAN private",
    });
    vi.spyOn(friendsApi, "continuation").mockResolvedValue({});
    vi.spyOn(friendsApi, "aiJoins").mockResolvedValue({ operations: [] });
    vi.spyOn(friendsApi, "invites").mockResolvedValue({ invites: [] });
    vi.spyOn(friendsApi, "cards").mockResolvedValue({ cards: [] });
    vi.spyOn(friendsApi, "diagnostics").mockResolvedValue({
      from_peer_id: "me",
      scope: "outbound_direct",
      running: false,
      links: [],
    });
  });

  afterEach(async () => {
    await i18n.changeLanguage("en");
    vi.restoreAllMocks();
  });

  it("shows '本机未加密保存' when secret_protection is 'file' and hides it when protected", async () => {
    vi.spyOn(friendsApi, "list").mockResolvedValue({
      friends: [],
      secret_protection: "file",
    });

    const { unmount } = render(
      <MemoryRouter initialEntries={["/friends"]}>
        <Friends />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/本机未加密保存/)).toBeInTheDocument();
    unmount();

    vi.spyOn(friendsApi, "list").mockResolvedValue({
      friends: [],
      secret_protection: "dpapi",
    });
    render(
      <MemoryRouter initialEntries={["/friends"]}>
        <Friends />
      </MemoryRouter>,
    );
    await screen.findByRole("heading", { level: 1 });
    expect(screen.queryByText(/本机未加密保存/)).not.toBeInTheDocument();
  });
});
