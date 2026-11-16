# S15 Codex桌面版独有-待核实：改动项

共 20 项。行号按基线 develop a8ec8d81。


说明：证据来自 Codex 桌面版 build 里的打包 JS（`C\C01.md` 逐条 grep 过原字符串）。桌面版闭源，没有 Rust 源码可对照，所以每项都标"待核实"：见到的是字符串和数据结构，行为细节是推断。每项写明"见到的原文"和"未见到的部分"。

## S15-I1 待核实：状态文件解不开时先备份，再让用户选"从备份恢复/新建空记录"
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js` 里 `"Showing app-server SQLite recovery confirmation"`，携带 `backupFolder`、`databasePath`。未见到：恢复动作的具体实现（在 app-server，闭源）。Ryn 是加密 JSON，不是 SQLite，只借"先备份、再让用户确认"的思路。
- 问题：`AgentStore` 读不开就抛 `agent_state_unreadable`（`rynmesh/agent_runtime/store.py:238-239, 247-250`）。启动时 `rynmesh/agent_runtime/routes.py:62-68` 只记 `startup_error`，4 个后台 worker 全部变空转（`routes.py:85-89`），界面只能看到 409 错误（见 `webapp/src/screens/RynAgent.test.tsx:1564`），用户没有出路，也没留备份。
- 涉及行：C01-2
- 要改：
  - `rynmesh/agent_runtime/store.py:247-250` 读取失败分支 —— 失败时先把坏的根文件和 tasks 目录复制到 `agent/corrupt-<时间戳>/`（只复制，不删原文件），失败信息带备份目录名（不含内容）。
  - `rynmesh/agent_runtime/store.py` 新增 `recover(mode)`：`restore_last_good`（若有上一次成功读取留下的副本）或 `start_empty`（把坏文件移走后新建空状态）。`.last-good` 副本需新增：每次成功写入后保留一份。
  - `rynmesh/agent_runtime/routes.py:62-68` —— `startup_error` 存下原因和备份目录；新增本地路由 `GET/POST /api/local/agent/recovery`（mode），恢复成功后重新 `reconcile()` 并启动 worker。
  - `rynmesh/agent_runtime/computer_status.py:1768-1771` —— 启动失败时状态里带 `recovery_available`。
  - `webapp/src/screens/RynAgent.tsx` —— 遇到 `agent_state_unreadable` 显示恢复卡片：说明备份位置，两个按钮，二次确认后调用（错误处理在 `RynAgent.tsx:67-76` 一带，未逐行核对）。
- 新增测试：`tests/test_agent_store_recovery.py`：损坏根文件、损坏单个任务 blob、两种恢复模式、备份不被覆盖、恢复后 worker 恢复。`webapp/src/screens/RynAgent.test.tsx`：恢复卡片出现与确认流程。
- 依赖：无
- 兼容和风险：`.last-good` 会让磁盘占用最多翻倍（状态有 16 MiB 归档阈值），只在写入成功后复制且限一份。恢复写入必须走现有加密和哈希校验。"新建空记录"会丢历史，必须二次确认。
- 工作量：中
- 影响：中

## S15-I2 待核实：Ryn 自己发起的 git 命令统一加固，并把 git 失败分成明确类别
- 见到的原文：`worker.js` 里 `{LC_MESSAGES:"C",LANGUAGE:"C",GIT_TERMINAL_PROMPT:"0",GIT_OPTIONAL_LOCKS:"0"}`，`core.hooksPath=NUL`（Windows）和 `core.fsmonitor=…`；以及错误分类表（`ambiguous_revision`、`bad_revision` 等，只核实到前两类原字符串）。未见到：fsmonitor 取值逻辑。
- 问题：项目检查直接 `subprocess.run(["git","-C",…])`，没设环境，也没禁钩子/fsmonitor（`rynmesh/agent_runtime/projects.py:289-293`）；失败统一写 `"unavailable"`（`projects.py:307-310`）。只读 git 命令只加了 `--no-pager`（`rynmesh/agent_runtime/local_processes.py:437-441`）。后果：抢 `index.lock`、仓库 fsmonitor 被执行、错误文本随系统语言变。
- 涉及行：C01-11, C01-12
- 要改：
  - `rynmesh/agent_runtime/projects.py:289-293` `inspect` 里的 git 调用 —— 抽出 `_git_background(root, args)`：env 加 `LC_MESSAGES=C`、`LANGUAGE=C`、`GIT_TERMINAL_PROMPT=0`、`GIT_OPTIONAL_LOCKS=0`；命令加 `-c core.hooksPath=NUL`（非 Windows 用 `/dev/null`）、`-c core.fsmonitor=false`。
  - `rynmesh/agent_runtime/projects.py:307-310` —— 失败时把 stderr 归一成类别（`not_a_repository`、`bad_revision`、`ambiguous_revision`、`corrupt_repository`、`lock_held`、`permission_denied`、`other`），写进 `*_error` 字段，保留 `"unavailable"` 兼容旧界面。
  - `rynmesh/agent_runtime/local_processes.py:437-441` git 只读分支 —— 在 `--no-pager` 后插入同样的 `-c` 参数；环境变量通过调用方的 env 合并（`child_environment` 定义在 `local_processes.py:64`，调用链未核实，开发时先确认能否传 env）。
- 新增测试：`tests/test_projects_git_hardening.py`：带恶意 fsmonitor/钩子的临时仓库验证不执行；持有 `index.lock` 时 `git status` 不卡；各错误类别一例。
- 依赖：无
- 兼容和风险：`core.fsmonitor=false` 可能让大仓库 status 变慢（对 3 秒超时的影响要测）；不改变用户自己通过 shell 工具发起的 git。
- 工作量：小
- 影响：中

## S15-I3 待核实：定时任务拒绝 DTSTART，suggested_create 先出待确认卡片
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`For mode=create, do not include DTSTART or convert local wall-clock times to UTC ... use mode=suggested_create so they can review it before saving.`。未见到：卡片界面本身。
- 问题：raw rrule 含 `DTSTART` 也被照收（`rynmesh/agent_runtime/automations.py:318-323` 只检查 `FREQ=`）；`suggested_create` 被映射成 `create` 直接保存（`automations.py:1106-1111`）。
- 涉及行：C01-14
- 要改：
  - `automations.py:318-323` —— raw rrule 含 `DTSTART` 时拒绝，提示改用 `time`/`repeat` 字段。
  - `automations.py:1106-1111` —— `suggested_create` 不再映射成 `create`：生成 pending 记录（状态 `suggested`，不调度），工具结果返回预览；用户在界面确认后才转正（需要确认路由和小卡片，webapp 位置未核实）。
- 新增测试：`tests/test_automations.py`：DTSTART 被拒；suggested_create 不被调度；确认后才调度。
- 依赖：无
- 兼容和风险：这是新建调度的预览，不是工具审批，与"自动处理 = 全自动"不冲突，但要让老板知晓。已有靠 suggested_create 直接建的流程会变两步。
- 工作量：小
- 影响：低

## S15-I4 待核实：设置项按 agent 访问级别分级，统一设置读写工具，并登记宿主工具面
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`if("read-write"!==n.agentAccess)throw Error(...)`，定时任务运行时 `filter(e=>"write_settings"!==e)`；工具经 `codex-app-tools` 暴露。未见到：设置表完整内容。管道/帧上限/对端校验对 Ryn 不适用（工具在进程内）。
- 问题：Ryn 只有读配置 `ryn_get_agent_configuration`（`rynmesh/agent_runtime/diagnostics.py:19`）和改默认模型 `ryn_set_default_agent_model`（`rynmesh/agent_runtime/default_model.py:11`，登记在 `rynmesh/agent_runtime/tools.py:80`）。想改别的设置，模型没有受控入口。
- 涉及行：C01-16, C01-18
- 要改：
  - 新建 `rynmesh/agent_runtime/app_settings.py` —— 设置表：键、类型、范围、`agent_access`（hidden/read-only/read-write）、修订号；工具 `ryn_read_settings`、`ryn_write_settings`（带 `expected_revision`，沿用 `default_model.py:5-25` 的 fingerprint/binding 模式）。
  - `rynmesh/agent_runtime/tools.py:80` 附近 —— 登记新工具；`ryn_set_default_agent_model` 保留，视为 `model_id` 键的特例。
  - `rynmesh/agent_runtime/diagnostics.py:19` —— `ryn_get_agent_configuration` 字段与设置表对齐。
  - 定时任务执行时的工具菜单（`automations.py` 内，具体函数开发时定位）—— 不提供 `ryn_write_settings`。
- 新增测试：`tests/test_app_settings.py`：只读/隐藏项写入被拒；修订冲突；定时任务菜单不含写设置。
- 依赖：S06 工具菜单登记规则
- 兼容和风险：初始只开放无安全影响的项（语言、主题、默认模型等）；权限模式、共享、凭据一律 hidden。
- 工作量：中
- 影响：中

## S15-I5 待核实：ryn_local_environment 报告自带运行时和 Office 组件路径
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`Workspace dependency tools are only supported for local threads.`、`load_workspace_dependencies takes no arguments.`。未见到：返回字段结构（"捆绑 Node/Python/Office 路径"来自文档）。
- 问题：`ryn_local_environment` 只返回系统、shell、rg、浏览器、截屏能力（`rynmesh/agent_runtime/local_tools.py:1657-1674`），不说自带的 Node、Python、已下载的 Office 组件在哪，模型自己找或乱装。
- 涉及行：C01-21
- 要改：`local_tools.py:1665-1674` —— 增加 `runtimes` 字段：Ryn 随包的 Node（chrome_runtime 用的）、可用 Python、已下载的 Office/中文字体目录；只报告存在的路径，不存在写 `absent`。来源模块开发时核实。
- 新增测试：`tests/test_local_tools.py`：有/无随包运行时两种环境下字段正确。
- 依赖：无
- 兼容和风险：返回体变大，注意输出上限；路径属本机信息，沿用现有可见性。
- 工作量：小
- 影响：中

## S15-I6 待核实：跨对话工具（读取、等待、新开、转发其他对话）
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`Use this reference with read_thread, send_message_to_thread, or wait_threads ...`、`Sending a message requires explicit user authorization ...`，需审批集合含 `create_thread`、`send_message_to_thread`、`fork_thread`、`handoff_thread`。未见到：各工具参数结构。
- 问题：Ryn 的 `ryn_send_message`、`ryn_list_agents` 只在当前根任务树内（`rynmesh/agent_runtime/subagents.py:164, 248-262`）。用户说"看看上一个对话做到哪了，接着做"，模型做不到。
- 涉及行：C01-17
- 要改：
  - `subagents.py:248` 旁新增工具 `ryn_list_threads`、`ryn_read_thread`（只读、限长、分页）、`ryn_create_thread`、`ryn_wait_threads`、`ryn_send_message_to_thread`。数据来自 `AgentStore` 任务表，只限同一 owner。
  - 提示词增加：其他对话发来的内容不算用户授权（防注入）。**不加确认弹窗**（老板已定）。
  - 读到的其他对话内容按不可信数据打标记，沿用现有工具结果标记。
- 新增测试：`tests/test_thread_tools.py`：只能读本 owner 的任务；被读内容里的指令不当授权；等待超时；分页。
- 依赖：S06 工具登记
- 兼容和风险：对话里可能有别的任务范围的敏感内容；读取要受任务范围限制，是否放开读全部对话需老板确认。
- 工作量：大
- 影响：中

## S15-I7 待核实：插件工具结果带 UI，在对话里用隔离 iframe 内联显示
- 见到的原文：`sandbox-preload.js`：通道清单 `["navigate","notifyMcpAppsHostContext",...,"runWidgetCode","setAdditionalGlobals","setSafeArea","setTheme","setWidgetData","setWidgetView"]`，以及 `"skybridge"!==r.searchParams.get("app")`。未见到：一次性 MessagePort 握手代码、独立源生成方式。
- 问题：Ryn 插件只能整页 iframe（`webapp/src/screens/ExtensionWebApp.tsx:305-320`，sandbox 含 `allow-same-origin`）。工具结果无法在对话流里给用户一块交互界面。
- 涉及行：C01-24
- 要改：
  - 新建 `webapp/src/components/ConversationWidget.tsx` —— 沙箱 iframe（不给 `allow-same-origin`，用独立源或 `srcdoc`），只开放固定消息通道（setTheme、setSafeArea、setWidgetData、宿主上下文通知），一次性 MessagePort 握手。
  - `webapp/src/screens/RynAgent.tsx`（工具结果渲染处，未逐行核对）—— 结果含 `ui_resource` 时渲染该组件。
  - 后端工具结果归一处（S08 负责）保留插件返回的 UI 资源字段，限大小，不进模型上下文。
  - `ExtensionWebApp.tsx:305-320` 仅作参照，不改。
- 新增测试：`webapp/src/components/ConversationWidget.test.tsx`：非法消息被丢弃；不能读父页面；超限不渲染。
- 依赖：S08 插件结果字段
- 兼容和风险：新的攻击面（执行第三方 HTML），安全评审先过；默认只对已安装且声明了 UI 的插件开启。
- 工作量：大
- 影响：中

## S15-I8 待核实：对话内联显示 agent 生成的 HTML 图表，交互状态可回传
- 见到的原文：仅 `visualize-cache-worker.js`：`plugins/cache/openai-curated-remote/visualize` 和开关 `visualizeLive`。**未见到** `window.openai`、`setWidgetState`、16KiB 上限（文档声称，bundle 里没有）。状态回传这一半完全待核实。
- 问题：Ryn 只能把 HTML/SVG 渲染后测量（`rynmesh/agent_runtime/local_tools.py:1675-1686` `ryn_render_inspect`），用户在对话里看不到交互图表。
- 涉及行：C01-25
- 要改：
  - `local_tools.py:1675` 旁新增工具 `ryn_show_widget` —— 接收 HTML 字符串（限大小），落到任务产物目录，返回 UI 资源。
  - 复用 S15-I7 的 `ConversationWidget`。
  - 状态回传：widget 经消息通道发状态（≤16 KiB 仅为设计假设），下一轮作为"用户交互状态"附在提示里，标不可信。
- 新增测试：`tests/test_show_widget.py`：超限被拒；脚本只在沙箱里跑；状态回传大小限制。
- 依赖：S15-I7
- 兼容和风险：同 I7；状态进入提示词有注入风险，必须标不可信。
- 工作量：中
- 影响：中

## S15-I9 待核实：内置"深度调研"技能
- 见到的原文：`early-bootstrap.js`：`deepResearch:{installWhenMissing:!0,name:"deep-research"},visualize:{installWhenMissing:!0,name:"visualize"}`（安装清单）。未见到：技能正文（"证据账本""逐页看图检查"来自文档）。
- 问题：内置技能没有调研类（`rynmesh/agent_runtime/knowledge.py:9-48`，现有 project-development、spreadsheet-work、document-work、browser-work、system-diagnostics、resource-report、source-configuration 等）。
- 涉及行：C01-26
- 要改：`knowledge.py:9` `SKILLS` 增加 `deep-research`（Ryn 自己写正文：先列问题和证据表，子 agent 交叉核对，引用来源，导出后核对）。`visualize` 技能等 S15-I8 完成后再加。
- 新增测试：`tests/test_knowledge.py`：新技能出现在列表、版本号、内容长度上限。
- 依赖：S15-I8（仅 visualize 部分）
- 兼容和风险：技能 id 不能和用户同名技能冲突。
- 工作量：小
- 影响：中

## S15-I10 待核实：MCP elicitation 的真实界面（模板挑选等表单问询）
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`join(i,"artifact-template-picker","server.mjs")` 与 `{command:s,args:[a],env:{}}`。未见到：HMAC 令牌、表单字段。
- 问题：`rynmesh/agent_runtime/code_mode.py:658` 里 `createElicitation` 直接返回 `accept`，MCP 服务器想问用户选项时用户看不到。
- 涉及行：C01-27
- 要改：`code_mode.py:658` —— 非审批类的表单问询转成"等待用户输入"状态（沿用 `waiting_input`），界面渲染简单表单，答复后回填；审批类仍按老板决定自动通过。模板挑选服务本身不做。
- 新增测试：`tests/test_code_mode.py`：表单问询进入 waiting_input；答复后继续；取消返回 cancel。
- 依赖：无
- 兼容和风险：怎么区分"审批型"与"表单型"问询要看 MCP 请求里的 schema，开发时核实。
- 工作量：中
- 影响：低

## S15-I11 待核实：桌面和浏览器连招走一个持久 JS 单元
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`tools:{js:{...},js_reset:{...}}`、`CUA_REPL_ENABLED_SURFACES`、`SKY_CUA_NATIVE_PIPE:"1"`。未见到：node_repl 内核（不在 bundle）。
- 问题：Ryn 已有 `ryn_js_execute` 与 sky 宿主调用（`rynmesh/agent_runtime/js_integration.py:910-920` 一带）和窗口短 ID（`native_computer.py:288-307`），但桌面动作仍一步一个原子工具。
- 涉及行：C01-34
- 要改：`js_integration.py:76` `capture_capability_catalog` —— 把桌面观察/点击/输入/等待暴露为单元内函数，同一单元里可连续调用；提供 `reset`。具体函数开发时定位。
- 新增测试：`tests/test_js_integration.py`：单元内多步桌面动作；中断后 reset；每步仍走状态令牌校验。
- 依赖：S11 JS 运行时改动
- 兼容和风险：不能绕过逐步校验和中断；单元内一步失败要整体返回失败位置。
- 工作量：大
- 影响：中

## S15-I12 待核实：应用内浏览器面板（可行性先验证）
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`"BrowserUser.claimTab":"dot"===t.browserSurface`、`"Tab.markDeliverable":!0,"Tab.markHandoff":!0},name:"ChatGPT in-app browser"`。未见到：Electron WebContents 与运行时的连接细节（Ryn 是 WebView，不通用）。
- 问题：Ryn 随包带同一套 browser-use 运行时（`rynmesh/agent_runtime/chrome_runtime/README.md:20-22`），但只连外部 Chrome 或本机 CDP（`scripts/cdp-pipe-server.mjs`，README:16 提到）。用户预览本地页面要另开浏览器。
- 涉及行：C01-35
- 要改：先出可行性验证——WebView2 能否暴露 CDP 给 `cdp-pipe-server.mjs`；通过后在 webapp 新增浏览器面板组件，实现 `markDeliverable`、`markHandoff` 两个标记接口。
- 新增测试：webapp 面板组件测试；CDP 连接测试需真机，VM 上标"未验证"。
- 依赖：无
- 兼容和风险：WebView2 可行性未知，可能做不成；先验证，不直接开发。
- 工作量：大
- 影响：中

## S15-I13 待核实：浏览器选区注入和标签页 @ 提及
- 见到的原文：`You MUST call getTabContext to read the user's selection`、`Invalid Chrome tab mention native transport message`（`early-bootstrap-480-5def667ddfcd786d.js`）。未见到：原生传输格式。
- 问题：模型不知道用户在页面里选了什么，聊天框也不能 @ 标签页（现有环境工具 `rynmesh/agent_runtime/local_tools.py:1656-1674` 没有标签信息）。
- 涉及行：C01-36
- 要改：在 S15-I12 的面板里把选区文字作为附件字段随消息发出；聊天输入框加标签页选择（webapp 文件开发时定位）。
- 新增测试：webapp 输入框测试。
- 依赖：S15-I12
- 兼容和风险：选区是用户内容，但页面本身不可信。
- 工作量：小
- 影响：低

