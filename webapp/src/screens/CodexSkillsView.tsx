import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  AlertTriangle,
  BookOpen,
  Check,
  ChevronDown,
  ChevronUp,
  Code2,
  Copy,
  ExternalLink,
  Eye,
  FileText,
  FolderOpen,
  Heart,
  Layers,
  LoaderCircle,
  MessageSquare,
  MoreHorizontal,
  PenTool,
  Play,
  Plus,
  Radar,
  RefreshCw,
  Search,
  Sparkles,
  Terminal,
  Trash2,
  Upload,
  X,
  Zap,
} from "lucide-react";
import {
  agentExtensions,
  expectedSkillRevision,
  isRevisionConflictError,
  type ExtensionOverview,
  type ExtensionPlugin,
  type ExtensionSkill,
  type ExtensionSkillInterface,
  type RecommendedSkill,
} from "../domain/agentExtensions";
import "./CodexSkillsView.css";

export const PENDING_SKILL_COMPOSER_PREFILL_KEY = "ryn_agent_pending_skill_prefill_v1";

export interface PendingSkillComposerPrefill {
  prompt: string;
  skillId?: string;
  createdAt: number;
}

export function writePendingSkillPrefill(prefill: PendingSkillComposerPrefill): void {
  try {
    window.sessionStorage.setItem(PENDING_SKILL_COMPOSER_PREFILL_KEY, JSON.stringify(prefill));
  } catch {
    // Ignore storage quota errors
  }
}

export function consumePendingSkillPrefill(): PendingSkillComposerPrefill | null {
  try {
    const raw = window.sessionStorage.getItem(PENDING_SKILL_COMPOSER_PREFILL_KEY);
    if (!raw) return null;
    window.sessionStorage.removeItem(PENDING_SKILL_COMPOSER_PREFILL_KEY);
    const parsed = JSON.parse(raw) as PendingSkillComposerPrefill;
    if (!parsed || typeof parsed.prompt !== "string") return null;
    return parsed;
  } catch {
    return null;
  }
}

interface SuggestedWorkflowIdea {
  id: string;
  titleZh: string;
  titleEn: string;
  descZh: string;
  descEn: string;
  iconCategory: string;
}

const SUGGESTED_WORKFLOW_IDEAS: SuggestedWorkflowIdea[] = [
  {
    id: "suggest-pr-review",
    titleZh: "自动化代码变更审查与回归验证",
    titleEn: "Automated PR Review & Regression Check",
    descZh: "检查当前分支改动、运行关键测试并输出结构化审查报告",
    descEn: "Inspect branch diffs, run targeted tests, and summarize actionable findings",
    iconCategory: "code",
  },
  {
    id: "suggest-release-notes",
    titleZh: "根据提交历史生成发布说明与日报",
    titleEn: "Generate Release Notes & Daily Summary",
    descZh: "汇总近期提交记录与任务产出，生成标准格式的团队周报或版本说明",
    descEn: "Collect recent commits and task outputs into structured release notes",
    iconCategory: "pen",
  },
];

function useSkillsText() {
  const { i18n } = useTranslation();
  const isZh = (i18n.resolvedLanguage || i18n.language || "").toLowerCase().startsWith("zh");
  return useCallback((zh: string, en: string) => (isZh ? zh : en), [isZh]);
}

function inferSkillIconCategory(id: string, title: string, description: string): string {
  const text = `${id} ${title} ${description}`.toLowerCase();
  if (text.includes("pdf") || text.includes("doc") || text.includes("report")) return "pdf";
  if (text.includes("deploy") || text.includes("cloudflare") || text.includes("vercel") || text.includes("netlify") || text.includes("server")) return "bolt";
  if (text.includes("security") || text.includes("threat") || text.includes("audit") || text.includes("review")) return "radar";
  if (text.includes("figma") || text.includes("design") || text.includes("ui") || text.includes("image") || text.includes("write")) return "pen";
  if (text.includes("search") || text.includes("query") || text.includes("diagnostics")) return "search";
  if (text.includes("chat") || text.includes("comment") || text.includes("speech") || text.includes("transcribe")) return "chat";
  if (text.includes("orchestrat") || text.includes("workflow") || text.includes("hierarchy") || text.includes("plan")) return "hierarchy";
  if (text.includes("cli") || text.includes("code") || text.includes("playwright") || text.includes("git") || text.includes("ci")) return "code";
  if (text.includes("creator") || text.includes("installer") || text.includes("magic")) return "sparkles";
  return "default";
}

function SkillAvatar({
  id,
  title,
  description,
  iface,
  size = "card",
}: {
  id: string;
  title: string;
  description: string;
  iface?: ExtensionSkillInterface;
  size?: "card" | "modal";
}) {
  const iconUrl =
    size === "modal"
      ? iface?.icon_large || iface?.icon_small
      : iface?.icon_small || iface?.icon_large;
  const brandColor = iface?.brand_color || undefined;
  const category = useMemo(
    () => inferSkillIconCategory(id, title, description),
    [id, title, description],
  );
  const iconPx = size === "modal" ? 22 : 18;

  if (iconUrl) {
    return (
      <div
        className={size === "modal" ? "codex-skill-modal-icon" : "codex-skill-icon-box"}
        style={brandColor ? { borderColor: `${brandColor}33` } : undefined}
      >
        <img
          src={iconUrl}
          alt=""
          className="codex-skill-icon-img"
          loading="lazy"
        />
      </div>
    );
  }

  return (
    <div
      className={size === "modal" ? "codex-skill-modal-icon" : "codex-skill-icon-box"}
      style={
        brandColor
          ? {
              color: brandColor,
              background: `${brandColor}14`,
              borderColor: `${brandColor}30`,
            }
          : undefined
      }
    >
      {category === "pdf" ? (
        <FileText size={iconPx} />
      ) : category === "bolt" ? (
        <Zap size={iconPx} />
      ) : category === "radar" ? (
        <Radar size={iconPx} />
      ) : category === "pen" ? (
        <PenTool size={iconPx} />
      ) : category === "search" ? (
        <Search size={iconPx} />
      ) : category === "chat" ? (
        <MessageSquare size={iconPx} />
      ) : category === "hierarchy" ? (
        <Layers size={iconPx} />
      ) : category === "code" ? (
        <Code2 size={iconPx} />
      ) : category === "sparkles" ? (
        <Sparkles size={iconPx} />
      ) : category === "heart" ? (
        <Heart size={iconPx} />
      ) : (
        <BookOpen size={iconPx} />
      )}
    </div>
  );
}

