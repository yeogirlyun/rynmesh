# Ryn 与公司插件安装包 — 2026-11-13

用户要求打包最新 develop 的 Ryn 与公司插件。本次在开始构建时锁定两个本地 develop 的已提交源码；已 fetch 正式远端确认两者包含当时的远端 develop。构建期间其他任务继续推进 develop，本包不含锁定之后的提交。

- Ryn 0.9.1：cb8ecc38c1139ef03c9bdf711531fa3d1570acff。
- 公司插件：f067acd5504688c043c3cd8bec173504a00ea55a，正式仓库 yeogirlyun/gshopper-ryn-node。
- 干净构建目录：D:/code/_build-tools/ryn-package-20261113、D:/code/_build-tools/company-package-20261113。构建后已跟踪源码无修改；Tauri 改写的 Cargo.toml 仅换行差异，核对无语义差异后恢复。
- 安装包：D:/code/rynmesh/releases/develop-company-20261113/Ryn-0.9.1-company-20261113-setup.exe。
- 大小：178810180 字节。
- SHA-256：315d92e1e18345a3abd6b53a2c3fac720aeb2826268b81f084e2358171753b89。
- 内容：Ryn、company-workspace、company-dashboard、company-affiliate；包含微软签名有效的 WebView2 联网引导安装器。

## 验证

Ryn TypeScript/Vite、原生电脑操作组件、原生网络模块、PyInstaller 内置节点、Tauri 桌面安装器、公司 Vite 与原版 Dashboard、公司服务冻结及最终 NSIS 合并均成功。Rust 依赖下载停滞后，从本机缓存按 Cargo.lock 校验补齐依赖，离线检查和桌面构建成功。公司运行包有 902 个白名单文件并生成逐文件 SHA-256。

最终构建脚本的隔离验收通过：首次安装、重复安装保留身份密钥与禁用插件设置、三个公司页面、company health ready 且 revision 与锁定提交一致，已有 Office 不受影响。公司服务出站网络阻断，未执行真实业务、未执行最终系统安装器、未更新本机正在运行的 Ryn。

分发目录含安装说明、SHA256SUMS.txt、安装器 JSON 回执及原生组件构建回执。最终安装器哈希与构建回执相符。构建产物、日志、缓存均未提交。