## S15-I14 待核实：CDP 桥对 cookie 和请求头类响应做脱敏
- 见到的原文：`early-bootstrap-339-8b97a30a8b57b9dd.js`：`let el={headers:ef,requestHeaders:ef,responseHeaders:ef,...};function ec(e,t){switch(e){case"Network.getCookies":case"Network.getAllCookies":case"Storage.getCookies":return ep(t,{cookies:em})`。**未见到** `em`/`ef` 是脱敏还是校验——核心待核实点。
- 问题：Ryn 自写的 `rynmesh/agent_runtime/chrome_runtime/scripts/cdp-pipe-server.mjs`（711 行）里搜 `Network.`/`Storage.` 无结果（已 grep 确认），cookie 和 Authorization 头会原样回给运行时和模型。
- 涉及行：C01-37
- 要改：`cdp-pipe-server.mjs` 的响应转发处（位置开发时定位）—— 对 `Network.getCookies/getAllCookies`、`Storage.getCookies` 的 `value` 替换为占位并保留名称/域；对 `authorization`、`cookie`、`set-cookie` 头脱敏。先写测试再比对 Codex 的 `em/ef`。
- 新增测试：`chrome_runtime/` 的 node 测试：模拟 CDP 响应，断言敏感值被替换，其他字段原样。
- 依赖：无
- 兼容和风险：有的任务确实要读 cookie（登录态检查），需"显式取回"开关，默认关；可能让现有技能失效，需回归。
- 工作量：中
- 影响：中

