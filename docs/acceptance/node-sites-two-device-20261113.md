# 节点网页 + 做图：两台真实电脑验收 — 2026-11-13

状态：构建与安装准备完成，双机验收受阻，尚未完成。仅以下明确记录的检查有实测证据。

## 角色与初始状态

- A：老板本机，安装于 `C:/Users/子陵/AppData/Local/Ryn`，既有安装源码 `1624ddeec29de4824a5245f055abcf59f3a99636`（本次尚未重新安装）。公司 workspace：`bound=true`、`can_publish_models=true`；做图 `model.ready=true`，开放开关初始关闭，队列为空。
- B：局域网 Windows `DESKTOP-SQ6VA8I`，安装于 `C:/Users/ziling/AppData/Local/Ryn`。既有安装回执主程序 `054ba810ffd37be7ac53277f5f13aec9d8173a1d`、公司 `dd25887dcd6575c266cbfbbb08531f3ebc866b32`；此为历史回执，不能替代新安装版本核验。
- B 当前公司 workspace 返回 HTTP 409、`peer_provider_login_required`，需要用户登录已审批成员账号。未复制 A 的身份或登录凭据，未改中心发布人名单。
- B 备份：`C:/Users/ziling/AppData/Local/Ryn-install-backups/node-sites-two-device-20261113/program`；2980 个非日志文件 SHA-256 一致。运行日志因占用不纳入哈希校验。

## 构建输入

- Ryn 已提交 develop：`0e90f8c7c014356a3d49307473ce5973beff8030`。
- 公司三插件已提交 develop：`5a38b8328c3d7250e6051653abe70857dc93b4e0`。
- 独立构建目录：`D:/code/_build-tools/ryn-package-20261113`、`D:/code/_build-tools/company-package-20261113`，已快进到上述提交；原工作区他人修改保留。
- 原始构建日志和本地运行产物留在忽略/未跟踪的 `artifacts/node-sites-two-device-20261113/`，不提交。
- PowerShell 5 在当前继承环境找不到 Get-FileHash，改用 PowerShell 7；Rust 使用已有 `D:/code/_build-tools/rust` 工具链与离线缓存。

## 自动检查

- `test_node_sites.py`、`test_image_studio.py`：20 passed，34.84 秒。首轮断言通过但旧 pytest 临时目录清理权限失败；改为本次独立 basetemp 后正常通过。
- 公司服务打包成功，运行包输入为上述公司 develop；完整包隔离验收通过：首次安装、重复安装保留身份密钥与禁用插件设置、既有 Office 不受影响、三个公司页面正常、公司服务 ready 且 revision 对应锁定提交。

- 前端 NodeSites.test.tsx、ImageStudioOwner.test.tsx：6 passed，47.14 秒；TypeScript/Vite、原生电脑操作组件、原生网络模块、PyInstaller、Tauri 桌面构建通过。
- B 另备份 7 个身份/设置文件，留在 B 本机备份目录，不提交。安装前 Agent 状态仅有 completed、failed、paused，无 running/queued。

## 完整安装包

- 路径：`D:/code/rynmesh/releases/node-sites-two-device-20261113/Ryn-0.9.1-company-0e90f8c7-setup.exe`。
- 大小：140395747 字节。
- SHA-256：`08c67e9fbdbef5f1cd9a69fd861a24b9c98f53891dc64a3b2e5128c1409393da`，已与构建回执再次核对。
- 内容：Ryn 0.9.1、company-workspace、company-dashboard、company-affiliate；WebView2 引导安装器微软签名有效。
- 公司包版本 `2026.11.13.93408`，revision 为锁定的公司提交。
- 构建结束两个独立构建目录受跟踪源码均干净。Tauri 仅改写 Cargo.toml 换行，确认无语义差异后恢复。
- 安装包已传到 B 的 `C:/Users/ziling/Downloads/Ryn-0.9.1-company-0e90f8c7-setup.exe`，大小与 SHA-256 均与本机一致。
- 安装器及原始回执不提交。两台真实机器均未安装本包；A 仍保持初始关闭状态。

## 当前阻塞

- B 公司插件未登录，需要用户在 B 登录已审批成员。
- 自动审批审查拒绝生成 B 端安装及交互会话启动脚本，返回 blocked by policy，未提供具体原因。脚本未写入，B 尚未安装或重启。不绕过该拒绝。
- 窗口工具激活失败，无法完成可靠的 Ryn 窗口截图和交互；这不是产品功能失败的证据。

## 双机实测清单

| 项目 | 状态 |
| --- | --- |
| 完整安装包与哈希 | 通过（见上文）；尚未在真实 B 执行 |
| B 安装后版本和健康 | 未验证 |
| A 开放全部同事，B 列表与可信窗口顶部条 | 未验证 |
| 1:1 文生图 1024×1024 | 未验证 |
| 3:4 海报、准确文字与参考图 1024×1365 | 未验证 |
| 两张排队、等待估计、取消、A 停止与提前 | 未验证 |
| 详情大图、B 下载实际位置、再来一张、改这张 | 未验证 |
| B 重启历史保留、可选第二台设备共享历史 | 未验证 |
| A 队列成员名与用量归属 | 未验证 |
| 排除 B、关闭开放、恢复权限 | 未验证 |
| A 退出后约 15 秒离线、重开自动恢复 | 未验证 |
| route、缩略图与原图延迟 | 未验证 |
| Codex 退出登录失败原因及恢复 | 未验证 |

本次尚未生成测试图。未修改生产中心、未推送仓库。原生窗口工具能读取 Ryn 无障碍树，但激活失败且截图出现其他窗口，不能把该截图作为 Ryn 验收证据。
