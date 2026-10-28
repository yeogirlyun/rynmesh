import { tr, useUILanguage } from "../uiI18n";
import { memo, useContext } from "react";
import Markdown, { defaultUrlTransform } from "react-markdown";
import remarkGfm from "remark-gfm";
import { AgentFileContext, AgentFileImage, AgentFileLink, findAgentFile } from "./RynAgentFileContext";

type MarkdownNode = { type: string; value?: string; url?: string; children?: MarkdownNode[] };

/** Render model output as content, never as executable HTML or remote images. */
function ChatMarkdown({ children, fileLinks = false }: { children: string; fileLinks?: boolean }) {
  useUILanguage();
  const context = useContext(AgentFileContext);
  const files = fileLinks ? context?.files || [] : [];
  // Markdown treats backslash-dot in Windows paths as an escape, even inside <...>.
  // Normalize only destinations backed by the task, preserving code and visible path text.
  const content = files.reduce((text, file) => {
    const path = file.absolute_path;
    if (!path?.includes("\\")) return text;
    const destination = path.replace(/\\/g, "/");
    return text.replaceAll(`](<${path}>)`, `](<${destination}>)`).replaceAll(`](${path})`, `](<${destination}>)`);
  }, children);
  // Turn literal filenames/paths in prose into links only when a task receipt backs them.
  const candidates = [...new Set(files.flatMap(file => [file.absolute_path, file.path, file.name]).filter((path): path is string => Boolean(path)))]
    .filter(path => findAgentFile(files, path)).sort((a, b) => b.length - a.length);
  function linkFiles() {
    return (tree: MarkdownNode) => {
      const visit = (node: MarkdownNode) => {
        if (!node.children || ["link", "image", "code", "inlineCode"].includes(node.type)) return;
        node.children = node.children.flatMap(child => {
          if (child.type !== "text" || !child.value || !candidates.length) { visit(child); return [child]; }
          const value = child.value; const parts: MarkdownNode[] = []; let cursor = 0; let scan = 0;
          while (scan < value.length) {
            const path = candidates.find(candidate => value.startsWith(candidate, scan)
              && !/[\p{L}\p{N}_./\\:-]/u.test(value[scan - 1] || "")
              && !/[\p{L}\p{N}_\\/-]/u.test(value[scan + candidate.length] || "")
              && !/^\.[\p{L}\p{N}_]/u.test(value.slice(scan + candidate.length)));
            if (!path) { scan++; continue; }
            if (scan > cursor) parts.push({ type: "text", value: value.slice(cursor, scan) });
            parts.push({ type: "link", url: path, children: [{ type: "text", value: path }] });
            scan += path.length; cursor = scan;
          }
          if (cursor < value.length) parts.push({ type: "text", value: value.slice(cursor) });
          return parts;
        });
      };
      visit(tree);
    };
  }
  return <Markdown remarkPlugins={[remarkGfm, linkFiles]} skipHtml urlTransform={url => findAgentFile(files, url) ? url : defaultUrlTransform(url)} components={{
    a: ({ children: label, href }) => {
      const file = href && findAgentFile(files, href);
      if (file) return <AgentFileLink file={file}>{label}</AgentFileLink>;
      return href ? <a href={href} target="_blank" rel="noopener noreferrer">{label}</a> : <span>{label}</span>;
    },
    code: ({ children: code, className }) => {
      const file = !className && typeof code === "string" && findAgentFile(files, code);
      return file ? <AgentFileLink file={file}><code>{code}</code></AgentFileLink> : <code className={className}>{code}</code>;
    },
    img: ({ alt, src }) => {
      const file = src ? findAgentFile(files, src) : undefined;
      return file?.preview === "image" ? <AgentFileImage file={file} alt={alt} /> : <span>{alt || tr("图片")}</span>;
    },
  }}>{content}</Markdown>;
}

export default memo(ChatMarkdown);
