# Ryn × 企业微信 CLI（wecom-cli）接入设计 v1

状态：设计草案，2026-10-20
相关：`ryn-org-workspace-v1.md`（组织空间）、`ryn-agent-extensions-v1.md`（插件 / Skills / MCP）

## 0. 结论

**做成一个内置插件「企业微信」，形状照搬 Computer Use。** 一个插件里包括三样东西：

1. **内置 MCP 工具**：给 Agent 用的类型化工具，每个工具都标好影响等级。
2. **内置 Skills**：工作流知识，由官方 15 个 Skill 改写而来。
3. **应用页卡片**：连接、授权、状态和权限开关。

真正的执行放在宿主进程托管的 `WeComFacade` 里。它只用固定参数调用固定版本的 wecom-cli，不经过 shell。

**一句话定位**：wecom-cli 本质上是"员工本人授权的企业微信助手机器人"，所以它应该装在**员工设备**上，不应该放在组织节点上。

不选的几种做法：

| 做法 | 为什么不作为正式方案 |
| --- | --- |
| 直接导入官方 Skill 包 | Ryn 现在就能导入（Claude / Codex 插件格式都支持），但只能通过 `ryn_shell_execute` 执行。问题是"给老板群发消息"和 `ls` 走同一级审批，凭证目录对 shell 可见，返回内容也没有标记为不可信。**只用于 P0 验证。** |
| 当作普通外部 MCP | 外部工具默认按"写 / 高风险"处理（见 `ryn-agent-extensions-v1.md`），连查通讯录都要确认，体验很差。另外 MCP 服务也不能给自己声明只读。 |
| 组织节点的身份源 | wecom-cli 用的是机器人凭证，不是服务端 API，做不了扫码登录和通讯录同步。M1 身份源仍然用 corpid + secret 调服务端接口。 |
| 在组织节点上统一跑一份 | 只有一个机器人身份，所有员工都会拿到授权人的权限，违反"数据权限在后端"的原则。 |

## 1. wecom-cli 是什么（2026-10-20 查证）

| 项 | 内容 |
| --- | --- |
| 来源 | 企业微信官方开源，`WecomTeam/wecom-cli`，MIT 许可，Rust 编写，通过 npm 分发（`@wecom/cli`，需要 Node 18 以上） |
| 平台 | macOS x64/arm64、Linux x64/arm64、Windows x64 |
| 认证 | `auth init`：默认扫码（5 分钟超时），也可以用 `--manual` 手动填写智能机器人的 Bot ID 和 Secret。支持 `--noninteractive`、`--no-browser`、`--output-qrcode <PATH>` |
| 凭证存放 | `~/.config/wecom/credentials.enc`（AES-256-GCM 加密，0600），密钥放在系统密钥库，没有密钥库时用 `.encryption_key` 文件。可以用 `WECOM_CLI_CONFIG_DIR` 改目录。`WECOM_CLI_ACCESS_TOKEN` 的优先级高于文件 |
| 命令形式 | `wecom-cli <service> [resource...] <method> [flags]`，用 `--json` 传完整请求体，`--set path=value` 改嵌套字段 |
| 通用参数 | `--dry-run`（只校验、打印请求，不发送）、`--page-count`、`-o`、`--output-dir`、`--schema`、`--doc` |
| 输出 | 标准输出是紧凑 JSON，分页时是 NDJSON。错误是 JSON（`type` / `code` / `message`）。退出码：0 成功，1 运行错误，2 用法错误。日志只写 stderr |
| 方法发现 | **在线获取，缓存 60 秒。** 也就是说，服务端可能在不升级 CLI 的情况下改动方法 |
| 业务面 | `message` `mail` `doc` `sheet` `smartsheet` `smartpage` `calendar` `meeting` `todo` `disk` `contact` `media` `identity` |
| 官方 Skills | 15 个：shared、contact、calendar、meeting、todo、email（只读）、disk、media、message、doc-manage、doc、sheet、smartsheet、smartpage、pptx |

**未确认的事项（P0 实测）：**

