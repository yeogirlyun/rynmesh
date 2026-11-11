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

### 2026-11-11 续验：正式发布与本机首次安装完成

以下为权限开放后的最终状态；上一节的“阻塞 / 未安装”描述保留为 2026-11-10 历史记录。

#### 发布版本与构建证据

- 仓库管理员开放 tag 创建权限后，使用获准账号正常推送 `v0.9.0` 到老板仓库 `yeogirlyun/rynmesh`。annotated tag 对象为 `affc18385a63d4c072d4a784b153d24fff815d81`，解引用后的构建提交为 `007fb4040c16be39bcff735efade83f19916f395`。已通过 `git ls-remote` 核对；未改写或删除任何已有 tag。
- tag 推送自动触发 [Build signed desktop update](https://github.com/yeogirlyun/rynmesh/actions/runs/42967306325)：Windows x86_64、macOS ARM、macOS Intel 及草稿上传全部成功。
- [Release package](https://github.com/yeogirlyun/rynmesh/actions/runs/42967306335)：Python 包、Linux deb 及实装 smoke、两个 macOS DMG 的签名和内置服务验证全部成功。
- [独立验签及附件一致性复核](https://github.com/yeogirlyun/rynmesh/actions/runs/42969111683)：三平台包均通过正式公钥验签。首次复核发现手工生成的附加校验清单使用 Windows 换行，统一为 LF 后重跑成功；没有更改安装包或签名密钥。
- 最终检查 20 个 Release 附件，包含全部 13 项必需附件：桌面清单、Windows exe/.sig、两种 macOS app.tar.gz/.sig、两种 DMG、Linux deb、Python wheel/源码包、Python latest 清单。桌面清单版本、三平台固定 tag URL、签名及用户指定说明均符合要求。
- **正式 Release：[Ryn v0.9.0](https://github.com/yeogirlyun/rynmesh/releases/tag/v0.9.0)**。公开发布时间为 **2026-11-11 14:04:57（Asia/Hong_Kong）**，已设为 Latest，非 draft、非 prerelease。
- 匿名访问 `https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json` 返回版本 `0.9.0`，内容与验收清单一致。Windows URL 返回 HTTP 200；从正式 Release 完整下载后再次校验签名、文件长度及 GitHub 提供的 SHA-256。
- 本轮正式 Windows 安装包 SHA-256：`fd534bf5acedda5ef52d82a8636df492affc1f274fa8e1ec00920e0f5b808e0b`。它来自本轮 tag 构建，取代历史草稿构建的安装介质哈希。
- 旧 0.8.0 草稿（ID `430732126`）的元数据、更新时间、全部附件 ID/名称/大小与原快照一致；未改动。未修改仓库规则或 secrets。

#### 本机首次安装及回滚备份

- 安装前版本 `0.8.0`；`Ryn.exe` SHA-256：`531A43D038B7707E2D0CACD9540C021C5C43CFF4D6E905CB62F5255096604B9F`。
- 重新按 11 月 11 日的现状完整备份至 **`D:\codex-temp\ryn-rollback-20261111-1345\Ryn`**：19,172 个文件，约 7.188 GB；复制无失败、无不匹配，备份 exe 的哈希与原文件一致。之前 11 月 10 日的备份也保留。
- 安装位置保持为 **`%USERPROFILE%\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Ryn`**，使用实际进程路径核对，未误装到另一份用户目录。
- 安装前 readiness 为 `ready=true`。通过 Ryn 设置 → 桌面端 → 退出 Ryn 执行与托盘退出相同的正常退出流程，确认主程序及安装目录内所有服务进程自行结束，没有强制结束进程。
- 使用正式 Release 安装包 `/S` 静默安装，并用 `/D=` 明确保持原安装位置；安装器退出码为 `0`。
- 安装后版本 **`0.9.0`**；`Ryn.exe` SHA-256：**`718BB64C017D5C5472E61B7427D2C76D8D3D05F877EC078CEDB820109E01C820`**。完成时间为 2026-11-11 14:15:41（Asia/Hong_Kong）。
- 在旧程序退出后、安装前保存 `plugin-services` 基线，并在安装后、启动前逐文件复核：**18,426 个文件全部保留，长度与 SHA-256 全部相同，缺失/变化为 0**。没有手动修改用户数据、配置或插件服务文件；原始基线和日志仅保留在本机。

#### 安装后逐项验收

| 检查 | 结果 |
|---|---|
| 新版程序启动、节点健康 | 通过；节点 `/health` 返回 `status=ok`、`desktop_managed=true` |
| 设置 → 软件更新 | 通过；显示 `Ryn 0.9.0`、`已是最新版本`；主动点击检查更新后完成并更新时间 |
| 内置更新公钥 | 通过；缺少公钥提示消失；自动下载和退出时自动安装均为开启状态 |
| 公司工作台 | 通过；页面正常加载，原公司账号登录状态和已启用插件保留 |
| Dashboard | 通过打开及一次只读查询；首次访问出现 10 秒业务请求超时提示，清除旧提示后重新点击查询，观察到加载态后数据正常返回，未再出现错误。未做业务写入或全量报表验收 |
| 联盟营销 | 通过；管理端、已认证账号和订单概览正常显示 |
| 插件服务与安装目录 | 通过；服务从原 `plugin-services` 路径启动，安装前后的 18,426 个文件完全一致 |
| Agent 打开、发送及回复 | 通过；从新版界面新建并发送合成验收消息，任务完成，实际回复 `RYN_090_INSTALL_OK`，无任务错误 |
| 历史记录与配置 | 既有任务、项目、模型选项及公司登录仍可见；没有执行清空、重置或迁移用户配置的操作 |

本次完成的是正式签名发布、公开下载、本机首次安装更新公钥及真实更新检查。**没有把“已是最新版本”当作跨版本自动安装已经实测**：当前没有高于 0.9.0 的正式版本，因此本机 0.9.0 → 下一版的自动下载、等待任务结束、静默安装及重启需要随下一次获准发布继续验证。真实传输、插件调用及未保存文档同时存在时的升级验收仍未完成；此前 VM 的测试签名升级证据继续有效，但不代替正式主机跨版本证据。历史全量 backend CI 的失败也未在本轮全部修复。

### 2026-11-12：防止后续本地构建丢失官方更新公钥

- 本机 11 月 11 日 17:35 的后续本地交付覆盖了前述正式安装：仍显示 `0.9.0`，构建记录为 `3c48e5e42ad86c6d902e57bd77f9fb39cf41de9f`，`Ryn.exe` SHA-256 为 `E78B9B265423392E36CA59C904C5CDEE3E7EDB90EC4904CFB96D61C6666F3772`。该程序没有嵌入官方公钥，软件更新页再次显示未配置公钥。这是打包输入遗漏，不是功能未合入 develop。
- 修复提交 `e34a594`，合入并推送老板仓库 develop 的构建提交为 `60fbe07ae08c447fe6166ab6d4287389108d071f`。固定的是公开验签公钥；没有读取、提交或修改签名私钥和 Secrets，没有改动任何 tag/Release。
- `build.rs` 对所有桌面构建默认嵌入仓库固定公钥，包括直接 Cargo/Tauri 构建；空环境变量不会取消信任。校验 minisign 格式及 Ed25519 公钥包，普通构建拒绝不同公钥，显式测试 endpoint 才允许测试公钥。Windows 打包脚本区分普通接收更新与生成签名更新包。
- 使用独立 Cargo 测试入口运行仓库的 `tests/updater_key.rs`：3 项测试通过，覆盖缺省/空值、公钥格式错误及测试公钥隔离。`git diff --check` 通过。
- 新安装前备份：`D:\codex-temp\ryn-rollback-20261112-updater\Ryn`，20,076 个文件，约 7.391 GB，复制无失败、无不匹配；备份 exe 与当前 exe 的 SHA-256 一致。安装路径仍为上节记录的用户目录，未清理用户数据、配置或插件。
- 本次重新检查公开 Latest 桌面清单，仍为 `0.9.0`，三个平台齐全。正式 Release 的安装包没有这次本地打包遗漏问题，因此本轮不重新发布同名版本。
- 从上述 develop 提交的独立工作树完成前端、原生组件、Python 节点、桌面壳和 NSIS 全量构建。第一次复用链接缓存被 `packaging_venv_shadowed` 正确拦截，换独立 venv 后通过，未绕过校验。Cargo.toml 只有工具换行规范化提示，`git diff --exit-code` 确认无源码差异。
- 安装介质：`D:\codex-temp\ryn-updater-delivery-60fbe07a\Ryn_0.9.0_x64-setup.exe`；SHA-256：`256913EE5BB82A2AB8D165A684472BC1FC2334DCA55B5DBD347F0F0B690599F5`。这是本地交付包，不是重新发布的 v0.9.0 Release 附件。
- 新构建的 `Ryn.exe` SHA-256：`7CFECE7AFA605C97CD7D7C2141D15933F3C8420D4028953AC56084DC57E44001`；二进制检查确认含固定官方公钥。按安装后的文件名放置节点和原生组件，`--ryn-computer-use-package-check` 返回 `ok=true`、`verified=true`、`releaseVerified=true`，正常关闭 worker、lease 和 client。
- **截至本节记录时尚未替换本机程序**：Windows 界面工具刷新窗口后仍报 `failed to activate captured window`，无法执行正常退出；当前 Ryn 仍在运行。已请用户从托盘正常退出，未强制终止程序或调用安装器。新版本更新页、三个公司插件、Agent 发消息及安装后插件目录一致性尚待安装后验收，不能将上述构建检查视作本机安装或跨版本自动升级已完成。
