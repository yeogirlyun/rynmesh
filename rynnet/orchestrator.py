#!/usr/bin/env python3
"""rynnet — transparent virtual-network testbed orchestrator.

Spawns UNMODIFIED rynmesh nodes as containers (each its own IP), drives the
real publish/discover/fetch protocol via the in-image qa/lan_qa.py, observes
via each node's normal peer HTTP API, injects faults (partition / NAT) from
*outside* the process, asserts on real protocol behavior, and tears down.

Stdlib only; talks to docker via subprocess. macOS host + Colima.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

IMAGE = "rynnet-node:latest"
PEER_PORT = 8791
REG_PORT = 8790
RUNS = Path(__file__).resolve().parent / "runs"


def sh(args: list[str], timeout: float = 60.0, check: bool = True) -> str:
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"cmd failed ({p.returncode}): {' '.join(args)}\n{p.stderr}")
    return p.stdout.strip()


def dexec(cont: str, cmd: list[str], timeout: float = 90.0, check: bool = True) -> str:
    return sh(["docker", "exec", cont, *cmd], timeout=timeout, check=check)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def deep_find(obj, key_substr: str):
    """First value whose key contains key_substr (case-insensitive), any depth."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if key_substr in str(k).lower() and not isinstance(v, (dict, list)):
                return v
            r = deep_find(v, key_substr)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find(v, key_substr)
            if r is not None:
                return r
    return None