- **身份语义**：文档只说可以用 `identity whoami` 查"机器人或授权人身份"，没有说明操作以谁的名义出现、能访问谁的数据。
- **开放范围**：社区文章转述"完整功能只对 10 人以下企业开放"，官方 README 没有写。另有一篇实测文章称只有 `doc` 能用，但那是早期版本（当时的命令格式跟现在不同）。
- **消息**：README 写的是"只能主动发给机器人近期有过会话的单聊或群"。能不能读历史消息，新旧文档说法不一致。
- **邮件**：CLI 支持发送、回复、转发，但官方 email Skill 有意限制为只读。

## 2. 放在 Ryn 的什么位置

```
 员工设备（Ryn）
┌──────────────────────────────────────────────────────────────────────┐
│ 空间（个人空间 或 某个组织空间，二选一）                                    │
│                                                                      │
│  Ryn Agent ──工具路由──> 内置插件 wecom ──> WeComFacade（宿主托管）      │
│  应用页卡片 ──连接/状态──┘                     │                        │
│  后台任务（只读轮询）────────────────────────────┤                        │
│                                               ▼                        │
│                       wecom-cli（固定版本，固定 argv，独立 config 目录）   │
└───────────────────────────────────────────────┬──────────────────────┘
                                                ▼
                                        企业微信开放平台
```

**空间绑定**：连接时就决定这个连接属于哪个空间，之后不能切换。

- **还没有加入 Ryn 组织**（小团队，或者公司就在用企业微信）：放个人空间。正好对上"10 人以下"这个开放范围。
- **已经加入 Ryn 组织**：放在该组织空间里。企业微信的数据属于工作数据，个人 Agent、个人记忆和个人模型 provider 都看不到。组织可以通过策略禁止或允许成员连接。

## 3. 接入后 Ryn 能做什么

### 3.1 在对话里直接办事

| 说法 | 背后的调用 |
| --- | --- |
| "约张三、李四下周二下午开个会，找个有投影的会议室" | contact 搜人 → calendar 查闲忙 → 订会议室 → meeting 创建（确认卡片会列出受邀人） |
| "昨天的项目周会讲了什么？把待办拆出来分给各人" | meeting 取纪要和转写 → 总结 → todo 创建，带参与人（确认） |
| "我这周还有哪些没完成的待办？哪些已经过期？" | todo 列表（只读，不需要确认） |
| "把 Q3 预算那个智能表格里超支的项目找出来" | doc 搜索 → smartsheet 读记录 → 分析 |
| "搜一下上个月供应商发来的报价邮件，汇总成一张表" | mail 搜索和读取（只读）→ 生成表格 |
| "把这份方案发到项目群" | message 先列出近期会话 → 选定会话 → 发文件（确认卡片里显示会话名和内容预览） |

### 3.2 只有 Ryn 能做的组合（差异化所在）

单独用 Claude Code 加官方 Skill 也能完成上面那些事。Ryn 的价值在于把企业微信和**用户自己的设备与数据**连起来：

1. **本机 / NAS / 其他设备 ↔ 微盘 / 在线文档**："把家里 NAS 上 2026 合同目录里最新的报价单导入成企业微信文档，再发给项目群。"文件访问走 Ryn 的资源权限，上传和导入走 wecom。
2. **Ryn Excel ↔ 在线表格 / 智能表格**：把智能表格拉到本地 xlsx，用 Ryn Excel 分析，再追加行写回去，或者生成一个新的子表。
3. **用私有模型处理敏感内容**：会议转写和邮件正文交给本地模型或私有模型总结，原文不进入云端模型（见 4.5 模型策略）。
4. **Craft 文档 → 企业微信**：本地写好的 docx / pptx，一键导入成在线文档或在线幻灯片。
5. **Ryn Drop / "打开方式"**：在系统的分享菜单和"打开方式"里加一个"发送到企业微信"，直接复用 3.1 里发消息的确认流程。

### 3.3 主动和后台功能

CLI 没有事件推送，所以后台只能**低频只读轮询**，写操作一律要用户确认。

