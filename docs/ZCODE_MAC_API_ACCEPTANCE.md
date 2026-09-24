# Mac Ryn API → Windows Codex 联调

2026-09-24 配置于 Mac `172.16.8.117`，zcode 尚未安装。Ryn 应用已运行。

- OpenAI-compatible Base URL：`http://127.0.0.1:8791/v1`
- 模型别名：`codex-windows`
- 路由：Mac Ryn → Windows Ryn (`172.16.8.191`) → Codex CLI
- Key 名称：`zcode Mac to Windows Codex test`，输出预算 100000 tokens。
- Key 及接入信息仅交付到 Mac `/Users/zhoulei/Desktop/Ryn-zcode-api.txt`，文件权限 0600。此文档不记录 Key。
- API 仅接受本机连接；Windows 直接访问 Mac `/v1/models` 返回 403，无 Key 的 Mac 本机访问返回 401。

## 真实测试结果

1. 带 Key 的 `/v1/models` 返回 `codex-windows`。
2. `/v1/chat/completions` 的 SSE 返回合成测试回答并以 `[DONE]` 正常结束，耗时 59.38 秒。任务 `api_5b93d7518a3d495a96f133e1798cc6bb`，B 端模型处理 13.859 秒，A 端连接协商约 32 秒。API 的远程调用仍使用显式 P2P，不能套用聊天页的 LAN-first 性能结果。
3. `/v1/responses` 强制返回客户端函数的测试失败（任务 `api_3aa480f2d0d24daeb8d202bf846039d0`）。B 端正常收到任务，但没有返回预期工具调用。单独使用同一 Codex CLI 复现时，适配器错误为 `required client tool was not returned`；合成测试中的模型消息称 code-mode host 被禁用。
4. 同一隔离配置、本地直接调用 Codex 的 GPT-5.5 可以返回 `client_echo` 工具及正确参数。默认 GPT-6-Astra 和 GPT-5.6-Sol 的上述测试未通过。独立命名空间或替换基础提示词没有解决本次兼容问题。这些诊断没有修改生产模型选择或启用 B 端执行宿主。

原有真实二进制测试使用本地假模型，只验证协议与隔离；不能据此宣称真实云模型的工具选择及执行转交已通过。完整 zcode 工具链仍待解决上述问题并实测，普通文本 API 已可调用。

模型别名是 Ryn 服务路由名称，不等于固定底层模型版本。当前生产 Codex 保持其默认模型；未自动切换 GPT-5.5。用户可选择先用兼容模型联调或保留默认模型测试文本。

接口参考：[Codex App Server 动态工具](https://learn.chatgpt.com/docs/app-server#dynamic-tool-calls-experimental)。实验性动态工具通过 `item/tool/call` 交给客户端处理。本实现继续保留 `environments=[]`、临时会话及 B 端不执行工具的约束。
