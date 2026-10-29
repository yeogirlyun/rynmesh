// Only the envelope's transport prefix changes. Signature verification remains
// in the local node's explicit invitation review, before any peer contact.
export function normalizeInviteLink(value: string): string | null {
  if (value.length > 16384) return null;
  const match = /^(?:ryn|rynmesh):\/\/join\/([A-Za-z0-9_-]+)$/.exec(value);
  return match ? `rynmesh://join/${match[1]}` : null;
}

export interface DeepLinkSource {
  getCurrent(): Promise<string[] | null>;
  onOpenUrl(callback: (urls: string[]) => void): Promise<() => void>;
}

export function subscribeInviteLinks(source: DeepLinkSource, receive: (invite: string) => void, invalid: () => void): () => void {
  let active = true;
  let stop: (() => void) | undefined;
  let receivedEvent = false;
  const accept = (urls: string[] | null) => {
    if (!active || !urls?.length) return;
    // Never silently select one recipient from a batch of different invitations.
    if (urls.length !== 1) { invalid(); return; }
    const invite = normalizeInviteLink(urls[0]);
    if (invite) receive(invite); else invalid();
  };
  void source.onOpenUrl((urls) => { receivedEvent = true; accept(urls); }).then(async (unlisten) => {
    stop = unlisten;
    if (!active) { unlisten(); return; }
    const current = await source.getCurrent();
    if (!receivedEvent) accept(current);
  }).catch(() => { if (active) invalid(); });
  return () => { active = false; stop?.(); };
}
