import { ArrowUpRight, Code2, FileText, ListChecks, Sparkles } from "lucide-react";
import { Trans, useTranslation } from "react-i18next";
import type { AgentProject } from "../domain/rynAgent";

export default function RynAgentWelcome({ project, onPrompt }: { project?: AgentProject; onPrompt: (prompt: string) => void }) {
  const { t, i18n } = useTranslation("rynAgent");
  const zh = i18n.language.startsWith("zh");
  const cards = [
    { Icon: Code2, title: zh ? "理解项目" : "Explore a project", description: zh ? "梳理结构，找到关键入口" : "Map the structure and key entry points", prompt: zh ? "帮我梳理当前项目的结构、核心模块和关键入口，先阅读再给出说明。" : "Read the current project and explain its structure, core modules, and key entry points." },
    { Icon: ListChecks, title: zh ? "推进任务" : "Move work forward", description: zh ? "拆解问题，实施并验证" : "Plan, implement, and verify a change", prompt: zh ? "我想改进当前项目。请先检查项目状态，帮我找出值得优先处理的问题，并说明建议的改进方案。" : "Inspect the current project and identify issues worth addressing first. Explain the proposed improvements." },
    { Icon: FileText, title: zh ? "整理资料" : "Make sense of files", description: zh ? "提炼要点，形成清晰结果" : "Turn source material into clear findings", prompt: zh ? "帮我整理资料并提炼要点。我会提供文件，请先确认资料范围，再给出结构化总结。" : "Help organize and summarize my files. I will provide the source material; confirm the scope and produce a structured summary." },
  ];
  return <div className="ra-welcome ra-welcome-focus">
    <div className="ra-welcome-mark"><Sparkles size={25} strokeWidth={1.5} /></div>
    <div className="ra-welcome-copy"><span className="ra-welcome-eyebrow">{project ? project.name : "RYN AGENT"}</span><h2>{project ? <Trans ns="rynAgent" i18nKey="projectWelcome" values={{ name: project.path }} components={{ project: <span className="ra-welcome-project" /> }} /> : t("welcome")}</h2><p>{zh ? "交代目标，一起完成。过程可追踪，结果可查看。" : "Start with a goal. Follow the progress. Review the result."}</p></div>
    <div className="ra-start-cards">{cards.map(({ Icon, title, description, prompt }) => <button type="button" key={title} onClick={() => onPrompt(prompt)}><Icon size={20} strokeWidth={1.6} /><strong>{title}</strong><span>{description}</span><ArrowUpRight className="ra-start-arrow" size={15} /></button>)}</div>
    <small>{zh ? "选择一个起点，或在下方直接描述你的任务" : "Choose a starting point, or describe your task below"}</small>
  </div>;
}
