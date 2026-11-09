/** Every task error and refused send gets plain words and at least one way forward. */
export type AgentErrorAction = "continue" | "retry" | "switch_model" | "new_task" | "open_sources";

interface ErrorHelp { zh: string; en: string; actions: AgentErrorAction[] }

const HELP: Record<string, ErrorHelp> = {
  agent_image_input_unsupported: {
    zh: "这次没能把图片交给当前模型。换一个带图片图标的模型，或者直接继续发文字。",
    en: "The image could not be given to the selected model. Pick a model with the image icon, or continue in text.",
    actions: ["switch_model", "continue"] },
  agent_model_image_rejected: {
    zh: "模型拒收了图片，Ryn 已经去掉图片重试。换一个带图片图标的模型可以直接看图。",
    en: "The model refused the image, so Ryn retried without it. Pick a model with the image icon to let it look.",
    actions: ["switch_model", "continue"] },
  agent_write_outcome_unknown: {
    zh: "上一步执行中被中断，可能已经部分完成。直接发消息就能继续，Agent 会先检查实际情况，不会自动重做。",
    en: "The last step was interrupted and may be partly done. Send a message to continue; the agent checks first and never repeats it on its own.",
    actions: ["continue"] },
  agent_task_limit: {
    zh: "这个对话太长了。再发一次消息，Ryn 会自动接到新对话里继续。",
    en: "This conversation is too long. Send the message again and Ryn continues in a new conversation.",
    actions: ["retry", "new_task"] },
  agent_operation_unresolved: {
    zh: "上一步的结果还在核对，请等几秒再发。",
    en: "The last step's result is still being checked. Send again in a few seconds.",
    actions: ["retry"] },
  agent_reconciliation_running: {
    zh: "正在核对被中断那一步的结果，最多一分钟，之后就能继续。",
    en: "Checking the interrupted step's result. This takes up to a minute; then you can continue.",
    actions: ["retry"] },
  agent_context_limit: {
    zh: "这条消息加上必须保留的内容，超过了模型能处理的长度。请缩短消息，或换一个上下文更大的模型。",
    en: "This message plus what must be kept is longer than the model can take. Shorten it, or pick a model with a larger context.",
    actions: ["switch_model", "new_task"] },
  agent_model_unavailable: {
    zh: "所选模型现在不可用（可能离线或已被移除）。请换一个模型，或检查模型来源。",
    en: "The selected model is unavailable right now (offline or removed). Pick another model or check the model sources.",
    actions: ["switch_model", "open_sources"] },
  agent_model_connection_failed: {
    zh: "连不上模型服务。请检查网络或模型来源后重试。",
    en: "Could not reach the model service. Check the network or model source, then retry.",
    actions: ["retry", "switch_model", "open_sources"] },
  agent_inference_timeout: {
    zh: "模型长时间没有回应。可以重试，或换一个更快的模型。",
    en: "The model did not answer in time. Retry, or pick a faster model.",
    actions: ["retry", "switch_model"] },
  agent_inference_failed: {
    zh: "模型这次没有给出结果。可以重试；反复出现时换一个模型。",
    en: "The model returned no result this time. Retry; if it keeps happening, pick another model.",
    actions: ["retry", "switch_model"] },
  agent_model_upstream_failed: {
    zh: "模型服务返回了错误。可以重试，或换一个模型。",
    en: "The model service returned an error. Retry, or pick another model.",
    actions: ["retry", "switch_model", "open_sources"] },
  agent_model_busy: {
    zh: "模型正忙，请稍后重试，或换一个模型。",
    en: "The model is busy. Retry shortly, or pick another model.",
    actions: ["retry", "switch_model"] },
  agent_model_quota_exhausted: {
    zh: "这个模型今天的额度用完了，换一个模型继续。",
    en: "This model's daily quota is used up. Pick another model to continue.",
    actions: ["switch_model"] },
  agent_model_protocol_invalid: {
    zh: "上游响应不完整或与所选协议不兼容，请检查模型来源后重试。",
    en: "The upstream response was incomplete or incompatible with the selected protocol. Check the model source before retrying.",
    actions: ["open_sources", "switch_model", "retry"] },
  agent_model_protocol_unsupported: {
    zh: "所选协议不支持当前请求参数，请检查模型来源设置。",
    en: "The selected protocol does not support these request parameters. Review the model source settings.",
    actions: ["open_sources", "switch_model"] },
  agent_storage_limit: {
    zh: "本机的 Agent 存储已满。请归档或删除不再需要的任务后重试。",
    en: "Agent storage on this device is full. Archive or delete tasks you no longer need, then try again.",
    actions: ["retry"] },
  agent_cancelled: {
    zh: "任务已取消。发消息可以继续这个对话。",
    en: "The task was cancelled. Send a message to continue this conversation.",
    actions: ["continue"] },
  agent_node_restarted: {
    zh: "Ryn 重启导致任务中断。发消息即可接着做。",
    en: "Ryn restarted while this task ran. Send a message to carry on.",
    actions: ["continue"] },
  agent_extension_model_not_allowed: {
    zh: "此任务包含受模型限制的数据。请切回允许的模型继续。",
    en: "This task contains model-restricted data. Switch back to an allowed model to continue.",
    actions: ["switch_model"] },
  agent_model_destination_unverifiable: {
    zh: "暂时无法验证共享模型。对话已保留，请检查提供模型的设备连接，重新选择模型后继续。",
    en: "Could not verify the shared model. Your conversation is saved. Check the provider connection, then select the model to continue.",
    actions: ["switch_model", "retry"] },
  agent_model_destination_changed: {
    zh: "共享模型的配置或权限已变化，请重新选择模型后发送。",
    en: "The shared model configuration or permission changed. Select the model again before sending.",
    actions: ["switch_model"] },
};

const LABELS: Record<AgentErrorAction, [string, string]> = {
  continue: ["继续对话", "Continue"],
  retry: ["重试", "Retry"],
  switch_model: ["换模型", "Switch model"],
  new_task: ["新对话", "New conversation"],
  open_sources: ["检查模型来源", "Check model sources"],
};

export function agentErrorHelp(code: string, zh: boolean): { text: string; actions: AgentErrorAction[] } {
  const help = HELP[code];
  if (help) return { text: zh ? help.zh : help.en, actions: help.actions };
  return {
    text: zh ? `任务没有完成（错误码 ${code}）。可以重试、换一个模型，或开一个新对话。`
      : `The task did not finish (error code ${code}). Retry, pick another model, or start a new conversation.`,
    actions: ["retry", "switch_model", "new_task"],
  };
}

export function agentErrorActionLabel(action: AgentErrorAction, zh: boolean): string {
  return LABELS[action][zh ? 0 : 1];
}

/** Codes a send can be refused with; each has its own explanation instead of "could not confirm". */
export const SEND_ERROR_CODES = new Set(Object.keys(HELP));

export const KNOWN_AGENT_ERROR_CODES = Object.keys(HELP);
