from rynmesh.services.net_egress_client import _resolve_provider


class _Store:
    peer_id = "self"
    def __init__(self, caps, peers): self._caps = caps; self._peers = peers
    def list_job_capacities(self, *, capability, network_id): return {"capacities": self._caps}
    def discover_peers(self, *, network_id): return self._peers


def test_resolve_prefers_net_egress_capability_provider():
    s = _Store(caps=[{"peer_id": "self"}, {"peer_id": "szprov", "node_name": "sz-egress"}],
               peers={"self": {}, "hk": {}})
    assert _resolve_provider(s, "", "rynmesh-main") == "szprov"


def test_resolve_falls_back_to_discovered_dict_keys():
    s = _Store(caps=[], peers={"self": {}, "hk": {}})   # discover_peers returns a DICT
    assert _resolve_provider(s, "", "rynmesh-main") == "hk"


def test_resolve_explicit_provider_passthrough():
    assert _resolve_provider(None, "explicit-id", "n") == "explicit-id"
