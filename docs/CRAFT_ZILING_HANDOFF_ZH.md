# Craft Suite → Rynmesh：子陵接手说明

日期：2026-10-13（America/New_York）。这是本机代码交接，不是云端任务。

## 代码和现有服务

继续工作的位置：`/Users/yeogirlyun/.codex/worktrees/clawpad-node-apps/rynmesh`，分支 `codex/clawpad-node-apps`。远端同名分支包含原来的 Resume／Recruit／Desk、Antigravity 修复以及本次 Craft 接入。此分支仍建立在未合并的 #103 上，不表示 main 已经包含这些功能。

Clawpad 源码位置为 `/Volumes/ExternalSSD/clawpad`，审查版本为 `e725fa980d2ebeda3afda3325727e76fc766551b`。13 份 `craft.json` 的 SHA-256 保存在 `rynmesh/node_apps/craft_catalog.py`。Clawpad 原有修改未被改变。原 MS-1／MS-2 试运行服务也没有重启或升级，本次 Craft 尚未部署给真实用户。

## 已完成的迁移

- 13 个 Craft 条目加入现有 Node Apps 目录；加上 Resume／Recruit／Desk，共 16 个应用条目。
- 沿用现有好友身份、逐应用授权、撤销、操作前审批、加密调用及原操作回执。好友默认看不到新的应用，需要节点所有者明确授权。
- 每个 Craft 适配器只提供真实的 `editor_info`：固定版本、Web 支持、来源、校验值和格式信息。不会虚构文档编辑 API，不会通过远程工具安装软件、启动本机应用或自动更新。
- 12 个 Web 编辑器可用 `rynmesh-craft` 明确安装并在独立源站提供。固定版本和 SHA-256；下载及解压有容量上限；检查路径穿越、链接、重复文件及大小写冲突；保留上游许可和说明文件；校验成功才原子发布。
- 每个编辑器使用独立端口／源站；静态服务不持有节点身份、令牌，不提供控制 API、上传接口、自动安装或后台任务。限制浏览器跨源请求、嵌入、弹窗、相机和麦克风权限。不要把上游编辑器挂到 Rynmesh 控制 API 同一源站，也不要为了方便关闭 CSP。
- Node Apps 前端区分浏览器编辑器和原生专用版本；有中英文说明，不再错误显示“创建服务工单”。
- FilmCraft 在当前固定版本中没有 Web 构建，因此只能展示来源与原生版本信息。没有安装 DMG 或自动启动原生程序。

| 应用 | 固定版本 | 接入方式 | 上游声明的格式（未逐项验收） |
| --- | --- | --- | --- |
| CADCraft | v0.6.2 | WebAssembly browser editor | dxf, dwg, pdf |
| DeckCraft | v0.6.1 | WebAssembly browser editor | pptx, pdf |
| DesignCraft | v0.7.1 | WebAssembly browser editor | indd, idml, pdf |
| EffectCraft | v0.9.2 | WebAssembly browser editor | mp4, mov, png |
| FigCraft | v0.5.1 | WebAssembly browser editor | fig, svg, png |
| FilmCraft | v0.7.2 | Native only; catalog entry | mp4, mov, mkv |
| GridCraft | v0.6.1 | WebAssembly browser editor | xlsx, csv |
| LightCraft | v0.7.1 | WebAssembly browser editor | raw, dng, jpg, png |
| PdfCraft | v0.7.1 | WebAssembly browser editor | pdf |
| PhotoCraft | v0.8.2 | WebAssembly browser editor | psd, png, jpg, tiff, webp |
| SoundCraft | v0.6.1 | WebAssembly browser editor | wav, mp3, flac |
| VectorCraft | v0.10.0 | WebAssembly browser editor | svg, ai, pdf |
| WordCraft | v0.6.1 | WebAssembly browser editor | docx, pdf |

## 本机运行方法

先使用该分支的独立开发环境安装项目。以下命令是操作说明；本次没有给生产服务注册自动启动。

```sh
python -m rynmesh.node_apps.craft_host list
python -m rynmesh.node_apps.craft_host install --app wordcraft --cache /path/to/private/craft-cache
python -m rynmesh.node_apps.craft_host serve --app wordcraft --cache /path/to/private/craft-cache --host 127.0.0.1 --port 8932
```

默认各应用有不同端口；也可以明确指定空闲端口。不要复用 Node Apps 的 8795／8796 端口。默认只绑定本机；若在 MS-2 明确部署网络静态服务，可指定 `--host 0.0.0.0 --public-host <实际主机名或IP> --port <独立端口>`。这是提供公开程序文件，不是文档存储或好友授权接口；用户文档不得上传到这个静态源站。远程浏览器的 WebGPU、安全上下文、文件权限及 HTTPS 要实际验证，不能从本机 loopback 结果推断。

