# Ryn Agent 扩展后续修正与验收

本页记录 MCP / Skill / Computer Use 首版之后的本轮修正和可核验结果，面向代码 reviewer。初始范围见[首版设计](ryn-agent-extensions-v1.md)。应用与项目测试由 Antigravity Gemini 4 (`gemini-4-argon-medium`) 修改，Codex 设计、审查并独立执行验收。原有四项失败、完整浏览器和固定三次时延验收已通过。扩大回归出现的另两项失败在独立、无其他测试负载的一轮原样复测中均通过；不将此表述为整套旧回归一次运行全绿。

原有四项失败已按实际工具协议修正。三个 malformed call 用例现在验证：可识别 call ID 的参数或工具错误生成配对的 `role=tool` 拒绝，保留有效兄弟调用，修正后的调用只执行一次；不通过重放整批响应来恢复。无效或重复身份、截断响应等整体协议错误仍在派发前拒绝。测试夹具同时按 Windows 换行和真实 receipt / event 结构修正，保留零非法派发、兄弟调用不重复等断言。

第四项是 streamed read 的预算提醒缺口。普通与流式路径共用预算策略；只有实际发布新工具消息时才消耗提醒等级，同批提醒只附在最后一条新消息的副本上。重复、失效或恢复路径不会污染原始 result、receipt、event、journal 或结果引用。独立真实加密存储验收覆盖 14 组边界并通过，详见[预算发布验收](../../work/agent-extensions-20261019/budget-publication-acceptance-review.txt)。

Python 访问 Chromium loopback CDP 时复用一个经过验证的 SSLContext：certifi 信任链、CERT_REQUIRED、check_hostname 保留。每次请求仍使用独立短生命周期 Client，关闭环境代理继承和重定向，并关闭连接；可重试的 GET 受调用预算约束，可能产生副作用的 PUT 不自动重试。短 Client 的 warm HTTP 微基准为 18–37ms，通过不等同于整条浏览器工具调用满足时延断言。相关实现见 [local_browser.py](../../rynmesh/agent_runtime/local_browser.py)，隔离性与边界审查见[只读报告](../../work/agent-extensions-20261019/browser-suite-order-readonly-review.txt)。

浏览器写操作采用新的原子派发保留接口 `OperationLedger.begin_dispatched`。在原跨进程文件锁内，完整 binding、preview、host classification、实际 host PID 和 receipt reserve 与 `status=dispatched` 一次加密、原子并同步持久化，然后才允许 perform。正常新浏览器写成功有两次 durable commit：动作前的 dispatched 保留和返回结果前的 confirmed 回执；取消、失败或恢复路径可能增加写入。

旧 `begin` 仍保持 intent 语义，Shell、旧 computer 和 MCP 沿用各自原流程；独立探针确认 Shell 正常路径仍为三次 commit。最初的 ledger.get、预览一致性、容量与保留空间、retired / binding 冲突、created=False 分支和每次实际动作及等待前的新鲜授权检查均保留。已 confirmed 的同 operation ID 重试先返回原结果，不因新预览失效而重做动作。

dispatched 表示进入不可自动重放的派发保留区，不证明外部动作已发生。首笔提交后即使崩溃于动作之前也维持 unknown；动作可能已发生而确认未落盘时同样阻止重放。提交后确认取消且 perform 尚未开始，才同步记录 not_executed；取消回执或确认回执写失败不能反推“未执行”。

| 验收项 | 当前结果 | 证据与限制 |
| --- | --- | --- |
| malformed 三类错误及 mixed 有效兄弟调用 | 已修正，当前源码回归中通过 | [设计与断言说明](../../work/agent-extensions-20261019/followup-fourfail-design.txt)；原有四项均不在最新失败列表中 |
| 流式预算发布与原始结构隔离 | PASS，14 组独立验收 | [真实发布结果与审查](../../work/agent-extensions-20261019/budget-publication-acceptance-review.txt) |
| verified SSLContext 与短 Client | 专项 PASS；warm HTTP 18–37ms | [原始请求结果](../../work/agent-extensions-20261019/browser-request-06-results.json)；全链浏览器验收另列 |
| 原子提交新增项目测试 | PASS，2 项，100.45s | 验证完整首次 dispatched 记录、两次提交及提交后取消；[项目测试文件](../../tests/test_agent_browser_paging.py) |
| 原子提交独立磁盘 / 硬杀 / 并发验收 | PASS，8 组，63.968s | [最终报告](../../work/agent-extensions-20261019/browser-ledger-combined-dispatch-acceptance-review.txt)、[v2 原始结果](../../work/agent-extensions-20261019/browser-ledger-combined-dispatch-acceptance-result-v2.json) |
| 较大回归 349 项、旧 local 35 项 | 已通过，但均为原子优化前结果 | 不能代替当前源码回归 |
| 当前受影响四文件回归 | 83 PASS / 2 FAIL，812.15s | [完整日志](../../work/agent-extensions-20261019/followup-atomic-affected-final.log)；另两项失败为原生 UIA 读回与 isolated host 30s 超时，原有四项已通过 |
| 上述两项独立控制复测 | PASS，2 / 2，9.34s | [原样复测日志](../../work/agent-extensions-20261019/followup-extra-two-quiet-final.log)；同一源码、原断言和 20s / 30s 限制，不注入夹具、不重试动作、不改实现；UIA 4.47s、host 2.80s |
| 当前完整 browser 17 项 | PASS，17 / 17，103.70s | [完整日志](../../work/agent-extensions-20261019/followup-browser-atomic-final.log)；原 1.5s 断言保持不变 |
| 固定 3 浏览器验收 | PASS，3 / 3；0.798708 / 0.964876 / 0.988221s | [全部样本、报告与源码哈希](../../work/agent-extensions-20261019/browser-final-three-20261019-150915-b2a5a561/summary.json)；独占、串行，各新解释器；不作为持续 SLA |
| 当前预览实例 | 已重启加载最终原子版本，HTTP 200 | [重启结果](../../work/agent-extensions-20261019/preview-followup-refresh-result.json)；同一 home、配置版本与启停状态保留，重启前 task 数 0 |
| 当前静态检查 | PASS | 八个相关源码/测试文件 Ruff 与 git diff --check 通过；CLI 临时权限文件不存在 |

