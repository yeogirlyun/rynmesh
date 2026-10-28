import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop } from "./nodeUrl";
import { reserveMcpAuthBrowser, validateMcpAuthUrl } from "./mcpAuthBrowser";

vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn() }));
vi.mock("./nodeUrl", () => ({ isTauriDesktop: vi.fn(() => false) }));
beforeEach(() => { vi.mocked(isTauriDesktop).mockReturnValue(false); vi.mocked(invoke).mockReset(); });
afterEach(() => { vi.restoreAllMocks(); });

const authorizationUrl = "https://login.example/auth?client_id=desktop&state=synthetic&code_challenge=synthetic&code_challenge_method=S256&scope=read&resource=https%3A%2F%2Fapi.example%2Fmcp&redirect_uri=http%3A%2F%2F127.0.0.1%3A1245%2Fcallback";

describe("MCP authorization browser", () => {
  it("reserves the web popup synchronously and navigates only after validation", async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    const reserved = reserveMcpAuthBrowser();
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(popup.opener).toBeNull();
    expect(popup.location.replace).not.toHaveBeenCalled();
    await reserved.open(authorizationUrl);
    expect(popup.location.replace).toHaveBeenCalledWith(authorizationUrl);
    reserved.cancel(); expect(popup.close).toHaveBeenCalledOnce();
  });

  it("uses the dedicated native command without opening a web popup", async () => {
    vi.mocked(isTauriDesktop).mockReturnValue(true);
    vi.mocked(invoke).mockResolvedValue(undefined);
    const open = vi.spyOn(window, "open");
    const reserved = reserveMcpAuthBrowser();
    await reserved.open(authorizationUrl); reserved.cancel();
    expect(invoke).toHaveBeenCalledWith("open_mcp_auth_browser", { url: authorizationUrl });
    expect(open).not.toHaveBeenCalled();
  });

  it("reports blocked popups before starting login", () => {
    vi.spyOn(window, "open").mockReturnValue(null);
    expect(reserveMcpAuthBrowser).toThrow("browser_open_failed");
  });

  it("does not navigate or invoke for a credential-bearing URL", async () => {
    const popup = { opener: window, location: { replace: vi.fn() }, close: vi.fn() };
    vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window);
    const reserved = reserveMcpAuthBrowser();
    await expect(reserved.open("https://login.example/auth?code=synthetic-private")).rejects.toThrow("unsupported_mcp_auth_url");
    expect(popup.location.replace).not.toHaveBeenCalled(); expect(invoke).not.toHaveBeenCalled();
  });

  it.each([authorizationUrl, "https://login.example:8443/auth", "http://localhost:1245/auth", "http://127.0.0.1:1245/auth", "HTTP://LOCALHOST:1245/auth"])("accepts supported authorization targets: %s", value => {
    expect(validateMcpAuthUrl(value)).toBe(new URL(value).href);
  });

  it.each([
    "file:///C:/Windows/cmd.exe", "javascript:alert(1)", "--help", "//login.example/auth",
    "http://login.example/auth", "http://localhost.evil.example/auth", "http://localhost./auth",
    "http://127.1/auth", "http://2130706433/auth", "http://0177.0.0.1/auth", "http://[::1]/auth",
    "http://127.0.0.1:0/auth", "https://user:synthetic@login.example/auth", "https://@login.example/auth",
    "https://login.example@evil.example/auth", "https://login.example/auth#fragment", "https://login.example/auth#",
    "https://login.example/auth\n--help", "https://login.example/auth\0", "https://login.example/auth --help",
    "https://login.example\\@evil.example/auth", "https:/login.example/auth", "https://[invalid/auth",
  ])("rejects unsafe input with only a fixed error: %s", value => {
    expect(() => validateMcpAuthUrl(value)).toThrow(/^unsupported_mcp_auth_url$/);
  });

  it.each(["access_token", "refresh_token", "id_token", "client_secret", "code", "code_verifier", "token", "secret", "password", "authorization", "CLIENT_SECRET", "%63ode"])("rejects decoded credential query keys: %s", key => {
    expect(() => validateMcpAuthUrl(`https://login.example/auth?${key}=synthetic-private`)).toThrow(/^unsupported_mcp_auth_url$/);
  });

  it("enforces the URL length limit", () => {
    const prefix = "https://login.example/auth?state=";
    const allowed = prefix + "x".repeat(8192 - prefix.length);
    expect(validateMcpAuthUrl(allowed)).toBe(allowed);
    expect(() => validateMcpAuthUrl(allowed + "x")).toThrow(/^unsupported_mcp_auth_url$/);
  });
});
