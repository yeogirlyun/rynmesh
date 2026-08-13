"""rynmesh.services — the first three Ryn-network services.

Wedge category: heavy AI compute on bounded I/O (the strongest fit per
sim/FINDINGS.md F5). All follow the same signal50_service.py pattern —
poll the mailbox, run, publish a signed result — and ship with stdlib-only
deterministic stub backends so the protocol path can be exercised on any
node without GPUs or external API keys. Real backends are env-swappable.
"""
from ._base import ServiceWorker  # noqa: F401