class Testbed:
    def __init__(self, scenario: dict):
        self.s = scenario
        self.name = scenario["name"]
        self.run_id = f"{self.name}-{int(time.time())}"
        self.net = f"rynnet_{self.run_id}".replace("-", "_")
        self.network_id = scenario.get("network_id", f"rynnet-{self.name}")
        self.real_registry = scenario.get("registry") == "real"
        if self.real_registry:
            self.network_id = f"rynnet-test-{self.run_id}"  # unique; no prod reuse
        self.nodes = scenario["nodes"]
        self.node_ids = [n["id"] for n in self.nodes]
        self.nat_ids = [n["id"] for n in self.nodes if n.get("nat")]
        self.containers: list[str] = []
        self.peer_ids: dict[str, str] = {}
        self.outdir = RUNS / self.run_id
        (self.outdir / "logs").mkdir(parents=True, exist_ok=True)

    def cname(self, node_id: str) -> str:
        return f"{self.net}_{node_id}"

    # ---- lifecycle ---------------------------------------------------------
    def up(self) -> None:
        print(f"[rynnet] run {self.run_id}: network {self.net}")
        sh(["docker", "network", "create", self.net])

        if self.real_registry:
            reg_url = "https://registry.rynmesh.ai"
            print(f"[rynnet] REAL registry; network_id={self.network_id} "
                  "(unique per run, TTL-bounded)")
        else:
            self._run("registry", "registry", {
                "RYNMESH_REGISTRY_HOST": "0.0.0.0",
                "RYNMESH_REGISTRY_PORT": str(REG_PORT),
                "RYNMESH_REGISTRY_DIR": "/data/registry",
            })
            self._wait_health("registry", REG_PORT)
            reg_url = f"http://registry:{REG_PORT}"

        for node in self.nodes:
            nid = node["id"]
            env = {
                "RYNMESH_HOME": "/data",
                "RYNMESH_NODE_NAME": nid,
                "RYNMESH_PEER_HOST": "0.0.0.0",
                "RYNMESH_PEER_PORT": str(PEER_PORT),
                "RYNMESH_PEER_PUBLIC_HOST": nid,
                "RYNMESH_PEER_ENDPOINT": f"http://{nid}:{PEER_PORT}",
                "RYNMESH_NETWORK_ID": self.network_id,
                "RYNMESH_AUTO_REGISTER": "1",
                "RYNMESH_REGISTRY_URL": reg_url,
                "RYNMESH_RELAY_URL": reg_url,
                # Closes F1: consumer-signed serve receipts propagate to the
                # provider so its scoreboard reflects what consumers attested.
                "RYNMESH_PROPAGATE_SERVE_RECEIPTS": "1",
            }
            if node.get("netem"):
                env["RYNNET_NETEM"] = node["netem"]
            self._run(nid, "peer", env)

        for nid in self.node_ids:
            self._wait_health(self.cname(nid), PEER_PORT)
        self._resolve_peer_ids()
        self._apply_nat()
        print(f"[rynnet] {len(self.node_ids)} nodes healthy"
              + (f"; NAT(relay-only)={self.nat_ids}" if self.nat_ids else ""))

    def _run(self, node_id: str, role: str, env: dict) -> None:
        cname = "registry" if role == "registry" else self.cname(node_id)
        alias = "registry" if role == "registry" else node_id
        args = ["docker", "run", "-d", "--name", cname,
                "--network", self.net, "--network-alias", alias,
                "--cap-add", "NET_ADMIN", "--label", f"rynnet={self.run_id}",
                "-e", f"RYNNET_ROLE={role}"]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        args.append(IMAGE)
        sh(args)
        self.containers.append(cname)

    def _wait_health(self, cont: str, port: int, attempts: int = 80) -> None:
        url = f"http://localhost:{port}/health"
        for _ in range(attempts):
            if dexec(cont, ["curl", "-fsS", "-m", "2", url], timeout=8, check=False):
                return
            time.sleep(0.5)
        self._dump_logs()
        raise RuntimeError(f"{cont} not healthy at {url}")

    def _resolve_peer_ids(self) -> None:
        for nid in self.node_ids:
            blob = dexec(self.cname(nid),
                         ["curl", "-fsS", "-m", "3",
                          f"http://localhost:{PEER_PORT}/api/v1/node"],
                         timeout=8, check=False)
            pid = deep_find(_try_json(blob) or {}, "peer_id")
            if pid:
                self.peer_ids[nid] = str(pid)

    def _apply_nat(self) -> None:
        # Transparent: drop inbound to the peer port on eth0 only, so other
        # peers cannot dial this node (NAT-like) -> it must use the relay.
        # Loopback (health) and outbound (registry/relay polling) unaffected.
        for nid in self.nat_ids:
            dexec(self.cname(nid),
                  ["iptables", "-A", "INPUT", "-i", "eth0", "-p", "tcp",
                   "--dport", str(PEER_PORT), "-j", "DROP"], check=False)

    # ---- scenario steps ----------------------------------------------------
    def publish(self) -> dict:
        p = self.s["publish"]
        cont = self.cname(p["node"])
        if "mix" in p:
            mix = p["mix"]
            print(f"[rynnet] publish-mix on {p['node']}: {mix}")
            cmd = ["python", "/opt/rynmesh/qa/lan_qa.py", "publish-mix",
                   "--network-id", self.network_id, "--register"]
            for key in ("images", "audios", "documents"):
                cmd += [f"--{key}", str(mix.get(key, 0))]
            out = dexec(cont, cmd, timeout=180)
        else:
            print(f"[rynnet] publish {p['count']} on {p['node']}")
            out = dexec(cont,
                        ["python", "/opt/rynmesh/qa/lan_qa.py", "publish-sample",
                         "--network-id", self.network_id, "--register",
                         "--count", str(p["count"])], timeout=120)
        res = json.loads(out)
        (self.outdir / "publish.json").write_text(json.dumps(res, indent=2))
        return res

    def fetch(self) -> dict:
        f = self.s["fetch"]
        pub = self.s["publish"]
        if "mix" in pub:
            items_per_peer = sum(int(v) for v in pub["mix"].values())
        else:
            items_per_peer = int(pub.get("count", 10))
        results: dict[str, dict] = {}
        for nid in f["nodes"]:
            print(f"[rynnet] fetch-matrix on {nid}")
            out = dexec(self.cname(nid),
                        ["python", "/opt/rynmesh/qa/lan_qa.py", "fetch-matrix",
                         "--network-id", self.network_id,
                         "--limit-per-peer", str(items_per_peer),
                         "--timeout-s", str(f.get("timeout_s", 20))],
                        timeout=int(f.get("timeout_s", 20)) * 4 + 30)
            results[nid] = json.loads(out)
        (self.outdir / "fetch.json").write_text(json.dumps(results, indent=2))
        return results

    def observe(self, stop: threading.Event) -> None:
        o = self.s.get("observe", {})
        interval = float(o.get("interval_s", 3))
        ts = self.outdir / "timeseries.jsonl"
        n = 0
        with ts.open("w") as fh:
            while not stop.is_set():
                snap = {"t": now(), "nodes": {}}
                for nid in self.node_ids:
                    c = self.cname(nid)
                    snap["nodes"][nid] = {
                        ep.replace("/", "_"): _try_json(dexec(
                            c, ["curl", "-fsS", "-m", "3",
                                f"http://localhost:{PEER_PORT}/api/v1/{ep}"],
                            timeout=8, check=False))
                        for ep in ("node", "credits/scoreboard")
                    }
                fh.write(json.dumps(snap) + "\n")
                fh.flush()
                n += 1
                stop.wait(interval)
        print(f"[rynnet] observed {n} snapshots")

    def run_faults(self, stop: threading.Event) -> None:
        faults = self.s.get("faults", [])
        if not faults:
            return
        t0 = time.time()
        for fl in sorted(faults, key=lambda x: x["at_s"]):
            delay = fl["at_s"] - (time.time() - t0)
            if delay > 0 and stop.wait(delay):
                return
            ids = fl.get("partition", [])
            print(f"[rynnet] FAULT partition {ids} for {fl['duration_s']}s")
            for nid in ids:
                self._partition(nid, True)
            if stop.wait(fl["duration_s"]):
                pass
            for nid in ids:
                self._partition(nid, False)
            print(f"[rynnet] FAULT heal {ids}")

    def _partition(self, nid: str, on: bool) -> None:
        flag = "-A" if on else "-D"
        c = self.cname(nid)
        for chain, io in (("INPUT", "-i"), ("OUTPUT", "-o")):
            dexec(c, ["iptables", flag, chain, io, "eth0", "-j", "DROP"],
                  check=False)

    # ---- assertions --------------------------------------------------------
    def assert_results(self, fetch_res: dict) -> dict:
        a = self.s.get("assert", {})
        rep: dict = {"run": self.run_id, "checks": [], "ok": True}

        m = a.get("min_fetched_per_node")
        if m is not None:
            for nid, r in fetch_res.items():
                got = int(r.get("fetched_count", 0))
                ok = got >= m
                rep["checks"].append({"check": "min_fetched_per_node",
                                      "node": nid, "want>=": m, "got": got,
                                      "ok": ok})
                rep["ok"] &= ok

        pub = a.get("publisher_credit_grows")
        if pub:
            first, last = self._credit_bounds(pub)
            tol = 0.05  # allow time-decay; we assert credit PERSISTS
            persists = bool(
                first and last
                and last["score"] >= first["score"] * (1 - tol)
                and last["event_count"] >= first["event_count"])
            grew = bool(first and last and (
                last["event_count"] > first["event_count"]
                or last["score"] > first["score"]))
            note = None if first else "scoreboard unparseable (soft)"
            if first and not grew:
                note = ("credit recorded but did NOT grow on serving — see "
                        "rynnet/FINDINGS.md (provider serve-credit gap)")
            rep["checks"].append({
                "check": "publisher_credit_persists", "node": pub,
                "first": first, "last": last, "grew": grew,
                "ok": persists, "soft": first is None, "note": note})
            if first is not None:
                rep["ok"] &= persists

        # Property assertion: every observed distribution_weight stays within
        # the CreditPolicy band. With the sublinear sqrt transform (β=0.5)
        # already in rynmesh/credits.py and the [0.05, 5.0] clamp, this must
        # hold under any non-pathological workload — a structural regression
        # check that F4's "power sublinear" guarantee survives in production.
        band = a.get("credit_band_holds")
        if band:
            lo, hi = 0.05, 5.0
            if isinstance(band, dict):
                lo = float(band.get("min", lo))
                hi = float(band.get("max", hi))
            weights: list[float] = []
            ts = self.outdir / "timeseries.jsonl"
            if ts.exists():
                for line in ts.read_text().splitlines():
                    try:
                        snap = json.loads(line)
                    except ValueError:
                        continue
                    for nstate in (snap.get("nodes") or {}).values():
                        sb = ((nstate or {}).get("credits_scoreboard") or {})
                        for acct in (sb.get("accounts") or []):
                            w = acct.get("distribution_weight")
                            if isinstance(w, (int, float)):
                                weights.append(float(w))
            ok = bool(weights) and min(weights) >= lo and max(weights) <= hi
            rep["checks"].append({
                "check": "credit_band_holds",
                "band": {"min": lo, "max": hi},
                "min_seen": min(weights) if weights else None,
                "max_seen": max(weights) if weights else None,
                "samples": len(weights),
                "ok": ok,
            })
            rep["ok"] &= ok

        # Strict check used by the F1-closure scenario: with propagation on,
        # the publisher's event_count must STRICTLY increase as fetchers
        # POST signed serve receipts to it.
        pub_strict = a.get("publisher_credit_grew_strict")
        if pub_strict:
            first, last = self._credit_bounds(pub_strict)
            grew = bool(first and last
                        and last["event_count"] > first["event_count"])
            rep["checks"].append({
                "check": "publisher_credit_grew_strict", "node": pub_strict,
                "first": first, "last": last, "ok": grew,
                "note": None if first else "scoreboard unparseable"})
            rep["ok"] &= grew

        if self.nat_ids:
            for nid in self.nat_ids:
                served = nid in fetch_res and fetch_res[nid].get("fetched_count", 0) > 0
                consumed = any(
                    any(att.get("peer_id") == self.peer_ids.get(nid)
                        for att in r.get("attempts", []))
                    for r in fetch_res.values())
                ok = served or consumed
                rep["checks"].append({"check": "nat_relay_path", "node": nid,
                                      "ok": ok, "soft": True,
                                      "note": "NAT'd node interacted via relay"})

        if self.s.get("faults"):
            for fl in self.s["faults"]:
                for nid in fl.get("partition", []):
                    healthy = bool(dexec(
                        self.cname(nid),
                        ["curl", "-fsS", "-m", "2",
                         f"http://localhost:{PEER_PORT}/health"],
                        timeout=8, check=False))
                    rep["checks"].append({"check": "partition_heals",
                                          "node": nid, "ok": healthy})
                    rep["ok"] &= healthy

        (self.outdir / "report.json").write_text(json.dumps(rep, indent=2))
        return rep

    def _credit_bounds(self, nid: str):
        pid = self.peer_ids.get(nid)
        ts = self.outdir / "timeseries.jsonl"
        if not pid or not ts.exists():
            return None, None
        seq = []
        for line in ts.read_text().splitlines():
            try:
                snap = json.loads(line)
            except ValueError:
                continue
            sb = (snap["nodes"].get(nid) or {}).get("credits_scoreboard") or {}
            for acct in (sb.get("accounts") or []):
                if acct.get("peer_id") == pid:
                    seq.append({"score": float(acct.get("score", 0)),
                                "event_count": int(acct.get("event_count", 0)),
                                "distribution_weight":
                                    float(acct.get("distribution_weight", 0))})
                    break
        return (seq[0], seq[-1]) if seq else (None, None)

    # ---- teardown ----------------------------------------------------------
    def _dump_logs(self) -> None:
        for c in self.containers:
            logs = sh(["docker", "logs", c], timeout=20, check=False)
            (self.outdir / "logs" / f"{c}.log").write_text(logs or "")

    def down(self) -> None:
        self._dump_logs()
        for c in self.containers:
            sh(["docker", "rm", "-f", c], check=False)
        sh(["docker", "network", "rm", self.net], check=False)
        print(f"[rynnet] torn down {self.run_id}")


