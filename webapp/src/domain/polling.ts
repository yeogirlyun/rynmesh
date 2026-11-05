/**
 * Background polling that cannot pile requests up.
 *
 * Every request from the webview to the local node shares one browser
 * connection pool (six per host). A poll that fires on a fixed interval while
 * its previous request is still running fills that pool, and the Agent's own
 * reads then wait in the browser until they time out. A poller here therefore
 * schedules its next run only after the current one has finished, bounds each
 * run with a timeout, slows down while the window is hidden, and cancels its
 * request when the view closes.
 */

/** Abort reason used when a run exceeds its timeout. */
export class PollTimeoutError extends Error {
  constructor() {
    super("poll_timeout");
    this.name = "PollTimeoutError";
  }
}

/** Abort reason used when the poller is stopped (view closed). */
export class PollStoppedError extends Error {
  constructor() {
    super("poll_stopped");
    this.name = "PollStoppedError";
  }
}

/** True when a run failed because it exceeded its timeout. */
export function isPollTimeout(reason: unknown): boolean {
  return reason instanceof PollTimeoutError;
}

export interface PollerOptions {
  /** One run. Throw (or reject) to count the run as failed. Check `signal.aborted` before applying results. */
  run: (signal: AbortSignal) => Promise<unknown>;
  /** Delay between the end of one run and the start of the next while visible. */
  intervalMs: number;
  /** A run is aborted with PollTimeoutError after this long. Evaluated at the start of each run. */
  timeoutMs: number | (() => number);
  /** Delay while the document is hidden. Defaults to intervalMs. */
  hiddenIntervalMs?: number;
  /** Upper bound for the failure backoff. Defaults to eight intervals. */
  maxBackoffMs?: number;
  /** Run once immediately on start (default true); otherwise wait one interval. */
  immediate?: boolean;
}

export interface Poller {
  /**
   * Run now. If a run is in flight, run once more right after it ends; repeated
   * calls meanwhile collapse into that one run. Resolves when the covering run
   * has finished (or the poller stops), and never rejects.
   */
  refresh(): Promise<void>;
  /** Clear timers, abort the in-flight run, and never run or call back again. */
  stop(): void;
}

function documentHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export function startPoller(options: PollerOptions): Poller {
  const interval = Math.max(0, options.intervalMs);
  const hiddenInterval = Math.max(0, options.hiddenIntervalMs ?? interval);
  const maxBackoff = Math.max(interval, options.maxBackoffMs ?? interval * 8);
  let stopped = false;
  let inFlight = false;
  let rerun = false;
  let failures = 0;
  let lastStarted = Number.NEGATIVE_INFINITY;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | null = null;
  let current: Array<() => void> = [];
  let afterRerun: Array<() => void> = [];

  const settle = (waiters: Array<() => void>) => { for (const resolve of waiters) resolve(); };
  const clearTimer = () => { if (timer !== undefined) { clearTimeout(timer); timer = undefined; } };

  const nextDelay = () => {
    const base = documentHidden() ? hiddenInterval : interval;
    if (failures === 0) return base;
    return Math.min(base * 2 ** (failures - 1), Math.max(base, maxBackoff));
  };

  const schedule = (delay: number) => {
    clearTimer();
    if (!stopped) timer = setTimeout(tick, delay);
  };

  function tick() {
    if (stopped || inFlight) return;
    clearTimer();
    inFlight = true;
    lastStarted = Date.now();
    const own = new AbortController();
    controller = own;
    const limit = typeof options.timeoutMs === "function" ? options.timeoutMs() : options.timeoutMs;
    const deadline = setTimeout(() => own.abort(new PollTimeoutError()), Math.max(0, limit));
    let ran: Promise<unknown>;
    try {
      ran = Promise.resolve(options.run(own.signal));
    } catch (error) {
      ran = Promise.reject(error);
    }
    ran.then(() => { failures = 0; }, () => { failures += 1; }).finally(() => {
      clearTimeout(deadline);
      if (controller === own) controller = null;
      inFlight = false;
      const finished = current;
      current = [];
      if (stopped) { settle(finished); return; }
      settle(finished);
      if (rerun) {
        rerun = false;
        current = afterRerun;
        afterRerun = [];
        tick();
        return;
      }
      schedule(nextDelay());
    });
  }

  const onVisibility = () => {
    if (stopped || documentHidden() || inFlight) return;
    const elapsed = Date.now() - lastStarted;
    if (elapsed >= interval) tick();
    else schedule(interval - elapsed);
  };
  if (typeof document !== "undefined") document.addEventListener("visibilitychange", onVisibility);

  if (options.immediate === false) schedule(nextDelay());
  else tick();

  return {
    refresh() {
      if (stopped) return Promise.resolve();
      return new Promise<void>((resolve) => {
        if (inFlight) {
          rerun = true;
          afterRerun.push(resolve);
        } else {
          current.push(resolve);
          tick();
        }
      });
    },
    stop() {
      if (stopped) return;
      stopped = true;
      clearTimer();
      if (typeof document !== "undefined") document.removeEventListener("visibilitychange", onVisibility);
      controller?.abort(new PollStoppedError());
      controller = null;
      settle(current);
      settle(afterRerun);
      current = [];
      afterRerun = [];
    },
  };
}
