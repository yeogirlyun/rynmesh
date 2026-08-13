# Reaching your node from anywhere

Your node's control API is private. On the machine itself nothing asks you for
anything — the node trusts a local request. From anywhere else it needs proof.

## The rule

A request is treated as **local** only when it arrives on the loopback
interface **and** carries no proxy headers (`x-forwarded-for`,
`cf-connecting-ip`, `x-real-ip`, `forwarded`, …).

That second half matters more than it looks. A tunnel daemon runs on your
machine and connects to the node from `127.0.0.1`, so every request from the
internet arrives looking local. Without the header check, putting a tunnel in
front of your node would hand node control — your content, settings, private
messages and publishing key — to anyone who found the hostname.

## Pairing a device

1. On the node machine, open **Settings → Trust & safety → Device token** and
   copy the token. (It also lives at `~/.rynmesh/control_token`, mode `0600`.)
2. Open your node's remote URL. It will ask you to unlock.
3. Paste the token once. A session cookie (HttpOnly, SameSite=Lax, Secure) keeps
   that browser paired for 30 days.

**Rotate** invalidates every paired device immediately — use it if a token
leaks. Failed unlock attempts are rate-limited per calling IP, counted against
the real client address rather than the tunnel's.

Scripts and agents can skip the cookie and send the token directly:

```bash
curl -H "Authorization: Bearer $(cat ~/.rynmesh/control_token)" https://you.rynmesh.ai/api/local/digest
```

## Exposing the node

```bash
cloudflared tunnel --url http://127.0.0.1:8791
```

The node is safe behind this on its own — the device token is the boundary.
Anything the tunnel adds is a second layer, not the first one.

## Optional: Cloudflare Access in front

If you want an SSO login before requests even reach the node, put Cloudflare
Access on the hostname and tell the node to accept its assertions:

```bash
export RYNMESH_ACCESS_TEAM_DOMAIN=yourteam.cloudflareaccess.com
export RYNMESH_ACCESS_AUD=<application audience tag>
```

The node verifies the assertion's **signature** against your team's published
keys, plus audience and expiry. It does not trust the header for existing —
anything that could reach the origin directly could set one. With these unset,
Access assertions are ignored entirely.

## What stays open

The peer API (`/api/v1`, `/api/peer`, `/health`) is how other nodes find and
talk to yours; it is not part of the control surface and has its own guard. Set
`RYNMESH_NETWORK_KEY` to require a shared key there too — unauthenticated probes
then get a generic 404 rather than a Rynmesh fingerprint.
