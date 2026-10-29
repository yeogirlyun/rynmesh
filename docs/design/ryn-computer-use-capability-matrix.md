# Ryn Computer Use：能力矩阵与固定对照验收

冻结版本 v1，2026-10-19。设计/验收 Codex，应用与项目测试 Gemini 4。本文件只冻结目标和验证方法，没有运行 Codex/Ryn UI，没有发送消息。关联[实施方案](ryn-computer-use-codex-parity-plan.md)、[REA 诊断](ryn-computer-use-rea-analysis-20261019.md)、[原生协议派工稿](../../work/agent-extensions-20261019/ryn-native-computer-protocol-v1-design.md)。

“对齐”指下列声明平台、模型路由和能力的可观察契约与固定场景通过，不指私有源码、内部算法、任意模型决策或未来版本完全相同。证据、实现、验收分别记录；静态取证不是运行成功。未实现/未验证/不支持都必须公开，不可删去行后宣称全功能完成。

## 基线与证据等级

Codex基线：Computer Use `26.1013.40934`、`@oai/sky 0.7.8`、Windows x64 helper SHA256 `b6b0e4979b89e5c22dab866f0974bc817c0d08cb359e1c2dd7be3dccb9e181eb`。本文件适用这一安装版本；版本/hash改变后先重新核对受影响能力。

| 标记 | 含义与可复查证据 |
| --- | --- |
| J | 已安装JS直接验证参数/转发/宿主调用；[JS报告](../../work/agent-extensions-20261019/rea-codex-sky-static-review.txt)、[原始哈希/可读映射](../../work/agent-extensions-20261019/rea-sky-js/source-manifest.json) |
| D | 安装包文档/Skill承诺；报告记录原始路径，`docs/sky-window2-api.md`、plugin `docs/{api,guidance,confirmations}.md`。不等于原生实际执行证据 |
| N | 已取证原生函数/调用关系；[REA证据与manifest](../../work/agent-extensions-20261019/rea-native-codex/manifest.json)。反编译并非原始源码，当前调用图覆盖有限 |
| B | 必须补的owned窗口黑盒证据；当前无本矩阵Codex/Ryn对照结果 |
| R | Ryn设计或可靠性要求；不宣称Codex存在同名字段/持久ledger实现 |

N已证范围：聚焦入口/最多20轮恢复/线程关联与解除/前台和根窗口关系；坐标路径可见WindowFromPoint、窗口关系/PID/相关区域检查。**所有动作是否走同一例程、完整UIA/截图缓存、所有键名、Swift后端委托和本机历史实际宿主分支仍待证。** 本轮不再长时间重新导入native分析；优先复用现有结果并补定向黑盒。

## 全面能力矩阵

以下“进度”全部是新parity链的状态，旧Python/Edit窗口有限验收不自动继承为完成。`TODO`为待实现/待验收；native-01正在开发骨架，尚未接到ready通知。

