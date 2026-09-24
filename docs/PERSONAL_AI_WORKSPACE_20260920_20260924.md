# Rynmesh 个人 AI 工作空间改版汇总

记录范围：2026-09-20 至 2026-09-24（香港时间）。整合分支：`feature/personal-ai-workspace`。

## 简介

这轮改版把 Rynmesh 的主要使用入口收拢为“自己的设备、自己的 AI、自己的文件”。用户先建立个人空间、连接电脑，再统一使用本地模型、已登录的 CLI 或外部模型 API；同一套界面提供 AI 对话、应用 API 接入、设备与任务管理，以及可选的 NAS 文件工作区。界面支持中英文、深浅主题和跟随系统。

9 月 22 日的个人优先界面与个人空间已形成提交；随后 NAS、AI 来源、CLI、隐私隔离、局域网发现、本地 GGUF 导入等开发此前主要存在于当前工作目录。本次先将这些代码、测试、设计和验收文档统一保存为 `48710c0`（306 个文件变更），避免分支名称掩盖实际已完成的工作。

这是开发整合记录。历史安装包、网页验收和当前源码不是同一个版本；每项功能的验证边界见下文，不能把各次测试数量相加当作最终版本的测试总数。

## 1. 需求与产品范围

| 编号 | 用户需求 | 当前实现与边界 |
| --- | --- | --- |
| R01 | 先连通自己的设备，提供容易理解的入口 | Home、Services、Devices、Tasks、Settings；Explore/Messages 保留。个人空间身份与设备关系有明确来源。 |
| R02 | 新界面统一、可日常使用 | 主次页面、配对/编辑/共享弹窗、全局搜索、窄窗口适配、深浅主题；中英文及系统语言。 |
| R03 | 电脑间共享 AI，不反复配置连接 | 签名设备成员关系、单次加密邀请、授权撤销；聊天自动选择 LAN HTTP 或 ICE/P2P，失败有明确状态。 |
| R04 | 集中管理已有 AI 能力 | 多个 OpenAI-compatible API 来源；Ollama/LM Studio；Codex/Claude CLI；本地导入模型。配置、验证、模型发现与共享入口集中。 |
| R05 | AI 工作台与外部应用接入 | 设备/服务/模型选择、聊天记录、Markdown、API Key、模型别名和多种兼容协议；工具执行留在发起应用。 |
| R06 | 提供端不持久保存推理正文，不调用其个人工具 | 临时 Codex 会话、独立配置目录、无执行环境、受限动态工具、结果短期内存交付、进程资源保护；不保证云厂商或操作系统绝对无留存。 |
| R07 | 直接使用自己已有的模型文件 | 本地单文件 GGUF 导入、校验、受管副本、运行组件、自测、启动/停止/卸载/随应用启动。Qwen 仅作测试样本，不作为预置下载要求。 |
| R08 | 从 Ryn 浏览和使用 NAS 文件 | 可选 SMB/WebDAV 插件、连接切换、目录/搜索/排序、上传下载、图文预览、显式 AI 读取与保存结果；连接由当前节点执行。 |
| R09 | Windows 与 Mac 桌面可运行和相互发现 | Windows 节点随包分发、托盘/启动选项/节点恢复；Apple Silicon 构建、CLI 发现及局域网互通修复。不同安装包覆盖范围分开记录。 |

远程桌面、任意家庭局域网代理、通用文件同步、NAS 全文索引、移动端完整客户端仍属于规划或后续范围。本轮没有把这些愿景写成已交付功能。

## 2. 开发时间线