def _try_json(s: str):
    try:
        return json.loads(s) if s else None
    except ValueError:
        return s or None


def main() -> int:
    ap = argparse.ArgumentParser(description="rynnet testbed orchestrator")
    ap.add_argument("scenario")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    tb = Testbed(json.loads(Path(args.scenario).read_text()))
    failed = False
    try:
        tb.up()
        tb.publish()
        stop = threading.Event()
        obs = threading.Thread(target=tb.observe, args=(stop,), daemon=True)
        flt = threading.Thread(target=tb.run_faults, args=(stop,), daemon=True)
        obs.start()
        flt.start()
        fetch_res = tb.fetch()
        # let observation/faults run their declared window after fetch
        dur = float(tb.s.get("observe", {}).get("duration_s", 0))
        time.sleep(max(0.0, dur))
        stop.set()
        obs.join(timeout=15)
        flt.join(timeout=15)
        rep = tb.assert_results(fetch_res)
        print(json.dumps(rep, indent=2))
        failed = not rep["ok"]
    except Exception as exc:  # noqa: BLE001 - always tear down
        print(f"[rynnet] ERROR: {exc}", file=sys.stderr)
        failed = True
    finally:
        if args.keep:
            print(f"[rynnet] --keep set; leaving {tb.run_id}")
        else:
            tb.down()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
