const FRIEND_DEEP_LINK_EVENT = "ryn-friend-deep-link";
const FRIEND_LINK_PREFIX = "rynmesh://join/";
const MAX_INVITE_LINK_LENGTH = 16_384;
let pendingFriendInvite: string | null = null;

export function validatedFriendInviteLink(raw: unknown): string | null {
  if (typeof raw !== "string" || raw !== raw.trim()) return null;
  if (!raw.startsWith(FRIEND_LINK_PREFIX) || raw.length > MAX_INVITE_LINK_LENGTH) return null;
  const token = raw.slice(FRIEND_LINK_PREFIX.length);
  return token && /^[A-Za-z0-9_-]+$/.test(token) ? raw : null;
}

export function queueFriendInviteDeepLink(raw: unknown): boolean {
  const link = validatedFriendInviteLink(raw);
  if (!link) return false;
  pendingFriendInvite = link;
  window.dispatchEvent(new Event(FRIEND_DEEP_LINK_EVENT));
  return true;
}

export function consumeFriendInviteDeepLink(): string | null {
  const link = pendingFriendInvite;
  pendingFriendInvite = null;
  return link;
}

export function onFriendInviteDeepLink(listener: () => void): () => void {
  window.addEventListener(FRIEND_DEEP_LINK_EVENT, listener);
  return () => window.removeEventListener(FRIEND_DEEP_LINK_EVENT, listener);
}

export async function installDesktopFriendDeepLinks(
  onLink: (link: string) => void,
): Promise<() => void> {
  if (!("__TAURI_INTERNALS__" in window)) return () => undefined;
  const { getCurrent, onOpenUrl } = await import("@tauri-apps/plugin-deep-link");
  const accept = (urls: string[] | null) => {
    for (const raw of urls ?? []) {
      const link = validatedFriendInviteLink(raw);
      if (link) {
        onLink(link);
        return;
      }
    }
  };
  accept(await getCurrent().catch(() => null));
  return onOpenUrl(accept);
}