| 日期 | 开发内容 | 可追溯证据 |
| --- | --- | --- |
| 9 月 20 日 | 未找到当天独立 Git 提交；不能据此补造开发记录 | 本地所有分支的日期筛选记录 |
| 9 月 21 日 | 23:52 从本地推理 P2P 分支切到个人优先 UI 分支 | 本工作目录 HEAD reflog |
| 9 月 22 日 | 英文个人界面、主题、二级页面、Windows 测试包；个人空间与邀请；AI 自动连接；桌面控制与节点恢复 | `ed29187`、`2a80888`、`4a8de1b`、`842dcf9`、`af2738f` |
| 9 月 22–23 日 | NAS 接入、真实 fnOS SMB 联调、桌面接入、文件工作区与图片预览 | [NAS 验收](NAS_PLUGIN_ACCEPTANCE.md) |
| 9 月 23 日 | AI 来源配置、CLI 工作台、模型选择、Markdown、服务品牌展示、语言选择与桌面打包 | [AI 来源](AI_SOURCES_ACCEPTANCE.md)、[CLI 历史验收](CLI_DESKTOP_ACCEPTANCE.md)、[工作台 QA](design/agent-api-sharing-v1/workbench-polish-qa.md)、[语言 QA](design/language-settings-qa.md) |
| 9 月 24 日 | 推理隐私与发起端工具协议、资源保护、Mac 打包与 CLI 发现、LAN 发现、回答交付竞态修复及跨设备验证 | [隐私与工具](INFERENCE_PRIVACY_ORIGIN_TOOLS.md)、[LAN 发现](LAN_DISCOVERY.md)、[Mac CLI](MACOS_CLI_DISCOVERY.md)、[Mac API 联调](ZCODE_MAC_API_ACCEPTANCE.md) |
| 9 月 24 日 | 本地 GGUF 文件导入、生命周期；服务导航及 AI 来源首次使用界面 | [模型导入 QA](design/local-model-install-2026-09-24/IMPLEMENTATION-QA.md)、[服务导航 QA](acceptance/service-navigation/design-qa.md) |
| 9 月 24 日 | 将当前改版工作保存到统一 feature 分支，编制需求、实现、测试与文档总览 | `48710c0` 及本报告 |

9 月 23–24 日的时间线依据现有验收文档，不能推断每个文件的精确完成时刻。文件修改时间也不等同于功能验收时间。

## 3. 功能与开发实现

### 3.1 个人界面、设备和连接

- 使用真正的 React 页面与组件实现参考设计；统一主导航、设备图标、任务状态和共享弹窗。
- 名称、昵称和私有备注分开处理；本地偏好按节点身份及客户端模式隔离，预览数据不混入真实设备。
- 个人空间使用签名成员关系确认设备归属，不能仅凭节点发现或信任列表推断所有权；加入空间不授予远程桌面控制权。
- 聊天 `auto` 路由优先新鲜 LAN 地址，再尝试原公布地址，失败后走现有 ICE/UDP。跨路线沿用同一任务 ID，避免重复推理和结算；已输出内容的流不自动重放。
- 增加签名 IPv4 multicast 发现和网络地址刷新；桌面健康检查、启动恢复和连接超时均有界。
- 入口：`webapp/src/personal/`、`rynmesh/personal_space.py`、`rynmesh/personal_space_routes.py`、`rynmesh/lan_discovery.py`、`rynmesh/services/peer_health.py`。

### 3.2 AI 来源、工作台和 API

- API 来源支持创建、编辑、禁用、删除、模型列表及手工模型 ID；密钥在节点侧加密保存并脱敏，不进入浏览器持久存储或服务发现。
- 更改 endpoint 不静默复用旧服务密钥；未经验证的草稿不进入可调用目录；共享须显式开启并经提供端检查。
- AI 工作台保留设备/服务/模型选择和本地加密对话历史；Markdown 支持常见文本结构，不开放原始 HTML 或自动加载远程图片。
- 服务区保留我的服务、浏览服务、AI 工作台、AI 来源、API 接入和设置的固定入口；首次使用提供 API、本地模型、本地 CLI 三种接入路径。
- 对外 API 仍是发起节点本机访问和独立 API Key；模型别名表示服务路由，不自动固定底层模型版本。
- 入口：`rynmesh/llm_package/sources.py`、`api.py`、`cli_adapter.py`、`webapp/src/screens/AISources.tsx`、`AIWorkspacePages.tsx`、`PrivateAIChat.tsx`、`ChatMarkdown.tsx`。

### 3.3 推理隐私、客户端工具和资源保护

