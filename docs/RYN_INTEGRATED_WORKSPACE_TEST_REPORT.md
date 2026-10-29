# Ryn 测试文档

分支：`feature/ryn-integrated-workspace`。记录日期：2026 年 10 月 12 日。

以下按功能记录已通过用例的条件、步骤和结果。自动用例使用隔离节点及测试模型；真实账号、CLI 和两机调用在对应章节单独标明。

## 测试环境

Windows、Python 3.12.10、Node.js 22.22.1。前端最终全量运行通过 660 项；后端全量测试结合定向复验，去重记录 3697 项通过用例。运行命令见 [开发文档](RYN_INTEGRATED_WORKSPACE_DEVELOPMENT.md#构建与测试)。

## Ryn 助手

条件：真实加密状态存储、后台工作器和确定性模型响应。步骤：创建任务，执行多轮模型与工具交互，追加消息，分别暂停、继续、取消；重启后读取原任务。

通过结果：任务状态与事件一致，消息和执行记录可恢复；取消后迟到模型回复按取消状态处理。

测试：`tests/test_agent_runtime.py`、`tests/test_agent_runtime_control.py`、`tests/test_agent_process_recovery.py`。

## 助手操作审批

步骤：提出写操作，在批准前检查业务状态；批准后读取结果；再分别测试拒绝、权限撤销、参数变化和重启后的审批。

通过结果：批准前保持原状态；批准后执行指定目标；拒绝和撤权按当前权限生效；输入回答与具体操作审批分别处理，原审批可持久读取。

测试：`tests/test_agent_approval_contract.py`、`tests/test_agent_operation_permissions.py`、`tests/test_agent_runtime.py`。

## 助手文件处理

条件：测试文本、PDF 提取资料和本机 WebDAV。步骤：授权目录，执行列表、分页读取和搜索；批准新建文本或文件夹，读取产物；检查文件移动、越界引用和撤权。

通过结果：返回内容对应授权资源，续读及范围检查生效，产物可按回执读取，原文件保留。固定 12000 字节请求上限的长文档用例生成了与 CHECKPOINT 值及合计一致的匿名报告。

测试：`tests/test_agent_resources.py`、`tests/test_agent_cloud_resources.py`、`tests/test_agent_resource_writes.py`、`tests/test_agent_artifacts.py`、`tests/test_agent_long_resource_context.py`。

## 助手管理模型和设备

步骤：查询模型、设备和服务，创建并测试来源草稿，确认保存；发现 CLI，确认接入并探测工具；选择默认模型，修改指定模型分享后回读。

通过结果：查询和配置对应实际目录，凭据通过专用入口处理；保存、默认选择和分享各自生效并保留回执。Antigravity 真实接入通过普通推理和两轮工具探针，模型进入可用助手目录，分享保持关闭。

测试：`tests/test_agent_source_drafts.py`、`tests/test_agent_cli_sources.py`、`tests/test_agent_default_model.py`、`tests/test_agent_atomic_sharing.py`。

## 助手预算与记录

步骤：设置调用和时间预算，执行模型、探针及摘要请求；调整预算后继续；构造长历史并压缩，重启读取精确引用，再核对实际写入结果。

通过结果：预算预留和计数一致，用户原文及完整证据保存，工具调用成对，序列化请求遵守上限。上下文、审批和压缩效率专项通过 26 项测试。

测试：`tests/test_agent_runtime_budget.py`、`tests/test_agent_budget_dispatch.py`、`tests/test_agent_checkpoint_refs.py`、`tests/test_agent_context_serialization.py`、`tests/test_agent_business_verification.py`。

## 统一模型池

步骤：准备多模型来源、本地模型和授权远端目录，读取模型池，切换筛选，打开配置和聊天，再选择助手默认模型。

通过结果：各来源按模型展示，模型 ID 和入口对应，能力状态分别呈现，默认选择和分享保持独立。

测试：`webapp/src/screens/ModelPool.test.tsx`、`tests/test_agent_model_visibility.py`、`tests/test_agent_default_model.py`。

## 逐模型设置

步骤：为一个来源创建多个模型，分别设置覆盖和继承；修改规则并派发请求，触发并发及每日额度检查，重启读取计数；分别执行三类能力探测。

通过结果：实际请求使用所选模型的适用规则，兄弟配置保留；版本冲突检查和持久计数生效，三类探测记录独立。

测试：`tests/test_model_pool_policies.py`、`tests/test_native_model_policy.py`、`webapp/src/screens/ModelPoolSettings.test.tsx`。

## 模型分享

步骤：对一个模型指定好友分享，检查目录和实际准入；保持同来源其他模型私有；撤销好友，使用旧绑定再次提交；关闭助手后执行手动共享调用。

通过结果：分享对象和接收者匹配，私有模型与其他授权保持对应状态，旧权限按当前准入处理。两机实测中双方助手关闭后的手动共享调用成功。

测试：`tests/test_agent_atomic_sharing.py`、`tests/test_agent_sharing_scope.py`、`tests/test_agent_peer_binding.py`、`webapp/src/screens/ModelPoolSharing.test.tsx`。

## 统一 AI 工作台

条件：生产网页、隔离本机节点和确定性 HTTP 模型。步骤：新建节点对话，打开原浏览器会话，选择来源并发送；刷新页面并重启节点，重新打开历史和旧参数链接。

通过结果：不同存储位置的历史正确显示，任务和回答可恢复，旧链接参数保留。

测试：`webapp/src/screens/PrivateAIChat.test.tsx`、`tests/test_ask_history.py`、`tests/test_ask_runs.py`；记录：[工作台验收](acceptance/unified-ai-workspace-20261007/README.md)。

## 聊天资料与引用

步骤：添加 NAS 文本和收藏资料，确认后发送；展开回答引用，保存回答到 NAS；更换当前附件，再保存旧回答；重启查看原引用。

通过结果：添加资料本身保持发送状态，引用对应实际资料，保存文件与回答逐字节一致；旧回答保留原目录，重启后引用可读。

测试：`webapp/src/screens/PrivateAIChat.test.tsx`、`tests/test_ask_context.py`、`tests/test_nas.py`；记录：[工作台验收](acceptance/unified-ai-workspace-20261007/README.md)。

## 网盘接入

真实账号步骤：在百度、Google 和 OneDrive 分别创建测试目录，上传文本，列举并下载，核对内容和哈希；重启后重新列举和下载。

通过结果：三家真实目录、上传、下载及重启后内容核对通过。自动用例还通过授权状态、令牌刷新、分页、上传冲突及传输域名检查。

测试：`tests/test_cloud_drives.py`、`tests/test_onedrive.py`、`webapp/src/personal/Drives.test.tsx`；记录：[网盘实测](acceptance/integrated-workspace-20261012/cloud-drives-passed.json)。

## 网盘加载优化

步骤：首次读目录后再次打开，重启恢复位置，后台刷新，快速切换网盘和目录；模拟网络中断、连接禁用和移除，再读取缓存。

通过结果：缓存及路径可恢复，后台更新和分页对应当前目录，旧请求与移除连接的结果按当前状态处理。历史同机缓存接口测量中，百度/Google 约为 4/3 ms，重启后首次缓存读取为 13/5 ms。

测试：`webapp/src/personal/DriveCache.test.tsx`、`tests/test_cloud_drives.py`；记录：[目录缓存实测](acceptance/integrated-workspace-20261012/cloud-drives-passed.json)。

## NAS 文件工作区

步骤：配置 SMB/WebDAV，浏览目录并排序搜索，上传下载和新建目录，预览文本与图片；分别修改写入和 AI 权限，移除连接后检查源文件。

通过结果：目录和文件内容对应连接，权限生效，上传保留同名内容，移除连接保留存储文件。SMB 具有 fnOS 实测记录。

测试：`tests/test_nas.py`、`webapp/src/personal/Nas.test.tsx`；记录：[NAS 验收](NAS_PLUGIN_ACCEPTANCE.md)。

## 跨设备 AI／P2P

自动步骤：连续调用复用连接，模拟丢失确认、慢消费和失效连接，撤销权限后再调用。真实两机步骤：执行 GLM 工具循环、两个会话续聊，取消一次调用，再在同进程发起新调用。

通过结果：顺序、背压、复用和权限检查生效；两机观察到 ICE/UDP 直连和独立历史。消费端约 0.109 秒返回取消，两端保存终态，后续新直连调用完成。

测试：`tests/test_ai_p2p_sessions.py`、`tests/test_llm_auto_connect.py`、`tests/test_consumer_cancellation.py`、`tests/test_llm_streaming.py`；记录：[两机](acceptance/integrated-workspace-20261012/two-host-passed.json)、[取消](acceptance/integrated-workspace-20261012/two-host-passed.json)。

## 本地 GGUF 模型

条件：构造的 GGUF 文件、测试运行组件及模型响应。步骤：导入并核对副本和哈希，启动自测，停止、卸载及重启；修改输出、并发和每日额度，检查原文件。

通过结果：导入校验和受管副本正确，原文件保留，生命周期和代际按规则更新，实际请求参数及持久额度检查通过。

测试：`tests/test_native_model.py`、`tests/test_native_model_policy.py`、`webapp/src/screens/components/NativeModelPanel.test.tsx`。

## AI 来源与 API 接入

步骤：增删改和禁用来源，读取或填写模型 ID；创建项目 Key、额度及别名，执行模型查询、文本、工具循环和流式请求，再检查权限及配额。

通过结果：来源配置及模型对应，密钥和公开投影分离；别名目标、项目鉴权、额度和响应协议按契约处理，工具结果可回传。

测试：`tests/test_ai_sources.py`、`tests/test_inference_api.py`、`tests/test_antigravity_cli.py`、`tests/test_llm_streaming_http.py`。

## 个人空间与设备

步骤：创建和加入个人空间，核对签名成员及有效期，更新设备名称和备注；执行局域网发现、地址刷新和连接诊断，再检查离开空间的准入状态。

通过结果：设备归属及访问按成员关系处理，发现消息和地址更新对应身份，设备偏好与诊断记录按对应节点保存。

测试：`tests/test_personal_space.py`、`tests/test_lan_discovery.py`。

## 界面与桌面版

步骤：切换语言，检查键和占位符，折叠菜单与任务列表，检查窄屏；构建原生程序并启动，读取健康、助手、模型来源和 NAS 接口。分阶段记录还检查了官方授权链接在系统 Chrome 打开。

通过结果：前端 660 项最终回归通过，语言和页面布局测试通过；Windows release 构建及 Ryn 窗口启动通过，五个运行接口返回 HTTP 200。wheel 内 241 个 Python 模块与源码一致。

测试：`webapp/src/Localization.test.tsx`、`webapp/src/personal/ServiceNavigation.test.tsx`、`tests/test_desktop_control.py`、`tests/test_desktop_defaults.py`。

## 数字服务交易 Exchange

条件：配置好的 v2 alpha 测试网络和确定性裁决模型。步骤：发布供需并协商订单，确认预留，交付和收款；测试退款、争议、申诉及到期处理，再提交重复或过期签名操作。

通过结果：订单、交付哈希、证据轮次和账本对应，重复及旧状态操作按协议校验，退款和到期操作使用确认后的结果。

测试：`tests/test_exchange.py`、`tests/test_exchange_hardening.py`、`tests/test_exchange_http_faults.py`。

## 测试记录

- [结果摘要](acceptance/integrated-workspace-20261012/passed-summary.json)
- [前端日志](acceptance/integrated-workspace-20261012/frontend-passed.log)
- [后端定向复验](acceptance/integrated-workspace-20261012/backend-retest-passed.log)
- [上下文与审批](acceptance/integrated-workspace-20261012/context-approval-passed.log)
- [参考上下文](acceptance/integrated-workspace-20261012/reference-context-passed.log)
