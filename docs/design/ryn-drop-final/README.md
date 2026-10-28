# Ryn 设备互传最终设计交付

> 2026-10-13 后续用户修订：设备页恢复原来的「左侧设备列表 + 右侧详情与访问权限」，首页保留网络图。设备页增加会话、传输与右键菜单。以 [UI_REVISION.md](UI_REVISION.md) 和当前产品截图为最新视觉基线；下方 HTML 中的设备网络图保留为历史设计参考。实现进展见 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)。

2026 年 10 月 13 日用户确认的实现基线。已包含开放式布局、网络连线图、设备右键菜单、节点消息和文件传输流程。实现以此目录为准，旧版目录仅作历史参考。

## 从这里开始

1. 打开 index.html，操作设备网络、会话、传输与接收请求。右键设备节点可发送文件、文件夹或消息。
2. 阅读 HANDOFF.md，按最终产品约束、状态契约和验收门槛实现。
3. 将 OPUS_PROMPT.md 的提示词发给 Opus，并让它在 D:\code\rynmesh 中执行。

index.html 可直接在浏览器打开，无需启动项目或访问运行中的后端。需要 HTTP 预览时，在仓库根目录运行：

```powershell
python -m http.server 8769 --bind 127.0.0.1 --directory docs/design/ryn-drop-final
```

然后打开 http://127.0.0.1:8769/index.html。8768 的旧预览不是最终交付的唯一存放位置；本目录可独立保留。

## 内容与权威顺序

| 文件 | 用途 |
| --- | --- |
| HANDOFF.md | 功能、交互、数据边界与正式验收要求 |
| index.html | 用户已确认的交互与视觉基准 |
| source.html | 可编辑的原型片段，可供理解结构与样式 |
| OPUS_PROMPT.md | 可直接复制给实现者的完整任务 |
| 01-devices.png 至 09-context-menu.png | 桌面、手机、深色和右键菜单的视觉参考 |
| verification.json | 原型演示流程、右键入口和多尺寸布局检查结果 |
| verify.mjs | 本机原型检查与截图脚本，运行依赖本机 Playwright 和 Edge |
| manifest.json | 最终文件清单及 SHA256，可用于识别这次交付 |

功能行为以 HANDOFF.md 为准，视觉以 index.html 为准。原型演示捷径不能覆盖正式要求；以后用户明确修改的要求优先。不要为了迁就原型的轻量脚本实现方式而改变项目现有架构。

## 边界

这是设计交付。设备、消息、速度、验证码、容量和任务进度均为示例，原型不会执行真实发送。verification.json 验证的是原型，不证明现有公网引擎支持 100 GB。真实产品能力由实现者按 HANDOFF.md 逐项实现并提供证据。

source.html 和 index.html 已冻结为确认基线。开发生产功能时不要顺手重绘本目录，使对照基线丢失。若用户后续要求更改设计，另存有明确标识的新版本。