## S15-I15 待核实：Windows 桌面元素跨帧稳定短 ID 和增量差异
- 见到的原文：**未见**。声称在 `codex-computer-use-swift.exe` 与 `browser-accessibility.wasm.br`，build JS 里 "Removed element IDs" 等 0 命中。
- 问题：Ryn 桌面侧只有窗口级 sky id（`rynmesh/agent_runtime/native_computer.py:288-307` `assign_sky_id`），元素没有跨帧 ID，每次整树重发。
- 涉及行：C01-39
- 要改：先确认原生桥（`native_computer.py` 的 `bridge`）能否返回元素层标识；能则在观察结果归一处做元素 ID 表并与上一帧比对，只发新增/变化/移除；不能则标"无法确认，需先改原生部分"。
- 新增测试：`tests/test_native_computer.py`：两帧间元素 ID 稳定；移除列表正确。
- 依赖：S11 原生桥
- 兼容和风险：原文未见，设计完全是推断；先做原型。
- 工作量：大
- 影响：中

## S15-I16 待核实：写操作后自动等待界面稳定
- 见到的原文：**未见**。声称在 `sky-window2-api.md` 与 ComputerUseCore，bundle 搜不到。
- 问题：写操作后固定只等 40 毫秒（`rynmesh/agent_runtime/native_computer.py:2358` `post_observation={"enabled": True, "settleDelayMs": 40}`）。打开文件、加载页面后，模型常看到旧界面。
- 涉及行：C01-41
- 要改：`native_computer.py:2358` —— `settleDelayMs` 改为"基础值 + 忙碌检测"：基础值可配置，检测到进度条/忙碌光标则追加等待，总上限（如 5 秒，可调）。忙碌检测由原生桥提供，是否已提供未核实。
- 新增测试：`tests/test_native_computer.py`：忙碌时延长、上限生效、取消时立即返回。
- 依赖：无
- 兼容和风险：每步变慢；只在检测到忙碌时才延长。
- 工作量：中
- 影响：中

