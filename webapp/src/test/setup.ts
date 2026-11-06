import "@testing-library/jest-dom/vitest";
import "fake-indexeddb/auto";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach } from "vitest";
import i18n from "../i18n";
import { server } from "./server";
import { resetAgentWorkspaceMemory } from "../domain/agentWorkspaceCache";

(globalThis as { __rynAgentRecordsDisabled?: boolean }).__rynAgentRecordsDisabled = true;

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(() => { void i18n.changeLanguage("en"); });

afterEach(() => {
  cleanup();
  server.resetHandlers();
  resetAgentWorkspaceMemory();
});

afterAll(() => server.close());

Object.defineProperty(window, "requestAnimationFrame", {
  configurable: true,
  value: (callback: FrameRequestCallback) => window.setTimeout(callback, 0),
});

Object.defineProperty(HTMLElement.prototype, "scrollTo", {
  configurable: true,
  value: () => undefined,
});
