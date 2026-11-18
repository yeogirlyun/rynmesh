import { render, screen, cleanup } from "@testing-library/react";
import i18next from "i18next";
import { afterEach, expect, it, vi } from "vitest";
import type { AgentTask } from "../domain/rynAgent";
import RynAgentConnection from "./RynAgentConnection";

afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const base = { id: "i6", title: "test", model_id: "model-a", status: "failed", error: "agent_model_quota_exhausted", events: [] } as unknown as AgentTask;
const props = { modelName: "model-a", busy: false, enabled: true, onAction: vi.fn() };

it("S01-I6 T1/T2 quota failure event shows reset estimate on actual failed task", async () => {
  await i18next.changeLanguage("zh-CN");
  vi.spyOn(Date, "now").mockReturnValue(new Date("2026-11-17T00:00:00Z").getTime());
  render(<RynAgentConnection {...props} task={{ ...base, events: [{ seq: 1, at: 1, type: "failed", error: base.error, diagnostics: { network_reason: "quota", reset_at: Date.now() / 1000 + 90 } }] }} />);
  expect(screen.getByText(/约 90 秒后恢复/)).toBeInTheDocument();
});

it("S01-I6 T1 region block guidance takes precedence over generic 403 authentication", async () => {
  await i18next.changeLanguage("zh-CN");
  render(<RynAgentConnection {...props} task={{ ...base, error: "agent_model_upstream_failed", events: [{ seq: 1, at: 1, type: "failed", diagnostics: { network_reason: "region_blocked", http_status: 403 } }] }} />);
  expect(screen.getByText("该线路在当前地区被拦截，请换线路")).toBeInTheDocument();
});

it("S01-I6 T4 completed task displays model mismatch metadata", async () => {
  await i18next.changeLanguage("zh-CN");
  render(<RynAgentConnection {...props} task={{ ...base, status: "completed", error: null, events: [{ seq: 1, at: 1, type: "model_mismatch", requested_model: "model-a", server_model: "other-model" }] }} />);
  expect(screen.getByText(/model-a.*other-model/)).toBeInTheDocument();
});
