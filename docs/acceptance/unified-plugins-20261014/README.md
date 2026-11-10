# 统一插件管理（2026-10-14，Windows 本地）

2026-10-15 后续交付：当前本机已安装并启用 16 项；Craft 桌面入口已改为 Ryn 独立编辑窗口，四件套实际打开及 PDF 原生保存/重开验证见 [本地编辑窗口验收](../native-editor-windows-20261015/README.md)。下方保留 2026-10-14 各阶段的原始结论。

本批承接 Craft 插件管理。按用户要求，将当前适合插件化的应用统一收进 `/settings/plugins`；新节点默认仅勾选 PDF、Word、Excel、PPT，启用后 Agent 默认可用，无独立 Agent 开关。未部署生产服务、未改变节点身份、未注册自动启动或监控。

## 范围与安装方式

| 类型 | 数量 | 安装和卸载 |
| --- | --- | --- |
| Craft Web 编辑器 | 12 | 固定版本及 SHA-256，确认后下载至用户数据目录；卸载删除该版本程序包 |
| Resume / Recruit / Desk | 3 | 可信轻量适配器随 Ryn 发行；添加时仅原子写入本地 manifest/receipt，无额外下载；卸载删除注册 |
| NAS | 1 | 现有 NAS 核心纳入相同轻量注册、启停、卸载，连接配置仍在原加密存储 |
| FilmCraft | 1 | 固定版本暂无 Web 构建，只展示来源与原生信息，不能安装或伪装为可用插件 |

共 17 个目录条目，16 个可安装/启用。没有将 Ryn 的聊天、模型来源、Exchange 等核心功能另行扩展为新产品或插件。

重型 ZIP/WASM 不进入主安装包。注册与程序包位于 `%LOCALAPPDATA%/Ryn/plugins/<节点哈希>/<插件>/<固定版本>/`；注册状态沿用该节点的 `craft-plugins.json`。插件目录中的 JSON 不会作为 Python、JavaScript、shell 或入口命令加载；实际适配器仅执行发行包中的白名单实现。内置适配器的 manifest 包含原 Clawpad 来源、操作合同哈希及固定版本；Craft 保留原固定版本、校验值与许可证。

## 生命周期与兼容

- 新节点不自动安装业务/NAS；默认四件套只是选项，下载仍需确认。
- 老节点仅迁移已有业务记录/操作或明确好友授权的业务插件；已有 NAS 配置迁移为已安装，并保留原启停状态。迁移只注册轻量插件，不联网下载，不改原业务或 NAS 加密数据。读取失败时显示错误并保留原文件。
- 一次性迁移有完成标记；用户卸载后重启不会被旧记录重新安装。
- 新的本机及好友操作由提供节点的插件 lease 把关；停用、卸载会隐藏提供节点目录中的能力，并轮换访问代次，使旧审批在重新启用或重装后仍失效。好友授权记录保留，新好友不会自动获得访问权。
- NAS 旧设置接口委托同一生命周期；正在执行的资源读写会阻止并发停用/卸载。安装不改变 `allow_ai`、可写权限、任务资源范围或模型数据授权；停用后恢复会使旧资源授权失效。
- 卸载业务/NAS 仅删除插件注册，不删除业务记录、历史操作回执、NAS 凭据或远端文件。历史回执仍按原身份和好友授权审计；新的数据访问需重新安装启用。
- Web 编辑器沿用单独源站、明确启动确认、固定本机地址校验与 CSP。轻量插件只能跳转到白名单内页，不接受任意 URL、命令或外部入口。

## Agent 实际能力

`ryn_list_plugins` / `ryn_plugin_info` 动态发现启用插件及真实能力。Resume、Recruit、Desk 的 28 个既有业务操作继续使用 `ryn_node_app_action`，私有读取和修改均沿用原操作审批。NAS 暴露现有资源发现、读取、搜索，以及受限文本文件/目录创建工具，保留原资源权限和写入审批。

Craft 当前仅接入真实编辑器信息；**没有接入通用文档编辑、合并、转换或导出 API**。插件安装与 Agent 默认可用不等于 Agent 已能编辑 Word、Excel、PPT、PDF。FilmCraft 无 Web 构建，不对 Agent 宣传为可用编辑工具。

## 界面与语言

应用分为常用办公、业务应用、设计与图像、音视频、存储；四件套按 PDF、Word、Excel、PPT 顺序排列。“添加插件”中统一展示全部条目，轻量插件明确标记无需下载。NAS 与业务插件使用同一卡片、开关、详情和卸载确认，无旧 NAS 独立折叠面板。中英资源及 Agent 工具名称已补齐。

