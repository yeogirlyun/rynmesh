import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { http, HttpResponse } from "msw";
import { server } from "../test/server";
import { CodexSkillsView } from "./CodexSkillsView";
import { normalizeOverview } from "../domain/agentExtensions";

function buildOverview(revision: number, extraSkills: Record<string, unknown>[] = []) {
  return {
    revision,
    generation: revision,
    updated_at: "2026-11-16T12:00:00Z",
    desktop_access: false,
    windows_supported: true,
    skill_roots: [
      { priority: 1, path: "/workspace/repo/.ryn/skills", scope: "repo", workspace_root: "/workspace/repo" },
      { priority: 2, path: "/workspace/repo/.agents/skills", scope: "repo", workspace_root: "/workspace/repo" },
      { priority: 3, path: "~/.ryn/skills", scope: "user", workspace_root: null },
    ],
    plugins: [],
    mcp_servers: [],
    skills: [
      {
        id: "alpha-skill",
        name: "alpha-skill",
        title: "Alpha Skill",
        description: "Initial alpha workflow",
        version: "1.0.0",
        revision: 1,
        content: "# Alpha",
        builtin: false,
        deletable: true,
        enabled: true,
        component_enabled: true,
        parent_enabled: true,
        source_format: "codex_local",
        scope: "repo",
        source_missing: false,
        plugin_id: null,
        files: [{ path: "SKILL.md", size: 7 }],
        warnings: [],
      },
      ...extraSkills,
    ],
  };
}

describe("S09-I4 CodexSkillsView focus generation reload, skill roots priority, and source_missing UI", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("checks generation when the controlled skills page becomes visible", async () => {
    const changed = vi.fn();
    server.use(
      http.get("*/api/local/agent/extensions", () => HttpResponse.json(buildOverview(12))),
      http.get("*/api/local/agent/extensions/skills/recommended", () => HttpResponse.json({ skills: [], count: 0, refreshed: false })),
    );
    render(<MemoryRouter><CodexSkillsView overview={normalizeOverview(buildOverview(10))} onOverviewChange={changed} /></MemoryRouter>);
    await waitFor(() => expect(changed).toHaveBeenCalledWith(expect.objectContaining({ generation: 12 })));
  });

  it("displays skill discovery roots with priority, marks source_missing skills, and reloads on focus when generation changes", async () => {
    let currentRevision = 10;
    let extra: Record<string, unknown>[] = [
      {
        id: "deleted-skill",
        name: "deleted-skill",
        title: "Deleted Repo Skill",
        description: "Removed from disk",
        version: "1.0.0",
        revision: 2,
        content: "# Deleted",
        builtin: false,
        deletable: true,
        enabled: false,
        component_enabled: true,
        parent_enabled: true,
        source_format: "codex_local",
        scope: "repo",
        source_missing: true,
        plugin_id: null,
        files: [{ path: "SKILL.md", size: 9 }],
        warnings: [],
      },
    ];

    let overviewFetches = 0;
    let deletedSkillIds: string[] = [];
    server.use(
      http.get("*/api/local/agent/extensions", () => {
        overviewFetches += 1;
        return HttpResponse.json(
          buildOverview(
            currentRevision,
            extra.filter(item => !deletedSkillIds.includes(String(item.id))),
          ),
        );
      }),
      http.get("*/api/local/agent/extensions/skills/recommended", () => {
        return HttpResponse.json({ skills: [], count: 0, refreshed: false });
      }),
      http.delete("*/api/local/agent/extensions/skills/:skillId", ({ params }) => {
        deletedSkillIds = [...deletedSkillIds, String(params.skillId)];
        currentRevision += 1;
        return HttpResponse.json({ deleted: true, id: String(params.skillId), revision: currentRevision });
      }),
    );

    render(
      <MemoryRouter>
        <CodexSkillsView />
      </MemoryRouter>,
    );

    // Displays initial skill, source_missing badge, and discovery roots with priority
    expect(await screen.findByText("Alpha Skill")).toBeTruthy();
    expect(screen.getByText(/源目录已删除|Source directory deleted/)).toBeTruthy();
    expect(screen.getByText("#1")).toBeTruthy();
    expect(screen.getByText("#2")).toBeTruthy();
    expect(screen.getByText("/workspace/repo/.ryn/skills")).toBeTruthy();
    expect(screen.getByText("~/.ryn/skills")).toBeTruthy();

    const fetchesAfterMount = overviewFetches;

    // Focus when generation has NOT changed keeps existing list without extra reload churn
    fireEvent.focus(window);
    await waitFor(() => {
      expect(overviewFetches).toBeGreaterThan(fetchesAfterMount);
    });
    expect(screen.queryByText("Newly Synced Skill")).toBeNull();

    // Backend task sync bumps overview generation/revision -> focus reloads and shows the new skill!
    currentRevision = 11;
    extra = [
      ...extra,
      {
        id: "newly-synced",
        name: "newly-synced",
        title: "Newly Synced Skill",
        description: "Auto-discovered during task start",
        version: "1.0.0",
        revision: 3,
        content: "# Newly Synced",
        builtin: false,
        deletable: true,
        enabled: true,
        component_enabled: true,
        parent_enabled: true,
        source_format: "codex_local",
        scope: "repo",
        source_missing: false,
        plugin_id: null,
        files: [{ path: "SKILL.md", size: 14 }],
        warnings: [],
      },
    ];
    fireEvent.focus(window);
    expect(await screen.findByText("Newly Synced Skill")).toBeTruthy();

    // Manual uninstall of source_missing skill removes it cleanly
    const uninstallBtn = screen.getByRole("button", {
      name: /卸载已缺失技能 Deleted Repo Skill|Uninstall missing skill Deleted Repo Skill/,
    });
    fireEvent.click(uninstallBtn);
    const confirmBtn = await screen.findByRole("button", { name: /确认移除|Confirm remove/ });
    fireEvent.click(confirmBtn);
    await waitFor(() => {
      expect(deletedSkillIds).toContain("deleted-skill");
    });
    await waitFor(() => {
      expect(screen.queryByText("Deleted Repo Skill")).toBeNull();
    });
  });
});