| ID | 能力及冻结契约 | Codex证据/仍待证 | Ryn阶段 / 进度 | 对照场景 |
| --- | --- | --- | --- | --- |
| APP1 | `list_apps`：id、windows；可选displayName/isRunning/lastUsedDate/useCount，缺失不伪造 | J/D；实际发现覆盖B | 2 / TODO | OWN01 |
| APP2 | `launch_app({app})`：返回id或明确exe标识；启动后重新发现，不猜窗口、不shell拼接 | J/D；原生启动/多实例B | 2 / TODO | OWN01 |
| WIN1 | `list_windows`与`get_window({id,app?})`；返回当前可定向Window | J/D；失效窗口原生错误B | 2 / TODO | OWN01/03 |
| WIN2 | 公共Window `{app,id,title?}`；app非空、id非负整数（JS接受数字字符串），额外字段不作为身份凭据 | J；不推定id内部格式 | 2/4 / TODO | OWN02/03 |
| WIN3 | `activate_window`和输入自动激活；前台关系而非仅HWND数值相等 | J/D/N；各入口完整路由B | 2 / TODO | OWN04/05 |
| WIN4 | HWND/PID/进程启动标识/重建代次；popup/owner/child关系；不同任务隔离 | N部分；完整缓存B；R窗口身份 | 2/3 / TODO | OWN03/05/12 |
| OBS1 | `get_window_state`默认截图true、文本false；两者false拒绝；返回window/screenshots/accessibility | J/D；默认返回accessibility=null的实际行为B | 2/4 / TODO | OWN06 |
| OBS2 | UIA tree及索引；focused_element、selected_text、selected_elements、document_text可选且不伪造 | J/D；原生COM遍历/截断/控件覆盖B | 2 / TODO | OWN07/08 |
| OBS3 | 截图id/url/zIndex，可选originX/Y、width/height；相关瞬态UI可能多图 | J/D；截图来源、popup组合/排序B | 2/3 / TODO | OWN06/09 |
| OBS4 | 目标被遮挡仍能采集目标画面；最小化/保护画面/不可采集须准确报告 | D（Skill声明WGC）；原生具体路径及结果B | 2 / TODO | OWN09 |
| OBS5 | screenshotId属于目标窗口缓存；元素索引属于最新观察；移动/缩放/改树/重建后旧观察重验 | D，JS只转发；原生失效规则B | 3 / TODO | OWN10/11/12 |
| OBS6 | 坐标为窗口相对值；逻辑尺寸/物理像素/DPI/多屏负坐标明确换算 | D；JS有限数Math.round已证；精确变换B | 3 / TODO | OWN11 |
| OBS7 | Ryn stateToken绑定task、WindowRef、截图/元素和代次，跨worker变更可察觉 | R，不声称Codex同名机制 | 3 / TODO | OWN10/12/17 |
| IN1 | `click`坐标或element_index；兼容elementIndex/element；click_count默认1且>=1；left/right/middle及l/r/m | J/D；原生按钮别名/多次点击B | 3/4 / TODO | OWN08/13 |
| IN2 | `press_key`：+拆分/trim；X keysym、左右修饰键、标点/Shift、KP_*与Numpad_*别名 | J拆分、D映射；native映射B | 3 / TODO | OWN14 |
| IN3 | `type_text`原样文本进入已观察的编辑焦点；控制键用press_key；中文/非BMP/多行 | J/D；输入方式、IME/焦点B | 3 / TODO | OWN14/15 |
| IN4 | `set_value`替换最新element_index的可编辑值，不能当成向任意焦点type | J/D；pattern/只读/密码行为B | 3 / TODO | OWN08/15 |
| IN5 | `scroll`必须window相对x/y、scrollX/Y；正下/右、负上/左，非element_index接口 | J/D；单位/实际量/目标paneB | 3 / TODO | OWN13 |
| IN6 | `drag`from/to窗口相对坐标，可附screenshotId；起终点/全过程目标与按键释放 | J/D；原生路径/跨区限制B | 3 / TODO | OWN13/18 |
| IN7 | `perform_secondary_action`：最新index及action，Raise/Scroll/Expand/Collapse等已返回label；文档称不区分大小写 | J/D；实际支持label/patternB | 3 / TODO | OWN08 |
| IN8 | 命中检查覆盖激活后实际坐标/窗口关系；不向覆盖窗、同PID无关窗或旧目标输入 | N部分；完整覆盖B；R强约束 | 2/3 / TODO | OWN05/10/11 |
| IN9 | 每次动作后重新观察；输入送达与业务完成分开；不自动整批/整JS重放 | D/R；Codex错误恢复细节B | 3/4 / TODO | OWN15/19/20 |
| HOST1 | 持久JS、`globalThis`跨cell、import `@oai/sky`、`nodeRepl.write`；Ryn门面明确来源 | J/D；完整Skill代表序列B | 4 / TODO | OWN19 |
| HOST2 | trusted setup/execute只暴露真实方法；task/turn作用域、Interrupt/SubagentStop/Stop→turn_ended | J与plugin manifest；native退出/缓存清理B | 4 / TODO | OWN18/19 |
| HOST3 | 自动image emitter、截图可显示；实际像素进入当前所选模型为独立门槛 | J发射路径；宿主最终入模B | 4/5 / TODO | OWN16 |
| HOST4 | app授权回调可挂起/续跑；权限拒绝/缺elicitation明确失败；不得因审批重跑先前动作 | J；native审批前无副作用B；R宿主审批 | 1/4 / TODO | OWN17/19 |
| HOST5 | close幂等、共享helper引用/lazy启动；stdio/native-pipe不同分支；父退出/超时结束pending | J；native/包装进程残留B | 1/2/4 / TODO | OWN18 |
| HOST6 | 请求期限：所取JS默认launch15s/其余10s，错误和中断可见；Escape/exit130使当前turn停止 | J/D；实际停止边界B | 1/2/4 / TODO | OWN18 |
| REL1 | 同server/scope并发首连保留健康赢家，配置代次/schema/instance仍绑定 | R，当前Ryn缺陷已复现 | 1 / TODO | OWN17 |
| REL2 | 全物理桌面跨进程输入串行；ready后宿主fresh guard+durable-dispatched才commit | R，不复制Codex私有审批 | 1/2 / TODO | OWN17 |
| REL3 | 有界脱敏code/type/stage/request/session/operation诊断；postdispatch错误仍unknown/no-replay | R，当前诊断丢失已复现 | 1 / TODO | OWN18/20 |
| SAFE1 | password值不导出，禁止目标/快捷键、明确用户授权与发送前核对、屏幕内容非指令 | D（完整Skill/guidance/confirmations）；R同宿主执行 | 2/4 / TODO | OWN07/17/19 |
| UI1 | engine就绪、plugin启用、双层desktop授权、连接、模型视觉分别显示；停止与错误恢复入口 | R | 5 / TODO | OWN16/17/18 |
| OPT1 | `start_audio_recording({max_duration_ms?})`/`stop_audio_recording`：仅SKY_ENABLE_AUDIO=1暴露；wav bytes/data_url/filepath | J条件API；录制原生/权限B | 条件项 / TODO；不得静默遗漏 | OWN21（默认不运行） |

