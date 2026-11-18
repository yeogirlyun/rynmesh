# Agent 改版 Windows 本机交付 · 2026-11-19

## 构建和测试

- `develop` 构建提交为 `a0771b867b2155c9a6ce4ed0867db90eeff1b312`，Windows x64 版本为 0.9.1。从该提交的独立克隆运行 `build-windows.ps1`，包含 Python 节点和原生 sidecar。PowerShell 5 缺少 `Get-FileHash`，实际构建使用 PowerShell 7，日志经 `cmd` 重定向到 `build.log`。
- 后端全量为 10,389 通过、99 失败、1 个清理报错、104 跳过。对 30 个待归因的失败串行复跑，25 个通过，余下 5 个经针对性修复后复测通过。文件预览单独串行 15 通过。合并后 `develop` 核心测试 45 通过。
- 前端 257 文件、1,688 用例通过，`tsc -b` 通过，Rust 91 通过、4 个既有忽略用例。真实 QuickJS 复测确认超限调用返回 `agent_model_output_limit`，其他调用继续执行。
- 后端其余失败与 `652528ad` 基线重合。原始重合的 69 个用例中，打包版本夹具已修复。其余 68 个用例 ID 见 `D:\code\rynmesh\artifacts\develop-release-20261119-a0771b86\baseline-failures.txt`，涵盖旧 Agent/模型与传输夹具、旧插件更新版本约束、Ask 预览校验，以及共享测试 venv 从源码路径启动文档 sidecar 时的导入问题。

## 安装和回滚

- 安装包 `D:\code\_build-tools\ryn-overhaul-a0771b86\webapp\src-tauri\target\x86_64-pc-windows-msvc\release\bundle\nsis\Ryn_0.9.1_x64-setup.exe`，SHA-256 `091542EC9EE3C81D8EA5C9D37A7DC34107D9E65A75095FDA58306F0C5A354714`。静默原位升级到 `E:\Apps\Ryn`，安装程序退出码为 0。旧 Ryn 通过应用菜单正常退出。
- 已核对三项安装文件 SHA-256。Tauri 打包时给主程序写入 NSIS 类型标记，主程序哈希按该标记校验。

| 文件 | 安装文件 SHA-256 |
| --- | --- |
| `Ryn.exe` | `094330ED593650108864F90B22C35BEAEBE6F0BB952182C7AFFE3285423B160D` |
| `rynmesh-peer.exe` | `A8D6B51E189B9340C9EFF43CA40EE71739C15FE50840E59556B3D925FBD08D27` |
| `ryn_computer_use.exe` | `830BD7A4EABE55DDF1A149128A21ADCD1A5CE8B9308DB3357FD1822823B071D7` |

- 在当前用户桌面会话打开了 Ryn 窗口，节点与公司插件健康检查均返回 HTTP 200。通过“应用 → 发现”的签名目录安装做图 `image-studio` 1.1.0，页面正常加载。按迁移提示，旧做图分享需在新应用中重新开启。本次仅打开页面，未生成图片。
- 原程序备份位于 `D:\code\rynmesh\artifacts\develop-release-20261119-a0771b86\program-backup`，共 8,143 个文件逐一核对 SHA-256。回滚时在当前用户会话运行 `pwsh -File "D:\code\rynmesh\artifacts\develop-release-20261119-a0771b86\rollback.ps1"`，脚本会恢复原程序、卸载注册项并重启核验。
- 按本次收尾范围，未做安装后真实 Agent 建任务和出图实测。Mac 的 S04-I23、S13-I6 和 GitHub 推送留待本次之外处理。
