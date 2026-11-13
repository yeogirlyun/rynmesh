# 节点网页 + 做图：本机安装（第 6 步前半）— 2026-11-13

## 构建

- 源码：develop `1624ddeec29de4824a5245f055abcf59f3a99636`（含第 1–5 步），版本 0.9.1。
- 独立构建目录 `D:/code/_build-tools/ryn-sites-20261113`（从本机仓库克隆，复用当天打包目录的依赖缓存）。构建后跟踪源码只有 Tauri 改写的 `webapp/src-tauri/Cargo.toml`。
- `webapp/src-tauri/scripts/build-windows.ps1` 成功：前端、原生组件、PyInstaller 内置节点、Tauri 桌面和 NSIS 安装包（`.../bundle/nsis/Ryn_0.9.1_x64-setup.exe`）。
- 坑：在 SSH 的 PowerShell 5 里用 `*>` 重定向时，构建工具写到 stderr 的警告会被当成终止错误；改用 `cmd /c "... > log 2>&1"`。
- 核对：内置节点里包含 `image_studio\web\index.html`、`app.js` 和 `rynmesh.image_studio`、`rynmesh.node_sites` 模块。

## 安装（老板主机）

- 位置 `C:/Users/子陵/AppData/Local/Ryn`（当前在用的安装）。方式同以往：先备份（979 个程序与公司服务文件，逐一校验，335 MB，`artifacts/develop-release-20261113-1624dde/program-backup`），只停止该目录下的 Ryn、节点、电脑操作和公司服务进程，替换 `Ryn.exe`、`rynmesh-peer.exe`、`ryn_computer_use.exe` 并校验哈希，在用户桌面重新启动 Ryn。安装前没有运行中的 Agent 任务。数据、配置和公司服务不变（公司服务来源 `5a38b83`）。
- 哈希：Ryn.exe `2efc4add…`，rynmesh-peer.exe `ae591351…`，ryn_computer_use.exe `28cbd2c2…`。
- 健康检查：节点 ok（desktop_managed），公司服务 ready，首次可用 ready，文件哈希一致。Ryn 在交互会话（会话 1）17:19 运行。
- 新功能核对：做图已登记（内置，关闭），Codex 已登录可出图；公司通道已连接、已绑定、可发布；同事名单可读。
- 回滚：`artifacts/develop-release-20261113-1624dde/rollback.ps1`。

## 还没做

- 同事目录里目前没有任何同事电脑在线绑定（同事需要先装支持公司通道的新版 Ryn 和公司插件），所以还不能做两台电脑之间的实测。
- 做图服务保持关闭，等实测时再打开。