OPT1不是默认Window2接口的启用项，但属于已发现的条件能力。默认关闭时记录未启用，不声称支持音频；若目标profile启用则必须补齐独立授权和验收，未通过不能称该profile全面对齐。Linux专有drag_handle/get_screenshot等不属于本Windows矩阵，保留在平台差异中。

## 固定owned窗口与场景

fixture必须由验收创建并保存PID/创建时间/HWND集合；包括：两个独立进程主窗、同进程第二无关窗、owned modal/child、Edit/Button/List/Tree/可滚动画布、密码/只读Edit、自绘弱UIA画布；每个可动作控件有只记录到本地的真实事件/文本counter。误目标窗同样记录事件，用以证明zero wrongtarget，而不是只检查目标成功。禁止测试终端/Run/认证/密码管理器/安全应用及Codex自身UI。任何采集仅限fixture画面；真实联系人不参与循环测试。

Codex对照只通过安装Skill规定的 `node_repl + @oai/sky`公开入口执行；不自写Codex helper协议、不直接启动Codex exe。Ryn用真实runtime/审批/native链；纯native测量单列。固定随机种子、内容、初始布局、应用版本和目标，A/B交替次序，模型推理不混入原生对照。

| 场景 | 固定操作/失败注入与判定 |
| --- | --- |
| OWN01 发现启动 | fixture未运行→launch→重新list→唯一选择→get；再覆盖已运行、多实例/同标题。歧义停止，不能猜目标 |
| OWN02 参数/结果 | 合法默认、数字字符串兼容、缺字段、负数、NaN/Infinity、空app、越界index、额外字段；按J/D冻结，错误行为差异留档 |
| OWN03 身份重建 | 关闭/重开、PID变化、同进程窗口重建、模拟/捕获句柄复用；旧WindowRef/token不能命中新对象 |
| OWN04 焦点 | 目标前台/后台/最小化/被另一fixture占前台；验证实际foreground和目标输入结果，聚焦失败零后续输入 |
| OWN05 窗口关系 | owned modal、child、同PID无关窗、跨PID遮挡窗；分别验证允许关系/重选窗口/拒绝，不以同PID一概放行 |
| OWN06 观察组合 | 默认、text-only、image-only、both、both-false；验证结构、真实焦点/截图，默认是否accessibility=null |
| OWN07 UIA语义 | focus/selection/document/tree真实内容、password/只读、缺pattern、自绘弱UIA；截断/缺失字段明确且不泄值 |
| OWN08 元素动作 | click/index别名、set_value、Expand/Collapse/secondary action大小写；读回真实控件状态；旧index改树后不得误用 |
| OWN09 截图真实性 | 全遮挡/部分遮挡、owned popup、最小化、采集失败；只读fixture随机水印和几何，不能把覆盖窗当目标；多图zIndex验证 |
| OWN10 快照过期 | 观察后布局/内容/树变化、截图来自另一窗、错误screenshotId/token、失效后重观；拒绝或安全重验，不能盲点旧坐标 |
| OWN11 DPI多屏 | 100/125/150/200%各配置；负坐标副屏/跨屏移动/缩放/尺寸变；标记点点击须命中fixture的预期像素区 |
| OWN12 任务隔离 | taskA观察，taskB无观察尝试；结束A后重用、worker重启、另node动作推进epoch；全部旧引用不得继续使用 |
| OWN13 鼠标 | left/right/middle及别名、single/double、水平/垂直正负scroll、drag；记录准确事件次数/顺序/最终位置和button-up |
| OWN14 键盘 | 字母/数字/Return/Tab/Escape、左右Ctrl/Shift/Alt、标点Shift、KP_0/Numpad别名；中文/emoji/空文本/多行；只向fixture输入 |
| OWN15 效果闭环 | 编辑焦点明确→type/set_value→读回；焦点落错控件拒绝；动作已送达但业务UI未变须报告未核验，不自动再打一遍 |
| OWN16 模型图像 | 每路由只存在于截图像素的随机nonce/图形，文件名/metadata/文本不含答案；记录请求图像hash、模型观察与界面显示分别结果 |
| OWN17 竞态授权 | 同scope首连；2真实进程/不同node争同桌面锁；排队与ready时撤销global/task/插件/schema/审批binding；未commit零动作 |
| OWN18 生命周期 | 排队/ready/activation/input中cancel；turn_ended/实体Escape；EOF、父退出、超时、worker hardkill；清理线程关联/keys/buttons/owned进程 |
| OWN19 Skill宿主 | 原始Skill代表性的两cell观察→单动作→刷新；跨cell变量；动作1完成→动作2审批挂起→续跑；动作1 counter始终1；拒绝后不再输入 |
| OWN20 unknown防重 | native动作counter先fsync后RPC error/isError/断线/退出/完成落盘失败；重建runtime、同operationId/模型新call均不自动重做；诊断关联可查 |
| OWN21 条件音频 | 仅显式启用profile后，自有合成音源+明确录制授权，时长/停止/wav/清理；默认profile验证方法不暴露；本轮不录用户音频 |

