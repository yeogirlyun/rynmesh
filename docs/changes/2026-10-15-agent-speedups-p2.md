# Agent 提速与 Codex 线程续用 P2 交付说明

日期：2026-10-15。交付分支：`feature/ryn-integrated-workspace`。
个人仓库为 `yyeogirl/rynmesh`，上游仓库为 `yeogirlyun/rynmesh`；本次将同一开发分支同步到两边，不合入 `main`，不发布或替换安装版。

## 交付范围

提速主体已包含在现有功能快照 `63f9b97` 中：Codex 按调用方和窗口复用进程/线程并发送增量消息，核心工具常驻、工具发现累加、多文件补丁、连续调度、并行只读工具、按模型窗口控制上下文、长输出保留首尾、按任务分文件加密存储与增量轮询。

本次增量提交修复主机验收发现的 P2：权限说明以前随当前写入回执是否存在而增删，改变系统提示普通文字，使首个 shell/write 后及同窗追问重新建线程。现在每步固定附带相同权限说明，只有带标签 JSON 状态行变化；权限回执和宿主授权检查保持原有逻辑。

新增 `test_system_prompt_changes_only_in_state_lines_after_a_write`，确认写入回执仍出现在模型上下文中，同时提示变化能被线程增量规则接受。本次还提交完整主机验收报告，并同步两处 README 的实测状态和存储目录名称。

## 验证结果

指定回归组为 `test_agent_speedups.py`、`test_codex_thread_reuse.py`、`test_agent_codex_loop.py`、`test_agent_default_workspace.py`：**80 passed、0 failed、0 skipped，14.22 s**。

真实 Codex CLI 的隔离后端仅监听回环地址，home 和工作区均为全新临时目录。复测没有访问安装版或原用户数据目录。

| 场景 | 总耗时 | 线程序列 | 结果 |
| --- | ---: | --- | --- |
| C 多文件创建并改函数名 | 38.359 s | started → resumed ×4 | shell 及两次 patch 后续用 |
| F 在 C 同窗追加测试 | 17.328 s | resumed ×3 | 首步延续 C 的同一进程和线程 |
| B 只读 OS 检查 | 23.141 s | started → resumed | shell 后续用，无审批 |

本轮 10 次场景模型调用共 2 次 started、8 次 resumed，fallback=0、rebuilt=0，普通提示文字变化数为 0。F 首步适配器耗时 **5.725 s**，比本轮 C 首步 **13.737 s** 低 58.3%；上次缺陷现场的 F 首步为 19.446 s。首步耗时与场景总耗时不是同一指标，跨轮绝对差值也受上游时段和主机负载影响。

此前三轮适配器对照共 48/48 步成功，后续步骤中位数从 stateless 的 13.343 s 降至 reuse 的 2.686 s。真实旧状态副本中 15 个任务迁移及内容校验通过，单事件只改变目标任务文件和根文件。

## 验收边界

这次 P2 的三个通过标准均已满足，不代表整个广泛测试组全部通过。首次广泛组为 1686 passed、27 failed、4 skipped；其中包含已知 5 项计时 fixture 问题、其他测试前置条件不一致和桌面/固定时限相关失败。前端首次 22 项失败在四个文件的单 worker 复跑中全部通过；参考 worker 的 12 项首消息 BrokenPipe 在主机没有复现。详细失败定位、补跑结果及不能确认的原因均保留在报告中。

没有把其他历史失败改成通过，没有重跑发布构建，也没有安装或部署。原始机器证据仍在本机临时目录，提交的报告仅包含脱敏统计、复现步骤和定位，不包含用户任务正文、账户、金额或凭据。

详细证据：[主机验收及 P2 复测](../acceptance/agent-speedups-20261015/live-results.md)、[Agent 提速说明](../acceptance/agent-speedups-20261015/README.md)、[Codex 会话复用说明](../acceptance/codex-session-reuse-20261015/README.md)。
