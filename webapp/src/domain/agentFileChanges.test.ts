import { afterEach, describe, expect, it, vi } from "vitest";
import { agentFileChanges } from "./agentFileChanges";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("S05-I11 agentFileChanges request error & timeout handling", () => {
  it("T5: reports HTTP 502 status on non-JSON HTML error responses, preserves JSON detail, and aborts after 30s timeout", async () => {
    // 1) 502 Bad Gateway returning HTML body instead of JSON
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<html><body><h1>502 Bad Gateway</h1></body></html>", {
          status: 502,
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );
    await expect(agentFileChanges.review("task-502")).rejects.toThrow(/502/);

    // 2) Non-OK JSON response preserves detail
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "agent_review_pause_task_first" }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
    await expect(agentFileChanges.preview("task-409")).rejects.toThrow("agent_review_pause_task_first");

    // 3) Hung request aborts after 30s via AbortController and reports timeout
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    let capturedSignal: AbortSignal | null | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
        capturedSignal = init?.signal;
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => {
            reject(new DOMException("The operation was aborted.", "AbortError"));
          });
        });
      }),
    );

    const pending = agentFileChanges.review("task-timeout");
    const assertion = expect(pending).rejects.toThrow(/timeout/i);
    await vi.advanceTimersByTimeAsync(30000);
    await assertion;
    expect(capturedSignal?.aborted).toBe(true);
  });
});
