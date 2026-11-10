# Ryn Agent 扩展首版验收记录

2026-10-19，工作目录 `D:\code\rynmesh`，分支 `feature/ryn-integrated-workspace`。
Codex 负责设计、独立复核和验收；应用实现与项目测试由本地 Antigravity 的
`gemini-4-argon-medium` 完成。设计契约见 [ryn-agent-extensions-v1.md](ryn-agent-extensions-v1.md)。

本次完成插件、Skills、MCP 三个管理列表及详情、搜索、导入预览、兼容性提示、
启停、编辑、删除和实际连接测试。入口为 `/agent/extensions`，同时提供
`/settings/extensions` 路由。STDIO 与 Streamable HTTP 的 MCP 工具接入实际
Agent 工具循环；导入的技能可选择、引用并读取附属参考文件。密钥加密保存，
API 掩码返回；工具绑定配置版本，写入错误保留未知结果并阻止自动重放。

兼容范围是 Codex、Claude 和便携插件包中的 MCP 配置与 SKILL.md/附属文件。
专有 Apps、hooks、OAuth 流程不在本版兼容范围，导入时显示提示。旧式 SSE
传输配置明确标为不支持；已接受的 HTTP 能力是 Streamable HTTP 中的 JSON/SSE
响应。内置电脑控制使用 Ryn 自己的 Windows MCP provider，需要单独开启桌面
权限；Ryn 原有浏览器工具继续使用原有入口。没有复用 Codex 私有电脑控制服务。

| 验证范围 | 结果与边界 |
| --- | --- |
| 前端生产构建 | TypeScript 与 Vite 构建成功；保留既有 bundle 大小/动态导入提示 |
| 前端组件与文案 | 3 个测试文件、28 项通过 |
| 扩展后端 | runtime/store/manifest/transport 4 个文件、27 项通过，33.32 秒 |
| 旧资源范围与权限相关检查 | 17 项通过，包括修正真实工具绑定的夹具与全局/任务两层桌面权限 |
| 静态检查 | 改动涉及的扩展/运行时及新增扩展测试 Ruff 通过，git diff --check 通过 |
| 官方 MCP SDK | 真实 STDIO、Streamable HTTP/SSE 的发现、echo、PNG 返回通过 |
| 写入异常 | HTTP 401/403、JSON-RPC 错误、isError、断线与重建均保留未知结果；实际计数 1，不自动重放 |
| 版本与撤权竞争 | 初始化期间停用/撤销权限时零写入；旧工具绑定不能写到替换后的目标 |
| 持久会话 | 首个请求结束后同进程继续下一次调用；真实当前取消与服务器撤权仍生效 |
| Windows 原生完整链路 | 专用测试窗口观察、读取、审批前零动作、批准后一次 set_value、MCP 与原生双重读回通过 |
| 截图与任务隔离 | 两张专用窗口 PNG 经可信 artifact 接口读取且 SHA 一致；另一任务不能复用观察记录；清理进程、缓存和账本通过 |
| 冻结 worker | 独立 PyInstaller EXE 的 initialize/tools/list/ping 通过，3 个工具，无节点目录副作用；不代表完整安装包验收 |
| 实际浏览器 | 插件导入、技能详情、连接测试、停用状态下成功/失败反馈、测试中关闭、浅/深色、320/390px、原生弹窗及焦点恢复通过 |
| 异步页面竞争 | 独立实际 TSX probe 确认关闭后不再卡住，迟到响应不会覆盖替换后的实例 |

原生控制验收只操作了验收脚本创建的 Windows 窗口及其 Edit 控件，截图为该
460×180 窗口；不据此声称任意应用的 UIA、鼠标键盘或真实上游模型识别率已经
验证。完整 Windows 安装包和其他系统的电脑操作尚未验收。

扩大旧回归时原始结果是 324 通过、12 失败。与本次绑定/权限契约相关的夹具
已修正并单独通过。另 4 项旧 local_tools 测试在独立导出的 HEAD `64a5db0`
上同样失败：3 项旧工具拒绝反馈断言和 1 项流式读取预算提示断言，未在本次
扩展范围内改写。还有浏览器 1.5 秒时延断言曾在当前树失败（1.594 秒），
随后阶段观测中当前树 0.764 秒、HEAD 0.881 秒均通过，相关 13 个源文件
归一化哈希相同；没有证据归因于扩展，但不将这次时延失败宣称已修复。
因此不声称整个旧回归套件全绿。

以上是首版交付时的历史结果。随后用户要求继续修复原有四项测试和浏览器时延，
本轮修正、当前源码验收与保留的失败记录见
[后续修正与验收](ryn-agent-extensions-followup-acceptance.md)。

完整本机过程证据保存在 `work/agent-extensions-20261019/`：
`task.json` 记录实际执行模型与会话；`backend-core-final-02.log`、
`frontend-tests-final.log`、`frontend-build-final.log` 为最终构建测试；
`computer-use-runtime-acceptance-review.txt`、`frozen-worker-review.txt`、
`browser-timing-attribution-review.txt` 和 `baseline-comparison-review.txt`
保留独立结果与局限。实际页面截图为 `actual-agent-extensions-final.png`。

可交互预览在 `http://127.0.0.1:19055/agent/extensions/mcp`，使用任务专用节点
目录，没有远程模型或发现；示例插件已恢复原配置并成功发现 4 个工具。
该节点的桌面权限保持关闭。临时 CLI 权限文件已删除，原权限配置已恢复；
用户原有节点和线上服务未部署改动。
