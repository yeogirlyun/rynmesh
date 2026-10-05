import { tr } from "../uiI18n";
import { nodeControlUrl } from "./nodeUrl";

export type AISource = {
  id: string; name: string; kind: "api" | "local"; provider: string; base_url: string;
  models: string[]; context_window: number; max_output_tokens: number;
  request_defaults: Record<string, unknown>; enabled: boolean; sharing: boolean;
  status: string; has_key: boolean; api_key?: string; clear_key?: boolean;
};
export const sourcePresets = [
  { id: "zai", name: "Z.ai · GLM", url: "https://api.z.ai/api/paas/v4", model: "glm-5.3-flash" },
  { id: "openai", name: "OpenAI", url: "https://api.openai.com/v1", model: "" },
  { id: "deepseek", name: "DeepSeek", url: "https://api.deepseek.com", model: "deepseek-chat" },
  { id: "qwen", get name() { return tr("通义千问"); }, url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "" },
  { id: "openrouter", name: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "" },
  { id: "custom", get name() { return tr("自定义兼容 API"); }, url: "", model: "" },
  { id: "ollama", name: "Ollama", url: "http://127.0.0.1:11434/v1", model: "" },
  { id: "lmstudio", name: "LM Studio", url: "http://127.0.0.1:1234/v1", model: "" },
];
export function newSource(provider = "zai"): AISource {
  const preset = sourcePresets.find(p => p.id === provider)!;
  return { id: "new", name: preset.name, kind: ["ollama", "lmstudio"].includes(provider) ? "local" : "api",
    provider, base_url: preset.url, models: preset.model ? [preset.model] : [],
    context_window: provider === "zai" ? 1000000 : 32768, max_output_tokens: provider === "zai" ? 128000 : 4096,
    request_defaults: provider === "zai" ? { thinking: { type: "enabled" }, reasoning_effort: "max" } : {},
    enabled: true, sharing: false, status: "untested", has_key: false };
}
export async function sourceRequest<T>(path = "", body?: unknown, method = "POST"): Promise<T> {
  const response = await fetch(nodeControlUrl(`/llm/sources${path}`), {
    credentials: "include", ...(body !== undefined ? { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { method: method === "DELETE" ? method : "GET" }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || tr("来源操作失败，请重试"));
  return result;
}