| 功能 | 内容 |
| --- | --- |
| 早报 | 工作日早上汇总今天的日程和会议、过期待办、未读邮件摘要，显示在 Ryn 首页卡片 |
| 会后纪要 | 发现会议已结束且有纪要时，起草总结和待办，等用户确认后再发群、建待办 |
| 周报草稿 | 汇总本周的文档、待办和会议，生成周报文档（私有），用户确认后再分享 |
| 截止提醒 | 待办临近截止时发系统通知 |

### 3.4 做不到或不做

- **实时收消息、@机器人自动回复**：CLI 没有事件和长连接接口。这件事归组织 IM（`ryn-org-workspace-v1.md` 第 9 节）。
- **给任意人或任意群发消息**：只能发给机器人近期有过会话的对象。
- **审批、打卡、客户联系 / 外部联系人**：CLI 不提供。
- **v1 不发邮件**：跟官方 Skill 保持一致，邮件只读。

## 4. 设计要点

### 4.1 安装与版本

- **由 Ryn 管理安装**：装到 `home/tools/wecom-cli/<version>/`，固定版本号，校验 npm 包的 integrity，不使用全局安装。下载需要用户同意，确认时写清楚包名、版本和大小。
- **不自动安装 Node**：检测不到 Node 18 以上时，在卡片上说明原因并引导用户自行安装。
- 如果检测到全局已经安装了 wecom-cli，只用来提示，**不用它执行**。原因是版本和配置目录都不受 Ryn 控制。
- 最低版本按官方 shared Skill 的要求：1.2.1。

### 4.2 凭证

- 每个空间单独一个目录：`WECOM_CLI_CONFIG_DIR=<空间目录>/connectors/wecom/`，个人空间和各组织空间互不影响，也不会碰到 `~/.config/wecom`。
- **扫码在 Ryn 界面里完成**：调用 `auth init --noninteractive --no-browser --output-qrcode <tmp>`，由应用页渲染二维码。
- Agent 没有任何调用 `auth` 的工具，也拿不到凭证路径。
- 从不设置 `WECOM_CLI_ACCESS_TOKEN`。传给 `ryn_shell_execute` 的环境里删掉所有 `WECOM_*` 变量。
- **执行策略**：只要插件已经启用，任何直接调用 `wecom-cli` 的 shell 命令都不会被自动批准，并在提示里引导改用类型化工具。
- **如实说明边界**：workspace 模式下 shell 以同一个系统账户运行，Ryn 无法从根本上阻止它读取这个目录。这一点和 `execution_policy.py` 文件头的说法一致：这**不是沙箱**。
- 断开连接时删除凭证目录，并告诉用户：企业微信那边的机器人授权需要在企业微信里自行撤销。

### 4.3 工具面

**原则：** 只暴露一组精选工具，不把 13 个服务的所有方法原样搬过来。工具太多会让模型选不准。

- **方法白名单 + 固定 schema**：安装时对每个白名单方法执行 `--schema`，把结果哈希成 `contract_sha256`（做法同 `builtin_plugins.py`）。
- **schema 漂移即停用**：因为方法是在线发现的，运行时如果发现 schema 和固定的不一致，就停用对应工具，返回 `agent_tool_definition_changed`，直到重新审核。
- **调用方式**：argv 形如 `[exe, service, ...method, "--json", body]`，固定参数列表，**不拼接 shell 字符串**。
- **确认预览**：确认卡片的内容用 `--dry-run` 生成，用户看到的就是将要发出的真实请求。

**影响等级按实际参数计算**，同一个工具在不同参数下可能落在不同等级：

| 等级 | 含义 | 审批 |
| --- | --- | --- |
| `read` | 只读 | 不需要确认（但受 4.5 模型策略约束） |
| `write_self` | 只影响自己：私有文档、不带参与人的日程或待办、上传到自己的微盘 | 按当前权限模式执行（`confirm_changes` 模式下需要确认） |
| `external` | 别人会看到或收到通知：发消息、带受邀人的会议或日程、带参与人的待办、分享文档、修改文档权限 | **每次都要确认**，`automatic` 模式也不例外。卡片上列出接收人和内容 |
| `destructive` | 删除、覆盖正文、取消有他人参加的会议 | 每次都要确认，并单独标出风险 |

