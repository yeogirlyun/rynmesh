import type { GroundedArticleContext } from "./groundedContext";

const DEFAULT_TTL_MS = 5 * 60 * 1000;
const handoffs = new Map<string, { expiresAt: number; context: GroundedArticleContext }>();

function cloneContext(context: GroundedArticleContext): GroundedArticleContext {
  return JSON.parse(JSON.stringify(context)) as GroundedArticleContext;
}

function purgeExpired(now: number) {
  handoffs.forEach((entry, id) => {
    if (entry.expiresAt <= now) handoffs.delete(id);
  });
}

function opaqueId() {
  if (typeof crypto === "undefined" || !crypto.getRandomValues) {
    throw new Error("Secure grounded-context handoff is unavailable");
  }
  const bytes = crypto.getRandomValues(new Uint8Array(24));
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function createGroundedContextHandoff(
  context: GroundedArticleContext,
  options: { now?: number; ttlMs?: number } = {},
) {
  const now = options.now ?? Date.now();
  purgeExpired(now);
  const id = opaqueId();
  handoffs.set(id, {
    expiresAt: now + Math.max(1, options.ttlMs ?? DEFAULT_TTL_MS),
    context: cloneContext(context),
  });
  return id;
}

export function consumeGroundedContextHandoff(id: string, now = Date.now()) {
  purgeExpired(now);
  const entry = handoffs.get(id);
  if (!entry) return null;
  handoffs.delete(id);
  return cloneContext(entry.context);
}

export function discardGroundedContextHandoff(id: string) {
  handoffs.delete(id);
}
