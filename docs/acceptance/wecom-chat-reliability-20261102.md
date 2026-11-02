# 企业微信聊天与 Agent 发送交付记录

日期：2026-11-02

公司插件功能已合并至其 develop，构建源码提交为 `07b1f3b36f905f232ee284f2f14fd1dd1d9342d6`。构建来自干净 worktree `D:/code/_build-tools/company-develop-20261102`，完整重建前端、财务资源和 PyInstaller 服务。主程序复用现有插件 MCP 入口，本轮无需主程序代码修改或重新打包。原公司仓库工作区的其他未提交改动未动。

完成本地发送队列、逐条确认与失败重试、请求去重、历史缓存及后台刷新、会话阅读位置恢复、提交后清空原草稿、图片及文件选择/拖入/粘贴预览。Agent 与界面共用发送服务，提供账号会话查询、附件添加、发送、结果查询和重试五个工具。

## 验收

- 15 项定向后端检查通过；Vite 和完整服务打包通过。全项目 TypeScript 检查仍有既有 AffiliateGuidePage 的三个错误，本轮未扩展修复。
- 浏览器验证连续发送、失败重试、新草稿保留、刷新失败保留历史、会话滚动位置恢复、图片选择和粘贴、多文件合成拖放及移除、21 MiB 文件拦截。正常文本本地入队呈现测得约 9 ms。合成拖放不代表已测试资源管理器原生拖放。
- 经用户指定会话，通过 MCP 接口真实发送文本、生成 PNG 和 UTF-8 TXT，三项均取得原生消息及服务器确认；用户确认接收端图片和文件均正常。真实发送不依赖 UI 自动化。
- 安装后健康接口返回 `ok: true, status: ready`；在线 MCP tools/list 返回全部五个 wecom 工具。

## 安装与回滚

安装目录：`C:/Users/子陵/AppData/Local/Packages/OpenAI.Codex_2p2nqsd0c76g0/LocalCache/Local/Ryn/plugin-services/company-d439cee6f72fe3c0c8e1`。

宿主自动切换服务，核对新进程 PID 29152 的可执行路径与上述目录一致，清单源码提交与构建提交一致。沿用数据目录 `D:/code/chat-backend/integrations/company-app/run/local-test-20261029`，未替换账号配置或用户数据库。

旧运行目录 `company-121861bab891717098e4` 保留；同级注册表备份为 `services.json.before-8b6ff2720b6b48148ad3da80d9659578`。需要回滚时恢复该注册表备份，由宿主重新加载旧服务，不回滚或覆盖用户数据。

当前发送适配器绑定 WXWork 5.0.13.6005，单附件限制 20 MiB。原始回执、会话标识、凭据、数据库和构建产物均未提交。
