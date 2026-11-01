# Ryn 开发文档

分支：`feature/ryn-integrated-workspace`

## 项目结构

Ryn 由 Python 节点、React 前端和 Tauri 桌面组成。节点提供本机控制接口，处理加密存储、模型派发、业务工具和节点通信。

```text
rynmesh/
  agent_runtime/       助手任务、工具、权限、预算和结果
  llm_package/         来源、模型适配器、本地模型和 P2P 推理
  ai_access/           模型目录及好友访问授权
  cloud_drives.py      云网盘连接和操作
  onedrive.py          Microsoft Graph 连接器
  drive_cache.py       目录缓存
  nas.py               NAS、连接权限和文件操作
  peer_http.py         节点应用和 HTTP 入口
  exchange/            数字服务交易
webapp/
  src/domain/          API 客户端和数据类型
  src/screens/         助手、模型池、聊天等页面
  src/personal/        个人空间、设备和文件页面
  src/locales/         前端语言资源
  src-tauri/           原生窗口、托盘和后台进程
tests/                 后端测试
```

前端使用 `webapp/src/domain/nodeUrl.ts` 解析本机接口地址。网页开发通过 Vite 代理访问节点，桌面启动时从 Tauri 读取节点端口。

## 本地开发

以下命令从仓库根目录执行。验证环境使用 Python 3.12 和 Node.js 22。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,documents]" build pytest-xdist==3.8.0
python -c "from rynmesh.peer_http import main; main()"
```

另开终端启动前端：

```powershell
cd webapp
npm ci
npm run dev
```

默认节点地址为 `http://127.0.0.1:8791`，前端地址为 `http://localhost:5173`。节点目录由 `RYNMESH_HOME` 指定，默认使用用户目录下的 `.rynmesh`。

本机控制接口使用 `X-Ryn-Local-Token`，令牌保存在节点目录的 `control_token`。通过本机客户端提交配置，凭据保留在节点侧。

## Ryn 助手

后端由 `rynmesh/agent_runtime/runtime.py` 推进模型和工具循环，`store.py` 保存加密任务，`state.py` 处理状态。HTTP 入口在 `rynmesh/agent_runtime/routes.py`，前端在 `webapp/src/screens/RynAgent.tsx`。

创建请求保存输入、模型和范围，可通过 UUID 格式的 `client_request_id` 去重。控制操作检查修订号。重启时核对在途操作，待确认任务保留原审批内容，已经派发的写入按操作回执恢复。模型结束本轮和目标核验分别记录。

## 助手操作审批

审批由 `rynmesh/agent_runtime/operation_permissions.py` 和 `operation_ledger.py` 管理，工具在 `tools.py` 中提供预览和执行入口。前端通过任务 actions 接口提交审批 ID 和控制修订号。

审批绑定工具参数、目标和相关版本，在业务执行入口重新检查。写入保存唯一操作 ID、派发状态及回执。明确未执行的拒绝可以交回模型纠正；结果不明的已派发写入进入核对流程，避免直接重复执行。

## 助手文件处理

资源读取在 `rynmesh/agent_runtime/resources.py`、`resource_documents.py`，写入在 `resource_writes.py`，结果在 `results.py`。PDF 提取使用 `rynmesh/services/document_extract_child.py` 子进程。

工具接收签名资源引用，核对连接、目录范围、操作权限和当前资源归属。目录每页最多 10 项，文本读取每次最多 2000 字符，搜索最多返回 6 个片段。续读绑定内容版本；读取途中移动或撤权时重新核对。写入使用新名称，产物由实际操作回执定位。

## 助手管理模型和设备

`rynmesh/agent_runtime/tools.py` 注册模型、设备和服务查询。`source_drafts.py` 管理来源草稿，`cli_sources.py` 处理 CLI 接入，`default_model.py` 和 `sharing.py` 处理默认选择与分享。

