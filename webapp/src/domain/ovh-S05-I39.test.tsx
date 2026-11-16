import { act, render, screen } from "@testing-library/react";
import { useCallback } from "react";
import { afterEach, expect, it, vi } from "vitest";
import i18n from "../i18n";
import { OfficePreview } from "../personal/OfficePreview";
import { type OfficePreviewLoader, readAgentFilePreview } from "./agentFilePreview";
import { AgentAPIError } from "./rynAgent";

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  await i18n.changeLanguage("en");
});

function LegacyPreviewHarness({ taskId, fileId, sha256 }: { taskId: string; fileId: string; sha256: string }) {
  const load = useCallback<OfficePreviewLoader>(
    (signal, options) => readAgentFilePreview(taskId, { id: fileId, sha256 }, signal, options),
    [taskId, fileId, sha256],
  );
  return <OfficePreview load={load} />;
}

it("T9: polls every 2 seconds on 202 converting while showing 正在转换… and renders preview on 200", async () => {
  await i18n.changeLanguage("zh-CN");
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });

  let attempt = 0;
  const fetchMock = vi.fn(async () => {
    attempt += 1;
    if (attempt <= 2) {
      return new Response(JSON.stringify({ status: "converting" }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(
      JSON.stringify({
        kind: "document",
        truncated: false,
        sha256: "sha_legacy_doc",
        blocks: [{ type: "paragraph", style: "", runs: [{ text: "转换后的合同内容", bold: false, italic: false }] }],
      }),
      { status: 200, headers: { "Content-Type": "application/json" } },
    );
  });
  vi.stubGlobal("fetch", fetchMock);

  render(<LegacyPreviewHarness taskId="task_39" fileId="file_doc" sha256="sha_legacy_doc" />);

  // Flush initial fetch -> 202 {"status": "converting"}
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
  expect(fetchMock).toHaveBeenCalledTimes(1);
  expect(screen.getByRole("status")).toHaveTextContent("正在转换…");

  // Advance 2000ms -> 2nd poll still 202
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(fetchMock).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("status")).toHaveTextContent("正在转换…");

  // Advance another 2000ms -> 3rd poll returns 200 and renders document content
  await act(async () => {
    await vi.advanceTimersByTimeAsync(2000);
  });
  expect(fetchMock).toHaveBeenCalledTimes(3);
  expect(screen.getByText("转换后的合同内容")).toBeInTheDocument();
});

it("T9: stops polling after 3 minutes of 202 converting with agent_preview_timeout", async () => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });

  const fetchMock = vi.fn(async () =>
    new Response(JSON.stringify({ status: "converting" }), {
      status: 202,
      headers: { "Content-Type": "application/json" },
    }),
  );
  vi.stubGlobal("fetch", fetchMock);

  const controller = new AbortController();
  const promise = readAgentFilePreview("task_39", { id: "file_slow", sha256: "sha_slow" }, controller.signal);
  const caught = promise.catch(err => err);

  await act(async () => {
    await vi.advanceTimersByTimeAsync(180_000 + 2_000);
  });

  const error = await caught;
  expect(error).toBeInstanceOf(AgentAPIError);
  expect((error as AgentAPIError).code).toBe("agent_preview_timeout");
  // 1 initial + 90 polls at 2s intervals across 180s
  expect(fetchMock.mock.calls.length).toBeGreaterThanOrEqual(90);
});
