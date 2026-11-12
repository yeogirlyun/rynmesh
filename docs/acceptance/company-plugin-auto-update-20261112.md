# 公司三合一插件自动更新验收记录（2026-11-12）

## 交付与集成基线

| 仓库 | 任务分支 | 集成分支 `develop` 提交 | 说明 |
| --- | --- | --- | --- |
| `rynmesh` (`yeogirlyun/rynmesh`) | `codex/plugin-service-updates-20261112` | 代码提交 `af398ad` + 本验收文档提交（Fast-forward 合入 `develop`，基线 `305f474`） | 通用插件服务更新器、桌面壳快速重启切换、设置页与插件页更新 UI、安装包信任保留与防降级 |
| `gshopper-ryn-node` (`yeogirlyun/gshopper-ryn-node`) | `codex/plugin-service-updates-20261112` | `c7e6c7ab654afa5090b02a14b778288a90226545`（基线 `410720e`） | 仅保留插件服务包两个认证下载接口（含 Range 断点续传、缓存策略、限流与无泄露 401）及三合一插件构建发布脚本 |
| `chat-backend/integrations/company-app` | `codex/plugin-service-updates-20261112` | `e460fcdb56e4dec61501dab51aff8fa4d2e45ed9`（基线 `9e86a248`） | 本地服务健康检查返回 `version`/`revision`/`busy`、单调递增版本号、单命令签名包构建脚本、PyInstaller `uvicorn` PEP 420 子模块完整打包 |

- 本次严格遵守范围边界：**未推送远端、未部署 `hk036`、未覆盖或重启宿主机现有 Ryn 安装**；仅在本地完成开发、单元测试、虚拟机实机验收与本地 `develop` 分支集成。
- 仓库内仅包含发布公钥（`deploy/windows/company-service-update.pub` / `integrations/company-app/deploy/service-update.pub`），不包含任何私钥、凭据、令牌、数据库或构建产物。

---

## 核心改动摘要

1. **后端插件更新下载接口（`gshopper-ryn-node`）**
   - 移植并收敛 `src/controller/ryn/rynUpdates.controller.ts`：仅暴露 `GET /ryn/updates/packs/:id/latest.json` 与 `GET /ryn/updates/packs/:id/blobs/:sha256`，不包含 `desktop/*` 本体下载接口（Ryn 本体继续走 GitHub）。
   - 复用设备现有 `peer.connect` 登录令牌鉴权与 `RynRateLimitService` 限流；未登录或令牌无效一律先返回 `401 {"error": "unauthorized"}`，不泄露包是否存在、版本号或文件哈希。
   - 包目录由环境变量 `RYN_UPDATES_DIR` 指定；清单返回 `Cache-Control: private, no-cache`，文件块返回 `Cache-Control: private, max-age=31536000, immutable` 并支持 HTTP `Range`（`206` / `416`）。

2. **Ryn 通用插件服务更新器与桌面快速切换（`rynmesh`）**
   - `rynmesh/node_apps/service_updates.py`、`rynmesh/node_apps/release_declarations.py`、`scripts/publish_service_pack.py`：
     - 跟随「自动下载更新」开关，每小时检查一次、失败 15 分钟重试；公钥仅从本机 `plugin-services/updates.json` 读取，校验 `ryn.service.release` Ed25519 签名；登录令牌严格限制发往签发同源，跨域地址或跨域重定向立即拒绝。
     - 增量复用已装文件，仅下载哈希变化的 blob，逐文件校验字节大小与 SHA-256，写入独立的新版本目录，不改动运行中目录。
     - 原子同步切换：服务运行时、插件声明（`ryn-declarations.json`）与外置技能包（`ryn-skills/`）一起切换与回退；保留登录态、`COMPANY_APP_HOME` 用户数据、插件启用状态及安装包授予的远端/本地 MCP 只读工具信任。
     - 切换后在窗口期内轮询健康检查并核对返回的 `revision`；失败自动回退上一版本并将故障 `revision` 记入 `quarantined` 永久跳过。
     - `min_host` 高于当前 Ryn 版本时不下载并提示先更新 Ryn；已装版本不低于清单版本（含旧版整包重装）时绝不降级。
   - 桌面壳快速重启（`webapp/src-tauri/src/plugin_services.rs`）：新增 `restart_plugin_service` Tauri 命令，用户点击「立即更新」切换配置后立即重启对应本地服务进程，避免等待约 30 秒的常规巡检周期。
   - 通用化边界检查：Ryn 更新器、桌面壳与前端代码中不含「公司」「企业微信」等业务硬编码，显示名称、关联插件、更新地址与公钥均由安装包写入的 `plugin-services/updates.json` 驱动。

