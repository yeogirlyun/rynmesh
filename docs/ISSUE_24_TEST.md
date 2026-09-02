# Issue #24 测试文档

## 自动化覆盖

`PrivateAIChat.test.tsx` 覆盖：

1. 原有新建、搜索、连续对话、清空确认流程回归；
2. 两个相同模型别名但不同 peer/package 的服务可区分；
3. 选择器展示 Provider、package、context、output limit、输入/输出价格；
4. A/B 切换只加载各自历史，返回 A 可恢复 A 的最近会话；
5. 草稿跨切换保留；
6. 请求精确提交所选 peer ID 与 package ID；
7. URL 参数随切换更新并保留无关参数；
8. 提交未返回 task ID 到任务完成的整个期间禁止切换；
9. offline 服务可查看历史但不能提交；
10. busy 服务可由 URL 恢复且不能提交；
11. 发现结果删除当前服务后标记 disappeared，历史不清除；
12. 最新请求 gate 拒绝较慢的旧异步读取。

`llmConversationStore.test.ts` 随全量测试继续覆盖 AES-GCM 加密写入、复合键过滤和损坏记录恢复。

## 执行命令

在 `webapp` 目录执行：

```powershell
npm ci
npm run lint
npm test -- --run src/screens/PrivateAIChat.test.tsx src/domain/llmOrders.test.ts
npm test
npm run build
```

## 可视化验收步骤

1. 用唯一端口启动 `npm run dev -- --host 127.0.0.1 --port 43124`；
2. 打开带 `client=fixture` 的 Private AI URL；
3. 关闭首次引导，展开 Provider / model；
4. 检查选择卡的身份、容量、上下文、输出和价格字段；
5. 发送 fixture 请求并确认历史、回复、费用和加密状态可见；
6. 检查浏览器 console warning/error。

多 Provider、offline、busy、disappeared 和请求中禁用状态由确定性的 Vitest 场景生成，避免依赖线上
Provider 状态产生不可重复结果。
