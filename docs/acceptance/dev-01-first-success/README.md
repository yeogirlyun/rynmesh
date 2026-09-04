# DEV-01 验收记录：首次启动三分钟获得价值

状态：工程实现完成；自动化、浏览器 UI 与 Windows 桌面物理验收通过；macOS 物理验收待完成。

## 实现基线

- 分支：`feature/user-requirements-suite`
- 实现 commit：`28bc4f55776694db8e1a05bf2a83f37347517aa6`
- 开发环境：Windows，Python 3.12.10，Node 22.22.1
- feature flag：backend `RYNMESH_FIRST_SUCCESS_V1_ENABLED`；Webapp
  `VITE_RYN_FIRST_SUCCESS_V1_ENABLED`，值为 `0` 时关闭。

## 已通过

- node 端持久化、状态聚合、local API、真实 consumption/feedback 事件挂钩；
- 进度文件原子写入、重复事件幂等、显式 reset、关闭后 Home 可继续；
- Webapp 任务式首次流程、真实内容查看、保存偏好、成功页和下一步；
- API 故障不会把整个 Webapp shell 判定为 offline；
- 无模型、无 Peer、Registry 状态不参与阻断判断；
- 全部来源失败显示安全恢复动作，有可用内容时继续；
- 个人数据导出与 `onboarding` 范围擦除；
- fixture 浏览器 E2E：ready → opened → saved signal → completed；
- full backend、Webapp test、TypeScript、production build、Ruff 通过。
- Windows GNU Tauri/NSIS 构建成功；打包后的 `app.exe` 使用干净 home 启动真实
  `rynmesh-peer.exe`，健康检查通过并从 14 个来源获得 30 项真实内容。
- 首次流程键盘焦点限制在 modal 内，关闭组件后恢复到原触发元素；组件测试覆盖
  正向 Tab、Shift+Tab 和焦点恢复。

## UI 证据与标注

- [带编号标注的三阶段总图](screenshots/dev01-ui-annotated.png)
- [推荐就绪原图](screenshots/01-ready.png)
- [真实内容打开原图](screenshots/02-opened.png)
- [完成状态原图](screenshots/03-complete.png)
- 标注源：[ui-annotations.html](ui-annotations.html)

标注 1–3 分别说明：真实 node 里程碑、真实推荐/ContentViewer 事件链、隐私与下一步。

## 自动化结果

- focused backend：54 passed；
- full backend：601 passed，1 skipped；
- Ruff：passed；
- Webapp：10 files / 40 tests passed；
- TypeScript lint：passed；
- production build：passed。

详细命令与覆盖范围见 [TEST_REPORT.md](TEST_REPORT.md)。

## 尚未关闭的验收项

- macOS 干净安装冷启动；
- 部分来源失败、离线缓存的桌面录屏；

Windows 冷启动中 12/14 来源健康、2 个来源失败，应用仍在三分钟内进入 `ready`，
证明部分来源失败不阻断首次价值；离线缓存已有自动化覆盖，但没有伪造桌面录屏。

这些属于物理环境/桌面层验收，不能用 fixture 或 CI 替代，因此本记录不把 DEV-01
标成最终 accepted。代码已保持可继续开发，不阻断 DEV-02。

## 回滚

关闭两个 feature flag 可隐藏 UI/API，不删除 `first-success.json`、consumption、反馈或
缓存。旧版本会忽略该版本化文件。详见 [MIGRATION_ROLLBACK.md](MIGRATION_ROLLBACK.md)。
