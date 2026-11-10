# Issue #24 产品文档：Private AI 会话内切换 Provider / 模型

## 用户问题

Private AI 原先只在进入页面时选择一次服务。用户想更换模型或 Provider 时必须离开聊天页，
并且无法在选择前比较容量、上下文窗口和价格。模型别名还可能重复，单看别名无法确认请求会发给谁。

## 产品目标

用户可以在 Private AI 页内完成 Provider / 模型选择，并在切换前看到：

- 模型别名与 package ID；
- Provider 名称与缩略 peer ID；
- ready、busy、offline 或 disappeared 状态；
- context window 与最大输出 token；
- 输入、输出单价、币种和最低费用。

服务的唯一身份是 `peer_id + package_id`。每个身份拥有独立的本地加密会话桶；切换不会复制、
迁移或混合历史。未发送草稿不属于任何 Provider，因此切换后继续保留。

## 用户流程

1. 用户打开 `Provider / model` 选择器比较服务。
2. 用户选择目标服务；页面先读取目标服务自己的加密历史。
3. 读取完成后，标题、历史列表和 URL 同时切换；若无历史则创建空会话。
4. 切回原服务时恢复原服务最近使用的会话。
5. URL 中的 `peer`、`service`、`network` 可在刷新后恢复同一服务，其他参数不丢失。

## 异常和安全体验

- 提交、运行或取消任务期间，所有其他 Provider 选项禁用。
- Provider busy、offline 或从发现结果消失时，历史和草稿仍保留，但发送按钮禁用。
- 发现请求临时失败时保留最后一次成功快照，不把全体 Provider 误判为离线。
- 发送前再次检查当前会话的 `serviceKey`、`providerPeerId` 与当前发现记录；不一致则只在本地报错。
- Provider 必然在推理时看到明文；页面继续明确展示此隐私边界。

## 非目标

- 不跨 Provider 自动迁移对话。
- 不做多 Provider 同题比较。
- 不改 LLM 协议、结算、加密存储 schema 或本地节点网关。
- 不包含流式输出（Issue #23）。
