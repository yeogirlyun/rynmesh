# 桌面端自动更新（Codex 式）验收 — 2026-11-10

分支 `claude/desktop-auto-update-20261110`。设计稿（Claude Design）经用户确认：左上角「更新」按钮、弹出说明、安装遮罩、更新完成提示、设置 → 软件更新、一键发版。

## 自动测试

- 前端：`UpdatesPanel.test.tsx` 9 项；Shell / 设置页 / 翻译等相邻测试 11 个文件 211 项，全部通过（`vitest --maxWorkers=1`）。`tsc -p tsconfig.app.json` 通过。
- Rust：`cargo test` 全部通过（updates 8 项：官方地址校验、测试服务器同源、「立即更新」不关文档、偏好默认值和旧文件兼容、更新完成判断、旧安装包清理、配置为 quiet 安装且不允许 http）。
- Python：`tests/test_set_release_version.py` 3 项通过（7 个版本文件一次改齐，只改版本行，保留换行符，拒绝非 X.Y.Z）。

## 端到端（本 VM，真实安装，非沙箱）

方法：一次性测试签名密钥（不是发布密钥）；测试构建在编译时写入本机清单地址 `http://127.0.0.1:47811/desktop-latest.json`（`RYN_DESKTOP_UPDATE_TEST_ENDPOINT`，仅测试构建）。用 `build-windows.ps1 -Updater` 打出 0.8.90 / 0.8.91 / 0.8.92 三个签名 NSIS 包，真实安装到 `%LOCALAPPDATA%\Ryn`，隔离的数据/配置目录，CDP 驱动界面。安装前在 `plugin-services\e2e-marker\keep.txt` 写入标记。

| 场景 | 结果 |
|---|---|
| 0.8.90 启动，更新服务器不可用 | 第一次后台检查失败（failures=1），左上角不显示任何按钮 ✅ |
| 设置 → 软件更新 | 版本卡片、「连不上 GitHub，稍后会自动重试」、两个开关、更新说明 ✅（`02-settings-zh.png`）|
| 发布 0.8.91，点「检查更新」 | 下载 58 MB 并验签 → ready；左上角出现蓝色「更新」✅ |
| 点「更新」 | 弹出：版本、当前版本、更新说明（来自清单）、重启并更新 / 稍后、「退出时自动安装」提示 ✅（`04-popover.png`）|
| 点「重启并更新」 | 「正在更新 Ryn…」遮罩（`05-installing.png`）→ Ryn 退出 → 静默安装（无窗口、无 UAC）→ 约 20 秒后自动重新打开为 0.8.91，环境（数据目录、配置）保持 ✅ |
| 重新打开后 | 右下角「已更新到 Ryn 0.8.91 · 看看新内容」，6 秒后消失并确认 ✅（`06-updated.png`）|
| 发布 0.8.92，下载好后从托盘路径「退出 Ryn」 | 确认框多一行「退出时将安装 Ryn 0.8.92」；按确定 → Ryn 退出、静默安装、**不**重新打开；`Ryn.exe` 为 0.8.92 ✅ |
| 手动启动 0.8.92 | 「已更新到 Ryn 0.8.92」；updatedFrom=0.8.91；旧的 0.8.91/0.8.92 下载包已清理 ✅ |
| `plugin-services` | 两次更新后标记文件原样保留 ✅ |

测试中发现并已修复：弹出面板被页面内容盖住（侧栏是独立层）；安装后 58 MB 下载包残留；设置顶部标签栏缺「软件更新」；Ryn 隐藏在托盘时更新提示会在看不到时自动消失。

测试后已恢复 VM 原安装（Ryn 0.8.0，`Ryn.exe` SHA-256 `567FD533…21B3`，来自 `_accept\Ryn-fix-2fa075c-setup.exe`），删除测试标记，停止本地服务器。

## 未覆盖

- 「等任务完成」的真实忙碌场景（VM 无模型，未跑 Agent 任务）：只有单元测试。
- 连续 3 次失败后的红色按钮：只有单元测试。
- macOS 安装与重启；真实 GitHub Release + 正式发布密钥（本机无 GitHub 凭据）；`Publish new Ryn version` 工作流尚未在老板仓库运行过。
- 目前所有已装的 Ryn 都没有更新公钥，需要手动装一次由发布工作流产出的版本。

