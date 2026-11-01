# Plugin peer providers

A portable plugin can supply an authenticated directory of people, devices and shared models. This is an independent permission source: it does not join a personal space, create friendships or grant device control.

```json
{
  "ryn": {
    "peerProvider": {
      "url": "https://provider.example/connect",
      "oauth": {"clientId": "desktop-client", "scopes": ["peer.connect"]}
    }
  }
}
```

The resource and its OAuth server must use HTTPS on the same origin. No token, client secret or private key belongs in a plugin. The host discovers OAuth metadata, uses S256 PKCE and a one-use loopback callback, and encrypts credentials with the local node key. Installing a plugin does not sign in or publish a model. Enabling it and selecting **Sign in** opens the system browser; the plugin details also show a browser link and connection status.

After sign-in, the host proves possession of its Ed25519 identity and binds its X25519 messaging key to the current login. The provider controls membership, devices and authorization. The host refreshes the directory and verifies device reachability with a signed challenge; a directory entry alone is not an online claim.

In plugin settings, choose **Choose local models to publish**, select the models, then save. The first version publishes ready API/native model services with exact model bindings. CLI agent containers are excluded. Personal sharing settings are separate. Consumers select the resulting provider model in the existing model picker. The provider node checks both its explicit local selection and current remote authorization before dispatch and before returning a result; active streams recheck at least once per second. Model changes require a new publication selection. Previously delivered stream fragments cannot be recalled.

Provider devices also appear in Device Drop. Open a device conversation to send text. Messages use the existing signed encrypted transport, with live authorization on both devices and a source-specific conversation key. Failed deliveries require an explicit retry. Files, synchronization, remote tools and device control are not granted. No new mailbox, group chat or relay is included.

Disabling, uninstalling or replacing the plugin invalidates its local credentials, selections and cached directory. Sign-out also attempts remote token revocation; its response reports whether that revocation was confirmed. The provider independently rejects disabled members and revoked device sessions, including requests using old endpoint information.

## Host control API

All routes require the existing local-owner protection and return fixed errors without upstream error bodies. The prefix is `/api/local/peer-providers`:

| Method and path | Purpose |
| --- | --- |
| `GET /` | Installed provider states |
| `GET /{plugin_id}/status` | Sign-in, binding and selected model IDs |
| `POST /{plugin_id}/login/start` | Start browser sign-in |
| `POST /{plugin_id}/bind` | Re-prove this node and update endpoints |
| `GET /{plugin_id}/catalog` or `POST /{plugin_id}/refresh` | Refresh directory |
| `GET /{plugin_id}/models` | Eligible local model choices |
| `POST /{plugin_id}/publish` | Set explicit `service_ids` |
| `POST /{plugin_id}/logout` | Clear local access and attempt revocation |

## Provider protocol

The resource implements `POST /challenge`, `POST /bind`, `GET /directory`, `PUT /models` and `POST /authorize`. Protected-resource metadata binds the exact resource to its issuer. A binding challenge is a short-lived JSON UTF-8 statement with version `ryn.peer-bind.v1`, issuer, resource, grant/member/challenge IDs, nonce, expiry and the exact node fields. The host checks those fields and signs the original bytes. Successful challenges are one-use on the provider.

Directories use `ryn.peer-directory.v1` and contain only members, verified device keys/endpoints and public model metadata. Endpoints are HTTP(S) origins without trailing slashes; credentials, paths, query strings and local metadata/loopback addresses are rejected. The host never forwards OAuth credentials to a peer endpoint. `authorize` supports only `model.invoke` (including `service_id`) and `message.text`; it is an online decision for the current caller, not a transferable credential.

The data path reuses HTTP or existing P2P/ICE. The existing global network-key perimeter is retained when configured; provider HTTP requests do not carry that private mesh key. Reachability still depends on listener/firewall/NAT configuration. Separate MCP resources keep their own authorization and policies; this provider interface does not automatically implement or grant MCP access.

## Local verification

Run the focused provider/import/model/Drop tests with the repository Python environment and a fresh temporary test directory. The tests use synthetic identities and explicitly injected authorization services, with outbound network denied and owned loopback ports permitted. HTTP and ICE/QUIC tests verify two-node text delivery; model tests exercise real signed envelopes, the existing inference dispatcher, source separation, revocation, streaming, cancellation and transport credential isolation. These tests do not verify a real external identity provider, deployment, or public internet reachability.

### Verification record — 2026-10-22

The final focused runs passed: 51 OAuth/provider and lifecycle checks, 31 import checks, 94 inference/Drop/legacy binding checks, and 51 UI checks. Three optional native Rust transport cases were skipped; the Python ICE/QUIC text path was exercised. The web application build also passed. These are scoped checks, not a claim that the entire repository test suite ran.

From the repository root (use a fresh `--basetemp` directory):

```powershell
.venv/Scripts/python.exe -m pytest tests/test_peer_providers.py tests/test_peer_provider_security_review.py -q --basetemp=.codex-tmp/provider-core
.venv/Scripts/python.exe -m pytest tests/test_peer_provider_import.py -q --basetemp=.codex-tmp/provider-import
.venv/Scripts/python.exe -m pytest tests/test_external_model_access.py tests/test_external_model_review.py tests/test_agent_peer_binding.py tests/test_llm_service_view.py tests/test_llm_streaming.py tests/test_drop_providers.py tests/test_drop_engine.py tests/test_drop_p2p.py -q --basetemp=.codex-tmp/provider-data-path
```

From `webapp/`:

```powershell
node node_modules/vitest/vitest.mjs run src/screens/PeerProviderSettings.test.tsx src/screens/AgentExtensions.test.tsx src/personal/drop/Drop.test.tsx
npm run build
```

Independent review reproduced and fixed two invalidation races: an unobserved disable/enable cycle now invalidates credentials using the installation revision, and an old publication response cannot restore a newer cancelled selection. The regression tests preserve both reproductions. Shared working-tree edits were retained; this work does not include deployment or a packaged desktop installation test.
