"""View model for discovered AI services.

A registry advertisement only *locates* a provider. It says nothing about whether this
node may use the service, and its ``online`` flag is the provider's own claim. This
module derives, from facts the local node controls, the three things a screen needs:

* ``relationship``  who the provider is to this node (self / space_member / friend / discovered)
* ``presence``      what this node last *observed* about the device (not what it claimed)
* ``availability``  ready / needs_connection / offline, with a machine-readable reason

Only stable fields are produced (no timestamps) so two consecutive reads compare equal.
The original record fields, including ``online``, are left untouched for compatibility;
``online_claim`` repeats the provider's claim under an honest name.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping

READY = "ready"
NEEDS_CONNECTION = "needs_connection"
OFFLINE = "offline"


def presence_from_probe(peer_id: str, *, self_id: str, probe: Mapping[str, Any] | None) -> dict[str, str]:
    """Map the latest ``PeerHealthProbe`` row to a presence without trusting any claim."""
    if peer_id == self_id:
        return {"state": "online", "basis": "self"}
    if not probe:
        return {"state": "unconfirmed", "basis": "none"}
    basis = str(probe.get("presenceSource") or "unknown")
    if probe.get("online") is True:
        return {"state": "online", "basis": basis}
    if probe.get("online") is False:
        return {"state": "offline", "basis": basis}
    return {"state": "unconfirmed", "basis": basis}


def _relationship(peer_id: str, row: Mapping[str, Any], *, self_id: str, members: frozenset[str]) -> str:
    if peer_id == self_id or row.get("local_only") is True or row.get("access") == "self":
        return "self"
    if peer_id in members:
        return "space_member"
    if row.get("access") == "friend":
        return "friend"
    return "discovered"


def _entitlement(row: Mapping[str, Any], relationship: str, *, allows_ai: bool) -> tuple[bool, str]:
    """Whether this node is entitled to use the service, and why (or why not)."""
    if relationship == "self":
        return True, "self"
    if row.get("binding_verified") is True:
        # The provider's signed catalog response already bound this model to us.
        return True, "catalog_verified"
    if relationship == "friend":
        return True, "friend"
    if relationship == "space_member":
        if allows_ai:
            return True, "space_member"
        return False, "space_ai_access_off"
    return False, "advertisement_only"


def annotate_services(
    rows: Iterable[dict[str, Any]],
    *,
    self_id: str,
    members: Iterable[str] = (),
    allows_ai: Callable[[str], bool] = lambda _peer: False,
    probe_for: Callable[[str], Mapping[str, Any] | None] = lambda _peer: None,
) -> list[dict[str, Any]]:
    """Return copies of ``rows`` with ``relationship``/``presence``/``availability`` added."""
    member_set = frozenset(members)
    allowed: dict[str, bool] = {}
    result: list[dict[str, Any]] = []
    for row in rows:
        peer_id = str(row.get("peer_id") or "")
        relationship = _relationship(peer_id, row, self_id=self_id, members=member_set)
        if peer_id not in allowed:
            allowed[peer_id] = bool(allows_ai(peer_id)) if relationship == "space_member" else False
        entitled, reason = _entitlement(row, relationship, allows_ai=allowed[peer_id])
        presence = presence_from_probe(peer_id, self_id=self_id, probe=probe_for(peer_id))
        claim = bool(row.get("online"))
        if not entitled:
            state = NEEDS_CONNECTION
        elif presence["state"] == "offline":
            state, reason = OFFLINE, "peer_unreachable"
        elif not claim:
            state, reason = OFFLINE, "provider_offline"
        else:
            state = READY
        result.append({
            **row,
            "online_claim": claim,
            "relationship": relationship,
            "presence": presence,
            "availability": {"state": state, "reason": reason},
        })
    return result


def is_usable(row: Mapping[str, Any]) -> bool:
    """Single question every consumer asks. Falls back to the claim for unannotated rows."""
    availability = row.get("availability")
    if isinstance(availability, Mapping):
        return availability.get("state") == READY
    return bool(row.get("online"))