来源接入依次经过草稿、固定消息探测、版本检查和保存。凭据由专用 credential 接口提交，模型看到公开配置和凭据存在标记。更换默认模型影响新任务，当前任务模型通过独立接口修改。设备状态区分当前观察和历史探测记录。

## 助手预算与记录

预算在 `rynmesh/agent_runtime/budget.py`、`timing.py`，操作回执在 `operation_ledger.py`，核验在 `verification.py`、`read_verification.py`。前端分别由 `RynAgentBudget.tsx`、`RynAgentRecords.tsx` 和 `RynAgentVerification.tsx` 展示。

预算在派发前预留，收到用量后结算，保留未知用量的状态。压缩保留用户原文、工具调用配对、写入回执和精确引用；完整序列化请求检查上下文上限，空间不足时先释放可重载的工具定义。核验状态与任务本轮结束状态分开保存。

## 统一模型池

前端入口为 `webapp/src/screens/ModelPool.tsx`，数据客户端为 `webapp/src/domain/aiSources.ts`。`rynmesh/agent_runtime/models.py`、`ai_access/catalog.py` 和 `llm_package/space_catalog.py` 组合本机和授权远端模型目录。

目录使用稳定模型 ID，同时保留上游模型名称、来源 ID、服务 ID 和目的地版本。配置、连通证据、工具证据和分享状态分别返回。手动工作台、助手和 API 映射复用目录与现有准入逻辑。

## 逐模型设置

`rynmesh/llm_package/model_settings.py` 保存覆盖值、有效值、继承字段和规则版本；`model_dispatch.py` 是适用来源的实际请求入口，`native_policy.py` 处理 GGUF 规则。

配置接口为 `PUT /api/local/llm/sources/{source_id}/models/config`，使用 `expected_rule_revision`。探测接口为对应的 `models/test`，probe 区分 connection、model_exists、tools。输出上限进入实际请求，并发按模型计数，每日额度持久保存。兄弟模型的显式配置和代际独立处理。

## 模型分享

`rynmesh/ai_access/store.py` 保存好友授权，`catalog.py` 提供授权目录；`agent_runtime/sharing.py` 处理指定好友的完整预览和事务。`llm_package/peer_binding.py` 绑定模型及接收者权限代际。

分享设置、逐好友授权和模型规则都有版本检查。专用单好友分享工具在一项确认中设置所选接收者，并展示其他已有授权的变化。提供端在适配器准入时重新核对授权，撤销后更新目录广告。前端为 `webapp/src/screens/ModelPoolSharing.tsx`。

## 统一 AI 工作台

路由入口在 `webapp/src/App.tsx`，工作台由 `AIWorkspace.tsx`、`PrivateAIChat.tsx` 及现有 Ask 模块组成。前端连接复用 `domain/nodeClient.ts` 和来源目录。

新对话使用节点历史及持久任务；旧浏览器会话按原存储读取。CLI 模型选择随节点对话保存，并写入确认后的任务。历史和资料参数在路由转换中保留，工作台刷新或重启读取归档结果。

## 聊天资料与引用

工作台复用现有 NAS 读取权限和收藏文章副本准备流程。资料在发送前由节点重新读取并准备，回答归档保存实际引用的资料 ID 和片段大小。

保存操作绑定该回答当时的引用目录，重新检查当前写权限后新建文件。NAS 文本入口仍使用 256 KiB 上限。前端入口在 `webapp/src/personal/NasPreview.tsx` 和工作台资料组件。

## 网盘接入

`rynmesh/cloud_drives.py` 提供 Google、百度和连接公共流程，`onedrive.py` 实现 Microsoft Graph。页面为 `webapp/src/personal/Drives.tsx`，系统授权链接由 `webapp/src-tauri/src/drive_browser.rs` 打开。

授权校验 state、PKCE 或设备码轮询状态。连接保存令牌并刷新，取消后检查迟到授权结果。文件和分页地址按提供商官方域名规则校验，预授权传输地址与认证接口分别处理。OneDrive 使用分块上传，Google 和百度分别使用其连接器流程。