function formatScopeLabel(
  scope: string | undefined,
  builtin: boolean,
  tx: (zh: string, en: string) => string,
): string {
  if (builtin) return tx("内置", "Built-in");
  switch ((scope || "user").toLowerCase()) {
    case "system":
      return tx("系统", "System");
    case "repo":
      return tx("项目", "Repo");
    case "plugin":
      return tx("插件", "Plugin");
    case "curated":
      return tx("官方精选", "Curated");
    default:
      return tx("用户", "User");
  }
}

export interface CodexSkillsViewProps {
  /** Optional controlled overview from AgentExtensions parent */
  overview?: ExtensionOverview | null;
  /** Optional busy ID map from AgentExtensions parent */
  busyIds?: Record<string, boolean>;
  /** Optional flag blocking creation/import when revision is stale */
  createActionsBlocked?: boolean;
  /** Optional controlled skill toggle callback */
  onToggleSkill?: (skill: ExtensionSkill, nextEnabled: boolean) => void | Promise<void>;
  /** Optional controlled skill uninstall callback (opens parent confirm dialog) */
  onRequestRemoveSkill?: (skill: ExtensionSkill) => void;
  /** Optional callback to open AgentExtensions Import Skill modal */
  onOpenImportModal?: () => void;
  /** Optional callback when overview is updated after refresh/install */
  onOverviewChange?: (nextOverview: ExtensionOverview) => void;
  /** Optional callback when user clicks "Try now" or "+ New skill" */
  onTrySkillPrompt?: (prompt: string, skillId?: string) => void;
  /** True when rendered as a standalone full-pane view inside RynAgent */
  embeddedInAgent?: boolean;
}

