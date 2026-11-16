import { expect, it } from "vitest";
import { agentErrorHelp } from "./rynAgentErrors";

it("S01-I24 prompts checking proxy settings when terminal CLI works but Ryn connection fails", () => {
  const zh = agentErrorHelp("agent_model_connection_failed", true);
  const en = agentErrorHelp("agent_model_connection_failed", false);
  expect(zh.text).toContain("终端能用而 Ryn 不能用时，检查代理设置");
  expect(en.text.toLowerCase()).toContain("proxy");
});