## 网盘加载优化

`rynmesh/drive_cache.py` 保存有容量约束的加密目录缓存。`webapp/src/domain/nas.ts` 管理前端缓存、位置和请求，页面在 `Drives.tsx`。

缓存命中先返回列表，超过 30 秒后台更新。网络 I/O 移出全局状态锁，每个网盘独立排队并复用连接，同目录并发读取合并。Google 和百度按每页 200 项逐步返回；保存位置与请求标记用于丢弃旧目录结果。

## NAS 文件工作区

后端为 `rynmesh/nas.py`，前端为 `webapp/src/personal/Nas.tsx`、`NasPreview.tsx`。接口使用 `/api/local/plugins/nas` 前缀和本机认证。

保存连接前尝试列出配置根目录，路径解析限制在指定根。文件上传下载上限 64 MiB，文本预览和手动资料入口上限 256 KiB。SMB 上传先写临时同级文件，再以不覆盖方式发布；WebDAV 使用相应协议操作。预览与 AI 读取分别检查权限。

## 跨设备 AI／P2P

`rynmesh/llm_package/p2p.py` 处理连接与签名加密消息，`p2p_session.py` 提供会话通道和连接池，`consumer_cancellation.py` 中断消费端等待。

协议使用消息序号、确认重传、窗口和背压处理丢包及慢消费。连接池限量缓存并回收空闲连接；失效连接按后续请求重建。路由切换沿用任务标识，工具循环继续使用目的地和授权绑定。流式任务已经输出时避免重放推理。

## 本地 GGUF 模型

`rynmesh/llm_package/gguf_import.py` 解析并校验文件，`native_model.py` 管理导入和生命周期，`native_worker.py` 运行模型，`native_policy.py` 控制适用模型规则。前端为 `webapp/src/screens/components/NativeModelPanel.tsx`。

专用鉴权上传接口流式写盘，检查大小、磁盘空间和超时。运行组件按固定版本和 SHA 校验。导入、卸载和运行实例变化更新模型代际；输出、并发和每日额度在实际请求入口执行，计数重启后保留。

## AI 来源与 API 接入

来源在 `rynmesh/llm_package/sources.py`，API 接入在 `api.py`，模型适配器在 `adapters.py`、`cli_adapter.py`、`antigravity_session.py`。页面在 `webapp/src/screens/AISources.tsx` 和 `AIWorkspacePages.tsx`。

项目推理 Key 与本机控制令牌分别鉴权。`GET /v1/models` 返回可调用模型，推理路由包括 `/v1/chat/completions`、`/v1/responses` 和 `/v1/messages`。别名绑定目标，项目额度在派发前预留。模型返回工具请求，由发起应用执行后提交结果；API 转发按隐私和短期回执策略处理正文。

## 个人空间与设备

`rynmesh/personal_space.py` 和 `personal_space_routes.py` 管理空间成员，`lan_discovery.py` 管理签名局域网发现，`services/peer_health.py` 提供连接诊断。

设备归属依据签名成员关系和有效期核对。发现消息更新合适的局域网地址，网络变化后刷新公布信息。名称、昵称和本机私有备注分别保存，偏好按节点身份和客户端模式隔离。前端使用个人空间、设备页面和助手设备查询入口。

## 界面与桌面版

`webapp/src/personal/Shell.tsx` 管理主布局，`i18n.ts` 和 `locales/` 提供语言资源。Tauri 的 `src/lib.rs`、`node.rs`、`desktop.rs`、`preferences.rs`、`localization.rs` 和 `drive_browser.rs` 管理窗口及节点生命周期。

桌面启动解析动态端口，复用已有健康节点或启动受管子进程。本机认证、单实例和退出进程树处理独立于窗口显示。外部授权地址校验官方 HTTPS 主机。打包脚本包含节点、文档提取依赖和语言文件。