- 9 月 24 日方案替代旧 Codex 持久会话/续接方案：每轮临时会话、独立临时配置目录、禁用提供端执行环境，结束回收进程树和目录。
- 工具定义由发起应用提供，模型工具请求通过兼容协议返回，参数经过 Schema 验证；Ryn A/B 节点不代替发起应用运行命令或写文件。
- API 转发不保存回答正文；提供端只为短时重试保留有界加密结果；异步聊天结果领取后移除。修复“任务成功元数据先写入、回答尚未交付”导致轮询提前结束的问题。
- 请求体、并发、CLI 时间/输出/子进程资源受限；资源不足拒绝新请求。Windows、Linux、macOS 保护能力不同，不能宣称等价硬隔离。
- 协议实现不等于所有真实云模型均支持工具：当前 Mac → Windows Codex 的普通文本 API 已有成功记录，完整 zcode 工具链仍有模型兼容性阻塞。
- 入口：`codex_session.py`、`process_guard.py`、`safety.py`、`task_protocol.py`、`routes.py`；协议细节以 [隐私文档](INFERENCE_PRIVACY_ORIGIN_TOOLS.md) 为准。

### 3.4 本地模型

- 选择用户自己的 GGUF 文件，检查版本、元数据、架构、量化与张量范围；生成受管副本并保存指纹，不修改原文件。
- 固定版本运行组件按 SHA 校验；模型真实回答自测后才显示运行。支持名称、启动/停止/卸载、随 Ryn 启动和共享偏好。
- 大文件仅通过专用鉴权导入端点流式写盘，具有大小、磁盘空间和超时限制；普通推理请求仍保留小请求上限。
- 当前一个受管模型；分片、IQ 系列量化、LoRA、多模态输入未纳入；Mac Metal 与 Windows NVIDIA 路径尚缺实机验证。
- 入口：`gguf_import.py`、`native_model.py`、`native_worker.py`、`webapp/src-tauri/src/local_model.rs`、`NativeModelPanel.tsx`。

### 3.5 NAS 文件工作区

- 可选启用；多连接、SMB/WebDAV、独立读写与 AI 读取权限；连接凭据不在 API 响应中返回。
- 目录路径导航、文件夹优先排序、名称/日期/大小排序、搜索、上传、新建目录、下载；文本及图片侧栏预览、缩放和大图。
- 只有用户显式提交才读取 NAS 文本供 AI 使用，完成结果可保存回 NAS；关闭插件或禁止 AI 后拒绝新的读取。
- 文件上传下载上限 64 MiB，文本预览/AI 输入上限 256 KiB。已实测 fnOS SMB，不据协议预设名称宣称所有 NAS 品牌均已验收。
- 入口：`rynmesh/nas.py`、`webapp/src/personal/Nas.tsx`、`NasPreview.tsx`、`scripts/nas_acceptance.py`、`deploy/nas-test/compose.yml`。

### 3.6 本地化和桌面分发

- 英文、简体中文、跟随系统；翻译显示标签，保持协议字段、模型 ID、存储键及用户文本不变。
- 网页语言与原生桌面托盘/启动文案同步，语言偏好按设备保存。
- Windows：内置冻结节点、动态端口、本地认证、托盘、单实例、开机启动、后台选项、退出回收节点。
- Mac：Apple Silicon 原生构建、打包源码一致性检查、冻结 CLI 子进程、Launch Services CLI 发现、本机地址解析修复。
- 入口：`webapp/src/i18n.ts`、`uiI18n.ts`、`locales/`、`webapp/src-tauri/src/localization.rs`、`desktop.rs`、`scripts/build-macos-arm64.sh`。

## 4. 测试与验收证据

### 4.1 已有记录（历史分阶段验收）

