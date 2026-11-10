# DEV-03 会话迁移与回滚报告

## v1 → v2

旧 Private AI `LLMConversation` 读取后转换为 `ryn.assistant-conversation.v2`：

- 新 ID 使用 `migrated_<old-id>`，避免覆盖原记录。
- Provider service key、显示名与 peer ID 固化在 `computeTarget`。
- 旧会话一律标记为 `friend` privacy boundary，不会误当成本机历史。
- 已完成消息迁移为 completed；task ID 保留为 run ID。
- 原 v1 记录在 v2 保存成功前后都不删除，可供回滚读取。
- `migratedFrom` 防止重复迁移。
- 单条解密损坏只跳过该条，不影响其它会话。

## 回滚策略

- 回滚 UI 只需撤销统一入口提交；加密数据库与 v1 原记录保持不变。
- node Assistant API 可由 `RYNMESH_ASSISTANT_WORKSPACE_V1=0` 关闭。
- 回滚不得清除 v2 bucket；只有用户在 Privacy & data 显式选择 Clear Ask Ryn 才擦除。

## 验证

`assistantStore.test.ts` 覆盖迁移后的 boundary/provider、v2 可读性及原 v1 记录仍存在。