没有相应屏幕/缩放/音频配置时记 `BLOCKED(environment)`，不能以skip当PASS。不能在已有用户工作窗口上创建遮挡/夺焦测试；这些动作只对fixture及其明确的对照运行授权范围执行。

## 次数、成功门槛与时延口径：运行前冻结

1. 每一正常native行为场景/分支、每个声明支持的显示配置，每引擎 **30次**：5次独立冷worker＋25次热会话，轮次A/B交替；每轮复原fixture并记录seed。OWN02的每个确定参数case至少1次，时序相关case按30次。正常受控case须30/30完成，且Ryn不能低于Codex；不能利用Codex偶发失败下调基础正确性。
2. 每个故障/撤权/取消分支每引擎 **20次**；Ryn独有ledger/两阶段保证不假装Codex也有同实现，仅验Ryn安全契约。并发首连/跨进程锁 **50轮**，每轮至少2个真实进程/控制器；同key连接另含8线程竞争。禁止自动重试掩盖失败，失败重跑必须保留原始轮次并整体另编号。
3. 每个支持的模型路由做 **10个独立随机图像挑战**，含纯视觉观察和视觉后fixture动作；10/10回答和动作目标正确。先确定用户选择的模型/账号/路由，不静默切换或调用付费替代模型。此项实际推理耗时单列。
4. **安全零容忍：wrongtarget=0，非授权物理动作=0，实际写入replay=0，取消生效后的新应用输入=0，遗留自有keys/buttons=0。** 统计边界为引擎接受cancel后不得开始新的应用输入；已提交OS的事件可能稍后投递，必须记录发送/接收时间不能改称未发生。仅释放本操作已按下按键的清理事件单列，不能伪装成业务输入。cancel收到后仍有未来业务输入计划即FAIL。
5. 每次记录单调时钟：入口开始、锁等待、ready、fresh authority、durable dispatch、commit发送、native首个副作用/末个输入、native响应、确认落盘、入口返回、独立效果读回。分别报告cold/warm的样本数、成功率、p50/p95/max；p95采用nearest-rank，N=30时第29值，不把它解释成置信界限。不混用CPU时间/服务器日志墙钟计算耗时。
6. **原生对照时延**从公开客户端方法调用开始至Promise/对应native响应结束，包含本地IPC与锁等待，排除模型/人工审批；观察包含截图/UIA编码。另报Ryn完整工具入口时延（必须含权限/ledger持久化）和观察→动作→读回全链，不用native小数代替产品时延。锁竞争、启动、视觉推理、人工等待分别分布，不丢慢样本。
7. 预先采用工程门槛：同机同fixture的每个可比较正常分支，Ryn原生warm p95 ≤ `1.5 × Codex warm p95 + 250ms`；不含冷启动/故障注入，仍完整公布其结果。公开请求预算默认普通10s/launch15s，不因失败临时放宽。Ryn全工具入口warm p95额外≤对应Codex客户端p95的`1.5倍+1000ms`，包含同步持久化；这两个门槛是本项目选择，不是已证Codex SLA。若产品不能达到，记录FAIL与取舍，不删durability/撤权检查换数字。
8. 任何矩阵/阈值/样本调整须在下一次运行前记录v2、理由及受影响组，保留v1所有失败；不能事后调整标准给既有结果补PASS。真正业务发送不参与稳定性循环，结果unknown不重发。

