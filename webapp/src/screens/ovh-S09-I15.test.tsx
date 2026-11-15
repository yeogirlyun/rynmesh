import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { MemoryEditor } from "./RynAgentMemory";

const DEST_ALPHA = {
  catalog_model_id: "claude-4-sonnet",
  provider_peer_id: "local_node",
  source_id: "src-anthropic",
  destination_revision: "rev-a1",
};

it("S09-I15: renders background learning daily usage (local timezone note), recent failure reason, and next retry time from capture_status", async () => {
  const retryEpochSeconds = 1_763_200_800;
  server.use(
    http.get("*/agent/memory/destinations", () =>
      HttpResponse.json({
        destinations: [
          {
            label: "Claude 4 Sonnet",
            destination: DEST_ALPHA,
            available: true,
          },
        ],
        default_destination: DEST_ALPHA,
      }),
    ),
    http.get("*/agent/memory", () =>
      HttpResponse.json({
        schema_version: 1,
        generation: 3,
        reset_epoch: 0,
        policy_revision: 1,
        settings: {
          capture_enabled: true,
          use_enabled: true,
          activation_policy: "automatic",
          extraction_model_id: DEST_ALPHA.catalog_model_id,
          extraction_destination: DEST_ALPHA,
          default_exposure: { mode: "selected_models", allowed_destinations: [DEST_ALPHA] },
          budgets: {
            max_daily_extraction_calls: 32,
            max_daily_extraction_tokens: 65536,
          },
        },
        entries: [],
        candidates: [],
        tombstone_count: 0,
        suppressed_slot_count: 0,
        capture_status: {
          today_day: "2026-11-15",
          daily_calls_used: 4,
          daily_calls_budget: 32,
          daily_tokens_used: 3200,
          daily_tokens_budget: 65536,
          queued_count: 1,
          failed_count: 1,
          last_error: "extraction_timeout",
          next_retry_at: retryEpochSeconds,
          recent_jobs: [
            {
              id: "mjob_1",
              state: "queued",
              last_error: "extraction_timeout",
              retry_at: retryEpochSeconds,
              updated_at: retryEpochSeconds - 120,
            },
          ],
        },
      }),
    ),
  );

  render(<MemoryEditor initialOpen />);

  const statusLine = await screen.findByText(/后台学习：今日 4\/32|Background learning: today 4\/32/);
  expect(statusLine).toBeInTheDocument();
  expect(statusLine.textContent).toMatch(/按本机时区计日|local time zone/i);
  expect(statusLine.textContent).toMatch(/extraction_timeout/);
  expect(statusLine.textContent).toMatch(/下次重试|next retry/i);
});
