// A confirmed operation must never be reported as a failure just because the follow-up status
// reload could not be fetched. This helper separates the two awaits so a mutation failure still
// surfaces through onError exactly as before, while a reload failure after a confirmed mutation
// surfaces as a non-error notice instead.
export const RELOAD_FAILURE_NOTICE = "Done on the node. The latest status could not be loaded; refresh to see it.";

export interface RunThenReloadHandlers {
  onError: (cause: unknown) => void | Promise<void>;
  onNotice: (message: string) => void | Promise<void>;
}

export async function runThenReload(
  operation: () => Promise<unknown>,
  reload: () => Promise<unknown>,
  { onError, onNotice }: RunThenReloadHandlers,
): Promise<void> {
  try {
    await operation();
  } catch (cause) {
    await onError(cause);
    return;
  }
  try {
    await reload();
  } catch {
    await onNotice(RELOAD_FAILURE_NOTICE);
  }
}