## S15-I17 待核实：可选的受管 worktree（创建偏好、保留数、自动清理）
- 见到的原文：`early-bootstrap.js`：`au=o.enum(["ask","always","never","dirty"])`、`defaultCreationPreference ... agentAccess:"read-only",default:"ask"`；`worker.js`：`agentAccess:"read-write",default:15 ... key:"worktree-keep-count"`。保留数/自动清理在开源 `worktree/src/settings.rs:14-33` 也有；桌面独有的是 `dirty` 偏好和 `agentAccess`。
- 问题：Ryn 没有 worktree，子 agent 只有 `write_scope` 路径列表（`rynmesh/agent_runtime/subagents.py:146-156`），两个 worker 子 agent 同时改同一目录的代码会互相踩。
- 涉及行：C01-6
- 要改：
  - 新建 `rynmesh/agent_runtime/worktrees.py` —— `create/list/remove`，调用 git（用 S15-I2 的加固参数），放在应用数据目录下，不放进用户仓库。
  - 设置项 `worktree.creation_preference`（ask/always/never/dirty）、`worktree.keep_count`（默认 15）、自动清理，注册进 S15-I4 的设置表。
  - `subagents.py:146-156` —— `ryn_spawn_agent` 的 `worker` 角色可选 `isolated: true`，绑定到 worktree。
  - `rynmesh/agent_runtime/projects.py:283-286` —— 项目概览显示 worktree 列表。
