import { invoke } from "@tauri-apps/api/core";
import { isTauriDesktop } from "./nodeUrl";

export function validateMcpAuthUrl(value: string): string {
  if (value.length > 8192 || /[\u0000-\u0020\u007f\\]/.test(value)) throw new Error("unsupported_mcp_auth_url");
  const authority = value.match(/^[a-z][a-z0-9+.-]*:\/\/([^/?#]*)/i)?.[1];
  if (!authority || authority.includes("@")) throw new Error("unsupported_mcp_auth_url");
  let url: URL;
  try { url = new URL(value); } catch { throw new Error("unsupported_mcp_auth_url"); }
  if (url.username || url.password || value.includes("#") || url.port === "0" ||
      (url.protocol !== "https:" && !(url.protocol === "http:" && /^(127\.0\.0\.1|localhost)(:\d{1,5})?$/i.test(authority)))) {
    throw new Error("unsupported_mcp_auth_url");
  }
  for (const key of url.searchParams.keys()) {
    if (/^(access_token|refresh_token|id_token|client_secret|code|code_verifier|token|secret|password|authorization)$/i.test(key)) throw new Error("unsupported_mcp_auth_url");
  }
  return url.href;
}

/** Call directly in the owner's click handler, before starting the HTTP request. */
export function reserveMcpAuthBrowser() {
  const desktop = isTauriDesktop();
  const popup = desktop ? null : window.open("about:blank", "_blank");
  if (!desktop && !popup) throw new Error("browser_open_failed");
  if (popup) popup.opener = null;
  return {
    async open(value: string) {
      const url = validateMcpAuthUrl(value);
      if (desktop) await invoke("open_mcp_auth_browser", { url });
      else popup!.location.replace(url);
    },
    cancel() { popup?.close(); },
  };
}
