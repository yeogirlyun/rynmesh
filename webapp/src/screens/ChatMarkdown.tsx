import { tr, useUILanguage } from "../uiI18n";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

/** Render model output as content, never as executable HTML or remote images. */
export default function ChatMarkdown({ children }: { children: string }) {
  useUILanguage();
  return <Markdown remarkPlugins={[remarkGfm]} skipHtml components={{
    a: ({ children: label, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{label}</a>,
    img: ({ alt }) => <span>{alt || tr("图片")}</span>,
  }}>{children}</Markdown>;
}
