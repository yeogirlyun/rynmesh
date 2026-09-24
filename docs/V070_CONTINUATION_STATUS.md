# v0.7.0 后续执行状态

更新：2026-09-18。用户要求四项产品想法也必须实现。现已完成全部十二项代码交付，分别提交独立 PR；尚未合并或发布。维护者评审和外部实机验收不能计作完成。

## 本轮补齐的五项

| Issue / PR | 已实现与已验证 | 产品边界 |
|---|---|---|
| #73 / [#77](https://github.com/yeogirlyun/rynmesh/pull/77) 同步健康 | 逐设备最近确认、积压、拒绝、过期/失败/截断状态；安装包页面验证缺少 ACK 和积压数量 | 最近确认不等于在线或双向全量一致；本机隔离与采集失败单独显示 |
| #74 / [#78](https://github.com/yeogirlyun/rynmesh/pull/78) 好友离线与提问 | Friend updates 与 For You 接入统一保存/提问；安装包完成下载、正文阅读、来源展示、进入 Ask 材料审核 | 不自动发给 AI；已保存副本保留；本轮浏览器没有断开来源或运行模型 |
| #75 / [#79](https://github.com/yeogirlyun/rynmesh/pull/79) 好友本周分享 | 每日摘要本地聚合、当前权限/关注过滤、去重、UTC 周一边界及上限说明；安装包出现真实测试卡片 | 不把好友私有内容送入公开摘要、邮件或 AI |
| #76 / [#80](https://github.com/yeogirlyun/rynmesh/pull/80) 共享阅读清单 | 两好友显式加入，双方添加/删除、各自读过状态，离线待确认队列、重试与撤销；安装包双节点新增与读过状态汇合 | 创建端串行确认；仅两人清单；不共享整份私人阅读历史，不收回已有副本 |
| #68 / [#69](https://github.com/yeogirlyun/rynmesh/pull/69) 跨设备清理运行时 | 六类已有清理适配器、接收端本地审核、持久化计划、部分失败恢复、绑定回执与原始目标全集确认；三节点 HTTP 和安装包状态验证 | 整类本地清理，不是按跨设备记录 ID 精确匹配；每个接收端确认自己的全部审阅记录；旧本地回执保持原语义 |

## 前七项交付

- #23 / [PR #60](https://github.com/yeogirlyun/rynmesh/pull/60)：direct 流式、取消、恢复和归档。真实本地 GGUF 已观察生成中片段、两次成功归档及一次取消；本轮又验证了真实模型 SSE 重连、权限撤销、模型进程退出/重启、提供端/消费端进程强制退出与恢复；详见集成验收记录。
- #61 / [PR #62](https://github.com/yeogirlyun/rynmesh/pull/62)：好友分享进入 For You 主排序并展示来源。
- #63 / [PR #64](https://github.com/yeogirlyun/rynmesh/pull/64)：三至五好友链路诊断、原因与恢复；同机真实 TCP 故障/恢复已验收，跨公网物理设备尚未验收。
- #26 / [PR #66](https://github.com/yeogirlyun/rynmesh/pull/66)：共享服务 descriptor/hook 与五个界面迁移。真实付费视频、出口及视频订单持久化恢复不属于共享框架已完成能力。
- #65 / [PR #67](https://github.com/yeogirlyun/rynmesh/pull/67)：隔离文案、状态缓存、拒绝集合容量、按角色隐藏校验码。安装包隔离和诊断截断已验证。
- #30 / [PR #71](https://github.com/yeogirlyun/rynmesh/pull/71)：邀请创建前的可达范围说明；安装 wheel 双节点邀请、首次消息、重启保留已验证。支持桌面安装器的完整首次路径仍待实机。
- #41 / [PR #72](https://github.com/yeogirlyun/rynmesh/pull/72)：ryn/rynmesh 深链注册、冷/热启动入口和本地审核；两个 macOS 架构编译已通过，OS 实际唤起尚未验证。

## 集成与验收

十二个 PR 合入独立工作区 D:/code/rynmesh-worktrees/v070-followup-integration；无残余合并冲突。生产代码集成提交 dce6cf9，后续 0f54ab3 合入按 pair ID 验证回执的测试修正，以及已有 Settings 焦点断言修复（只保留了一个冲突的说明注释，产品代码无冲突）。

- 后端：1626 passed / 29 skipped，4 warnings。Windows 跳过项未算通过；Linux CI 独立验证各 PR。
- 前端：Node 22.22.1，398 passed / 63 files；TypeScript、生产构建、Ruff 和 diff 检查通过。
- CI 曾发现清理测试错误依赖随机配对 ID 的列表顺序，已在 b208fbb 改为按 pair ID 验证两个目标，8 项相关测试复测通过；Settings 测试也补上 focus effect 等待，5 项相关 UI 测试通过。#69 最终 1c38af8 的 9/9 CI 全部通过并已转待评审。#77/#78/#79/#80 最新头提交也均 9/9 CI 通过。
- 从 clean archive 构建并安装 wheel，在三个全新临时数据目录上通过实际 HTTP 和浏览器验证五项新增流程。不是仅运行开发服务器，也没有替换页面状态 API。
- wheel SHA-256：e8368c5b95a06d0d7f2b0d579354c2bdf240d027b0820d9fa5f12bc8e5a3d9a9。
- 清理提案和本地数量审核通过浏览器执行；仅针对自建临时数据的清理执行由验收脚本调用 owner API，浏览器验证单端完成不误报、全部回执后确认。未删除用户实际内容。

详细验收记录：D:/code/rynmesh-worktrees/v070-followup-integration/docs/acceptance/v070-followup-integration-20260918.md。各新 PR 描述亦已补充对应安装包证据。

最后核对：全部 12 个 PR 最新头提交均为 open、非草案、9/9 CI 成功，尚未合并。临时浏览器页、验收节点及模型进程已关闭。集成验收报告提交 397ad18。

## 仍未完成

1. 维护者评审、合并、发布。这十二个 PR 交付不等同于新版发布。
2. 支持桌面安装器及操作系统深链唤起实机验收（#30/#41）；当前 Windows 环境不具备受支持 macOS 桌面安装验收环境。
3. 不同物理设备/公网/NAT 网络验收；本轮真实模型与进程故障验证均为 Windows 本机 CPU，不能代替跨机器、支持桌面和 GPU 环境组合。
4. 旧 issue #16/#17/#24/#25 已给出可关闭证据与 v0.7.0 链接，但当前 GitHub 账号关闭操作返回 403，需维护者关闭。#30/#41 保留上述验收项。

当前不存在“只评估未实现”的四项产品想法，也不存在仅设计的 #68 运行时。所有外部限制都保留在这里，不把它们伪装成已完成。
