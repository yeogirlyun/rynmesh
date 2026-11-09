# develop 主程序与公司插件打包检查（2026-11-10）

## 源码与范围

- 主程序：`develop`，`cc94d2a3970ce6243894a19aab05af1d9fb55064`，版本 `0.8.0`。
- 公司插件：`develop`，`d86f942b3d7e60364d58b7dc2b85ada1ff1d8744`。
- 主程序使用 `D:/code/rynmesh`；插件使用干净工作区 `D:/code/_build-tools/company-develop-20261102`。两者构建时均无已跟踪文件改动。
- `D:/code/chat-backend` 的原工作区仍在旧任务分支且有大量未提交修改；没有切换、提交、覆盖其内容。
- 本轮只检查、构建和测试，没有修改产品代码，没有更新正在使用的安装或用户数据，没有发布远程资源。

## Claude 已集成的主要修改

主程序近期合并包括：插件页在短暂健康检查失败时保持打开；OAuth 更新等待时间调整；默认自动处理模式；工具错误不再永久锁定对话；停止、重启和结果不确定后可以继续对话；长对话压缩和自动接续；本地及共享模型的图片能力判断；插件工具目录后台及每回合刷新。

公司插件近期合并包括：登录及页面稳定性；明确企业微信审批查找能力和工具只读标记；聊天历史使用稳定引用；历史读取不受发送引用的 15 分钟时限影响。代码仍对发送检查有效期、账号、所有者和会话名称，读取也检查账号与所有者。插件进程重启或账号切换后仍需重新获取引用。

尚未进入 develop 的更新机制没有打入本包：主程序 `claude/codex-style-updates-20261109`（`789788c`、`e8f1b0d`），插件 `claude/service-pack-updates-20261109`（`932fe3b`）。

## 本机验证

| 检查 | 结果 |
|---|---|
| Agent 稳定性、长对话、图片能力、目录刷新、权限、共享模型、完整安装辅助程序等 13 个 Python 测试文件 | 519 通过 |
| 公司插件历史读取、只读声明、桌面服务和首次安装 4 个文件 | 24 通过 |
| Office 兼容安装、编辑器插件、图片路由及载体 4 个文件 | 81 通过、3 失败 |
| Agent、输入框、模型选择、错误说明及插件页面 7 个前端文件 | 148 通过、1 失败 |
| 桌面文件导入 Node 合约检查 | 5 通过 |
| 打包节点的两轮启动，真实终止进程树后重启 | 通过；约 7.56 秒、8.00 秒 |
| 打包节点健康、首页、Agent 设置、插件目录和对话接口 | 均 HTTP 200；默认权限 `automatic` |
| 公司插件打包服务三轮冷启动/强制重启 | 通过；约 4.922、1.593、1.609 秒；保留配置密钥及加密设置 |
| 完整包辅助程序首次安装、重复安装 | 通过；保留身份密钥和禁用插件设置 |
| 完整包三个公司插件页面与服务健康 | 通过；服务 `ready`，公司出站网络被阻断 |

总计 **777 通过、4 失败**，不是全绿。插件首次测试环境缺少 `tzdata`，补齐后 24 项全过；实际服务构建使用已有且依赖齐全的 `wecom-local-chat-venv`（Frida 17.24.3、psutil 7.2.2、tzdata 2026.6、PyInstaller 6.22.3）。

4 个最终失败均在导出的上一版 develop `fe17e67` 同环境、同断言复现：

- `test_agent_image_routes.py` 的 `test_codex_cli_image_inspect_preserves_turn_start_image_items_and_keeps_store_base64_free`：模型请求数 2，预期 3。
- 同文件 `test_inspect_image_uses_model_configured_output_up_to_16384_tokens`：模型请求数 2，预期 3。
- 同文件 `test_antigravity_cli_and_explicit_negative_models_refuse_both_tool_and_existing_canonical_images`：拒绝记录数 0，预期 1。
- `RynAgent.test.tsx` 的 `can select autonomous workstation execution and retain it in the workspace cache`：切换后找不到测试预期的 `Full access` 文案。

因此本轮选定测试未发现新增失败，但不代表所有功能或全量测试通过。未调用真实模型、未操作真实公司业务数据、未进行桌面 GUI 全流程验收。完整包测试运行的是安装辅助程序及打包服务，没有执行系统安装器。

## 已知交付限制

最新 develop 不再内置 Office 编辑器，而改为首次安装后下载。2026-11-10 本机对代码内发布地址的 Word 资源发送 HEAD 请求，返回 **HTTP 404**：

`https://github.com/yeogirlyun/rynmesh/releases/download/office-cjk-1/wordcraft-web-0.6.1-ryn-cjk-1.zip`

因此不能宣称全新安装后的 Office 下载可用。本次保持指定 develop 源码，不擅自混入旧版打包策略或发布远程资源。交付定位为带已知限制的候选包。

## 产物与回滚

产物目录：`D:/code/rynmesh/artifacts/develop-release-20261109-cc94d2a/`（11 月 9 日启动打包，11 月 10 日完成验收）。

- `Ryn_0.8.0_cc94d2a_x64-setup.exe`：主程序安装包。
- `Ryn_0.8.0_cc94d2a_company-d86f942_full-setup.exe`：主程序与 workspace、dashboard、affiliate 三个公司插件的完整候选包。
- `company-service-d86f942.zip`：独立公司插件服务包，含完整文件哈希清单。
- `company-workspace.zip`、`company-dashboard.zip`：插件声明包。
- 完整包旁的 `.exe.json` 保存两个源码提交、文件哈希和完整安装验收结果；`SHA256SUMS.txt` 保存交付文件校验值。
- 回归日志、旧版对照日志、`node-smoke.json`、`company-runtime-qa/verification.json` 留在本机产物目录，运行数据不提交。

本次未替换现有安装，因此无需执行回滚。原有 `artifacts/develop-release-20261109-fe17e67/Ryn_0.8.0_x64-setup.exe` 等旧包保留；日后实际更新本机前仍应备份当前程序并按项目规则验收。