本次已校验的 12 个构建缓存在 MS-1：`/Users/yeogirlyun/Library/Application Support/RynmeshCraft/review-20261013`。安装命令为同步、一次性操作；没有启动定时检查或自动更新。所有测试浏览器与临时静态监听器会在测试结束时停止。原先的 MS-1／MS-2 Node Apps 服务仅包含旧版本，不会自动看到新目录。

## 请你接手完成的用户交付

1. 基于此分支整合用户入口：应用分组、已安装／可用状态、明确的启动确认、独立源站的新窗口入口及错误提示；不要把 `editor_info` 成功显示成“已打开编辑器”。对来自其他节点的启动地址要校验并向用户明确展示目标节点，禁止直接执行模型提供的 URL 或命令。
2. 在明确选定的节点（可用 MS-2）部署这些独立静态编辑器，再接入用户访问路径。保留现有服务、身份和数据，先检查端口及资源；没有自动监控或自动启动的授权，不要注册这些服务。
3. 逐应用验收“新建→修改→导出→重新打开”，使用真实文件校验结果；优先 WordCraft、GridCraft、DeckCraft、PDFCraft 和 VectorCraft。上游自带样例只证明显示，不能当作用户工作流验收。记录不支持的浏览器功能、格式和大小限制。
4. 当前文档保存在编辑器浏览器或用户明确导出的文件中，未接入加密网络工作区／NAS／Exchange。若要保存到 Rynmesh，需要明确的资源读写、大小限制、权限、对象哈希、审批和交付回执。不能把这些文档塞进现有 128 KiB JSON 工作区或 24 KiB 工具参数。
5. Ryn Agent 现在能发现 Craft 并查询信息，但不能编辑文档。后续编辑工具必须逐项连接上游真实命令／API，验证结果，遵守现有用户侧执行和审批；不要增加通用 shell、任意安装或远程桌面权限。
6. 在可审查的 PR 中分别说明已实现、已验证和未完成事项；保留版本、校验值、许可证及上游来源。不要顺带开展其他产品扩展。

应用入口和程序分发本阶段免费；没有新增平台抽成、Ryncoin 铸币、结算、法币兑换或节点奖励。需要收费的实际作品／服务继续用现有 Exchange 流程，不能把编辑器下载或预算展示误认为支付成功。

验收数字与截图见 `docs/acceptance/craft-20261013/README.md`。本次不声称完整 Mac 后端通过；此前未解决的基线／平台问题仍见 `docs/reviews/ZILING_UPDATE_2026-10-12.md`。


## 本次验证结论与优先处理事项

实现提交：`946b35d25224b6e5c2c6317dda018883ef2679cf`。最终专项回归通过 568 项后端测试（包含 45 项应用／适配器测试）、666 项前端测试，以及 TypeScript／Vite 构建和本地 wheel 打包。两节点真实 HTTP 目录流程和 13 个逐应用授权检查通过。

12 个 Web 编辑器均已启动并显示画布，其中 11 个启动检查无异常。PhotoCraft v0.8.2 请求 `fonts/manifest.txt` 返回 404，该文件不在固定 ZIP 中；请在用户交付前核实字体包来源、许可证及校验值，或明确展示字体回退和语言限制，不要伪造空文件让测试显示通过。EffectCraft／LightCraft 使用不同的启动信号；强制软件 GPU 的测试曾卡住，默认 headless GPU 下可以启动。不能将启动截图当成文档编辑／导出正确性的证据。

上游 LightCraft 和 EffectCraft 确有浏览器内命令接口，可参考审查文档逐项封装；这些接口尚未开放给 Ryn Agent。编辑器可能使用自己的 Web Worker／Service Worker；本次说的“无后台任务”指 Rynmesh 安装器／静态服务没有自动下载、更新、轮询或守护任务，不是承诺上游浏览器程序不创建 worker。

完整 Mac 后端仍未宣称通过。现有 MS-1／MS-2 服务未升级，用户启动入口尚待你完成。请从此分支接手，避免重复实现已经完成的目录、校验、授权和审批逻辑。


## 可重复执行的基础验证

在本地 checkout 根目录、项目独立 Python 环境中执行：

```sh
PYTHONPATH="$PWD" python -m pytest tests/test_craft_host.py tests/test_node_apps.py tests/test_antigravity_usage_accounting.py -q
```

前端使用 Node 22.23.2：

```sh
npm --prefix webapp test -- --maxWorkers=4
npm --prefix webapp run build
```

不要把上游编译后的 WASM／ZIP 或本机缓存提交进 Git。需要更新版本时，逐项审查来源和许可证，再明确修改固定版本与校验值；不要恢复启动即检查 latest／自动下载的旧路径。
