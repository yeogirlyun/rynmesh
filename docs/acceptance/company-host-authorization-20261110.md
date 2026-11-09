# Company host authorization channel acceptance — 2026-11-10

This document covers the host half of the company-session protocol. It does not
claim WebView cookie acceptance, packaged-executable acceptance, or a stability
observation duration; those are recorded by the combined release acceptance.

## Protocol and boundaries

- The existing native `local-service/prepare` handshake registers the verified
  company loopback service. Its per-launch token/generation authenticates push,
  startup pull, and the internal account snapshot. A changed port without a
  matching installed binding is rejected. After the last local MCP binding is
  removed, only the previously verified service/port can be restarted.
- A sealed, persistent journal stores monotonic sequence numbers and cumulative
  global/per-plugin revocation watermarks. It survives failed delivery and lost
  ACKs. A new service generation replays the state before serving old cookies.
  Older ACKs cannot acknowledge newer events or a different service generation.
- Only explicit account identity sources (`company-workspace-mcp` and
  `company-dashboard-dashboard`) replace the company identity action. Other
  connection logins update their scope grant without replacing the person.
- Business requests do not call this channel. Management operations persist
  first, attempt bounded local delivery, and return `authorizationDelivery`.
  The owner-only delivery-status endpoint reads the journal without networking.
  A pending ACK is reported as pending, never as confirmed revocation.
- OAuth renewal runs independently of local event delivery. Scope reduction or
  invalidation records revocation; ordinary credential rotation changes only a
  monotonically ordered lease revision. No renewal changes account version.
- Single-company-connection logout cannot borrow a sibling's shared token again,
  cannot clear unrelated grants, and cannot revoke a still-shared upstream token.
  Non-shared remote revocation is best effort in the background after local
  removal, so remote availability does not gate local logout.
- Native service-token rotation preserves an exactly bound OAuth identity.
  Strictly equivalent company package re-import preserves prior enablement;
  changed packages remain disabled and must follow the existing activation flow.

Company-page offline logout is a second durable direction: the service sends
`pendingLogouts` through the authenticated state endpoint. Credential removal,
revocation watermarks, and event deduplication are one sealed host transaction.
The response includes `logoutAcks`; an old account action or a newer explicit
scope grant makes an obsolete logout an acknowledged no-op, so it cannot revoke
a later login. The service applies the returned authorization state before
clearing its local pending denial.

## Browser bootstrap

`POST /company/browser-bootstrap` is owner controlled and rejects origins other
than the desktop virtual origin. It accepts the fixed company plugin ID, checks
current enablement/source authorization and the registered service, and delivers
only a one-use ticket hash to that service. The response contains the clear
random ticket plus the registered origin so the host iframe bridge can check
the receiving origin. Neither a bearer token nor a refresh token crosses the
browser boundary. An outstanding ticket is invalidated by a newer revocation.

The service's browser-session tests cover consuming the ticket once; this host
suite verifies issuing/transport/binding and rejects a source logout racing with
issuance. Affiliate/workbench scopes preserve the original verified-member
mapping; the service still performs member/admin/business permission checks.
A prior scoped revocation cannot be bypassed by bootstrap or lease renewal.

## Isolated tests

`tests/test_company_authorization_channel.py` exercises synthetic identities and
rejects network access except sockets bound by the test itself (Windows asyncio
socketpair). Its 28 tests cover pre-send interruption, applied-before-ACK loss,
restart replay, cumulative/out-of-order state, generation rotation, scoped and
full logout, direct configuration-change recovery, refresh/discovery stability,
launch credential rotation, ticket privacy and concurrent logout, HTTP origin
and secret enforcement, lease expiry reduction/order, independent stalled
remote renewal, non-identity connection login, and exact re-import.

Related regressions include `test_agent_local_services.py`,
`test_mcp_oauth_routes.py`, `test_mcp_oauth_config.py`,
`test_mcp_oauth_session_revision.py`, `test_agent_extensions_store.py`,
`test_agent_extensions_oauth.py`, and `test_mcp_oauth_incident_replay.py`.
Exact combined-run results and the local log path are reported in the release
acceptance record. These tests do not call production business endpoints or
mutate production financial data.

Final host-focused run: **125 passed in 14.39 seconds**, including the four
offline-logout cases and related store/config/route/service regressions. The
final OAuth/incident rerun passed **58 tests in 103.73 seconds**. Logs are
`artifacts/session-stability-20261110/host-offline-logout-final.log` and
`artifacts/session-stability-20261110/host-oauth-final.log`; these local logs are
not committed. Total selected host tests: **183 passed**.
