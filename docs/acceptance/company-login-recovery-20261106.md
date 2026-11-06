# Company login recovery — automated verification, 2026-11-06

Company OAuth requests now carry an issuer-scoped stable installation hint stored in the encrypted OAuth store. Relogin/config changes preserve it. Device quota failures remain distinguishable, and plugin startup shows a visible loading state.

Validation: 45 OAuth tests passed; 15 plugin frame/kept-page tests passed. Real QR scan and installed application verification are still pending; these automated checks do not establish end-to-end login acceptance.

## Reimport follow-up

An imported HTTP connection retains its instance and authorization generation only when its owner, identity group and complete transport/auth configuration match. Plugin and execution revisions still advance. OAuth ignores plugin UI revisions and temporary disablement when retaining credentials, while disabled use and stale executions remain blocked. A refresh completed during an unchanged reimport is saved without revoking the rotated credential. Changed import connections revoke their previous grants.

Validation: all 50 OAuth tests and all 7 extension-store tests passed; the 5 reimport cases passed again after adding disable/re-enable coverage. The earlier installed build (5fc7312 / efc3a8f) completed a real user-confirmed phone scan: workbench signed in, Dashboard rendered journal rows, Affiliate rendered a real WeCom session and order statistics. Reimport acceptance on the follow-up installed build is pending.

## Tool discovery found by installed acceptance

The c88b78e installed build retained the OAuth session across an actual publisher-package reimport, but cleared the discovered tool catalog. Dashboard identity lookup then failed with `agent_mcp_tool_disabled`. Login previously triggered discovery, so retaining login exposed this missing enable-time step. Enabling an authenticated HTTP plugin now rediscovers an empty `NeedsTest` catalog; disabled and signed-out endpoints are skipped. No previous catalog or stale executable binding is reused.

Validation: 173 OAuth, local-route and plugin-view tests passed, including a real loopback OAuth login followed by reimport, enable through the HTTP route, catalog discovery and a tool call using the unchanged credential. Manual discovery on the installed c88b78e build restored the identity page without another scan; automatic installed verification remains pending.

## Installed-service restart follow-up

The 054ba81 installed build passed a real Dashboard reimport (stable authorization/configuration, new plugin revision, automatic discovery of 58 tools). A second user-confirmed phone scan replaced the identified installation grant, remaining at 1/5; all three actual business pages rendered. Stability subsequently failed: the company service hung/restarted and WebView2 retained a chrome-error://chromewebdata document. Its load event does not imply a successful iframe navigation.

Managed plugin frames now observe the owned service health and process identity every five seconds. A service replacement remounts the frame even if downtime occurred between polls; an outage displays the existing recovery message and retries. Healthy frames retain their DOM and page state. The status command checks only the installer-owned process and does not refresh the tool catalog, adopt a foreign listener, or restart processes.

Validation: 17 plugin-frame/kept-page tests passed, including late outage recovery and replacement after an error-document load without an error event; 5 native supervisor tests passed (2 packaged-fixture tests remain explicitly ignored in this unit run). Companion company commit 8d4569b isolates Windows handle enumeration in a bounded child process. Full-package and installed stability acceptance remain pending.