3. **前端界面（`webapp`，对齐 Claude 设计稿 4 张画板）**
   - `webapp/src/screens/UpdatesPanel.tsx` & `UpdatesPanel.css`：
     - 「设置 → 软件更新」在 Ryn 本体卡片下方渲染「已安装的插件 / Installed plugins」（无更新源时整段隐藏），覆盖已是最新、下载中（进度条）、已下载好（更新说明 +「立即更新」）、等任务结束、已退回旧版、需要先更新 Ryn、需要登录及人话错误提示。
     - 保持原有 2 个开关，说明文案扩展为同时覆盖 Ryn 与已安装插件；左上角更新胶囊按钮保持仅服务 Ryn 本体。
     - 右下角一次性更新完成 Toast（`DesktopUpdateOverlay`），确认或自动消隐后调用 `/acknowledge` 不再重复弹出。
   - `webapp/src/screens/ExtensionWebApp.tsx`：对应插件服务处于 `applying` 状态时展示「正在更新…」过渡页，新版本就绪后自动重新挂载打开插件页面。

4. **插件健康修订号与单命令发布（`company-app`）**
   - `runtime_health.py`：`/api/local/runtime/health` 返回 `version`、`revision`（`source_commit`）及运行中请求 `busy` 标记。
   - `desktop_bundle.py` / `build_desktop_service.py` / `release_service_pack.py`：固化单调递增的 UTC `YYYY.M.D.HHMMSS` 版本号规则，提供单命令构建服务 + 生成签名清单与去重 `blobs/` 输出待上传目录（不自动上传）。
   - 实机冻结验收补修：修复新版 `uvicorn` 采用无 `__init__.py` 的 PEP 420 命名空间子目录（如 `uvicorn.protocols.http.auto`）时 PyInstaller `--collect-submodules uvicorn` 漏收动态导入模块的问题，显式枚举并注入全部 `uvicorn` 子模块隐藏导入。

---

## 单元测试结果

| 仓库 / 模块 | 命令范围 | 结果 |
| --- | --- | --- |
| `rynmesh` Python | `pytest tests/test_service_updates.py tests/test_full_package_install.py tests/test_installer_connection_trust.py` | **76 passed** (7.86s) |
| `rynmesh` Webapp | `vitest run src/screens/UpdatesPanel.test.tsx src/screens/ExtensionWebApp.test.tsx` + `npm run build` | **32 passed** + `tsc && vite build` 通过 |
| `rynmesh` Tauri Rust | `cargo test` (`webapp/src-tauri`，含 macOS/Unix 条件编译路径静态兼容核查) | **69 passed, 0 failed** |
| `gshopper-ryn-node` | `jest test/service/ryn/{rynUpdates,rynEndpoints,rynMcp,rynOAuth,rynPeer}.test.ts` | **5 suites, 58 passed** |
| `company-app` | `pytest integrations/company-app/tests/test_desktop_runtime.py` | **11 passed** |

---

## 虚拟机（`SG-VM`）10 项端到端实机验收结果

验收环境：`SG-VM` Windows 11 隔离目录，使用候选分支编译的真实 `Ryn.exe`、`rynmesh-peer.exe`、`company-service.exe`，配合临时测试签名密钥对、本机双端口 HTTPS 假更新服务器（含 58 个 `readOnlyHint: true` 工具的模拟远端 MCP 端点）以及 WebView2 CDP 实机界面驱动。

- 基线包 `v1`：`version = 2026.11.12.100000`，`856` 个文件，总大小 `214,287,905` 字节（`569` 个去重 blob）
- 升级包 `v2`：`version = 2026.11.12.110000`，`856` 个文件，总大小 `214,287,961` 字节（`569` 个去重 blob，更新了可执行程序、清单与外置技能）

