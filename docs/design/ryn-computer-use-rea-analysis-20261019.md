# Ryn 与 Codex 电脑控制链路：REA 对照与故障诊断

2026-10-19，针对真实企业微信任务 `agent_a9af98ce69c34e9bad9581b2434c9409` 的后续检查。

结论：当前实现存在实质缺口和两项已复现缺陷。Ryn 并未复用 Codex 的完整电脑控制引擎；插件格式、Skill 导入和界面相似不能证明宿主运行时、原生聚焦和模型视觉能力相同。企业微信那次 focus 中断的底层异常没有保存，目前仍不能唯一确定其历史原因。

本轮使用 REA 检查已安装的 Codex 插件、`@oai/sky` JavaScript 和原生 PE，通过 Ghidra 12.1.4 反编译相关聚焦函数；另以当前 Ryn 源码、实际 STDIO、独立加密 ledger 和纯本地假桌面对象重现错误路径。没有修改应用或项目测试，没有操作真实企业微信窗口，也没有输入或发送消息。

## 1. 实际执行的引擎不同

当前安装版 Codex `computer-use/26.1013.40934` 插件只有 manifest、Skill、三份文档和图标，共六个文件。manifest 声明 skills 及调用既有 `node_repl.turn_ended` 的生命周期 hooks，没有携带可独立启动的 MCP 服务或电脑控制原生程序。Skill 依赖 Codex 宿主的 `cua_node/@oai/sky`。

`@oai/sky` 当前版本 0.7.8。Windows JS 经过宿主受信任 RPC，选择原生管道或默认的 `bin/windows/codex-computer-use.exe`。Ryn 的内置 MCP 则由 `sys.executable -m rynmesh.agent_runtime.extensions.computer_use_server` 启动本仓库 Python provider，调用 `local_desktop.py` 的 ctypes / PowerShell UIA 实现。Ryn 接受 Codex/Claude 导入格式，不会因此获得 Codex 宿主的运行时服务。

依据：[JS、manifest 与文件哈希审查](../../work/agent-extensions-20261019/rea-codex-sky-static-review.txt)、[原始与可读 JS 映射](../../work/agent-extensions-20261019/rea-sky-js/source-manifest.json)、[Ryn 默认 worker](../../rynmesh/agent_runtime/extensions/store.py:311)。

## 2. 原生聚焦逻辑没有完整复刻

已安装的 Codex 原生程序为 x86-64 PE，1,551,152 bytes，SHA-256 `b6b0e4979b89e5c22dab866f0974bc817c0d08cb359e1c2dd7be3dccb9e181eb`。REA/Ghidra 观察到 3,845 个函数及 1,599 个已定义字符串；函数和字符串数量不是完整源代码恢复声明。

反编译的聚焦例程 `0x1400af6f0` 包含最多 20 轮尝试：检查前台窗口与目标关系，获取当前 / 目标 / 前台窗口线程，关联输入线程，调用 ShowWindow、BringWindowToTop、SetFocus、SwitchToThisWindow 和 SetForegroundWindow；失败时还有键盘事件恢复分支，之后解除线程关联并再次校验。入口 `0x1400a8c67` 还处理最小化状态并调用此例程。辅助窗口关系函数 `0x1400abd23` 包含根窗口关系判断，不能简化成只判断一个 HWND 相等。

Ryn 当前 `local_desktop.py:204–212` 对普通输入先调用一次 ShowWindow、一次 SetForegroundWindow，随后立即比较 GetForegroundWindow 与目标 HWND，不包含上述有界恢复流程。这是已确认的实现差异；没有实测证明 Codex 对这台机器的企业微信一定能成功，也不能据此倒推出旧 Ryn focus 失败必然是焦点限制。

主要证据：

- 原生概览 `ev_27c70dba1370de566f28f675cf6f08ce3986567a5997644b613a7a7f14451e89`。
- 聚焦例程 `ev_d2e1b2c5562cd3e55f5b0d0c6e77b876e05266b3f7427420c10c153c23879ec6`。
- 聚焦入口 `ev_afe302dbdbb703ce26535c2418c6f72f07dfe90d768cda968b02a5968e00ab76`。
- 前台校验 / 窗口关系 / SetFocus / 键盘分支均保存在 [原生证据目录](../../work/agent-extensions-20261019/rea-native-codex)。反编译结果是分析输出，未执行这些函数。

还找到 Codex 的坐标命中检查例程 `0x1400a956e`：经 WindowFromPoint 取得实际命中窗口，并检查目标窗口关系、PID 和相关区域，不匹配时返回错误构造路径。证据 `ev_0ba0144b601b491437ffc0e08be7dd9d40b92a98b065b8b54bf607e67c6215b9`。Ryn 目前只检查坐标落在目标矩形内，再注入鼠标事件；矩形内坐标不能单独证明命中了目标窗口。此处静态证据不等同于完整还原 Codex 所有点击或截图状态规则。

## 3. 企业微信所需的视觉链不完整

真实任务的 MCP read_window 只返回 Window 和两个 Pane，没有搜索框、联系人和输入区的内部控件。这已经说明标准 Edit 控件测试不能覆盖企业微信的界面。