export function CodexSkillsView({
  overview: controlledOverview,
  busyIds: controlledBusyIds,
  createActionsBlocked = false,
  onToggleSkill: controlledToggleSkill,
  onRequestRemoveSkill: controlledRequestRemoveSkill,
  onOpenImportModal,
  onOverviewChange,
  onTrySkillPrompt,
  embeddedInAgent = false,
}: CodexSkillsViewProps) {
  const tx = useSkillsText();
  const navigate = useNavigate();
  const searchInputRef = useRef<HTMLInputElement | null>(null);

  const [localOverview, setLocalOverview] = useState<ExtensionOverview | null>(null);
  const [localBusyIds, setLocalBusyIds] = useState<Record<string, boolean>>({});
  const [recommended, setRecommended] = useState<RecommendedSkill[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [installingIds, setInstallingIds] = useState<Record<string, boolean>>({});
  const [notice, setNotice] = useState<{ tone: "info" | "error"; text: string } | null>(null);

  const [searchQuery, setSearchQuery] = useState("");
  const [scopeFilter, setScopeFilter] = useState<"all" | "user" | "system" | "repo" | "plugin">("all");
  const [showAllRecommended, setShowAllRecommended] = useState(false);
  const [hideSuggestions, setHideSuggestions] = useState(false);

  const [selectedSkillId, setSelectedSkillId] = useState<string | null>(null);
  const [selectedRecommended, setSelectedRecommended] = useState<RecommendedSkill | null>(null);
  const [confirmRemoveSkill, setConfirmRemoveSkill] = useState<ExtensionSkill | null>(null);

  const overview = controlledOverview !== undefined ? controlledOverview : localOverview;
  const busyIds = controlledBusyIds ?? localBusyIds;

  const updateOverview = useCallback(
    (next: ExtensionOverview) => {
      if (onOverviewChange) {
        onOverviewChange(next);
      } else {
        setLocalOverview(next);
      }
    },
    [onOverviewChange],
  );

  // Initial load of recommended skills + automatic local Codex skill discovery
  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    async function init() {
      try {
        if (controlledOverview === undefined) {
          const ov = await agentExtensions.overview(controller.signal);
          if (active) setLocalOverview(ov);
        }
        const rec = await agentExtensions.recommendedSkills(false, controller.signal);
        if (active) setRecommended(rec);
      } catch {
        // Ignore initial fetch errors in test environments without mocked /skills/recommended
      }
    }

    void init();
    return () => {
      active = false;
      controller.abort();
    };
  }, [controlledOverview]);

  // Ctrl+F / Cmd+F keyboard shortcut to focus search input (matching Codex skills-page-cc58e762964c.js)
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
        if (searchInputRef.current && document.activeElement !== searchInputRef.current) {
          event.preventDefault();
          searchInputRef.current.focus();
          searchInputRef.current.select();
        }
      }
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const handleRefreshAll = useCallback(async () => {
    setRefreshing(true);
    setNotice(null);
    try {
      const [nextOverview, nextRecommended] = await Promise.all([
        agentExtensions.refreshSkills(),
        agentExtensions.recommendedSkills(true).catch(() => recommended),
      ]);
      updateOverview(nextOverview);
      setRecommended(nextRecommended);
    } catch {
      try {
        const fallbackOverview = await agentExtensions.overview();
        updateOverview(fallbackOverview);
      } catch {
        setNotice({
          tone: "error",
          text: tx("刷新技能列表失败，请稍后重试。", "Failed to refresh skills."),
        });
      }
    } finally {
      setRefreshing(false);
    }
  }, [recommended, tx, updateOverview]);

  const handleToggleSkill = useCallback(
    async (skill: ExtensionSkill, nextEnabled: boolean) => {
      if (controlledToggleSkill) {
        await controlledToggleSkill(skill, nextEnabled);
        return;
      }
      const busyKey = `skill:${skill.id}`;
      setLocalBusyIds(prev => ({ ...prev, [busyKey]: true }));
      try {
        const updated = await agentExtensions.updateSkill(skill.id, {
          enabled: nextEnabled,
          expected_revision: expectedSkillRevision(skill),
        });
        setLocalOverview(prev =>
          prev
            ? {
                ...prev,
                skills: prev.skills.map(item => (item.id === updated.id ? updated : item)),
              }
            : prev,
        );
      } catch (err) {
        if (isRevisionConflictError(err)) {
          const fresh = await agentExtensions.overview();
          setLocalOverview(fresh);
        }
        setNotice({
          tone: "error",
          text: tx("更新技能开关失败。", "Failed to update skill state."),
        });
      } finally {
        setLocalBusyIds(prev => ({ ...prev, [busyKey]: false }));
      }
    },
    [controlledToggleSkill, tx],
  );

  const handleInstallRecommended = useCallback(
    async (rec: RecommendedSkill) => {
      setInstallingIds(prev => ({ ...prev, [rec.id]: true }));
      setNotice(null);
      try {
        const res = await agentExtensions.installRecommendedSkill({
          skill_id: rec.id,
          repo_path: rec.repo_path,
        });
        updateOverview(res.overview);
        setRecommended(prev =>
          prev.map(item => (item.id === rec.id ? { ...item, installed: true } : item)),
        );
        if (selectedRecommended?.id === rec.id) {
          setSelectedRecommended(null);
          setSelectedSkillId(res.skill.id);
        }
        setNotice({
          tone: "info",
          text: tx(`已安装并启用技能「${rec.title}」。`, `Installed and enabled "${rec.title}".`),
        });
      } catch {
        setNotice({
          tone: "error",
          text: tx(`安装技能「${rec.title}」失败。`, `Failed to install "${rec.title}".`),
        });
      } finally {
        setInstallingIds(prev => ({ ...prev, [rec.id]: false }));
      }
    },
    [selectedRecommended?.id, tx, updateOverview],
  );

  const handleConfirmStandaloneDelete = useCallback(async () => {
    if (!confirmRemoveSkill) return;
    const target = confirmRemoveSkill;
    const busyKey = `skill:${target.id}`;
    setLocalBusyIds(prev => ({ ...prev, [busyKey]: true }));
    try {
      await agentExtensions.deleteSkill(target.id, expectedSkillRevision(target));
      const nextOv = await agentExtensions.overview();
      updateOverview(nextOv);
      setRecommended(prev =>
        prev.map(item => (item.id === target.id ? { ...item, installed: false } : item)),
      );
      setConfirmRemoveSkill(null);
      if (selectedSkillId === target.id) {
        setSelectedSkillId(null);
      }
    } catch {
      setNotice({
        tone: "error",
        text: tx("移除技能失败。", "Failed to remove skill."),
      });
    } finally {
      setLocalBusyIds(prev => ({ ...prev, [busyKey]: false }));
    }
  }, [confirmRemoveSkill, selectedSkillId, tx, updateOverview]);

  const handleTriggerPrompt = useCallback(
    (prompt: string, skillId?: string) => {
      if (onTrySkillPrompt) {
        onTrySkillPrompt(prompt, skillId);
        return;
      }
      writePendingSkillPrefill({
        prompt,
        skillId,
        createdAt: Date.now(),
      });
      navigate("/agent");
    },
    [navigate, onTrySkillPrompt],
  );

  const handleNewSkill = useCallback(() => {
    const hasSkillCreator = overview?.skills.some(s => s.id === "skill-creator" && s.enabled);
    const prompt = hasSkillCreator
      ? "$skill-creator help me create a reusable skill"
      : "Help me create a new SKILL.md workflow skill for my tasks.";
    handleTriggerPrompt(prompt, hasSkillCreator ? "skill-creator" : undefined);
  }, [handleTriggerPrompt, overview?.skills]);

  const pluginsById = useMemo(() => {
    const map = new Map<string, ExtensionPlugin>();
    for (const p of overview?.plugins ?? []) {
      map.set(p.id, p);
    }
    return map;
  }, [overview?.plugins]);

  const installedIds = useMemo(() => {
    return new Set((overview?.skills ?? []).map(s => s.id));
  }, [overview?.skills]);

  const scopeCounts = useMemo(() => {
    const skills = overview?.skills ?? [];
    const counts = {
      all: skills.length,
      user: 0,
      system: 0,
      repo: 0,
      plugin: 0,
    };
    for (const s of skills) {
      const sc = (s.scope || (s.builtin ? "system" : s.plugin_id ? "plugin" : "user")).toLowerCase();
      if (sc === "system" || s.builtin) counts.system += 1;
      else if (sc === "repo") counts.repo += 1;
      else if (sc === "plugin" || s.plugin_id) counts.plugin += 1;
      else counts.user += 1;
    }
    return counts;
  }, [overview?.skills]);

  const filteredSkills = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return (overview?.skills ?? []).filter(skill => {
      if (scopeFilter !== "all") {
        const sc = (skill.scope || (skill.builtin ? "system" : skill.plugin_id ? "plugin" : "user")).toLowerCase();
        if (scopeFilter === "system" && sc !== "system" && !skill.builtin) return false;
        if (scopeFilter === "user" && sc !== "user") return false;
        if (scopeFilter === "repo" && sc !== "repo") return false;
        if (scopeFilter === "plugin" && sc !== "plugin" && !skill.plugin_id) return false;
      }
      if (!q) return true;
      return (
        skill.title.toLowerCase().includes(q) ||
        skill.id.toLowerCase().includes(q) ||
        skill.description.toLowerCase().includes(q) ||
        (skill.short_description || "").toLowerCase().includes(q)
      );
    });
  }, [overview?.skills, scopeFilter, searchQuery]);

  const filteredRecommended = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    return recommended.filter(item => {
      if (item.installed || installedIds.has(item.id)) return false;
      if (!q) return true;
      return (
        item.title.toLowerCase().includes(q) ||
        item.id.toLowerCase().includes(q) ||
        item.short_description.toLowerCase().includes(q) ||
        item.description.toLowerCase().includes(q)
      );
    });
  }, [installedIds, recommended, searchQuery]);

  const visibleRecommended = useMemo(() => {
    if (searchQuery.trim() || showAllRecommended) {
      return filteredRecommended;
    }
    return filteredRecommended.slice(0, 8);
  }, [filteredRecommended, searchQuery, showAllRecommended]);

  const selectedInstalledSkill = useMemo(
    () => (overview?.skills ?? []).find(s => s.id === selectedSkillId) ?? null,
    [overview?.skills, selectedSkillId],
  );

  return (
    <section
      aria-label={tx("技能", "Skills")}
      className={`codex-skills-root${embeddedInAgent ? " codex-skills-embedded" : ""}`}
    >
      {/* Codex Skills Sticky / Page Header */}
      <div className="codex-skills-topbar">
        <div className="codex-skills-heading-group">
          <div className="codex-skills-title-row">
            <h2 className="codex-skills-title">{tx("技能", "Skills")}</h2>
          </div>
          <p className="codex-skills-subtitle">
            {tx(
              "使用 Skills 添加可复用的工作流与领域指令。",
              "Add reusable workflows with Skills.",
            )}
            <a
              href="https://developers.openai.com/codex/skills"
              target="_blank"
              rel="noreferrer"
            >
              {tx("了解更多", "Learn more")}
            </a>
          </p>
        </div>

        <div className="codex-skills-controls">
          <button
            type="button"
            className="codex-skills-btn"
            disabled={refreshing}
            onClick={() => void handleRefreshAll()}
            title={tx(
              "重新扫描本机 .codex/skills、.agents/skills 与官方推荐技能",
              "Rescan local .codex/skills, .agents/skills, and recommended skills",
            )}
          >
            <RefreshCw size={14} className={refreshing ? "spin" : undefined} />
            <span>{tx("刷新", "Refresh")}</span>
          </button>

          <label className="codex-skills-search-wrap">
            <Search size={14} className="codex-skills-search-icon" />
            <input
              ref={searchInputRef}
              id="skills-search"
              type="search"
              className="codex-skills-search-input"
              aria-label={tx("搜索技能", "Search skills")}
              placeholder={tx("搜索技能…", "Search skills")}
              value={searchQuery}
              onChange={event => setSearchQuery(event.target.value)}
              onKeyDown={event => {
                if (event.key === "Escape") {
                  event.stopPropagation();
                  setSearchQuery("");
                  event.currentTarget.blur();
                }
              }}
            />
            {searchQuery ? (
              <button
                type="button"
                className="codex-skills-search-clear"
                aria-label={tx("清除搜索", "Clear search")}
                onClick={() => {
                  setSearchQuery("");
                  searchInputRef.current?.focus();
                }}
              >
                <X size={13} />
              </button>
            ) : null}
          </label>

          {onOpenImportModal ? (
            <button
              type="button"
              className="codex-skills-btn"
              disabled={createActionsBlocked}
              onClick={onOpenImportModal}
            >
              <Upload size={14} />
              <span>{tx("导入技能", "Import skill")}</span>
            </button>
          ) : null}

          <button
            type="button"
            className="codex-skills-btn codex-skills-btn-primary"
            disabled={createActionsBlocked}
            onClick={handleNewSkill}
          >
            <Plus size={15} />
            <span>{tx("新建技能", "New skill")}</span>
          </button>
        </div>
      </div>

      {/* Scope Filter Tabs */}
      <div className="codex-skills-filterbar">
        <div
          className="codex-skills-scopes"
          role="group"
          aria-label={tx("技能范围筛选", "Skill scope filter")}
        >
          {(
            [
              { key: "all", label: tx("全部", "All"), count: scopeCounts.all },
              { key: "user", label: tx("用户", "User"), count: scopeCounts.user },
              { key: "system", label: tx("系统", "System"), count: scopeCounts.system },
              ...(scopeCounts.repo > 0
                ? [{ key: "repo" as const, label: tx("项目", "Repo"), count: scopeCounts.repo }]
                : []),
              ...(scopeCounts.plugin > 0
                ? [{ key: "plugin" as const, label: tx("插件", "Plugin"), count: scopeCounts.plugin }]
                : []),
            ] as const
          ).map(tab => (
            <button
              key={tab.key}
              type="button"
              className={`codex-skills-scope-tab${scopeFilter === tab.key ? " active" : ""}`}
              aria-pressed={scopeFilter === tab.key}
              onClick={() => setScopeFilter(tab.key)}
            >
              <span>{tab.label}</span>
              <span className="codex-skills-scope-count">{tab.count}</span>
            </button>
          ))}
        </div>
      </div>

      {notice ? (
        <div
          className={`ae-banner ${notice.tone === "error" ? "ae-banner-error" : ""}`}
          role="status"
        >
          <span>{notice.text}</span>
          <button
            type="button"
            className="ae-btn ae-btn-sm"
            onClick={() => setNotice(null)}
          >
            {tx("关闭", "Dismiss")}
          </button>
        </div>
      ) : null}

      {/* Section 1: Based on your Computer History (Suggested Skills) */}
      {!hideSuggestions && !searchQuery.trim() && scopeFilter === "all" ? (
        <div className="codex-skills-section">
          <div className="codex-skills-section-header">
            <h3 className="codex-skills-section-title">
              <Sparkles size={13} />
              <span>
                {tx("基于你的工作区与常用操作建议", "Based on your Computer History")}
              </span>
            </h3>
            <button
              type="button"
              className="codex-skills-collapse-btn"
              onClick={() => setHideSuggestions(true)}
            >
              {tx("隐藏建议", "Dismiss")}
            </button>
          </div>
          <div className="codex-skills-grid">
            {SUGGESTED_WORKFLOW_IDEAS.map(idea => {
              const title = tx(idea.titleZh, idea.titleEn);
              const desc = tx(idea.descZh, idea.descEn);
              return (
                <div
                  key={idea.id}
                  className="codex-skill-card"
                  onClick={() =>
                    handleTriggerPrompt(
                      `$skill-creator Help me create a skill for "${title}": ${desc}`,
                      "skill-creator",
                    )
                  }
                >
                  <div className="codex-skill-card-left">
                    <SkillAvatar
                      id={idea.id}
                      title={title}
                      description={desc}
                    />
                    <div className="codex-skill-card-copy">
                      <div className="codex-skill-card-title-row">
                        <span className="codex-skill-card-title">{title}</span>
                      </div>
                      <p className="codex-skill-card-desc" title={desc}>
                        {desc}
                      </p>
                    </div>
                  </div>
                  <div
                    className="codex-skill-card-actions"
                    onClick={event => event.stopPropagation()}
                  >
                    <button
                      type="button"
                      className="codex-skills-btn"
                      style={{ height: 30, padding: "0 11px", fontSize: 12 }}
                      onClick={() =>
                        handleTriggerPrompt(
                          `$skill-creator Help me create a skill for "${title}": ${desc}`,
                          "skill-creator",
                        )
                      }
                    >
                      <span>{tx("创建技能", "Create skill")}</span>
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      {/* Section 2: Installed Skills (Codex 2-column SkillCard Grid) */}
      <div className="codex-skills-section">
        <div className="codex-skills-section-header">
          <h3 className="codex-skills-section-title">
            <span>{tx("已安装", "Installed")}</span>
            <span className="codex-skills-section-count">{filteredSkills.length}</span>
          </h3>
        </div>

        {filteredSkills.length === 0 ? (
          <div className="ae-empty">
            <BookOpen size={26} />
            <p>{tx("没有匹配的技能。", "No matching skills.")}</p>
          </div>
        ) : (
          <div className="codex-skills-grid">
            {filteredSkills.map(skill => {
              const parentPlugin = skill.plugin_id ? pluginsById.get(skill.plugin_id) : undefined;
              const isBusy = Boolean(busyIds[`skill:${skill.id}`]);
              const parentBlocked = !skill.parent_enabled && skill.component_enabled;
              const shortDesc = skill.short_description || skill.summary || skill.description;

              return (
                <article
                  key={skill.id}
                  className={`codex-skill-card${!skill.enabled ? " is-dimmed" : ""}`}
                  onClick={() => setSelectedSkillId(skill.id)}
                >
                  <div className="codex-skill-card-left">
                    <SkillAvatar
                      id={skill.id}
                      title={skill.title}
                      description={shortDesc}
                      iface={skill.interface}
                    />
                    <div className="codex-skill-card-copy">
                      <div className="codex-skill-card-title-row">
                        <h3 className="codex-skill-card-title">{skill.title}</h3>
                        <span className="codex-skill-pill">
                          {formatScopeLabel(skill.scope, skill.builtin, tx)}
                        </span>
                        {parentPlugin ? (
                          <span className="codex-skill-pill">
                            {tx(`插件：${parentPlugin.title}`, `Plugin: ${parentPlugin.title}`)}
                          </span>
                        ) : null}
                        {parentBlocked ? (
                          <span className="codex-skill-pill codex-skill-pill-warning">
                            {tx(
                              "父插件已停用",
                              "Parent plugin off",
                            )}
                          </span>
                        ) : !skill.enabled ? (
                          <span className="codex-skill-pill">
                            {tx("已停用", "Disabled")}
                          </span>
                        ) : null}
                        {skill.warnings.length > 0 ? (
                          <span
                            className="codex-skill-pill codex-skill-pill-warning"
                            title={skill.warnings.join("\n")}
                          >
                            <AlertTriangle size={11} />
                            <span>{skill.warnings.length}</span>
                          </span>
                        ) : null}
                      </div>
                      <p className="codex-skill-card-desc" title={skill.description}>
                        {shortDesc}
                      </p>
                    </div>
                  </div>

                  <div
                    className="codex-skill-card-actions"
                    onClick={event => event.stopPropagation()}
                  >
                    <button
                      type="button"
                      className="codex-skill-icon-action"
                      aria-label={tx("查看详情", "View details")}
                      title={tx("查看详情", "View details")}
                      onClick={() => setSelectedSkillId(skill.id)}
                    >
                      <Eye size={15} />
                    </button>
                    <button
                      type="button"
                      role="switch"
                      aria-checked={skill.component_enabled}
                      aria-label={tx(`启用技能 ${skill.title}`, `Enable skill ${skill.title}`)}
                      className="ae-switch"
                      disabled={isBusy}
                      onClick={() => void handleToggleSkill(skill, !skill.component_enabled)}
                    >
                      <span className="ae-switch-thumb" />
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>

      {/* Section 3: Recommended Curated Skills (from .codex/vendor_imports/skills/skills/.curated) */}
      {filteredRecommended.length > 0 && scopeFilter === "all" ? (
        <div className="codex-skills-section">
          <div className="codex-skills-section-header">
            <h3 className="codex-skills-section-title">
              <span>{tx("推荐技能", "Recommended")}</span>
              <span className="codex-skills-section-count">{filteredRecommended.length}</span>
            </h3>
          </div>

          <div className="codex-skills-grid">
            {visibleRecommended.map(rec => {
              const isInstalling = Boolean(installingIds[rec.id]);
              return (
                <article
                  key={rec.id}
                  className="codex-skill-card"
                  onClick={() => setSelectedRecommended(rec)}
                >
                  <div className="codex-skill-card-left">
                    <SkillAvatar
                      id={rec.id}
                      title={rec.title}
                      description={rec.short_description}
                      iface={rec.interface}
                    />
                    <div className="codex-skill-card-copy">
                      <div className="codex-skill-card-title-row">
                        <h3 className="codex-skill-card-title">{rec.title}</h3>
                      </div>
                      <p className="codex-skill-card-desc" title={rec.description}>
                        {rec.short_description || rec.description}
                      </p>
                    </div>
                  </div>

                  <div
                    className="codex-skill-card-actions"
                    onClick={event => event.stopPropagation()}
                  >
                    <button
                      type="button"
                      className="codex-skill-install-btn"
                      disabled={isInstalling || createActionsBlocked}
                      aria-label={tx(`安装技能 ${rec.title}`, `Install skill ${rec.title}`)}
                      title={tx(`安装 ${rec.title}`, `Install ${rec.title}`)}
                      onClick={() => void handleInstallRecommended(rec)}
                    >
                      {isInstalling ? <LoaderCircle size={15} /> : <Plus size={16} />}
                    </button>
                  </div>
                </article>
              );
            })}
          </div>

          {!searchQuery.trim() && filteredRecommended.length > 8 ? (
            <button
              type="button"
              className="codex-skills-collapse-btn"
              onClick={() => setShowAllRecommended(prev => !prev)}
            >
              {showAllRecommended ? (
                <>
                  <ChevronUp size={14} />
                  <span>{tx("收起", "Show less")}</span>
                </>
              ) : (
                <>
                  <ChevronDown size={14} />
                  <span>
                    {tx(
                      `查看全部 ${filteredRecommended.length} 个推荐技能`,
                      `See ${filteredRecommended.length - 8} more recommended skills`,
                    )}
                  </span>
                </>
              )}
            </button>
          ) : null}
        </div>
      ) : null}

      {/* Codex Skill Contents Modal (for Installed Skill) */}
      {selectedInstalledSkill ? (
        <CodexSkillContentsModal
          skill={selectedInstalledSkill}
          parentPlugin={
            selectedInstalledSkill.plugin_id
              ? pluginsById.get(selectedInstalledSkill.plugin_id)
              : undefined
          }
          busy={Boolean(busyIds[`skill:${selectedInstalledSkill.id}`])}
          onClose={() => setSelectedSkillId(null)}
          onToggle={next => void handleToggleSkill(selectedInstalledSkill, next)}
          onRequestRemove={() => {
            const target = selectedInstalledSkill;
            setSelectedSkillId(null);
            if (controlledRequestRemoveSkill) {
              controlledRequestRemoveSkill(target);
            } else {
              setConfirmRemoveSkill(target);
            }
          }}
          onTryNow={() => {
            const prompt =
              selectedInstalledSkill.interface?.default_prompt ||
              `Use $${selectedInstalledSkill.id} to help with this task.`;
            setSelectedSkillId(null);
            handleTriggerPrompt(prompt, selectedInstalledSkill.id);
          }}
        />
      ) : null}

      {/* Codex Skill Contents Modal (for Recommended Curated Skill Preview) */}
      {selectedRecommended ? (
        <CodexRecommendedSkillModal
          skill={selectedRecommended}
          installing={Boolean(installingIds[selectedRecommended.id])}
          onClose={() => setSelectedRecommended(null)}
          onInstall={() => void handleInstallRecommended(selectedRecommended)}
          onTryNow={async () => {
            const target = selectedRecommended;
            setSelectedRecommended(null);
            await handleInstallRecommended(target);
            const prompt =
              target.interface?.default_prompt || `Use $${target.id} to help with this task.`;
            handleTriggerPrompt(prompt, target.id);
          }}
        />
      ) : null}

      {/* Standalone Confirm Remove Modal (when used inside RynAgent without AgentExtensions parent) */}
      {confirmRemoveSkill ? (
        <div
          className="codex-skill-modal-backdrop"
          onMouseDown={e => {
            if (e.target === e.currentTarget) setConfirmRemoveSkill(null);
          }}
        >
          <div
            className="codex-skill-modal"
            style={{ maxWidth: 440 }}
            role="dialog"
            aria-modal="true"
            aria-label={tx(
              `移除技能 ${confirmRemoveSkill.title}`,
              `Remove skill ${confirmRemoveSkill.title}`,
            )}
          >
            <div className="codex-skill-modal-header">
              <div className="codex-skill-modal-title-wrap">
                <h2 className="codex-skill-modal-title">
                  {tx(
                    `移除技能 ${confirmRemoveSkill.title}`,
                    `Remove skill ${confirmRemoveSkill.title}`,
                  )}
                </h2>
              </div>
              <button
                type="button"
                className="codex-skill-icon-action"
                aria-label={tx("关闭对话框", "Close dialog")}
                onClick={() => setConfirmRemoveSkill(null)}
              >
                <X size={16} />
              </button>
            </div>
            <div className="codex-skill-modal-body">
              <p className="codex-skill-modal-desc">
                {tx(
                  `确定要从已安装列表中移除技能「${confirmRemoveSkill.title}」吗？`,
                  `Are you sure you want to remove "${confirmRemoveSkill.title}" from installed skills?`,
                )}
              </p>
            </div>
            <div className="codex-skill-modal-footer">
              <div className="codex-skill-modal-footer-left" />
              <div className="codex-skill-modal-footer-right">
                <button
                  type="button"
                  className="codex-skills-btn"
                  onClick={() => setConfirmRemoveSkill(null)}
                >
                  {tx("取消", "Cancel")}
                </button>
                <button
                  type="button"
                  className="codex-skills-btn codex-skills-btn-danger"
                  onClick={() => void handleConfirmStandaloneDelete()}
                >
                  {tx("确认移除", "Confirm remove")}
                </button>
              </div>
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function CodexSkillContentsModal({
  skill,
  parentPlugin,
  busy,
  onClose,
  onToggle,
  onRequestRemove,
  onTryNow,
}: {
  skill: ExtensionSkill;
  parentPlugin?: ExtensionPlugin;
  busy: boolean;
  onClose: () => void;
  onToggle: (next: boolean) => void;
  onRequestRemove: () => void;
  onTryNow: () => void;
}) {
  const tx = useSkillsText();
  const [moreMenuOpen, setMoreMenuOpen] = useState(false);
  const [copied, setCopied] = useState(false);
  const [activeFilePath, setActiveFilePath] = useState("SKILL.md");
  const [activeFileContent, setActiveFileContent] = useState(skill.content);
  const [loadingFile, setLoadingFile] = useState(false);

  useEffect(() => {
    setActiveFilePath("SKILL.md");
    setActiveFileContent(skill.content);
  }, [skill.id, skill.content]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation();
        if (moreMenuOpen) {
          setMoreMenuOpen(false);
        } else {
          onClose();
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [moreMenuOpen, onClose]);

  const handleSelectFile = useCallback(
    async (filePath: string) => {
      setActiveFilePath(filePath);
      if (filePath === "SKILL.md") {
        setActiveFileContent(skill.content);
        return;
      }
      setLoadingFile(true);
      try {
        const res = await agentExtensions.readSkillFile(skill.id, filePath);
        setActiveFileContent(res.content);
      } catch {
        setActiveFileContent(tx("无法读取该文件内容。", "Unable to read file contents."));
      } finally {
        setLoadingFile(false);
      }
    },
    [skill.content, skill.id, tx],
  );

  const handleCopyMarkdown = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(activeFileContent || skill.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 1800);
    } catch {
      // Ignore clipboard permission errors
    }
    setMoreMenuOpen(false);
  }, [activeFileContent, skill.content]);

  const handleOpenOrReveal = useCallback(
    async (action: "open" | "reveal") => {
      setMoreMenuOpen(false);
      try {
        await agentExtensions.openSkillPath(skill.id, action);
      } catch {
        // Ignore if running in headless test
      }
    },
    [skill.id],
  );

  const canUninstall = skill.deletable && !skill.builtin && skill.scope !== "system";
  const shortDesc = skill.short_description || skill.summary || skill.description;

  return (
    <div
      className="codex-skill-modal-backdrop"
      onMouseDown={event => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="codex-skill-modal"
        role="dialog"
        aria-modal="true"
        aria-label={skill.title}
      >
        {/* Codex Modal Header */}
        <div className="codex-skill-modal-header">
          <div className="codex-skill-modal-identity">
            <SkillAvatar
              id={skill.id}
              title={skill.title}
              description={shortDesc}
              iface={skill.interface}
              size="modal"
            />
            <div className="codex-skill-modal-title-wrap">
              <h2 className="codex-skill-modal-title">{skill.title}</h2>
              <div className="codex-skill-modal-subtitle">
                <span>{tx("技能", "Skill")}</span>
                <span>·</span>
                <code>${skill.id}</code>
                <span>·</span>
                <span>v{skill.version}</span>
                <span>·</span>
                <span className="codex-skill-pill">
                  {formatScopeLabel(skill.scope, skill.builtin, tx)}
                </span>
                {!skill.enabled ? (
                  <span className="codex-skill-pill">{tx("已停用", "Disabled")}</span>
                ) : null}
              </div>
            </div>
          </div>

          <div className="codex-skill-modal-header-controls">
            <button
              type="button"
              role="switch"
              aria-checked={skill.component_enabled}
              aria-label={tx(`启用技能 ${skill.title}`, `Enable skill ${skill.title}`)}
              className="ae-switch"
              disabled={busy}
              onClick={() => onToggle(!skill.component_enabled)}
            >
              <span className="ae-switch-thumb" />
            </button>

            <button
              type="button"
              className="codex-skill-icon-action"
              aria-label={tx("更多操作", "More actions")}
              title={tx("更多操作", "More actions")}
              onClick={() => setMoreMenuOpen(prev => !prev)}
            >
              <MoreHorizontal size={16} />
            </button>

            <button
              type="button"
              className="codex-skill-icon-action"
              aria-label={tx("关闭对话框", "Close dialog")}
              onClick={onClose}
            >
              <X size={16} />
            </button>

            {moreMenuOpen ? (
              <div className="codex-skill-more-menu" role="menu">
                <button
                  type="button"
                  className="codex-skill-more-item"
                  role="menuitem"
                  onClick={() => void handleOpenOrReveal("open")}
                >
                  <ExternalLink size={14} />
                  <span>{tx("打开 SKILL.md", "Open SKILL.md")}</span>
                </button>
                <button
                  type="button"
                  className="codex-skill-more-item"
                  role="menuitem"
                  onClick={() => void handleOpenOrReveal("reveal")}
                >
                  <FolderOpen size={14} />
                  <span>{tx("在文件资源管理器中显示", "Reveal in File Explorer")}</span>
                </button>
                <button
                  type="button"
                  className="codex-skill-more-item"
                  role="menuitem"
                  onClick={() => void handleCopyMarkdown()}
                >
                  {copied ? <Check size={14} /> : <Copy size={14} />}
                  <span>{tx("复制 Markdown 内容", "Copy Markdown")}</span>
                </button>
              </div>
            ) : null}
          </div>
        </div>

        {/* Codex Modal Body */}
        <div className="codex-skill-modal-body">
          <p className="codex-skill-modal-desc">{skill.description}</p>

          {!skill.parent_enabled && skill.component_enabled ? (
            <div className="ae-banner ae-banner-warning" role="status">
              <span>
                {tx(
                  `所属插件「${parentPlugin?.title || skill.plugin_id}」已停用，此技能在父插件启用前不会对新任务生效。`,
                  `Parent plugin "${parentPlugin?.title || skill.plugin_id}" is disabled. This skill remains inactive until the parent plugin is enabled.`,
                )}
              </span>
            </div>
          ) : null}

          {skill.warnings.length > 0 ? (
            <div className="ae-banner ae-banner-warning" role="status">
              <div>
                <strong>
                  {tx("兼容性与文件引用提示", "Compatibility & file reference warnings")}
                </strong>
                <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                  {skill.warnings.map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              </div>
            </div>
          ) : null}

          {skill.files.length > 0 ? (
            <div className="codex-skill-files-bar" aria-label={tx("技能包文件", "Skill package files")}>
              {skill.files.map(file => (
                <button
                  key={file.path}
                  type="button"
                  className={`codex-skill-file-chip${activeFilePath === file.path ? " active" : ""}`}
                  onClick={() => void handleSelectFile(file.path)}
                >
                  <FileText size={12} />
                  <code>{file.path}</code>
                </button>
              ))}
            </div>
          ) : null}

          <div className="codex-skill-markdown-box">
            {loadingFile ? (
              <div style={{ display: "flex", alignItems: "center", gap: 8, padding: 12 }}>
                <LoaderCircle size={16} />
                <span>{tx("正在加载文件内容…", "Loading file contents…")}</span>
              </div>
            ) : (
              <pre className="codex-skill-markdown-pre">{activeFileContent}</pre>
            )}
          </div>
        </div>

        {/* Codex Modal Footer */}
        <div className="codex-skill-modal-footer">
          <div className="codex-skill-modal-footer-left">
            {canUninstall ? (
              <button
                type="button"
                className="codex-skills-btn codex-skills-btn-danger"
                aria-label={tx("移除技能", "Remove skill")}
                onClick={onRequestRemove}
              >
                <Trash2 size={14} />
                <span>{tx("移除技能", "Remove skill")}</span>
              </button>
            ) : (
              <span style={{ fontSize: 12, color: "var(--pf-muted)" }}>
                {tx(
                  "系统与内置技能可停用，但不可删除。",
                  "System & built-in skills can be disabled but cannot be removed.",
                )}
              </span>
            )}
          </div>

          <div className="codex-skill-modal-footer-right">
            <button type="button" className="codex-skills-btn" onClick={onClose}>
              {tx("关闭", "Close")}
            </button>
            <button
              type="button"
              className="codex-skills-btn codex-skills-btn-primary"
              disabled={!skill.enabled}
              onClick={onTryNow}
            >
              <Play size={13} />
              <span>{tx("立即试用", "Try now")}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

function CodexRecommendedSkillModal({
  skill,
  installing,
  onClose,
  onInstall,
  onTryNow,
}: {
  skill: RecommendedSkill;
  installing: boolean;
  onClose: () => void;
  onInstall: () => void;
  onTryNow: () => void;
}) {
  const tx = useSkillsText();

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div
      className="codex-skill-modal-backdrop"
      onMouseDown={event => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="codex-skill-modal"
        role="dialog"
        aria-modal="true"
        aria-label={skill.title}
      >
        <div className="codex-skill-modal-header">
          <div className="codex-skill-modal-identity">
            <SkillAvatar
              id={skill.id}
              title={skill.title}
              description={skill.short_description}
              iface={skill.interface}
              size="modal"
            />
            <div className="codex-skill-modal-title-wrap">
              <h2 className="codex-skill-modal-title">{skill.title}</h2>
              <div className="codex-skill-modal-subtitle">
                <span>{tx("推荐技能", "Recommended Skill")}</span>
                <span>·</span>
                <code>${skill.id}</code>
                <span>·</span>
                <span className="codex-skill-pill">{tx("官方精选", "Curated")}</span>
              </div>
            </div>
          </div>

          <div className="codex-skill-modal-header-controls">
            <button
              type="button"
              className="codex-skill-icon-action"
              aria-label={tx("关闭对话框", "Close dialog")}
              onClick={onClose}
            >
              <X size={16} />
            </button>
          </div>
        </div>

        <div className="codex-skill-modal-body">
          <p className="codex-skill-modal-desc">{skill.description}</p>
          {skill.interface?.default_prompt ? (
            <div className="codex-skill-markdown-box">
              <div
                style={{
                  fontSize: 11.5,
                  fontWeight: 600,
                  color: "var(--pf-muted)",
                  marginBottom: 6,
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                <Terminal size={12} />
                <span>{tx("默认试用提示词 (Default Prompt)", "Default Prompt")}</span>
              </div>
              <pre className="codex-skill-markdown-pre">{skill.interface.default_prompt}</pre>
            </div>
          ) : null}
          <div className="codex-skill-markdown-box">
            <pre className="codex-skill-markdown-pre">{skill.content}</pre>
          </div>
        </div>

        <div className="codex-skill-modal-footer">
          <div className="codex-skill-modal-footer-left">
            <button
              type="button"
              className="codex-skills-btn"
              disabled={installing}
              onClick={onInstall}
            >
              {installing ? <LoaderCircle size={14} /> : <Plus size={14} />}
              <span>{tx("安装技能", "Install skill")}</span>
            </button>
          </div>

          <div className="codex-skill-modal-footer-right">
            <button type="button" className="codex-skills-btn" onClick={onClose}>
              {tx("关闭", "Close")}
            </button>
            <button
              type="button"
              className="codex-skills-btn codex-skills-btn-primary"
              disabled={installing}
              onClick={onTryNow}
            >
              <Play size={13} />
              <span>{tx("安装并试用", "Try now")}</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
