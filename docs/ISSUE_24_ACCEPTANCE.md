# Issue #24 验收文档

验收日期：2026-09-02

分支：`codex/issue-24-provider-switching`

## 验收结论

本地实现达到 Issue #24 的功能和回归验收标准。所有结论均由自动化或实际 fixture 页面验证，
未使用线上真实资金和真实 Provider。

## 标准逐项结果

- [x] 用户可在 Private AI 页内切换 Provider / 模型。
- [x] 选择前可见 Provider、package、online/busy、context、output limit 和价格。
- [x] 相同 alias 的服务仍以精确 `peer_id + package_id` 区分和提交。
- [x] 历史始终归属原复合服务键，A/B 往返无消息串线。
- [x] 发送前校验会话身份和当前发现记录，无法错发到标题之外的 Provider。
- [x] URL 用 replace 同步 peer/service/network，刷新可恢复并保留其他参数。
- [x] submitting/running/cancelling 生命周期内禁止切换。
- [x] offline、busy、disappeared 状态可理解，历史和草稿不删除。
- [x] 加密历史读取/写入失败不会永久锁住切换，原 Provider、历史和草稿保持不变。
- [x] 加密存储、取消、结算和 node-only gateway 边界未改变。
- [x] 聚焦测试、全 Webapp、TypeScript 和生产构建通过。

## 实际执行证据

| 检查 | 结果 |
| --- | --- |
| `npm run lint` | PASS，TypeScript 无错误 |
| `npm test -- --run src/screens/PrivateAIChat.test.tsx`（第一轮） | PASS，1 file / 6 tests |
| `npm test`（第一轮） | PASS，9 files / 42 tests |
| `npm run build`（第一轮） | PASS，1739 modules，Vite production build 完成 |
| 浏览器 fixture 页面 | PASS，选择器和完成会话均实际操作 |
| 浏览器 console | PASS，0 warnings / 0 errors |

最终提交前已重新执行包含新增 helper 测试的聚焦、全量、lint 和 build；最终数字记录如下。

## 可视化证据

- [Provider 比较面板](evidence/issue-24/provider-comparison.png)
- [完成一次聊天](evidence/issue-24/completed-chat.png)

截图验证桌面宽度下的真实布局、选择器字段、聊天完成状态、费用和本地加密提示。双 Provider 精确切换、
请求中禁用、offline/busy/disappeared 与返回原历史由自动化测试验收；fixture 默认目录只有一个 Provider，
因此不伪造线上多 Provider 截图。

## 最终复验

| 命令 | 最终结果 |
| --- | --- |
| `npm test -- --run src/screens/PrivateAIChat.test.tsx`（存储失败回归） | PASS，1 file / 8 tests，10.70s |
| `npm run lint` | PASS，`tsc -b --noEmit` |
| `npm test` | PASS，10 files / 46 tests，13.65s |
| `npm run build` | PASS，1739 modules，最终 763ms Vite build |
| `git diff --check` | PASS，仅 Git 的 LF/CRLF 工作区提示，无 whitespace error |