| 序号 | 验收场景 | 实测结果与关键指标 | 结论 |
| --- | --- | --- | --- |
| 1 | **正常升级（旧版 → 新版）** | `2026.11.12.100000` → `2026.11.12.110000` 升级成功；`peer.connect` 登录会话保留、`COMPANY_APP_HOME` 用户数据保留；`company-workspace`、`company-dashboard`、`company-affiliate` 三个插件均保持启用、HTTP `200` 且在 WebView2 中正常打开 `iframe`；外置技能同步切换至 `v2`；插件页实测出现「正在更新」过渡页，完成后右下角弹出一次「已更新到 `2026.11.12.110000`」提示。 | **通过** |
| 2 | **增量下载（只下载变化文件）** | `v2` 整包共 `856` 个文件、`214,287,961` 字节（`204.36 MiB`）；本机复用未变化文件后，实际仅请求下载 **5 个变化 blob，共 `11,586,255` 字节（`11.05 MiB`）**，节省 `202,701,706` 字节（**节省 94.59%**）。 | **通过** |
| 3 | **新版启动失败自动回退与永久跳过** | 部署退出码为 `1` 的签名故障包 `2026.11.12.120000`（修订号 `badd0000...0001`），点击「立即更新」后在验证窗口超时内自动回退至 `2026.11.12.110000` 并恢复本地服务健康（HTTP `200`）；状态转为 `rolled_back`，界面显示「已自动退回」提示；故障修订号写入 `quarantined`，再次触发检查下载 **0 个 blob**，不再重试该故障版本。 | **通过** |
| 4 | **重装旧版整包不降级** | 在已升级到 `2026.11.12.110000` 的安装目录上重新执行 `v1`（`2026.11.12.100000`）整包安装脚本 `full_package_install.py`：安装回执 `kept_newer_runtime = true`，活动服务保持 `2026.11.12.110000`；随后向返回 `v1` 清单的服务器检查更新，下载 **0 个 blob**，不发生降级。 | **通过** |
| 5 | **Ryn 本体版本过旧（`min_host`）** | 服务器提供 `min_host = "99.0.0"` 的签名包（当前 Ryn `0.9.0`）：状态置为 `requires_host`，实际下载 **0 个 blob / 0 字节**，设置页卡片提示需要 Ryn `99.0.0` 或更高版本并提供「更新 Ryn」入口。 | **通过** |
| 6 | **未登录态保护** | 未携带有效令牌请求更新清单返回 HTTP `401`（响应体仅 `{"error": "unauthorized"}`，无任何版本或更新存在性泄露）；本地状态为 `login_required`，设置页显示需登录后检查更新，同时现有 `v1` 三个插件页面全部返回 HTTP `200` 正常可用。 | **通过** |
| 7 | **安全校验全面拒绝** | 逐项注入四类异常均返回 HTTP `409` 拒绝且活动版本保持不变：① 篡改清单签名 → `service_update_bad_signature`；② 非本机信任私钥签名 → `service_update_untrusted_signer`；③ 篡改文件块字节 → `service_update_checksum_mismatch`；④ 文件块 `302` 跨域重定向 → `service_update_download_origin_rejected`。 | **通过** |
| 8 | **未装公司插件的纯净 Ryn** | 无 `plugin-services/updates.json` 时，`GET /api/local/service-updates` 返回 `{"services": []}`，WebView2 打开「设置 → 软件更新」不渲染「已安装的插件 / Installed plugins」分段。 | **通过** |
| 9 | **更新后保持只读工具信任** | 升级至 `v2` 后对 `company-dashboard-dashboard` 执行包含 58 个 `readOnlyHint: true` 工具的发现同步：`installer_config_trusted = true`，58 个工具全部保持 `installer` 来源只读信任，`server_hint_candidates = 0`，界面无「58 个工具声明为只读」提示。 | **通过** |
| 10 | **「立即更新」恢复耗时** | 从在「设置 → 软件更新」点击「立即更新」按钮起，到新版本 `company-service`（`2026.11.12.110000`）启动通过健康及修订号校验、插件页面自动重新挂载恢复 `iframe` 止，实测端到端总耗时 **11.55 秒**（相比原先约 35 秒周期巡检大幅缩短，符合 10 秒左右的设计预期）。 | **通过** |

验收结束后，已清理 `SG-VM` 内的临时测试私钥、临时测试 CA 证书、计划任务及隔离运行目录进程（核实残留进程数为 `0`）。
