"""One directory of devices and the services they offer.

Before this module the device list and the AI service list were two discovery
pipelines with different record types, different time-to-live values and different
notions of "online". A provider could therefore be a usable service while not being a
device at all. The directory is the single place that

* defines how long each kind of observation is believed (``FreshnessPolicy``),
* makes every provider a device (``add_advertised_providers``), and
* joins devices, relationship, observed presence and services into one snapshot
  (``build_snapshot``) that every screen and API reads.

It performs no I/O. Callers hand it observations; it never asks the network anything.
See docs/PEER_DIRECTORY_REDESIGN.md.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .llm_package.service_view import presence_from_probe

# Order matters: the strongest relationship evidence about a peer wins.
_RELATIONSHIP_RANK = {"self": 3, "space_member": 2, "friend": 1, "discovered": 0}


@dataclass(frozen=True)
class FreshnessPolicy:
    """How long each class of observation is believed. The only place these live.

    * ``peer_record_max_age_hours``: a peer's signed registration (default 24h).
    * ``advertisement_max_age_hours``: a provider's service advertisement. Providers
      republish every ~30s, so a much shorter window keeps ghosts out of the list.
    * ``fresh_window_s``: inside it a record counts as live; older ones are shown
      as ``stale`` rather than hidden, so a device never silently disappears.
    """

    peer_record_max_age_hours: float = 24.0
    advertisement_max_age_hours: float = 1.0
    fresh_window_s: float = 180.0

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "FreshnessPolicy":
        env = os.environ if environ is None else environ
        try:
            peers = float(env.get("RYNMESH_DISCOVERY_MAX_AGE_HOURS", "") or cls.peer_record_max_age_hours)
        except ValueError:
            peers = cls.peer_record_max_age_hours
        return cls(peer_record_max_age_hours=peers if peers > 0 else cls.peer_record_max_age_hours)


def add_advertised_providers(
    records: list[dict[str, Any]],
    capacities: Iterable[Mapping[str, Any]],
    *,
    self_id: str,
    network_id: str,
) -> list[dict[str, Any]]:
    """Return ``records`` plus a device for every provider that only advertised capacity.

    A provider that published a capacity record but has no (unexpired) peer record is
    still a device on the network. Its record carries no endpoint, so presence for it
    comes from the heartbeat (``updated_at``) alone.
    """
    known = {str(record.get("peer_id") or "") for record in records}
    result = list(records)
    for capacity in capacities:
        peer_id = str(capacity.get("peer_id") or "")
        if not peer_id or peer_id == self_id or peer_id in known:
            continue
        known.add(peer_id)
        result.append({
            "peer_id": peer_id,
            "node_name": str(capacity.get("node_name") or ""),
            "endpoints": [],
            "network_id": str(capacity.get("network_id") or network_id),
            "updated_at": str(capacity.get("updated_at") or ""),
            "discovery_source": "capacity",
            "capabilities": list(capacity.get("capabilities") or ()),
        })
    return result


def _age_seconds(value: Any, now: datetime) -> float | None:
    text = str(value or "").strip()
    if not text or text == "local":
        return None
    try:
        seen = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return (now - seen).total_seconds()


def _strongest(*values: str) -> str:
    return max(values, key=lambda value: _RELATIONSHIP_RANK.get(value, 0))


def build_snapshot(
    items: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
    *,
    self_id: str,
    network_id: str,
    members: Iterable[str] = (),
    friend_ids: Iterable[str] = (),
    probe_for: Callable[[str], Mapping[str, Any] | None] = lambda _peer: None,
    policy: FreshnessPolicy | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Join devices (``peer_item`` dicts) with their services into one snapshot.

    Guarantees by construction: every service belongs to a device in ``peers``, and a
    device's ``presence`` is the one observation its services were judged against.
    """
    policy = policy or FreshnessPolicy()
    now = now or datetime.now(timezone.utc)
    member_set = frozenset(members)
    friend_set = frozenset(friend_ids)
    entries: dict[str, dict[str, Any]] = {}
    for item in items:
        peer_id = str(item.get("id") or "")
        if peer_id:
            entries[peer_id] = {**item, "services": []}
    for row in services:
        peer_id = str(row.get("peer_id") or "")
        if not peer_id:
            continue
        if peer_id not in entries:
            # Known only from a service catalog: still a device (it is where the service runs).
            entries[peer_id] = {
                "id": peer_id, "slug": "", "name": str(row.get("node_name") or peer_id), "endpoint": "",
                "lanEndpoints": [], "network": str(row.get("network_id") or network_id), "tier": "unverified",
                "credits": 0.0, "weight": 0.0, "lastSeen": str(row.get("updated_at") or ""),
                "discoverySource": "service", "served": 0, "fetched": 0, "trustedRoot": False,
                "isSelf": peer_id == self_id, "services": [],
            }
        entries[peer_id]["services"].append(dict(row))

    peers: list[dict[str, Any]] = []
    for peer_id, entry in entries.items():
        is_self = peer_id == self_id or bool(entry.get("isSelf"))
        base = "self" if is_self else "space_member" if peer_id in member_set else "friend" if peer_id in friend_set else "discovered"
        relationship = _strongest(base, *(str(row.get("relationship") or "discovered") for row in entry["services"]))
        probe = probe_for(peer_id)
        presence = presence_from_probe(peer_id, self_id=self_id, probe=probe)
        # What the probe actually reached: HTTP at the advertised address, and the signed LAN one.
        reachability = {
            "http": None if not probe else bool(probe.get("httpReachable")),
            "lan": None if not probe else bool(probe.get("lanReachable")),
        }
        age = None if is_self else _age_seconds(entry.get("lastSeen"), now)
        source = str(entry.get("discoverySource") or "")
        stale = (not is_self) and source != "lan" and age is not None and age > policy.fresh_window_s
        peers.append({
            **entry,
            "isSelf": is_self,
            "relationship": relationship,
            "presence": presence,
            "reachability": reachability,
            "freshness": {"source": source, "age_s": None if age is None else int(max(age, 0)), "stale": stale},
        })
    peers.sort(key=lambda peer: (not peer["isSelf"], str(peer.get("name") or ""), peer["id"]))

    # Stable digest of what a screen would render, so clients can skip identical snapshots.
    shape = [
        [p["id"], p["relationship"], p["presence"]["state"], p["freshness"]["stale"],
         sorted((s.get("service", {}).get("package_id", ""), s.get("availability", {}).get("state", ""))
                for s in p["services"])]
        for p in peers
    ]
    revision = hashlib.sha256(json.dumps(shape, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return {
        "revision": revision,
        "generated_at": now.isoformat(),
        "network_id": network_id,
        "self_id": self_id,
        "policy": {
            "peer_record_max_age_hours": policy.peer_record_max_age_hours,
            "advertisement_max_age_hours": policy.advertisement_max_age_hours,
            "fresh_window_s": policy.fresh_window_s,
        },
        "peers": peers,
    }


def directory_violations(snapshot: Mapping[str, Any]) -> list[str]:
    """Check the consistency invariants (I1-I3 in the design). Empty means consistent."""
    problems: list[str] = []
    for peer in snapshot.get("peers", []):
        for service in peer.get("services", []):
            if service.get("peer_id") != peer["id"]:
                problems.append(f"I1: service {service.get('peer_id')} listed under {peer['id']}")
            state = service.get("availability", {}).get("state")
            if state == "ready" and peer["presence"]["state"] == "offline":
                problems.append(f"I3: ready service on offline device {peer['id']}")
            if state == "ready" and peer["relationship"] == "discovered" \
                    and service.get("availability", {}).get("reason") not in {"catalog_verified"}:
                problems.append(f"I4: ready service on unconnected device {peer['id']}")
    return problems
