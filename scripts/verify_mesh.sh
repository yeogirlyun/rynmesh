#!/usr/bin/env bash
set -uo pipefail
# Verify the local rynmesh node discovers all expected mesh peers and they are reachable.
# Usage: ./verify_mesh.sh [peer_base_url]   (default http://127.0.0.1:8791)
BASE="${1:-http://127.0.0.1:8791}"
EXPECTED=(your-mac ms-2 m4-mini m2-mini hk-server sz-egress)

AUTH_HDR=()
if [ -n "${RYNMESH_NETWORK_KEY:-}" ]; then
  _tok="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(("rynmesh-net-key:"+sys.argv[1]).encode()).hexdigest())' "$RYNMESH_NETWORK_KEY")"
  AUTH_HDR=(-H "x-ryn-auth: $_tok")
fi

json="$(curl -fsS -X POST "$BASE/api/local/peers/discover" -H 'content-type: application/json' -d '{}')" || {
  echo "✗ cannot reach local node at $BASE (is rynmesh-peer running?)"; exit 1; }

names="$(printf '%s' "$json" | python3 -c 'import sys,json; print("\n".join(p.get("name","") for p in json.load(sys.stdin)))')"

missing=0
for n in "${EXPECTED[@]}"; do
  if printf '%s\n' "$names" | grep -qx "$n"; then echo "✓ discovered $n"; else echo "✗ MISSING   $n"; missing=1; fi
done

# Reachability of each discovered http(s) endpoint
printf '%s' "$json" | python3 -c '
import sys, json
for p in json.load(sys.stdin):
    ep = p.get("endpoint", "")
    if ep.startswith(("http://", "https://")):
        print(p.get("name", ""), ep)
' | while read -r name endpoint; do
  if curl -fsS "${AUTH_HDR[@]+"${AUTH_HDR[@]}"}" --max-time 6 "$endpoint/health" >/dev/null 2>&1; then
    echo "  ↳ $name reachable ($endpoint)"
  else
    echo "  ↳ $name UNREACHABLE ($endpoint)"
  fi
done

[ "$missing" -eq 0 ] && echo "ALL 6 NODES PRESENT" || echo "SOME NODES MISSING"
exit "$missing"