## 平台与模型路由声明（独立于工具能列出）

| 平台/范围 | 当前状态 | 发布条件 |
| --- | --- | --- |
| 当前Windows交互桌面 x64 | native-01开发中，TODO；实际OS build/GPU/DPI/monitor在run manifest冻结 | 全normal/fault矩阵、独立安装，无错误目标/重放 |
| Windows10 x64 / 其他Windows11 build | TODO，未经本矩阵验收 | 每个声明配置重跑；不能从当前机器外推 |
| Windows ARM64 | Codex JS有候选helper路径；Ryn暂无已验实现 | 单独构建/运行/原生依赖验收前不支持 |
| 锁屏/UAC安全桌面、认证/密码管理器/安全应用、终端/Run、Codex自身UI | 禁止或明确不支持，不做自动化对照 | 不绕过Skill/宿主边界，不以提权当修复 |
| macOS/Linux | 本Windows parity范围外；Sky其他平台方法不能假装存在 | 单独方案/矩阵，不自动继承 |

| 模型来源/路由 | 当前证据与TODO | 必须通过 |
| --- | --- | --- |
| local_api，声明supports_image_input=true | 现有ryn_image_inspect有路由，**新统一视觉闭环未验** | OWN16实际请求像素+当前所选模型观察+fixture动作，错误路由不能冒充支持 |
| Codex CLI | 有图像载体候选，当前电脑图片存盘不代表已入CLI | 独立CLI请求载图/会话恢复/路径权限及OWN16 |
| Antigravity CLI / Gemini4 | 现有文本桥接未证明能读MCP截图 | 补图像承载后OWN16，否则明确阻止依赖视觉流程 |
| 其他CLI/GLM等API或代理 | 逐模型、逐来源、逐版本TODO | 不能只靠模型品牌/名字推断视觉；同OWN16 |
| 显式独立视觉模型 | 可选设计，未验 | 用户明确配置，记录观察模型/决策模型和图像hash，不静默换账号/模型 |

## 阶段进度与交付记录

| 阶段 | 当前进度 | 下一门槛 |
| --- | --- | --- |
| 0 冻结基线 | JS/部分N取证已有；本矩阵v1冻结，全部B待做 | 补未证行为及fixture manifest |
| 1 通信/诊断 | 两缺陷已复现，修复验收TODO | OWN17/18/20与既有独立probe |
| 2 原生引擎 | Gemini native-01开发，尚未ready/验收 | 原生协议/自建窗口/焦点/唯一核心 |
| 3 快照/完整输入 | TODO | OBS/IN全行及多屏/取消 |
| 4 视觉/JS/Sky | TODO | 每条路由OWN16＋原始Skill序列 |
| 5 UI/安装 | TODO | 干净安装、真实状态与恢复入口 |
| 6 对照/真实应用 | TODO，未运行本矩阵 | 全矩阵实证；最后单次真实任务 |

每次交付附：源码/二进制/Skill hash、fixture/run manifest、平台/模型路由、全部轮次原始结果、counter与安全事件、分段时延、失败/blocked项和清理证明。更新实现/验收状态时必须链接实际证据，不覆盖本冻结契约。

真实企业微信任务仍未发送。最终依据已有用户授权，唯一核对周英俊单人会话、精确文本“测试电脑控制”、只发一次并只读核验；任何发送unknown都停止且不重发。本矩阵的循环只能使用owned fixture，不能向真实联系人反复测试。