- 新增测试：`tests/test_worktrees.py`：创建、保留数淘汰最旧、脏仓库时 dirty 偏好触发、拒绝删除有未提交改动的。
- 依赖：S15-I2, S15-I4
- 兼容和风险：**整组 worktree（I17-I20）是否做，取决于老板是否要 Ryn 面向开发者做多 agent 并行改代码**，建议先问老板。非 git 目录不支持。
- 工作量：大
- 影响：中

## S15-I18 待核实：异步创建 worktree，可查进度，可接回已存在目录
- 见到的原文：`early-bootstrap-480-5def667ddfcd786d.js`：`Check get_worktree_creation_status for an immediate progress snapshot ... If Git finished, use attach_worktree with that path.`。未见到：幂等判定条件。
- 问题：大仓库检出可能很久，同步等会让工具超时；中断后会重复检出。
- 涉及行：C01-7
- 要改：`worktrees.py` 增加后台创建 + 操作号；工具 `ryn_worktree_status`、`ryn_attach_worktree`（路径已在 `git worktree list` 里则接回，不重复检出）。基线中无对应代码（`subagents.py:146-156` 只有 write_scope）。
- 新增测试：`tests/test_worktrees.py`：创建中断后 attach 成功；重复 attach 幂等。
- 依赖：S15-I17
- 兼容和风险：操作号要持久化，进程重启后能判断"已完成/失败/未知"。
- 工作量：中
- 影响：中

