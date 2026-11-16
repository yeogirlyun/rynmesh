import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { http, HttpResponse } from "msw";
import i18n from "../i18n";
import { server } from "../test/server";
import type { AgentFileArtifact, AgentTask } from "../domain/rynAgent";
import { TaskArtifacts } from "./RynAgentArtifacts";
import "./rynAgentText";

afterEach(async () => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  await act(async () => {
    await i18n.changeLanguage("en");
  });
});

const baseTask: AgentTask = {
  id: "s05-i13-task",
  title: "Generate report and data",
  model_id: "model",
  status: "running",
  revision: 2,
  control_revision: 1,
  scope_revision: 1,
  created_at: 1,
  updated_at: 1,
  pending_approval: null,
  steps: 2,
  result: "",
  error: null,
  messages: [{ role: "user", content: "Generate report" }],
  events: [],
  limits: {},
};

const deliverableChart: AgentFileArtifact = {
  id: "file_chart",
  name: "chart.png",
  path: "chart.png",
  media_type: "image/png",
  preview: "image",
  role: "deliverable",
  status: "current",
  bytes: 4096,
  sha256: "sha-chart",
  receipt_sha256: "sha-chart",
  created_by_task: true,
  revisions: 1,
  goal_revisions: [1],
  mentioned_in_answer: false,
};

const supportingJson: AgentFileArtifact = {
  ...deliverableChart,
  id: "file_json",
  name: "result.json",
  path: "result.json",
  source: "command",
  attribution: "observed_during_command",
  media_type: "application/json",
  preview: "text",
  role: "supporting",
  created_by_task: false,
};

const helperScript: AgentFileArtifact = {
  ...deliverableChart,
  id: "file_helper_py",
  name: "compute.py",
  path: "compute.py",
  media_type: "text/x-python",
  preview: "text",
  role: "helper" as AgentFileArtifact["role"],
};

const dotRynScratch: AgentFileArtifact = {
  ...deliverableChart,
  id: "file_ryn_tmp",
  name: "temp.json",
  path: ".ryn/scratch/temp.json",
  media_type: "application/json",
  preview: "text",
  role: "supporting",
};

describe("S05-I13 live task files, supporting json visibility, and unbacked mentions", () => {
  it("T2: shows current files during execution with in-progress / still-writing badges and lists json in supporting while excluding helper and .ryn/", async () => {
    const user = userEvent.setup();
    await act(async () => {
      await i18n.changeLanguage("zh-CN");
    });

    server.use(
      http.get("*/api/local/agent/tasks/s05-i13-task/artifacts", () =>
        HttpResponse.json({
          artifacts: [],
          files: [deliverableChart, supportingJson, helperScript, dotRynScratch],
          pending_files: true,
          unbacked_mentions: [],
        }),
      ),
    );

    const { container } = render(<TaskArtifacts task={baseTask} />);

    const region = await screen.findByRole("region", { name: "目前的文件" });
    expect(region).toBeVisible();
    expect(screen.getByText("进行中")).toBeVisible();
    expect(screen.getByText("还在写入")).toBeVisible();
    expect(screen.getByRole("button", { name: "打开 chart.png" })).toBeVisible();

    const details = container.querySelector("details.ra-files-supporting");
    expect(details).not.toBeNull();
    expect(details).not.toHaveClass("ra-sr-only");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("本任务的其他文件（1）")).toBeVisible();

    await user.click(screen.getByText("本任务的其他文件（1）"));
    expect(screen.getByRole("button", { name: "打开 result.json" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "打开 compute.py" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打开 temp.json" })).not.toBeInTheDocument();
  });
});
