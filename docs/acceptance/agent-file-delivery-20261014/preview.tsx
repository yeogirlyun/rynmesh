import ReactDOM from "react-dom/client";
import { TaskArtifacts } from "./screens/RynAgentArtifacts";
import ChatMarkdown from "./screens/ChatMarkdown";
import RynAgentProgress from "./screens/RynAgentProgress";
import type { AgentTask } from "./domain/rynAgent";
import i18n from "./i18n";
import "./screens/rynAgentText";
import "./styles/tokens.css";
import "./styles/reset.css";
import "./personal/personal.css";
import "./screens/RynAgent.css";

void i18n.changeLanguage("zh-CN");
const { root } = await fetch("/api/local/preview").then(response => response.json());
const result = `已完成全部相关产品线合并，已计入的五项不重复累加，并更新相关合计公式。\n\n原表其他未涉及行的 \`#DIV/0!\` 保留。\n\n[查看验收记录](<${root}/文件交付验收记录.md>)\n\n![鹈鹕骑自行车动画](<${root}/pelican-cycle.svg>)`;
const task: AgentTask = { id: "delivery-preview", title: "文件交付", model_id: "preview", status: "completed", revision: 1, created_at: 1, updated_at: 1, messages: [], events: [], limits: {}, steps: 6, pending_approval: null, error: null, result };
ReactDOM.createRoot(document.getElementById("root")!).render(<main className="ra-workspace" style={{ height: "100vh", fontFamily: "Arial, 'Microsoft YaHei', sans-serif" }}>
  <header className="ra-header"><strong>Ryn 助手</strong><small style={{ color: "var(--pf-muted)" }}>文件交付 UI 验收</small></header>
  <div className="ra-scroll" style={{ padding: "30px 24px", overflow: "auto" }}><div className="ra-thread" style={{ maxWidth: 736, margin: "0 auto" }}>
    <TaskArtifacts task={task}>
      <div className="ra-message ra-user"><ChatMarkdown>所有相关产品线都合并。已经计入的五项不重复相加，完成后把生成的文件给我。</ChatMarkdown></div>
      <RynAgentProgress active={false} messages={[{ role: "assistant", content: "核对表格、更新合计公式并保存文件。", tool_calls: [{ id: "1", function: { name: "ryn_file_write", arguments: "{}" } }] }]} />
      <div className="ra-message ra-assistant"><ChatMarkdown fileLinks>{result}</ChatMarkdown></div>
    </TaskArtifacts>
  </div></div>
</main>);