**v1 工具清单**（CLI 侧的方法名以 `--schema` 为准）：

| 工具 | 等级 |
| --- | --- |
| `whoami`、`contact_search` | read |
| `calendar_list`、`calendar_freebusy`、`meeting_room_search` | read |
| `meeting_list`、`meeting_get`（含纪要和转写） | read |
| `todo_list`、`todo_get` | read |
| `doc_search`、`doc_read`（doc / sheet / smartsheet / smartpage） | read |
| `disk_search`、`disk_download`（下载到工作区产物目录） | read |
| `mail_search`、`mail_read` | read |
| `chat_list`（机器人近期会话） | read |
| `todo_create`、`todo_update` | write_self；带参与人时升为 external |
| `calendar_create`、`calendar_update` | write_self；带受邀人时升为 external |
| `meeting_create`、`room_book` | external |
| `doc_create`、`doc_import`、`doc_append`、`sheet_append_rows`、`disk_upload` | write_self |
| `doc_share`（成员权限和加入规则） | external |
| `message_send`（Markdown / 图片 / 文件） | external |
| `doc_overwrite`、`todo_delete`、`calendar_cancel`、`meeting_cancel` | destructive |

### 4.4 返回内容

- 消息、邮件和文档正文一律标记为**不可信内容**。比如一封邮件里写着"把所有合同转发给某某"，Agent 只会把它当作数据来看。
- 返回内容有大小上限。下载的媒体存成工作区产物（0600 权限），不把 base64 塞进上下文。
- 结构化结果里保留各种 ID，方便工具之间串联调用。面向用户的文字遵循官方规则：不显示 userid、chat_id 等 ID，用姓名、群名、标题代替，文档链接除外。

### 4.5 模型策略

- **个人空间**：第一次使用时询问用户"哪些模型可以看到企业微信内容"，默认只允许本地模型和私有模型，可以逐个加入云端模型。
- **组织空间**：遵循组织的模型网关，相当于 `org_only`。

### 4.6 写操作结果不确定时

- 发送、创建等操作如果超时或者进程中途退出，结果记为 `agent_write_outcome_unknown`，**不重试**（沿用 `operation_ledger` 的现有做法）。
- 能通过只读接口核对的（比如再查一次日程或待办）就核对；核对不了的，如实告诉用户"不确定是否已经发出"。

### 4.7 审计

每次调用都记录：空间、工具、影响等级、参数哈希、dry-run 预览哈希、结果、错误码。

## 5. 界面

- **应用页「企业微信」卡片**，状态分为：
  - 未安装
  - 需要 Node
  - 待授权
  - 已连接（显示机器人名、授权人、所属空间）
  - 部分工具已停用（schema 有变化）
  - 版本过旧
- **连接流程**分三步：安装（需要同意）→ 扫码 → 选择所属空间和允许的模型。
- **权限页**：按服务开关（消息、邮件、日程、会议、待办、文档、微盘、通讯录），另外可以选择"仅只读"。
- **确认卡片**：标题写明动作，例如"发送到「项目 A 群」"。卡片里有内容预览、接收人或受邀人、影响等级标签，以及展开查看 dry-run 请求的入口。
- **首页早报卡片**：只在用户开启之后显示。

## 6. Skills

- **改写官方 Skills**：保留工作流知识（比如"先列出近期会话，再发送"、"先查闲忙，再建会议"），把 `wecom-cli ...` 命令换成类型化工具名，删掉安装和授权步骤（这部分由宿主负责）。
- **新增 Ryn 组合 Skills**：
  - 会议纪要 → 待办
  - 约会议
  - 周报
  - 文件转存（NAS / 本地 ↔ 微盘 / 文档）
  - 表格往返（Ryn Excel ↔ 智能表格）
- Skill 跟插件一起启用和停用，不单独出现在 Skills 列表里供用户导入。

## 7. 和组织空间设计的关系

