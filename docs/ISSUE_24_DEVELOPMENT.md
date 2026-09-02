# Issue #24 开发文档

## 设计概览

本需求只修改 Webapp。后端发现、订单、结算和 IndexedDB 加密接口保持不变。

### 复合身份

`llmServiceRecordKey()` 仍生成稳定的 `peer_id::package_id`。新增的显示和状态 helper 不创建第二种
身份格式：

- `llmServiceAvailability()`：统一 ready / busy / offline 判断；
- `llmProviderLabel()`、`shortPeerId()`：只负责展示；
- `llmServicePricingLabel()`：统一选择器和详情中的价格文本；
- `createLatestRequestGate()`：只允许最新的异步会话桶读取提交 UI 状态。

### 切换事务

`loadServiceBucket()` 的提交边界如下：

1. 获取 generation token，进入 switching 状态；
2. 以目标复合键调用 `listConversations()`；
3. 无历史时创建并加密保存空会话；
4. 仅当组件仍挂载且 token 仍为最新时，批量更新 service、conversations、selected ID；
5. 使用 React Router `replace` 更新 URL，保留未知 query 参数。

目标历史准备好之前继续显示旧 Provider 和旧历史，因此不会出现 A 标题配 B 消息或反向串线。

### 发现刷新

页面可见时每 10 秒刷新，页面从隐藏恢复时立即刷新。刷新按复合键对账：

- 当前服务仍存在：更新容量和在线状态，不改变用户选择；
- 当前服务消失：保留最后元数据和本地历史，标为 disappeared；
- 刷新失败：保留最后成功结果，下次重试。

### 发送安全门

发送操作不直接相信视觉选择。它要求：

- conversation.serviceKey 等于选中服务复合键；
- conversation.providerPeerId 等于选中 peer；
- 当前发现列表仍有完全相同的复合键；
- 当前状态为 ready。

请求中的 `provider_peer_id` 和 `service_id` 从通过检查的当前发现记录派生。所有订单仍只通过
`NodeClient.submitLLMOrder()` 进入本地节点。

### StrictMode

挂载标记在每次 effect setup 中重置，避免 React StrictMode 的开发期 setup/cleanup/setup 流程永久阻止
异步初始状态提交。该问题通过真实 Vite fixture 页面验收发现并修复。

## 变更文件

- `webapp/src/domain/llmOrders.ts`
- `webapp/src/domain/llmOrders.test.ts`
- `webapp/src/screens/PrivateAIChat.tsx`
- `webapp/src/screens/PrivateAIChat.module.css`
- `webapp/src/screens/PrivateAIChat.test.tsx`
- `docs/ISSUE_24_*.md`
- `docs/evidence/issue-24/*`