独立八组使用实际加密 ledger、真实 LocalTools.execute / reconcile、独立临时 home 和仅写自有计数器的 perform fixture。它们没有启动 Chrome，也不构成真实浏览器时延或原生动作验收的替代。

| 独立边界 | 已确认行为 |
| --- | --- |
| 持久化顺序与旧路径 | 浏览器 perform 前可从新 ledger 实例读到完整 dispatched；浏览器两次、Shell 三次 commit，动作各一次；旧 begin 形状不变 |
| 首次提交后、动作前硬杀 | 计数 0，重启仍 unknown，同 ID 禁止重放 |
| 动作落盘后硬杀或确认写失败 | 计数保持 1，unknown，不执行第二次 |
| confirmed 后硬杀并重建 tools | 返回原结果；预览、派发和新增 ledger 写均为 0 |
| 提交后取消及取消回执失败 | 零 perform；成功持久化才为 not_executed，失败保留 dispatched / unknown |
| 两个真实进程竞争同 ID | 一个赢家；created=False 不覆盖赢家 PID、分类、预览和保留空间；实际计数 1 |
| 容量、预览、冲突与 retired | 完整新增字段计入字节边界；保留其他操作回执空间；拒绝路径零动作、原记录不变 |
| 提交前和提交后 I/O 异常 | 均零 perform；依据真实磁盘状态恢复，不能由异常推断允许重放 |

这些结果绑定同一稳定源码快照：operation_ledger `e1cf714b…`、local_tools `f2899823…`、local_browser `53bf4684…`。执行前后完整哈希一致，完整值在 v2 结果中；所有自建 worker 和 launcher 已回收。Windows venv 的 thin launcher 与实际解释器 PID 不同，最终硬杀证据通过 marker、CIM 精确命令行与父链、owned Job 归属核实后直接终止实际 worker。首轮探针 PID 断言错误的原始记录保留，未作为最终硬杀证明；修正细节见[最终报告](../../work/agent-extensions-20261019/browser-ledger-combined-dispatch-acceptance-review.txt)。

优化前完整浏览器验收曾三次出现 14 PASS / 1 FAIL（1.813s、请求到达观察、1.796s），也有一次 full-wire 15 PASS。相关观察夹具与 PID 修正、原始日志均保留，不能用单例通过覆盖完整上下文失败。历史入口见[顺序与会话审查](../../work/agent-extensions-20261019/browser-suite-order-readonly-review.txt)、[持久化成本审查](../../work/agent-extensions-20261019/browser-local-execute-cost-review.txt)和[原子提交验收](../../work/agent-extensions-20261019/browser-ledger-combined-dispatch-acceptance-review.txt)。

受影响四文件回归与独立磁盘 / 硬杀探针曾并行，后续两项控制复测则在其他测试结束后串行执行。原生 Desktop 源码与 HEAD 一致，该测试 AST 也未改；Shell case 直接使用旧 begin/update，未进入新 browser 分支。超时 case 的原目录中连首次 key 文件和 agent 目录都不存在，无法据此归因于 Shell / atexit；UIA 原日志没有实际读回值，也不能证明异步读回缺陷。两项都只进行了一轮独立控制复测且通过，因此未凭现有证据修改这些实现或延长测试限制；真实重负载根因仍未确定，历史失败保留。[UIA 审查](../../work/agent-extensions-20261019/native-uia-readonly-review.txt)、[host 审查](../../work/agent-extensions-20261019/shell-host-timeout-readonly-review.txt)。

真实完整上下文观察到 ledger 落盘耗时波动，三次 fsync 合计曾约 0.601s，独例约 0.002s；本轮合并了一个冗余提交，没有关闭 fsync、降低授权新鲜度或把完成回执异步化。导航等待预算覆盖导航响应与 DOM 等待，并非包含预览、权限读取、持久化、浏览器启动 / 新标签创建和结果确认的全链硬 deadline。最终完整套件和固定三次均通过原断言，正常两次 durable commit 仍受真实磁盘延迟影响，有限本机样本不能保证任意负载下的硬 deadline。

关于 Agent 是否能给自己创建 Skill：模型可以通过已有文件工具创建或编辑 SKILL.md 及支持文件。当前没有提供原生“自行安装 / 导入 / 启用 Skill 或 MCP 插件”的 Agent tool；安装和启用仍通过现有配置界面及管理接口完成。能写出 Skill 文件不能作为该 Skill 已安装启用的证据。

后续真实企业微信任务未通过：Ryn + Gemini 4 经正式 computer-use MCP 找到了企业微信窗口，但首个 focus 写操作中断为 `agent_write_outcome_unknown`，未进入联系人搜索或消息输入 / 发送。该应用仅返回少量顶层 UIA 控件，CLI 原生视觉链也存在能力限制。专用测试窗口与上述浏览器测试通过不能作为企业微信已可用的证明；[真实任务记录](../../work/agent-extensions-20261019/wecom-control-live/acceptance.md)保留具体边界和证据。
