# Plugin page and sign-in stability — 2026-11-09

## Symptoms

1. An installed plugin page was often replaced by "插件服务暂时无响应，正在等待自动恢复…" and came back only after waiting and refreshing.
2. A plugin connection signed in through remote MCP OAuth was intermittently reported as needing sign-in again.

## Causes in Ryn

- `ExtensionWebApp` polls `plugin_service_instance` every 5 s; the Rust probe allows 2 s for the plugin's health response. A single missed probe set an error, which unmounted the plugin frame and showed the error page, retried 10 s later. A busy plugin service (database work, many requests) was treated as a dead one, and the user's page state was lost.
- `request_json` gave OAuth token requests 10 s. When an authorization server needed longer for a renewal, Ryn gave up while the server completed it; with a rotating server, Ryn then re-sent the already-used refresh token, which a strict server treats as theft and revokes.

## Changes

- An open plugin page is never replaced by an error for a slow or restarting service. It stays mounted; after three consecutive missed probes (~15 s) a small status line says the plugin is responding slowly. When the service process exited, the page asks the supervisor to start it again at once and reloads the frame only after a new process is up. The full error page remains only for a page that has not loaded yet.
- OAuth token requests wait up to 30 s.
- The authorization server for the affected connection now keeps one stable device credential (renewals omit `refresh_token`). Ryn already keeps the held credential when a response omits it; this is now covered by a test, including a retried renewal after a failed response.

## Verification (VM, branch `claude/plugin-login-stability-20261109`)

- Vitest `ExtensionWebApp.test.tsx` + `KeptPluginPages.test.tsx`: 18 passed (serial). The previous test that asserted the page is replaced on the first missed probe was replaced by: two misses keep the same frame with no notice; the third shows the notice with the same frame; recovery removes it without a reload; an exited service shows "restarting" and reloads after the new process.
- Pytest `test_agent_extensions_oauth.py`, `test_mcp_oauth_routes.py`, `test_mcp_oauth_session_revision.py`, `test_mcp_oauth_incident_replay.py`, `test_mcp_oauth_config.py`: 141 passed (`-n 2`).
- VM note: with the shared-folder `.venv` site-packages, loopback OAuth tests time out (imports over the share stall the callback thread; an unchanged existing test fails the same way). Copying site-packages to local disk fixes it.

Not verified here: the installed desktop build on a physical machine.
