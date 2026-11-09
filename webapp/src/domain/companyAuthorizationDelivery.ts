import { useSyncExternalStore } from 'react';
import { nodeControlUrl } from './nodeUrl';

type Delivery = { sequence: number; acknowledged: boolean; pending: boolean };
let current: Delivery | null = null;
let timer: ReturnType<typeof setTimeout> | undefined;
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };

export function observeCompanyAuthorizationDelivery(value: unknown) {
  if (!value || typeof value !== 'object') return;
  const next = value as Delivery;
  if (!Number.isInteger(next.sequence) || typeof next.pending !== 'boolean' || typeof next.acknowledged !== 'boolean') return;
  if (current && (next.sequence < current.sequence || (next.sequence === current.sequence && current.acknowledged && !next.acknowledged))) return;
  current = next;
  for (const listener of listeners) listener();
  if (!next.pending) { clearTimeout(timer); timer = undefined; }
  else if (!timer) timer = setTimeout(poll, 2000);
}

async function poll() {
  timer = undefined;
  try {
    const response = await fetch(nodeControlUrl('/agent/extensions/company/authorization-delivery'), {
      credentials: 'include', cache: 'no-store', signal: AbortSignal.timeout(5000),
    });
    if (response.ok) observeCompanyAuthorizationDelivery(await response.json());
  } catch { /* Keep the pending notice while the node is unavailable. */ }
  if (current?.pending && !timer) timer = setTimeout(poll, 2000);
}

export function useCompanyAuthorizationDelivery() {
  return useSyncExternalStore(subscribe, () => current);
}