## S15-I19 待核实：项目环境配置（初始化脚本、清理脚本、快捷动作）
- 见到的原文：`worker.js`：`iT="CODEX_SOURCE_TREE_PATH",iS="CODEX_WORKTREE_PATH"`，动作 `icon:["tool","run","debug","test"]`，配置结构 `{version,name,setup,cleanup,actions}`。未见到：脚本执行时的超时和权限处理。
- 问题：项目检查只读分支和改动（`rynmesh/agent_runtime/projects.py:283-286`），没有"初始化环境""一键测试"的约定。
- 涉及行：C01-8
- 要改：`projects.py` 读取 `.ryn/environments/*.toml`（version、name、setup、cleanup、actions）；执行经现有 shell 工具（受现有权限和输出策略），注入 `RYN_SOURCE_TREE_PATH`、`RYN_WORKTREE_PATH`；项目页（webapp，位置未核对）列出 actions。
- 新增测试：`tests/test_projects_environment.py`：解析、非法 toml 报错、环境变量注入。
- 依赖：无（worktree 变量仅在 S15-I17 存在时有值）
- 兼容和风险：仓库里的脚本等同任意代码；自动处理模式下不加确认，但提示词要写明来源是仓库内容。
- 工作量：中
- 影响：中

## S15-I20 待核实：worktree 归档与恢复
- 见到的原文：仅工具名：`i.includes("archive_worktree")&&i.includes("restore_worktree")`。**未见**快照提交细节。
- 问题：清理 worktree 时有未提交改动会丢失。
- 涉及行：C01-9
- 要改：`worktrees.py` 增加 `archive`（未提交改动打成隐藏分支上的快照提交后移除目录）、`restore`（原路径恢复）。
- 新增测试：`tests/test_worktrees.py`：有未提交文件的归档与恢复后内容一致。
- 依赖：S15-I17
- 兼容和风险：隐藏分支会留在用户仓库引用里，需命名前缀并可清理。
- 工作量：中
- 影响：低