## 数字服务交易 Exchange

`rynmesh/exchange/service.py` 实现业务操作，`protocol.py` 校验签名及状态，`ledger.py` 管理账本，`routes.py` 提供本机及节点 HTTP 入口。前端为 `webapp/src/screens/Exchange.tsx`。

订单操作绑定网络、付款方、已审阅状态、轮次、证据和剩余操作数。交付接受绑定内容哈希；每方每轮的证据不可替换，申诉创建新一轮。退款、裁决和到期结算检查余额、预留及已审阅结果，按确定的状态转换写入账本并防止重复操作。

## 控制接口

下列路径以 `/api/local/agent` 为前缀，具体参数见 `routes.py` 和前端 `domain/rynAgent.ts`。

| 方法与路径 | 用途 |
| --- | --- |
| `GET/PUT /settings` | 助手设置 |
| `GET /models` | 模型目录 |
| `GET/POST /tasks` | 列出或创建任务 |
| `GET /tasks/{id}` | 任务详情 |
| `POST /tasks/{id}/messages` | 追加消息或调整目标 |
| `POST /tasks/{id}/actions` | 确认、暂停、继续、取消等操作 |
| `GET /tasks/{id}/events?after={seq}` | 增量读取事件 |
| `PUT /tasks/{id}/budget` | 调整预算 |
| `GET/PUT /tasks/{id}/scope` | 资料范围 |
| `GET/PUT /tasks/{id}/model` | 任务模型 |
| `POST /tasks/{id}/source-credential` | 提交来源凭据 |
| `GET /tasks/{id}/artifacts` | 文件产物 |
| `GET /tasks/{id}/results/{result_id}` | 分页读取工具结果 |

创建任务前启用助手并设置可用默认模型，最小请求体为：

```json
{"message":"列出本机可用模型"}
```

前端创建请求还提交 `model_id` 和 UUID 格式的 `client_request_id`。资料任务同时提交范围和对应的目的地版本。任务控制使用 `expected_control_revision`；修改资料范围还使用 `expected_scope_revision`，调整目标使用相应目标修订号。

模型来源接口位于 `/api/local/llm/sources`，文件连接和操作接口位于 `/api/local/plugins/nas`。

## 构建与测试

后端检查和打包：

```powershell
python -m ruff check rynmesh tests
python -m build --wheel --no-isolation
```

Windows 后端测试使用独立短路径临时目录。从仓库根目录运行：

```powershell
$rynTestRoot = Join-Path (Split-Path (Get-Location).Path -Parent) ('.ryn-test-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
if (Test-Path -LiteralPath $rynTestRoot) { throw 'Test directory already exists.' }
python -m pytest tests -q -n 4 --dist loadscope --basetemp $rynTestRoot --tb=short
```

前端测试和构建：

```powershell
cd webapp
npm test -- --maxWorkers=4
npm run build
```

Windows 桌面构建需要 Visual Studio Build Tools 和 Rust MSVC 工具链。安装包入口为 `webapp/src-tauri/scripts/build-windows.ps1`。只生成原生程序时，在 `webapp/` 执行：

```powershell
npm run tauri -- build --no-bundle --target x86_64-pc-windows-msvc --config src-tauri/tauri.windows.conf.json
```

## 数据和临时文件

节点目录保存身份、认证令牌、来源配置、会话和任务状态。模型密钥、网盘令牌、NAS 凭据和任务状态按各自存储模块加密处理。

`.codex-tmp/` 保存本机启动脚本和临时运行材料，由 Git 忽略。当前桌面启动脚本是 `.codex-tmp/integration-preview-20261012/Start-Ryn.cmd`，通过项目 Python 环境加载工作区源码，并使用原有节点目录。

[产品文档](RYN_INTEGRATED_WORKSPACE_PRODUCT.md) · [测试文档](RYN_INTEGRATED_WORKSPACE_TEST_REPORT.md)