2026-10-14 界面修订：管理页改为工具卡片，增加已安装/发现视图、按名称/用途/格式搜索，以及发现页分类；重绘四件套与其他工具图标，调整字号、信息层次、间距、深浅色外观和手机单列布局。每张已安装卡片保留直接打开、一个启停开关、实际 Agent 能力与详情/卸载菜单；操作成功提示改为自动收起的浮层，避免页面跳动。没有改变默认四件套、下载逻辑、启动确认或执行审批。

本轮相关前端 **16 项测试与生产构建通过**。实际临时 HTTP 节点检查了搜索、分类、单项安装确认、停用后禁止打开、FilmCraft 无安装按钮及卸载确认；中文、英文 390px 布局均无横向溢出，浏览器无 JavaScript 错误。最新截图：[管理页](../plugin-ui-refinement-20261014/plugins-desktop.png)、[发现插件](../plugin-ui-refinement-20261014/discover-desktop.png)、[深色](../plugin-ui-refinement-20261014/plugins-dark.png)、[手机](../plugin-ui-refinement-20261014/plugins-mobile.png)、[英文](../plugin-ui-refinement-20261014/plugins-english.png)、[卸载确认](../plugin-ui-refinement-20261014/uninstall-confirmation.png)。检查结果见 [ui-result.json](../plugin-ui-refinement-20261014/ui-result.json)。导航 Shell 使用示例数据，插件状态及操作使用实际临时节点。

## 验证记录

- 最终插件专项：`test_unified_plugins.py`、`test_craft_plugins.py`、`test_node_apps.py`、`test_nas.py` 共 **79 项通过**。覆盖 17 条目与默认四件套、真实加密业务操作、好友权限、迁移与失败恢复、旧审批失效、卸载保留数据、NAS 旧入口同步及权限保留、Agent 实际业务调用和旧能力拒绝。
- 扩大相关后端回归（包含 Craft Host、Antigravity、NAS/Agent 资源读写与云资源）**173 项通过、2 项失败**。失败为 `test_agent_resource_writes.py::test_runtime_restart_queries_business_ledger_without_replaying[True/False]`：任务在审批前已有 `agent_invalid_tool_call`，没有生成待审批条目。在独立进程中绕过本批 NAS/资源生命周期包装器后，两项仍失败；对比脚本见 `output/unified-plugin-baseline/check_resource_baseline.py`。本批未修改其 Agent 调度/恢复行为。随后新增的迁移缓存损坏恢复用例包含在最终 79 项专项中。
- 全前端 **109 文件、750 项测试通过**；TypeScript/Vite 生产构建通过。原构建的大体积 chunk、通知模块混合静态/动态导入提示仍存在。
- 实际临时本地 HTTP 管理器返回 17 项；首次选择仅 PDF、Word、Excel、PPT 勾选。通过真实安装接口注册 Resume/Recruit/Desk/NAS，并注册此前已校验的固定 Word 构建，均返回 200；管理页显示 5 个独立开关，停用 Resume 后能力列表清空，再启用恢复。Shell 使用示例导航数据，插件状态与安装/启停接口使用实际临时节点，未连接生产节点。
- 已检查中英文界面、1440px 桌面及 390px 手机布局，手机无横向溢出，浏览器无 JavaScript 错误。截图见 [默认选择](default-selection.png)、[全部条目](all-choices.png)、[统一管理](plugins-implemented.png)、[业务应用](business-apps.png)、[手机布局](mobile-apps.png)、[英文](plugins-english.png)；接口结果见 [http-ui-result.json](http-ui-result.json)。
- Python wheel 构建通过，新增内置插件注册模块及生命周期模块与源码一致，**不含 WASM/ZIP**，见 [package-result.json](package-result.json)。重型编辑器缓存仍位于仓库外。Ruff（本批插件模块与测试）及改动 whitespace 检查通过。

首批固定 Word 编辑器的真实下载、独立源站和 WASM 证据仍见 [Craft 验收](../craft-plugins-20261014/README.md)。这些启动证据不作为文档编辑验收。临时测试浏览器与 HTTP/编辑器监听器在验收结束后关闭。

尚未完成 12 个编辑器的“新建→修改→导出→重开”验收，未新增上游文档编辑 API。PhotoCraft 字体包缺失、WordCraft 中文缺字仍显示限制。当前主机未提供 Cargo/Rust，因此未生成完整新的 Windows/Tauri 安装包。未宣称整个后端或完整 Mac 后端通过。

复验：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_unified_plugins.py tests/test_craft_plugins.py tests/test_node_apps.py tests/test_nas.py -q
npm --prefix webapp test -- --maxWorkers=4
npm --prefix webapp run build
```
