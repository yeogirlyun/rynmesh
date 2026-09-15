import { expect, it, vi } from "vitest";
import { RELOAD_FAILURE_NOTICE, runThenReload } from "./actThenReload";

it("calls neither handler when the mutation and the reload both resolve", async () => {
  const operation = vi.fn().mockResolvedValue(undefined);
  const reload = vi.fn().mockResolvedValue(undefined);
  const onError = vi.fn();
  const onNotice = vi.fn();
  await runThenReload(operation, reload, { onError, onNotice });
  expect(operation).toHaveBeenCalledTimes(1);
  expect(reload).toHaveBeenCalledTimes(1);
  expect(onError).not.toHaveBeenCalled();
  expect(onNotice).not.toHaveBeenCalled();
});

it("reports the mutation failure through onError and never reloads", async () => {
  const failure = new Error("mutation failed");
  const operation = vi.fn().mockRejectedValue(failure);
  const reload = vi.fn().mockResolvedValue(undefined);
  const onError = vi.fn();
  const onNotice = vi.fn();
  await runThenReload(operation, reload, { onError, onNotice });
  expect(operation).toHaveBeenCalledTimes(1);
  expect(reload).not.toHaveBeenCalled();
  expect(onError).toHaveBeenCalledExactlyOnceWith(failure);
  expect(onNotice).not.toHaveBeenCalled();
});

it("reports a confirmed mutation whose reload fails through onNotice, never onError", async () => {
  const operation = vi.fn().mockResolvedValue(undefined);
  const reload = vi.fn().mockRejectedValue(new Error("reload failed"));
  const onError = vi.fn();
  const onNotice = vi.fn();
  await runThenReload(operation, reload, { onError, onNotice });
  expect(operation).toHaveBeenCalledTimes(1);
  expect(reload).toHaveBeenCalledTimes(1);
  expect(onError).not.toHaveBeenCalled();
  expect(onNotice).toHaveBeenCalledExactlyOnceWith(RELOAD_FAILURE_NOTICE);
});
