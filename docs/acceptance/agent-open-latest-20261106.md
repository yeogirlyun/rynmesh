# Agent 对话切换直接显示最新消息（2026-11-06）

## 现象

切换 Agent 对话时，内容先停在顶部，再一路滚动到底部。

## 原因

- `webapp/src/screens/RynAgent.css` 的 `.ra-workspace .ra-scroll` 设置了 `scroll-behavior: smooth`（10-28 起），所以打开对话时把 `scrollTop` 设到底部会播放成滑动动画。
- 打开时只有 `failed / interrupted / paused / waiting_approval / waiting_input` 会移到底部；已完成、运行中的对话不移动，停在上一个对话留下的位置或顶部。
- 定位放在 `useEffect` 中，可能先画出顶部一帧。

## 修改（分支 `claude/agent-open-latest-20261106`）

`webapp/src/screens/RynAgent.tsx`：

- 任意状态的对话在打开时都定位到最新消息；用 `useLayoutEffect` 在绘制前完成。
- 这一次跳转临时关闭平滑滚动（行内 `scroll-behavior: auto`），跳转后恢复，页面内其他跳转（对话导航、发送后定位）仍然平滑。
- 稍后才到的部分（文件列表、节点返回的新版本）在 2 秒内继续保持在底部；用户滚轮、触摸、按键或点按后立即停止。之后的刷新不会移动位置（原有行为）。

## 验证

- `RynAgent.test.tsx` 更新/新增：所有 7 种状态打开即到底部且之后的刷新不覆盖用户滚动；两个已完成对话之间切换，唯一一次跳转发生在 `scroll-behavior: auto` 下且之后恢复；稍后增高的内容保持底部，用户滚动后不再拉回。新增与扩展的 4 个用例在修改前的代码上失败，修改后通过。
- `RynAgent.test.tsx` + `RynAgentDeviceRecords.test.tsx`（`--maxWorkers=1 --no-file-parallelism`）：89 通过，1 失败；失败项 `can select autonomous workstation execution and retain it in the workspace cache` 在 develop 上同样失败（已知）。
- `tsc --noEmit`：无错误。
- Chromium（内置浏览器）对照页，滚动容器同样设置 `scroll-behavior: smooth`、可滚动 11900 px：原写法赋值后立即读取为 0（动画进行中）；新写法立即为 11900，行内样式恢复为空，计算值仍为 `smooth`。

## 未验证

- 未在用户桌面版 Ryn（真实长对话、真实模型）中目视确认；需从 develop 构建安装后检查。
