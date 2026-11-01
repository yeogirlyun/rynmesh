"""Stop waiting for an async transport after the local ledger confirms cancellation."""
from __future__ import annotations

import asyncio


class ConsumerOrderCancelled(Exception):
    """Local cancellation is authoritative, not a provider completion receipt."""


async def cancellable_exchange(exchange, cancelled):
    if cancelled():
        raise ConsumerOrderCancelled
    pending = asyncio.create_task(exchange())
    try:
        while True:
            done, _ = await asyncio.wait({pending}, timeout=.1)
            if done:
                # Preserve authenticated usage when a response is already here;
                # the caller still checks cancellation before exposing output.
                return pending.result()
            if cancelled():
                raise ConsumerOrderCancelled
    finally:
        if not pending.done():
            pending.cancel()
        # Drain cancellation so transport cleanup runs, including the pool's
        # failed-session eviction. Do not leave an abandoned response waiter.
        await asyncio.gather(pending, return_exceptions=True)
