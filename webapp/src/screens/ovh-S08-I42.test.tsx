import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n";
import { normalizeMcpServer } from "../domain/agentExtensions";
import { McpDataPolicy, McpDataPolicySummary } from "./McpDataPolicy";

const hash = "a".repeat(64);
const candidates = [
  { name: "read_items", title: "Read items", schema_sha256: hash },
];

function makeServer(extra: Record<string, unknown> = {}) {
  return normalizeMcpServer({
    id: "fixture-s08-i42",
    instance_id: "inst-1",
    revision: 3,
    discovery_revision: 2,
    transport: "streamable_http",
    enabled: true,
    server_hint_candidates: candidates,
    data_policy: { models: { mode: "any" }, memory: "allow" },
    ...extra,
  });
}

describe("S08-I42 McpDataPolicy copy and missing policy state", () => {
  afterEach(async () => {
    await i18n.changeLanguage("en");
  });

  it("T5: read-only hint description and confirm dialog explain both confirmation mode and automatic mode (zh & en)", async () => {
    await i18n.changeLanguage("zh-CN");
    const user = userEvent.setup();
    const view = render(<McpDataPolicy server={makeServer()} onRefresh={vi.fn()} />);

    const expectedZh = "确认模式下，确认后这些工具不再每次询问；自动处理模式本来就不询问，确认只读只影响并行和出错时是否中断任务";
    expect(screen.getByText((content) => content.includes(expectedZh))).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "检查只读设置" }));
    const zhDialog = screen.getByRole("alertdialog", { name: "确认只读工具" });
    expect(within(zhDialog).getByText((content) => content.includes(expectedZh))).toBeInTheDocument();

    view.unmount();
    await i18n.changeLanguage("en");
    render(<McpDataPolicy server={makeServer()} onRefresh={vi.fn()} />);
    expect(screen.getByText(/automatic mode/i)).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /Read items/ }));
    await user.click(screen.getByRole("button", { name: "Review read-only settings" }));
    const enDialog = screen.getByRole("alertdialog", { name: "Confirm read-only tools" });
    expect(within(enDialog).getByText(/automatic mode/i)).toBeInTheDocument();
  });

  it("T6: missing policy renders unknown status prompt and never renders unrestricted/allow defaults (zh & en)", async () => {
    await i18n.changeLanguage("zh-CN");
    const zhView = render(
      <McpDataPolicy
        server={makeServer({ data_policy: undefined, server_hint_candidates: [] })}
        onRefresh={vi.fn()}
      />
    );
    const zhStatus = screen.getByRole("status");
    expect(zhStatus).toHaveTextContent("数据策略未知，请刷新");
    expect(document.body.textContent).not.toContain("不限制");
    expect(document.body.textContent).not.toContain("长期记忆：允许");
    zhView.unmount();

    await i18n.changeLanguage("en");
    render(<McpDataPolicySummary policy={undefined} />);
    const enStatus = screen.getByRole("status");
    expect(enStatus).toHaveTextContent(/policy unknown/i);
    expect(document.body.textContent).not.toContain("unrestricted");
    expect(document.body.textContent).not.toContain("Long-term memory: allowed");
  });
});
