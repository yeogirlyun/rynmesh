import { useEffect, useMemo, useRef, useState } from "react";

interface ResourceOptions<T> {
  /** Include the client object as well as network/provider/order in the key. */
  scope: object;
  key: string;
  load: () => Promise<T>;
  intervalMs: number;
  enabled?: boolean;
  isTerminal?: (data: T) => boolean;
  onData?: (data: T) => void;
  onError?: (error: Error) => void;
}
interface Snapshot<T> { data?: T; loading: boolean; error: Error | null; }
interface Session<T> {
  refresh: () => Promise<T | undefined>;
  execute: <R>(operation: () => Promise<R>, apply?: (result: R) => T) => Promise<R>;
}

/** One completion-scheduled read loop. Only explicit operations may write remotely. */
function useServiceResource<T>(options: ResourceOptions<T>) {
  const { scope, key, intervalMs, enabled = true } = options;
  const callbacks = useRef(options);
  callbacks.current = options;
  const operationBusy = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [busy, setBusy] = useState(false);
  const session = useMemo<Session<T>>(() => ({ refresh: async () => undefined,
    execute: async () => { throw new Error("This service is no longer active."); } }), [scope, key, enabled, intervalMs]);
  const current = useRef(session);
  current.current = session;
  const actions = useMemo(() => ({ refresh: () => session.refresh(),
    execute: <R,>(operation: () => Promise<R>, apply?: (result: R) => T) => session.execute(operation, apply) }), [session]);
  const [snapshot, setSnapshot] = useState<{ session: Session<T>; value: Snapshot<T> }>({
    session, value: { loading: enabled, error: null },
  });

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let reading: Promise<T | undefined> | undefined;
    let revision = 0;
    let failures = 0;
    let terminal = false;
    let recheck = false;
    const valid = () => active && current.current === session;
    const publish = (value: Snapshot<T>) => { if (valid()) setSnapshot({ session, value }); };
    let value: Snapshot<T> = { loading: enabled, error: null };
    publish(value);
    const commit = (data: T) => {
      terminal = callbacks.current.isTerminal?.(data) ?? false;
      value = { data, loading: false, error: null };
      publish(value);
      callbacks.current.onData?.(data);
    };
    const schedule = () => {
      if (!valid() || !enabled || operationBusy.current || (terminal && !recheck)) return;
      clearTimeout(timer);
      const delay = recheck ? 0 : Math.min(60_000, Math.max(1, intervalMs) * 2 ** Math.min(failures, 5));
      recheck = false;
      timer = setTimeout(() => { void refresh(); }, delay);
    };
    const refresh = (): Promise<T | undefined> => {
      if (!valid() || !enabled) return Promise.resolve(undefined);
      if (reading) return reading;
      if (operationBusy.current) { recheck = true; return Promise.resolve(undefined); }
      clearTimeout(timer);
      const started = revision;
      const load = callbacks.current.load;
      reading = Promise.resolve().then(load).then((data) => {
        if (valid() && started === revision) { failures = 0; commit(data); return data; }
        return undefined;
      }).catch((cause: unknown) => {
        if (valid() && started === revision) {
          failures += 1;
          const error = cause instanceof Error ? cause : new Error("Could not refresh this service.");
          value = { ...value, loading: false, error };
          publish(value);
          callbacks.current.onError?.(error);
        }
        return undefined;
      }).finally(() => { reading = undefined; schedule(); });
      return reading;
    };
    session.refresh = refresh;
    session.execute = async (operation, apply) => {
      if (!valid()) throw new Error("This service is no longer active.");
      if (operationBusy.current) throw new Error("Another operation is still finishing. Check its status before trying again.");
      operationBusy.current = true;
      setBusy(true);
      revision += 1; // A read begun before the operation cannot undo its result.
      clearTimeout(timer);
      try {
        const result = await operation(); // Never retry a purchase or connection action.
        if (valid() && apply) commit(apply(result));
        return result;
      } finally {
        operationBusy.current = false;
        if (mounted.current) setBusy(false);
        recheck = true;
        if (!reading) schedule();
        // A new scope may have mounted while this explicit operation was pending.
        if (mounted.current && current.current !== session) void current.current.refresh();
      }
    };
    if (enabled) void refresh();
    return () => { active = false; revision += 1; clearTimeout(timer); };
  }, [session, enabled, intervalMs]);

  const value = snapshot.session === session ? snapshot.value : { loading: enabled, error: null, data: undefined };
  return { ...value, busy, ...actions };
}

export function useProviderDiscovery<T>(options: Omit<ResourceOptions<T[]>, "isTerminal"> & { identity: (provider: T) => string }) {
  const resource = useServiceResource({ ...options, load: async () => {
    const rows = await options.load();
    const unique = new Map<string, T>();
    for (const row of rows) { const id = options.identity(row); if (!unique.has(id)) unique.set(id, row); }
    return [...unique.values()];
  } });
  return { ...resource, providers: resource.data ?? [] };
}

export function useServiceOrder<T>(options: ResourceOptions<T>) {
  const resource = useServiceResource(options);
  // Fixture/legacy adapters can await an original task without owning timers in
  // the screen. This mode is exclusive with the automatic resource read loop.
  const lifetime = useMemo(() => ({ controller: new AbortController(), waiting: false }), [options.scope, options.key]);
  useEffect(() => {
    lifetime.controller = new AbortController();
    return () => lifetime.controller.abort();
  }, [lifetime]);
  const awaitTerminal = async (initial: T, read: () => Promise<T>): Promise<T> => {
    if (options.enabled !== false || !options.isTerminal || lifetime.waiting) throw new Error("An order tracker is already active.");
    lifetime.waiting = true;
    const signal = lifetime.controller.signal;
    try {
      let row = initial;
      while (!options.isTerminal(row)) {
        await new Promise<void>((resolve, reject) => {
          const stopped = () => { clearTimeout(timer); reject(new Error("This order view was closed; the task continues on the node.")); };
          const timer = setTimeout(() => { signal.removeEventListener("abort", stopped); resolve(); }, options.intervalMs);
          signal.addEventListener("abort", stopped, { once: true });
          if (signal.aborted) stopped();
        });
        row = await read();
        if (signal.aborted) throw new Error("This order view was closed; the task continues on the node.");
      }
      return row;
    } finally { lifetime.waiting = false; }
  };
  return { ...resource, awaitTerminal };
}
