# Node sites

A node site is a loopback web application on one node that selected peers open
inside their own Ryn, like a website whose address is that node. No public
port, domain or tunnel is involved. Version 1 uses the company (peer provider)
channel for identity and authorization. Design notes and measurements:
[design/node-sites-image-studio-v1.md](design/node-sites-image-studio-v1.md).

## Hosting a site

Register a loopback backend with the owner-only API. A new site starts closed.

```
POST /api/local/node-sites/mine   {"site_id": "image-studio", "title": "Image studio", "port": 18150, "entry": "/"}
-> {"site": {...}, "token": "<shown once>"}
PATCH /api/local/node-sites/mine/image-studio   {"enabled": true, "audience": {"mode": "all", "members": []}}
```

Ryn can also host built-in sites in-process (no port or token, same checks
and exposure switch); the first is the [image studio](IMAGE_STUDIO.md).

`audience.mode` is `all` (every member of the provider directory) or
`members` (listed member ids). Re-registering a site rotates its token and
keeps its exposure.

Every forwarded request carries headers the backend may trust only after it
checks `X-Ryn-Site-Token` against its token:

| Header | Value |
| --- | --- |
| `X-Ryn-Site-Token` | the registration token |
| `X-Ryn-Caller-Peer` | caller device id (base64 Ed25519 key) |
| `X-Ryn-Caller-Member` | caller member id, percent-encoded |
| `X-Ryn-Caller-Name` | caller member name, percent-encoded UTF-8 |
| `X-Ryn-Caller-Resource` | provider resource, percent-encoded |

Without the token check, any local program could call the backend with a forged
identity. Cookies and identity headers sent by the caller are dropped; only
`accept`, `accept-language`, `cache-control`, `content-type`, `if-*` and
`range` cross. Responses keep `content-type`, caching, `content-disposition`,
range and `location` headers; `Set-Cookie` is dropped and a redirect is kept
only when it stays on the site.

Checks on the hosting node, in order: signed envelope addressed to this node
(10-minute window), replay (nonce), directory membership with the bound
messaging key, the owner's switch and audience, at most 4 concurrent and 300
requests per minute per caller, and the provider's live `site.open`
authorization for this site id (cached 60 seconds). Unknown, closed and
not-allowed sites all answer the same 403.

## Opening a site

`GET /api/local/node-sites/remote` lists provider devices with their open sites
(offline devices are listed as offline). `POST /api/local/node-sites/open` with
`provider_resource`, `peer_id` and `site_id` returns a stable key and the URL
`http://<key>.site.localhost:<gateway-port><entry>`. The Ryn page
`/apps/sites` lists sites; `/apps/sites/open/<key>` shows one in a sandboxed
frame (`allow-scripts allow-forms allow-same-origin allow-downloads`, no
referrer) under a bar Ryn draws with the owner and online state, or an offline
screen. Status is checked every 15 seconds and the page reloads when the device
comes back. Offline means offline: nothing is queued.

The gateway listens on 127.0.0.1 on its own port (`RYNMESH_SITE_GATEWAY_PORT`,
else the last used one, else a free one), separate from the control API.
`<key>` is 128 random bits per (provider, device, site); a request whose Host
is not exactly a registered `<key>.site.localhost:<port>` gets 404. Each site
has its own origin, so storage and cookies are not shared between sites or
with Ryn. Every response gets this Content-Security-Policy: same-origin
scripts, styles (inline allowed), images, media, fonts and connections only,
no plugins, `frame-ancestors` limited to the Ryn desktop origins. Inline
scripts are blocked, so a site must ship its scripts as files.

## Transport and limits

Requests and responses are JSON envelopes signed by the device keys and sealed
to the two messaging keys, sent over the device route (LAN first, then direct
HTTP, then P2P). P2P links accept `/api/peer/site/*` alongside
`/api/peer/drop/*`. Request bodies up to 25 MiB, responses up to 32 MiB, 30
seconds per request. Methods: GET, HEAD, POST, PUT, PATCH, DELETE. No
WebSockets or streaming; long work should be submitted and then polled.

## Not done in version 1

The provider service on the company center must accept `site.open`; until
it does, every open is denied. There is no plugin manifest declaration yet:
sites are registered through the owner API. No friend or public audience.
macOS WKWebView resolution of `*.localhost` subdomains is unverified.
