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

function makeSnapshot(memoryBytes: number, memoryBytesLimit = 262144) {
  return {
    schema_version: 1,
    generation: 2,
    reset_epoch: 0,
    policy_revision: 1,
    memory_bytes: memoryBytes,
    memory_bytes_limit: memoryBytesLimit,
    settings: {
      capture_enabled: false,
      use_enabled: true,
      activation_policy: "automatic",
      extraction_model_id: "",
      extraction_destination: null,
      default_exposure: { mode: "selected_models", allowed_destinations: [] },
      budgets: {
        max_memory_bytes: memoryBytesLimit,
      },
    },
    entries: [],
    candidates: [],
    tombstone_count: 0,
    suppressed_slot_count: 0,
  };
}

it("S09-I33: warns '记忆账本接近上限，可重置' when memory_bytes >= 80% of memory_bytes_limit", async () => {
  server.use(
    http.get("*/agent/memory/destinations", () =>
      HttpResponse.json({
        destinations: [{ label: "Claude 4 Sonnet", destination: DEST_ALPHA, available: true }],
        default_destination: DEST_ALPHA,
      }),
    ),
    http.get("*/agent/memory", () => HttpResponse.json(makeSnapshot(215_000, 262_144))),
  );

  const view = render(<MemoryEditor initialOpen />);
  const banner = await screen.findByText(/记忆账本接近上限，可重置|Memory ledger is approaching capacity/);
  expect(banner).toBeInTheDocument();
  view.unmount();

  server.use(
    http.get("*/agent/memory", () => HttpResponse.json(makeSnapshot(50_000, 262_144))),
  );
  render(<MemoryEditor initialOpen />);
  await screen.findByRole("checkbox", { name: /在对话中使用已保存记忆|Use saved memory in tasks/ });
  expect(screen.queryByText(/记忆账本接近上限，可重置|Memory ledger is approaching capacity/)).toBeNull();
});