| 模块 | 已记录的验证 | 不代表什么 |
| --- | --- | --- |
| 个人界面/空间 | 52 前端测试、118 相关 Python 测试；主题/弹窗/390px 与 1280px；Windows 测试安装包启动与托盘/退出 | 干净机器安装、签名分发与物理跨网全覆盖 |
| 自动连接 | 134 后端、53 前端；三个冻结节点进程、真实 ICE/UDP 与直接 HTTP；无重复推理/结算 | 三台物理电脑或不同公网新包验收 |
| NAS | 真实 fnOS 浏览/上传/下载/权限拒绝/重启持久性；图像阶段 69 前端、20 NAS 后端 | 所有 NAS 系统、真实模型读 NAS、最终图片版 Windows 原生视觉均完成 |
| AI 来源 | 56 CLI/包测试、37 来源/API 测试；前端和 Windows 构建；实际节点配置检查 | 当时未填密钥的 GLM 已完成真实推理 |
| CLI/工作台 | 真实 Codex 文本、模型选择、Markdown 和界面验证；后续以临时会话隐私方案替代持久续接 | 旧安装包具备新隐私方案 |
| 隐私/API | Windows 138 相关测试、11 聊天测试、2 真实 Codex 二进制隔离测试；Mac 后续 140 测试 | 隔离测试中的本地假模型等于真实云模型工具链 |
| Mac 回答交付/LAN | Windows/Mac 最终相关组各 75 测试；已安装 Mac 调用 Windows Codex成功，`route=lan` 且未使用 relay | 全部网络稳定性；Mac 原生 UI 手动流程与 zcode 工具往返 |
| 本地模型导入 | 113 后端、7 前端；Windows CPU 实际导入约 1.83 GB GGUF、API 回答、自动启动、卸载与原文件哈希 | Mac Metal/NVIDIA、多节点共享实机与新安装包已验收 |
| 服务导航 | 60 测试/10 套件，最终 9 项聚焦复测；中英文、深浅、390px、实际来源页面；生产构建 | 新桌面包已更新、在线服务目录发现完整验收 |

历史结果取自各验收文档，存在交叠，不能累加。详细命令、环境、版本、失败与限制保留在原文档中。

### 4.2 本次整合复测

本次在 Windows 当前工作区执行，生产代码对应 `48710c0`；之后只补充文档。结果如下：

| 检查 | 本次实际结果 |
| --- | --- |
| 前端全部测试：`npm --prefix webapp test -- --reporter=dot` | **27 个文件、136 项通过**，75.67 秒 |
| 相关后端回归（16 个测试文件，命令见下面） | **253 项通过**，109.66 秒；覆盖 API/CLI/隐私、来源、NAS、原生模型、LAN、个人空间、回答交付和节点健康 |
| 真实 Codex 二进制隔离：启用 `RYNMESH_TEST_CODEX_BINARY=1`，运行 `tests/test_codex_isolation_live.py` | **2 项通过**，3.34 秒；使用本地假模型，无云推理 |
| `npm --prefix webapp run build` | **通过**；包括 TypeScript 编译和 Vite 生产构建；保留主 bundle 超过 500 kB 的体积提示 |
| 应用代码/测试/脚本/配置的 `git diff --check` | **通过**；全范围另有旧 Markdown 的双空格换行和文件末空行提示，保留原文档排版 |
| 四个本轮源分支的祖先检查 | **全部包含**；详见第 7 节 |

```powershell
.venv/Scripts/python.exe -m pytest tests/test_inference_privacy.py tests/test_cli_agent.py tests/test_llm_package.py tests/test_llm_hardening.py tests/test_inference_api.py tests/test_llm_auto_connect.py tests/test_ai_sources.py tests/test_nas.py tests/test_native_model.py tests/test_lan_discovery.py tests/test_macos_addresses.py tests/test_personal_space.py tests/test_async_private_answer.py tests/test_peer_health.py tests/test_peer_health_responsiveness.py tests/test_frozen_cli_worker.py -q
```

本次后端共 255 项通过（253 + 2，不与历史数字累加）。结构化结果见 [整合验证记录](acceptance/personal-ai-workspace/verification-20260924.json)。没有重新做付费云推理、真实 NAS 写入、跨机部署、Rust 原生测试或安装包发布；不是全仓库所有后端测试的运行结果。

## 5. 文档索引与阅读顺序

