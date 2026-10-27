import { tr } from "../uiI18n";
import { nodeControlUrl } from "./nodeUrl";

export interface ModelTokenWindowUsage {
  day?: string;
  requests: number;
  completed_requests: number;
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  total_tokens: number;
  cache_hit_rate: number;
}

export interface ModelTokenQuota {
  requests_per_day: number | null;
  tokens_per_day: number | null;
  shared_tokens_per_day?: number | null;
  requests_remaining: number | null;
  tokens_remaining: number | null;
  shared_tokens_remaining?: number | null;
  exhausted: boolean;
  exhausted_reason: "daily_request_quota_exceeded" | "daily_token_quota_exceeded" | "daily_shared_token_quota_exceeded" | null;
}

export interface ModelDailyTokenBucket {
  day: string;
  tokens: number;
  total_tokens: number;
  input_tokens?: number;
  output_tokens?: number;
  cached_tokens?: number;
  requests?: number;
}

export interface ModelTokenUsage {
  usage_key: string;
  today: ModelTokenWindowUsage;
  total: ModelTokenWindowUsage;
  daily?: ModelDailyTokenBucket[];
  by_caller?: {
    self: { today: ModelTokenWindowUsage; total: ModelTokenWindowUsage };
    shared: { today: ModelTokenWindowUsage; total: ModelTokenWindowUsage };
  };
  quota?: ModelTokenQuota;
  updated_at?: string | null;
}

export interface AIModelParameters {
  context_window: number;
  max_output_tokens: number;
  request_defaults: Record<string, unknown>;
  max_concurrent: number;
  requests_per_day: number | null;
  tokens_per_day?: number | null;
  shared_tokens_per_day?: number | null;
}
export type ModelShareScope = "personal_space" | "friends";
export type APIProtocol = "openai_chat" | "openai_responses" | "anthropic_messages";
export interface AIModelSettings {
  overrides: Partial<AIModelParameters> & { share_scope?: ModelShareScope };
  effective: AIModelParameters & { share_scope?: ModelShareScope };
  context_window_basis?: string;
  context_window_known?: boolean;
  inherited_fields: Array<keyof AIModelParameters | "share_scope">;
  rule_revision: string;
  destination_revision: string;
  evidence: { connection?: unknown; model_exists?: unknown; tools?: { status: string; checked_at: string | null } };
  usage?: ModelTokenUsage;
}

export type AISource = {
  id: string; name: string; kind: "api" | "local"; provider: string; base_url: string;
  protocol?: APIProtocol;
  models: string[]; context_window: number; max_output_tokens: number;
  context_window_mode?: "auto" | "manual";
  model_metadata?: Record<string, { context_window: number }>;
  request_defaults: Record<string, unknown>; enabled: boolean; sharing: boolean;
  status: string; has_key: boolean; api_key?: string; clear_key?: boolean;
  shared_models?: string[];
  source_revision?: string;
  tool_support?: Record<string, "supported" | "unsupported">;
  model_settings?: Record<string, AIModelSettings>;
  model_usage?: Record<string, ModelTokenUsage>;
};
export const sourcePresets = [
  { id: "zai", name: "Z.ai · GLM", url: "https://api.z.ai/api/paas/v4", model: "glm-5.3-flash" },
  { id: "openai", name: "OpenAI", url: "https://api.openai.com/v1", model: "" },
  { id: "anthropic", name: "Anthropic · Claude", url: "https://api.anthropic.com/v1", model: "" },
  { id: "deepseek", name: "DeepSeek", url: "https://api.deepseek.com", model: "deepseek-flash" },
  { id: "qwen", get name() { return tr("通义千问"); }, url: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "" },
  { id: "openrouter", name: "OpenRouter", url: "https://openrouter.ai/api/v1", model: "" },
  { id: "custom", get name() { return tr("自定义兼容 API"); }, url: "", model: "" },
  { id: "ollama", name: "Ollama", url: "http://127.0.0.1:11434/v1", model: "" },
  { id: "lmstudio", name: "LM Studio", url: "http://127.0.0.1:1234/v1", model: "" },
];
export function newSource(provider = "zai"): AISource {
  const preset = sourcePresets.find(p => p.id === provider)!;
  return { id: "new", name: preset.name, kind: ["ollama", "lmstudio"].includes(provider) ? "local" : "api",
    provider, protocol: provider === "anthropic" ? "anthropic_messages" : "openai_chat",
    base_url: preset.url, models: provider === "deepseek" ? ["deepseek-flash", "deepseek-v4-pro"] : preset.model ? [preset.model] : [],
    context_window: (provider === "deepseek" || provider === "zai") ? 1048576 : 32768, context_window_mode: "auto", max_output_tokens: provider === "deepseek" ? 393216 : provider === "zai" ? 131072 : 4096,
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

export function formatTokenCount(tokens: number | null | undefined): string {
  const value = Math.max(0, Number(tokens || 0));
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value >= 10_000_000 ? 1 : 2).replace(/\.0+$/, "")}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value >= 100_000 ? 0 : 1).replace(/\.0$/, "")}K`;
  return String(Math.round(value));
}

export async function resetModelUsage(usageKey: string, scope: "today" | "all" = "today"): Promise<{ ok: boolean; usage_key: string; scope: string; usage: ModelTokenUsage }> {
  const response = await fetch(nodeControlUrl("/llm/models/usage/reset"), {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ usage_key: usageKey, scope }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || tr("重置用量失败，请重试"));
  return result;
}

export async function configureModelQuota(
  usageKey: string,
  quota: { requests_per_day?: number | null; tokens_per_day?: number | null; shared_tokens_per_day?: number | null },
): Promise<{ ok: boolean; usage_key: string; quota: Record<string, number | null>; usage: ModelTokenUsage }> {
  const response = await fetch(nodeControlUrl("/llm/models/usage/quota"), {
    method: "PUT",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ usage_key: usageKey, ...quota }),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail || tr("保存配额失败，请重试"));
  return result;
}
