# Ryn 五项用户需求总体验收报告

日期：2026-09-04

分支：`codex/user-requirements-suite`

结论：Windows 候选版本及三轮跨需求旅程通过；跨平台最终签字暂不关闭

## 1. 交付范围

本报告覆盖 DEV-01 首次成功、DEV-02 一键本地 AI、DEV-03 统一 Ask Ryn、DEV-04
我的内容、DEV-05 简单配对与分享。每项完整产品/研发文档及证据入口见
[交付索引](../FIVE_USER_REQUIREMENTS_DELIVERY_INDEX.md)。

## 2. 需求级结论

| 需求 | 功能 | 安全/隐私 | 自动化 | UI 标注 | 真实环境 | 当前结论 |
|---|---|---|---|---|---|---|
| DEV-01 | 通过 | 通过 | 通过 | 已归档 | Windows 桌面通过 | Windows 通过；macOS 待测 |
| DEV-02 | 通过 | 通过 | 通过 | 已归档 | Windows 真实模型通过 | 正式支持 Windows CPU |
| DEV-03 | 通过 | 通过 | 通过 | 已归档 | Windows node/Web 通过 | Windows 通过；桌面壳随总回归通过 |
| DEV-04 | 通过 | 通过 | 通过 | 已归档 | Windows 文件语义通过 | Windows 通过；macOS 待测 |
| DEV-05 | 通过 | 通过 | 通过 | 已归档 | Windows 双节点通过 | Windows/LAN 通过；macOS 粘贴待实机 |

## 3. 最终质量门禁

- [x] Ruff 全仓通过。
- [x] Backend 全量：642 passed、1 skipped；skip 为既有环境条件项。
- [x] Webapp：15 个测试文件、60 个测试通过。
- [x] TypeScript typecheck 通过。
- [x] Vite production build 通过。
- [x] Rust format check 通过。
- [x] Windows GNU Tauri release build 通过。
- [x] Windows NSIS 安装器生成成功。
- [x] 打包 `app.exe` 在干净 home 启动真实冻结 sidecar。
- [x] sidecar 健康、桌面窗口出现、真实来源内容进入 ready。
- [x] 关闭验收进程后 sidecar 无残留，端口释放。
- [x] 完整 12 步跨需求 E2E 在三个互不复用的干净目录连续通过。

完整命令、摘要和二进制 SHA-256 见 [FINAL_TEST_RESULTS.txt](FINAL_TEST_RESULTS.txt)。

## 4. 跨需求验收条件

| 条件 | 结果 | 说明 |
|---|---|---|
| 非技术用户无需终端完成五个用户流程 | Windows 通过 | UI 均有任务入口；开发构建命令不属于用户流程 |
| 单机内容不依赖模型、Peer 或 Registry | 通过 | 桌面干净启动在模型/Peer 不可用时获得 30 项内容 |
| AI/Friend 故障不使整站 offline | 通过 | shell health 与子能力状态解耦测试通过 |
| Home、Ask Ryn、My Content、Friend messages 职责清晰 | 通过 | 旧 Peers 仅保留高级诊断路由 |
| 草稿、任务、阅读进度、会话可恢复 | 通过 | store migration/restart/分页测试通过 |
| 本地/好友边界发送前可见 | 通过 | Ask Ryn 跨边界确认和 Friend Review 均有 UI 证据 |
| 用户数据可导出/擦除，秘密默认不导出 | 通过 | unique marker 与 secret-free export 测试通过 |
| 只宣传真实验证的平台/网络 | 通过 | Local AI 只开放 Windows；Friend v1 只承诺 LAN/直接 IP |
| 旧数据与旧 URL 有兼容/回滚 | 通过 | 每项 migration/rollback 文档已归档 |
| 完整 12 步跨需求 E2E 连续三次 | 通过 | 三个干净 home，均为 12/12；真实模型下载/安装、重启、双节点和隐私导出均通过 |

完整步骤、三轮耗时和脱敏结果见
[跨需求 E2E 报告](CROSS_REQUIREMENT_E2E.md) 与
[机器可读证据](CROSS_REQUIREMENT_E2E_3_RUNS.json)。

## 5. Windows 桌面物理证据

从干净 `RYNMESH_HOME` 启动最终 GNU release `app.exe`：

```json
{"health":"ok","desktop_managed":true,"first_success_phase":"ready","content_ready":true,"item_count":30,"source_count":14,"healthy_sources":11,"failed_sources":3,"using_cache":false,"window_title":"Ryn"}
```

本轮桌面审计同时修复了 Windows 的 `.exe` sidecar 查找、`where.exe` 命令发现、
`cmd.exe /C` 高级覆盖、`%LOCALAPPDATA%` 日志目录、进程退出、冻结 sidecar 验证进程树
清理和日志菜单打开方式。
构建工具链全部放在隔离 worktree 临时目录，没有修改系统 Rust、Visual Studio 或 PATH。

## 6. UI 验收

五项均保留原始状态截图和带编号标注总图。DEV-01 另增加键盘焦点专项测试：modal
打开后焦点进入对话框，Tab/Shift+Tab 不逃出，卸载后焦点回到触发元素。

- [DEV-01 标注图](dev-01-first-success/screenshots/dev01-ui-annotated.png)
- [DEV-02 标注图](dev-02-local-ai/screenshots/dev02-ui-annotated.png)
- [DEV-03 标注图](dev-03-assistant/screenshots/dev03-ui-annotated.png)
- [DEV-04 标注图](dev-04-library/screenshots/dev04-ui-annotated.png)
- [DEV-05 标注图](dev-05-friends/screenshots/dev05-ui-annotated.png)
- [DEV-05 内容卡片确认拉取标注图](dev-05-friends/screenshots/dev05-content-fetch-annotated.png)

## 7. 发布阻断项

以下项目不阻断 Windows 内部评审，但阻断“Windows + macOS 全平台最终验收”签字：

1. 在 Apple Silicon 和仍宣称支持的 Intel Mac 上执行干净安装/冷启动。
2. 在 macOS 上验证 managed import 删除不影响原文件，以及 friend secret 文件权限。
这些是缺少当前宿主硬件/运行环境的验收缺口，不是已知功能测试失败。未完成前，发布说明
只能写“Windows x86_64 CPU / Friend LAN 候选版本已验证”，不能写“全平台完成”。

## 8. 本地提交与发布边界

实现和证据均只存在本地独立分支；未部署、未推送、未修改原工作树。生成的 NSIS
安装器仅用于本地验收，未签名、未上传，不应作为公开正式安装包分发。