Codex JS 的 get_window_state 会把截图 URL 交给宿主 image emitter；静态代码证明存在图片输出通路，但不证明某次历史模型请求收到了图片。Ryn 的 MCP adapter 把验证后的图片存为 PNG，返回路径、哈希和文字说明，要求后续调用 `ryn_image_inspect`；而 `runtime.py:496` 限定该工具只能使用支持视觉的 local_api 来源。这次使用的 Antigravity CLI 因而不能沿当前产品链路读图。

此外，Ryn provider 目前使用 ImageGrab 采集当前屏幕的窗口矩形。采集成功标志不能单独证明画面没有遮挡、仍属于目标应用或可供下一次输入使用；真实遮挡与过期截图场景尚未验收。

依据：[adapter 图片处理](../../rynmesh/agent_runtime/extensions/adapter.py:588)、[模型视觉入口](../../rynmesh/agent_runtime/runtime.py:496)、[provider 截图](../../rynmesh/agent_runtime/extensions/computer_use_server.py:175)、上述 JS 审查与原始 WeCom 任务记录。

## 4. 已复现：底层异常诊断被丢弃

`extensions/adapter.py:855–866` 在 pre-dispatch hook 之后遇到异常，只抛 `agent_write_outcome_unknown`，没有持久化原始 RPC / transport 错误码。上游原本保留了可识别的 agent_* 错误。

隔离探针使用真实 provider dispatch、Desktop.execute、STDIO manager 和加密 ledger，仅替换实际桌面 / User32 为假对象。两种不同情况——聚焦被拒、执行前窗口身份变化——都被降成相同的 dispatched 记录，error/error_result 为空；正常聚焦对照则 confirmed。三组均通过，证明诊断丢失，不证明历史企业微信属于其中哪一种。

修复时应保存有界、经过过滤的错误码、异常类型、请求 / 会话标识和执行阶段，继续保留 unknown 与禁止自动重放；不能仅凭 RPC 错误就推定没有副作用。

依据：[源码与隔离验证](../../work/agent-extensions-20261019/wecom-ryn-source-rootcause-review.txt)、[三组原始结果](../../work/agent-extensions-20261019/wecom-fake-provider-diagnostic-result.json)。

## 5. 已复现：MCP 并发首连会关闭正在执行的会话

`mcp_transport.py:1123–1180` 在锁外初始化候选会话，发布时无条件关闭缓存中的 current。两个同 server / scope 的首连竞争时，后完成初始化的一方可以关闭已经开始执行请求的健康会话。

真实 STDIO 探针控制交错：A 的 tools/call 已由自有服务记录到磁盘，B 才发布自己的会话并关闭 A。A 返回 `agent_cancelled, dispatched=True`，B 正常成功。没有引入调用重试，所有自建进程和线程均已清理。这是可复现的一般传输缺陷，但旧 WeCom 轨迹之前已有顺序读取，未证明发生了该首连竞争。

修复应采用同 key 单次连接，或发布时保留已就绪且身份 / 版本匹配的赢家，只关闭未发布的候选。

依据：[竞争审查](../../work/agent-extensions-20261019/wecom-stdio-first-connect-race-review.txt)、[原始交错证据](../../work/agent-extensions-20261019/wecom-stdio-first-connect-race-result.json)。

## 已排除的说法与尚未知内容

- 当前实现的持久取消回调只绑定 server / plugin 身份与配置，不捕获首次读取的 control_revision；真实 STDIO 对照也不支持“审批导致旧读取回调关闭会话”的假设。
- 当前内置电脑工具的同一 task 读写使用相同会话 scope；没有找到因读写切换就必然更换 provider 的依据。相关 HWND / ctypes 宽度检查也未解释该次 focus。
- 历史 focus 仅有约 0.367 秒后中断及 dispatched ledger，无底层回复、会话 PID或异常。确切原因无法从现存记录恢复，不能凭候选填补。
- Swift 辅助程序确实存在，但本轮未反编译该 56 MB 程序，也没有验证当前 Codex 历史操作选择了哪种后端；不声称已逆向全部 Codex 电脑控制。
- 没有运行 Codex 或 Ryn 对真实企业微信的输入动作。本轮诊断未输入或发送“测试电脑控制”。

## 修复与验收顺序

1. 保存底层诊断并修复 MCP 并发首连；保留原来的持久化、撤权和未知写入不重放语义。
2. 补齐 Ryn 自有 provider 的窗口身份、聚焦恢复、前台核验和坐标命中检查，采用明确有界的策略。
3. 打通选定模型实际可用的图像观察通路；不能用“截图文件存在”代替“模型看到图片”。明确 CLI / API 的能力声明和不支持时的反馈。
4. 用标准窗口、遮挡 / DPI / 过期截图和实际企业微信分别验收。最终按照既有用户授权，在唯一核对的周英俊单人会话中准备精确文本、仅发送一次并只读验证；当前仍未达到这一步。

此前专用原生 Edit 窗口和浏览器测试依然是有效的有限结果，但覆盖面不足以证明通用电脑控制或企业微信可用。