1. 产品方向：[个人资源网络提案](PERSONAL_RESOURCE_NETWORK_PROPOSAL.md)、[项目愿景](RYNMESH_VISION.md)、[产品里程碑](PRODUCT_MILESTONES.md)。愿景和现状应分别阅读。
2. 界面基线：[个人优先设计](design/personal-first-en-v1/README.md)、[实现记录](PERSONAL_FIRST_UI_IMPLEMENTATION.md)、[服务导航 QA](acceptance/service-navigation/design-qa.md)。旧设计中“尚未实现”等语句是当时状态。
3. 设备连接：[个人空间](PERSONAL_SPACE.md)、[自动连接](PERSONAL_SPACE_CONNECTIVITY.md)、[LAN 发现](LAN_DISCOVERY.md)、[Windows 直连](WINDOWS_DIRECT_CONNECTION.md)。
4. AI 与隐私：[来源验收](AI_SOURCES_ACCEPTANCE.md)、[隐私与发起端工具](INFERENCE_PRIVACY_ORIGIN_TOOLS.md)、[Mac API 联调](ZCODE_MAC_API_ACCEPTANCE.md)。CLI 持久线程相关旧记录已被隐私方案取代。
5. 模型/NAS：[模型导入验收](design/local-model-install-2026-09-24/IMPLEMENTATION-QA.md)、[NAS 使用/部署](NAS_PLUGIN.md)、[NAS 验收](NAS_PLUGIN_ACCEPTANCE.md)。
6. 桌面/语言：[Windows 构建](WINDOWS_BUILD.md)、[Mac CLI](MACOS_CLI_DISCOVERY.md)、[本地化](LOCALIZATION.md)、[语言设计 QA](design/language-settings-qa.md)。

## 6. 当前仍需完成的验收

- zcode 的真实多轮工具调用往返，以及默认模型的动态工具兼容问题；不能将普通文本成功等同于工具链成功。
- 将最新本地模型导入、导航及其他源码变化一起打包后，执行 Windows/Mac 干净安装、原生文件选择和原生窗口验收。
- Mac Metal、Windows NVIDIA、本地模型两台物理设备共享及新桌面版本不同公网连接测试。
- 最新 NAS 图片工作区 Windows 原生视觉复验；真实模型读取 NAS 的端到端场景。
- 发布签名、公证、升级/卸载与完整发布矩阵。

## 7. 分支整合清单

本次起点为 `codex/nas-plugin` / `development/personal-first-implementation` 的 `af2738f`。`development/personal-first-ui` 与 `codex/local-inference-api-p2p` 均已是其祖先，不需要重复 cherry-pick。

| 来源分支 | 整合时提交 | 处理结果 |
| --- | --- | --- |
| `codex/nas-plugin` | `af2738f` | 已包含；其工作目录中的后续改版以 `48710c0` 保存 |
| `development/personal-first-implementation` | `af2738f` | 已包含，与 NAS 原分支同一提交 |
| `development/personal-first-ui` | `7009670` | 已包含，是本轮实现的祖先；设计资料同时归档 |
| `codex/local-inference-api-p2p` | `7009670` | 已包含，是本轮实现的祖先 |
| `feature/p2p-peer-transit` | `c6d15f9` | 作为已有基础历史继承 |
| `codex/v070-followup-integration` | `397ad18` | **未并入**；属于 9 月 18 日的另一套内容产品架构，保留原开发分支 |

源分支已经存在继承关系，因此通过从其最新提交创建 feature 分支并收录未提交改版来集中管理，无需制造重复 merge/cherry-pick 提交。`git merge-base --is-ancestor <源分支> HEAD` 已验证上述四个本轮源分支全部包含。

旧 v0.7.0 整合线的预检发现 36 个冲突文件，试合并涉及约 600 个文件，包含内容阅读、好友、设备同步等另一条开发线。试合并已完整退出，没有用“全部选当前版本”伪造功能合入。这次范围限定为 9 月 20 日后的改版；该旧线和其独有提交需要独立适配验收，不能宣称本地全部 37 条历史分支均已合并。

交付仅限开发分支提交。没有更新 `main`、没有合入上游、没有创建 Pull Request，也没有发布安装包。

原分支与已有 worktree 保留。本机临时脚本/运行状态目录 `.codex-tmp/`、安装包/构建缓存与根目录演示文稿及其辅助输出不混入源码整合提交；文件仍在本地。
