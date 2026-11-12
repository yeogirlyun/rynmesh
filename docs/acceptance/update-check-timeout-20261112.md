# 更新检查等待时间修复（2026-11-12）

- 源码提交：`1b99117feea511271077f14202a6d3e717624b3f`，已快进集成到 `develop`。
- 清单请求同时使用 20 秒 HTTP 超时和异步总时限；超时释放请求与 busy guard。
- 检查页提供取消检查，取消会丢弃请求 future、不计失败，并恢复可重试状态。
- 下载单独配置 600 秒，不受清单的 20 秒限制；保留来源限制及验签。
- 桌面日志记录 manifest 开始、结果类别、毫秒耗时，不记录带签名的重定向下载地址。

## 验证

- `cargo test --manifest-path webapp/src-tauri/Cargo.toml --lib updates::tests`：10 项通过，包含停滞请求超时释放、取消正在等待的请求、随后重新检查。
- `node node_modules/vitest/vitest.mjs run src/screens/UpdatesPanel.test.tsx`：11 项通过，包含原检查尚未结束时取消、超时文案与重试入口。
- `npm run build`：TypeScript 与生产前端构建通过，仅既有分块大小提示。
- 干净构建目录：`D:/code/_build-tools/ryn-update-check-20261112`；对应上述已提交源码。
- 本地构建日志、程序备份与回滚脚本：`artifacts/update-check-20261112/`（不入库）。

## 本机交付

- Windows NSIS 包构建成功，原生 release 编译耗时 7 分 24 秒。
- 安装包：`artifacts/update-check-20261112/Ryn_0.9.0_1b99117f_x64-setup.exe`。
- 安装包 SHA-256：`01a1e74772565e4c5dd640c7613932fa576dad25b7e67629281ba1d9e9909bec`。
- 现有程序及插件备份 913 个文件、292278615 字节，逐文件哈希校验通过。
- 本机替换命令被自动审批以 `blocked by policy` 拒绝，命令未执行；已请求用户正常退出 Ryn，再继续无需强制停止进程的安装。
- 截至本记录，运行程序仍为原版本；不能把构建成功视为安装验收成功。