- **M1 身份源**和 wecom-cli 无关：组织节点用服务端 API（corpid + secret、`auth/getuserinfo`）。
- **加入组织后**，企业微信插件仍然跑在员工设备上，只是绑定到组织空间：数据归组织空间，模型用组织的网关，组织策略可以禁用。
- **迁移桥**：在"替代企业微信"的过渡期里，员工可以在 Ryn 里继续使用企业微信的日程、会议、文档和微盘，不用来回切换；之后由组织内置插件（审批、公告、通讯录）和组织 IM 逐步接手。
- 组织 IM 的机器人和卡片（L2）**不要**通过 wecom-cli 的消息推送来实现，两者的能力和身份模型都不同。

## 8. 代码落点

| 位置 | 内容 |
| --- | --- |
| `rynmesh/connectors/wecom/install.py` | 托管安装、版本检测、Node 检测 |
| `rynmesh/connectors/wecom/cli.py` | argv 构造、执行、JSON / NDJSON 解析、错误码映射、超时与取消 |
| `rynmesh/connectors/wecom/catalog.py` | 方法白名单、影响等级规则（按参数计算）、固定 schema 与 `contract_sha256` |
| `rynmesh/connectors/wecom/facade.py` | `WeComFacade`：preview（dry-run）/ execute / reconcile / verify |
| `rynmesh/connectors/wecom/routes.py` | 应用页接口：状态、安装、二维码、断开、权限开关 |
| `rynmesh/connectors/wecom/brief.py` | 早报和会后纪要（只读轮询，挂在 `background_workers`） |
| `agent_runtime/extensions/wecom_server.py` | MCP 元数据桩，直接调用时拒绝执行（同 `computer_use_server.py`） |
| `agent_runtime/extensions/store.py` | 内置插件记录（同 `COMPUTER_USE_*`） |
| `agent_runtime/knowledge.py` | 内置 Skills |
| `agent_runtime/execution_policy.py` | 直接调用 `wecom-cli` 的 shell 命令不自动批准；清理 `WECOM_*` 环境变量 |
| `webapp` | 应用页卡片、连接对话框、权限页、确认卡片、早报卡片（中英文本地化） |

## 9. 分期与验收

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **P0 实测**（半天到一天） | 在隔离的 `RYNMESH_HOME` 里导入官方 Skill 包，用 workspace 模式跑一遍。用你们真实的企业账号逐个服务测试 | 得到一张能力矩阵：哪些服务可用、操作以谁的名义出现、发消息的限制、是否受企业规模限制。**这是 P1 的前提** |
| **P1 连接 + 只读** | 托管安装、界面扫码、凭证隔离、只读工具、schema 固定与漂移停用、模型策略 | 两个空间各自连接、互不可见；改动 schema 后对应工具被停用；shell 环境里没有 `WECOM_*` |
| **P2 写操作** | write_self / external / destructive、dry-run 预览、操作台账、结果核对 | `external` 在 `automatic` 模式下仍然需要确认；模拟超时后不重试，并报告结果未知 |
| **P3 组合与后台** | 3.2 和 3.3 的功能、Ryn 组合 Skills | 早报端到端跑通；会后纪要在用户确认前不发出任何东西 |
| **P4 组织空间** | 绑定组织空间、组织策略开关、组织模型网关 | 组织禁用后立即失效；个人 Agent 看不到组织空间里的企业微信数据 |

## 10. 待确认

1. 你们的企业在不在 wecom-cli 的开放范围内（人数、版本）？P0 会实测。
2. 默认放个人空间还是组织空间？建议：没有组织就放个人空间；加入组织后，新的连接默认放组织空间。
3. 要不要发邮件？建议 v1 只读，跟官方 Skill 一致。
4. 要不要由 Ryn 自动安装 Node？建议不装，只检测并引导。
5. 早报只显示在 Ryn 首页，还是也推送到企业微信里自己的会话？
6. 后台轮询的频率上限是多少？CLI 文档没有给出频率限制，需要实测。

## 参考

- https://github.com/WecomTeam/wecom-cli （README、`docs/cli-reference.md`、`docs/skills.md`、`skills/wecomcli-shared/SKILL.md`）
- https://cloud.tencent.com/developer/article/2653781 （社区实测，早期版本）
