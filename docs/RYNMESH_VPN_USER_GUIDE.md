# Rynnode — Watch China TV via the Shenzhen VPN (user guide)

Step-by-step to use the Shenzhen `net.egress` VPN from the rynnode app: open the app, connect, launch a China-exit browser with the major CN video sites, watch, then review status / disconnect / reconnect.

> **What's running where (already deployed):**
> - **HK server** (`203.0.113.10`) runs the **registry** (`:8790`) the mesh coordinates through.
> - **Shenzhen node** (`sz-egress`) runs the **`net.egress` VPN provider** + a reverse tunnel.
> - Your **Mac** runs a local **rynnode** (the consumer the app drives) + the **`rynmesh-vpn`** data plane (bundled with the `rynmesh` package).
> The VPN tunnel itself is an `ssh -D` SOCKS5 proxy from your Mac → Shenzhen; traffic in the dedicated Chrome profile exits from a **China Telecom IP (`loc=CN`)**.

---

## 1. Open the app
**Double-click `scripts/rynnode.command`** in your Rynmesh checkout. It:
- starts your local rynnode (pointed at the HK registry),
- starts the UI, and
- opens it in your browser at **http://localhost:5173**.

(First double-click may prompt macOS Gatekeeper → right-click → **Open**. Or run it in Terminal: `~/…/rynmesh/scripts/rynnode.command`.)

## 2. Connect to the Shenzhen VPN
In the app: **Home → "Recommended Services" → Shenzhen VPN** card → click **Connect · 1 credit**.
- The node brokers a session from `sz-egress`, brings up the tunnel, and verifies the exit.
- The card flips to **● Connected · Shenzhen · `192.0.2.20` · loc=CN ✓ · up 0m**.

## 3. Watch China TV
Click **Watch CN TV** on the connected card. A dedicated **Shenzhen Chrome** window opens with the major mainland sites in tabs:
**CCTV-1, CCTV-5 (sports), Migu, Bilibili, iQiyi, Tencent Video, Youku, Mango TV.**
Everything in that window exits from China, so region-locked dramas / live TV / sports play. Browse and watch normally.

> The window labels itself **"Rynmesh Exit"** — it exits via whatever region the brokered session provides; here that's **Shenzhen / China** (verified `loc=CN`). (Older builds mislabeled this window "Avaryn HK Chrome"; that was fixed when the tunnel script moved in-tree as `rynmesh-vpn`.)

## 4. Review status
Back in the app, the Shenzhen VPN card shows the live session:
- **Connected · Shenzhen** + the **exit IP** + **loc=CN ✓**
- **uptime** (e.g. `up 12m`) — how long the VPN has been up
- **credits** charged for the session and the session **TTL**

> **Data transferred** is not shown yet — metering bytes through the `ssh -D` tunnel needs proxy-level accounting (a planned follow-up). Uptime, exit IP, region, and credits are live.

## 5. Disconnect / reconnect
- **Disconnect** on the card tears the tunnel down (the China exit stops; your normal browsing is unaffected the whole time — only the dedicated Chrome profile used the VPN).
- **Connect** again any time to re-establish and **Watch CN TV** reopens the sites.

---

## Prerequisites / health (already set up; here for reference)
- **SZ provider must be running:** on the Shenzhen box, `systemctl status rynmesh-egress` (the `net.egress` provider) + `rynmesh-peer` + `rynmesh-sz-tunnel`.
- **HK registry must be up:** `http://203.0.113.10:8790/health` returns ok; it advertises the provider (`/api/v1/jobs/capacity?capability=net.egress` lists `sz-egress`).
- **Your Mac needs:** `~/.ssh/config` with the `sz-egress-exit` alias (HostName/Port/ProxyJump/IdentityFile), and `rynmesh-vpn` on PATH (installed with the `rynmesh` package, e.g. `~/Library/Python/3.13/bin`).

## Troubleshooting
- **Card doesn't appear / "No services":** the SZ `rynmesh-egress` worker isn't advertising — check it's `active` and the HK registry lists it.
- **Connect → "exit not in CN" warning:** the tunnel came up but the geo check didn't return CN — retry; check the SZ box reachability.
- **Connect fails (`broker_failed` / timeout):** the SZ provider isn't polling work orders, or the HK registry is unreachable from your Mac.
- **"Watch CN TV" opens nothing:** ensure Google Chrome is installed; check `/tmp/rynnode.log`.
- **Sites still geo-blocked:** confirm the card shows `loc=CN ✓` and you're using the **Shenzhen Chrome window**, not your normal browser.