## v0.9.0 正式发布与首次安装

本节记录 2026-11-10 的实际交付状态；不能将草稿构建成功视为正式发布或自动升级端到端通过。

### 源码与交付修复

- v0.9.0 安装包构建源码：`007fb4040c16be39bcff735efade83f19916f395`，来自已提交并推送的 `develop`。本地已有对应的 annotated tag；GitHub 尚无 `v0.9.0` tag，禁止将其描述为已发布。
- [PR #126](https://github.com/yeogirlyun/rynmesh/pull/126)：Linux 打包补齐 llama runtime、实际 worker 崩溃恢复验收、Linux 日志遵守 XDG 路径。对应 [Ubuntu 实装验收](https://github.com/yeogirlyun/rynmesh/actions/runs/42833598001/job/129889789504) 通过。
- [PR #127](https://github.com/yeogirlyun/rynmesh/pull/127)：已有 tag 恢复时校验祖先关系并使用原 tag 源码，拒绝改写已发布 Release，允许恢复草稿附件；不移动或删除 tag。
- [PR #128](https://github.com/yeogirlyun/rynmesh/pull/128)：版本脚本同步网站当前版本展示，版本一致性验收不再硬编码旧版本。
- [PR #129](https://github.com/yeogirlyun/rynmesh/pull/129)：已有构建的独立验签上传入口支持指定版本，严格匹配草稿与构建提交，保留草稿说明，桌面校验清单与 Python 校验清单分开。集成提交 `c4e9347975035a06f28cc7a2eb517beadad2c61e`；仅修改上传恢复工具及测试，未改变上述安装包的运行时代码。
- 前三项组合后相关测试 62 项通过；0.9.0 版本同步后相关测试 16 项通过；上传恢复相关测试 12 项及 Ruff 通过。上传恢复测试包含临时 Ed25519 密钥签出的三平台模拟产物、错误草稿和已发布版本拒绝，以及保留其他附件。
- 历史全量 backend CI 仍有失败：PR #125 对应全量运行曾有 103 项失败。未将本轮相关测试通过冒充全量 CI 通过；本轮没有修复所有历史测试，也没有完成全部真实升级场景。

### 工作流与草稿

- [Publish new Ryn version](https://github.com/yeogirlyun/rynmesh/actions/runs/42836561482)：版本提交成功，创建 tag 被 GitHub `GH013: Cannot create ref due to creations being restricted` 拒绝。首次启动时早于本地修复推送完成，随后已正常合并远端版本提交并同步版本；没有强制推送。
- [Build signed desktop update](https://github.com/yeogirlyun/rynmesh/actions/runs/42837289408)：Windows x86_64、macOS Intel、macOS ARM 全部成功，使用 build-only 模式固定构建提交 `007fb4040c16be39bcff735efade83f19916f395`。
- [Release package](https://github.com/yeogirlyun/rynmesh/actions/runs/42837300432)：Python、Linux、macOS ARM 成功。Intel 首次运行签名、服务健康、原生运行时检查通过，但最终卸载测试 DMG 报资源忙；仅重跑失败 job 后成功，Python、Linux、macOS Intel、macOS ARM 最终全部通过。
- [三平台验签及恢复上传](https://github.com/yeogirlyun/rynmesh/actions/runs/42841058626)：成功；逐个验证正式更新公钥、签名 key ID、包内容签名和 trusted comment 签名，上传后核对 GitHub 返回的 SHA-256。
- [同一构建重复恢复上传](https://github.com/yeogirlyun/rynmesh/actions/runs/42841740783)：成功；8 个签名更新相关附件 ID 与 SHA-256 全部保持不变，没有删除或重建附件。
- [v0.9.0 草稿](https://github.com/yeogirlyun/rynmesh/releases/tag/untagged-732b8ca35564eca0d7d2)：Release ID `440487624`，target 为上述构建提交。尚未公开，尚未设为 Latest。
- 已核对 `desktop-latest.json` 的版本 `0.9.0`、完整三平台 URL/非空签名，以及与用户要求完全一致的说明：
  > Ryn 现在会自动更新：有新版本时左上角出现「更新」，点一下就重启到新版本；也可以等退出 Ryn 时自动安装。在 设置 → 软件更新 里可以查看版本和调整。
- URL 均指向 `https://github.com/yeogirlyun/rynmesh/releases/download/v0.9.0/`；Windows 包名为 `Ryn-0.9.0-windows-x86_64.exe`，SHA-256 为 `d966422242a5acbbeb9d5448deaf0f8139fede435caa39365d4521d9404a2b06`。
- 最终草稿包含三平台更新包及 `.sig`、desktop 清单、Python wheel/源码包及 `latest.json`、两个 macOS DMG、Linux deb 及校验文件，共 17 个附件；13 项必需附件全部存在、非空且有 SHA-256。
- 旧 Ryn 0.8.0 草稿（Release ID `430732126`）的元数据、更新时间、全部附件 ID/名称/大小与操作前快照一致；未修改。

### 本机安装前记录

- 当前版本：`0.8.0`。实际安装目录：`%USERPROFILE%\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Ryn`（本次会话看到的 `%LOCALAPPDATA%\Ryn` 对应此目录）。
- 当前 `Ryn.exe` SHA-256：`531A43D038B7707E2D0CACD9540C021C5C43CFF4D6E905CB62F5255096604B9F`。
- 已完整复制安装目录至 `D:\codex-temp\ryn-rollback-20261110-215645\Ryn`；复制 16,458 个文件、约 6.582 GB，没有失败或不匹配，备份 exe 的 SHA-256 与原文件一致。
- 已在本机保存 `plugin-services` 的 15,712 项文件路径、长度和 SHA-256 基线。原始日志、配置、文件清单和构建产物仅在本机，不提交公开仓库。
- 升级前节点健康接口为正常；真实 Agent 任务返回 `RYN_UPGRADE_PRECHECK_OK`，另一真实任务执行中观察到 readiness 的 `agent_running`，完成后返回 `RYN_BUSY_GUARD_OK`。
- UI 实查设置 → 软件更新显示 `0.8.0` 和未配置官方更新签名公钥提示，符合旧安装基线。

### 首次安装与自动升级验收状态

| 检查 | 本次结果 |
|---|---|
| 正式发布为 Latest、公共 latest 清单为 0.9.0 | 阻塞：tag 创建权限；尚未发布 |
| 公共 Windows 下载 URL 返回 HTTP 200 | 待正式发布后验证；草稿附件存在不等于公共下载成功 |
| 本机首次安装 0.9.0 / 安装后 exe 版本及 SHA-256 | 未执行；当前稳定安装保持 0.8.0，备份已准备 |
| 软件更新页显示 0.9.0、已是最新版本、无缺少公钥提示 | 未执行安装后检查 |
| 公司工作台、Dashboard、联盟营销及插件服务 | 未执行安装后检查；plugin-services 基线已保存，目录未改动 |
| Agent 打开及发消息 | 升级前真实任务通过；升级后未执行 |
| 真实任务、传输、插件调用、未保存文档时的升级行为 | 真实 Agent 忙碌检测通过；实际安装等待、传输、插件调用、未保存文档场景仍未完成 |
| 自动下载、验签、安装、重启全过程 | 正式包构建及离线验签通过；本机真实自动升级端到端未完成 |

阻塞原因已核实：`release-tag-protection`（规则 ID `21416456`）对 `refs/tags/v*` 启用创建、修改、删除及非快进限制，且没有配置获准发布账号。当前 git 凭据与 Actions 均无法创建 tag。需要仓库管理员配置合法的 tag 创建权限，保留已有 tag 不可改写/删除的保护，并将 `v0.9.0` 创建在构建提交 `007fb4040c16be39bcff735efade83f19916f395` 上。未改规则或 secrets，也未尝试绕过限制。

解除阻塞后，应先确认该 tag 精确指向上述提交、草稿附件完整且所有验证通过，若管理员创建 tag 触发新的打包运行，应等待其结束并重新核对全部附件，再公开 Release 并设为 Latest，核对公共清单和下载，然后使用已备份的本机执行首次安装及逐项验收，最后补全安装后版本/哈希和实际结果。不要直接重跑发版入口产生指向不同提交的同名 tag。
