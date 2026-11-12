# Ryn 软件与插件更新

## 软件订阅

桌面端固定检查：

`https://github.com/yeogirlyun/rynmesh/releases/latest/download/desktop-latest.json`

使用 Tauri updater 的签名安装包。前端、原生桌面壳和 Python sidecar 一起更新，不调用 pip。原来的 `latest.json` 和非打包 Python 节点更新流程保持独立。

像 Codex 一样：启动后 30–59 秒第一次检查，之后每小时检查一次（失败 15 分钟后重试），发现新版本就在后台下载并核对签名，不打扰。

清单检查最多等待 20 秒，设置页可随时取消检查；取消会终止请求，不计为更新失败。安装包下载独立使用 600 秒超时。桌面日志记录清单请求开始、结果类别和耗时（不记录签名下载 URL），便于定位网络等待。

- **下好后**：左上角 Ryn 标志旁出现蓝色「更新」。点开看更新说明，再点「重启并更新」：Ryn 退出、静默安装（Windows 按当前用户安装，无 UAC、无安装窗口）、自动重新打开。重新打开后右下角提示一次「已更新到 Ryn x.y.z」。
- **正在忙**：Agent 任务、文件传输、插件调用或 AI 任务在跑时，按钮变成黄色「等任务完成」，空闲后自动安装；也可以点「立即更新」中断任务。打开的文档（编辑器窗口）永远不会被替用户关掉，只能等用户关闭。
- **不点也行**：默认在用户退出 Ryn 时安装已下载的更新（不重新打开）。
- **失败**：后台失败连续 3 次才在左上角显示红色「更新失败」（原因 + 重试）；用户点了更新后的失败立即显示。
- 设置 → 软件更新（`/settings/updates`）：版本、状态、「自动下载更新」「退出 Ryn 时自动安装」两个开关、更新说明。偏好保存在桌面配置目录 `updates/preferences.json`。

安装前停止本机 sidecar 和插件服务；安装启动失败时恢复它们。下载文件按版本缓存，安装时再次验证签名。Windows 更新只覆盖安装器自己的文件，`plugin-services`（公司插件）和用户数据不动。

这不是全应用崩溃回滚器：尚未实现跨安装器的独立恢复启动器。新版本安装成功但运行异常时，使用前一版签名安装包修复。数据迁移仍须保持向后兼容；本更新流程不覆盖用户数据。

端到端测试构建可在编译时设置 `RYN_DESKTOP_UPDATE_TEST_ENDPOINT`（本机清单地址，包必须同源，仍校验编译进去的公钥），配合测试专用密钥和 `dangerousInsecureTransportProtocol`。正式发布工作流从不设置它。

## 首次配置发布签名

1. 在安全的发布机器运行 `npm run tauri signer generate -- -w <仓库外的密钥路径>`，保存私钥和密码。不得提交私钥。
2. 老板仓库配置 Actions variable `RYN_DESKTOP_UPDATE_PUBLIC_KEY` 为生成的公钥文件内容，配置 secrets `TAURI_SIGNING_PRIVATE_KEY` 和 `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`。
3. `webapp/src-tauri/desktop-update.pub` 固定官方**公开验签公钥**（不是签名私钥）。所有桌面构建，包括普通本地 Cargo/Tauri 构建，均由 `build.rs` 默认嵌入它；空环境变量也不会移除公钥。环境变量 `RYN_DESKTOP_UPDATE_PUBLIC_KEY` 如有设置，必须是有效且与固定公钥一致的 minisign 公钥；不同的测试公钥仅允许配合显式 `RYN_DESKTOP_UPDATE_TEST_ENDPOINT`。轮换官方公钥必须审查源码变更并安排兼容升级。运行时不从网络或普通配置文件更换信任公钥。
4. Windows 本地签名构建：设置上述环境变量，运行 `webapp/src-tauri/scripts/build-windows.ps1 -Updater`。macOS 构建额外传 `--config src-tauri/tauri.updater.conf.json`。

签名更新密钥不同于 Windows Authenticode / macOS 开发者签名。后者仍按平台发布流程配置。

## 发布桌面版本（一个按钮）

在老板仓库 Actions 里运行 **Publish new Ryn version**，填版本号（例如 `0.9.0`，必须比已有 tag 新）和更新说明：

1. `scripts/set_release_version.py` 把版本号写进 Python、Tauri、Cargo、npm 所有文件，提交到 `develop` 并打 tag `v0.9.0`。
2. 用填写的更新说明创建草稿 Release，然后启动 **Release package**（Python 包、macOS/Linux 安装包）和 **Build signed desktop update**（Windows x86_64、macOS Intel、macOS Apple Silicon 的签名更新包 + `desktop-latest.json`，更新说明写进清单）。
3. 约 30–40 分钟后，在草稿里确认 `desktop-latest.json` 和安装包都在，点 **Publish release**（保持 Latest）。已安装的 Ryn 一小时内收到。

不会自动发布，不会覆盖已发布版本。也可以手动更新版本、推 tag，或手动运行 **Build signed desktop update** 并指定 tag。尚未发布 `desktop-latest.json` 时检查失败是正常状态；不能用原来的 Python 清单替代。

**第一次**：旧的本地构建可能没有更新公钥，必须手动安装一次带官方公钥的版本。现在普通本地打包也保留官方验签公钥，无须签名私钥便可接收官方自动更新；`-Updater` 和签名私钥仅用于生成可分发的签名更新包。安装交付前必须核对构建提交，并在设置 → 软件更新实际检查更新，不能仅凭相同的版本号判断安装正确。

## 公共 / 公司插件

当前支持现有可下载的 Craft 静态 Web 插件。内置 resume、recruit、desk、nas 仍随主程序更新。独立的原生自动化组件继续使用其已有的固定版本，不被 Web 插件更新包替换。Agent 的 Skills/MCP 扩展不走此安装器，远端 MCP 服务自身发布也不需要客户端下载代码。

在软件更新页选择已安装插件，配置：

- **公共 Git Release**：GitHub Release 的 JSON 资产地址；默认建议老板仓库的 `<plugin-id>-latest.json`。也可指定发布者自己的 GitHub 仓库。包必须来自同一仓库的固定 tag Release。
- **公司后台 URL**：公司 HTTPS 清单地址。包及重定向必须保持同源。可在服务进程环境里配置 `RYN_PLUGIN_TOKEN_<NAME>`，界面只保存环境变量名；Bearer Token 不写入更新状态，也不发往其他域名。
- **发布者公钥**：独立信任配置，不能从远程清单自我授权。由发布者提供 Ed25519 原始 32 字节公钥的 Base64。
- **空闲自动更新**：每个订阅单独开启；未配置订阅、不曾安装的插件不会自动下载。

启动后每分钟调度，距上次检查超过 6 小时才拉取清单。发现兼容版本后自动安装，已有调用租约会阻止切换；已经启动的编辑器服务保守地视为使用中（包含浏览器中可能仍有的未保存文档），关闭文档并重启 Ryn 后才能自动切换。不强制关闭编辑器。

清单采用 `rynmesh.crypto.SignedPayload`：`alg`、`public_key`、`signature` 和 `payload`。签名覆盖 canonical JSON，payload 示例：

```json
{
  "kind": "ryn.plugin.release",
  "schema": 1,
  "id": "wordcraft",
  "version": "0.8.0",
  "runtime": "static-web",
  "permissions": [],
  "min_host_version": "0.7.0",
  "max_host_version": "0.9.0",
  "url": "https://github.com/OWNER/REPO/releases/download/v0.8.0/wordcraft.zip",
  "size": 123456,
  "sha256": "64 lowercase hex characters"
}
```

包继续遵循 Craft 的 ZIP 目录结构和安全解包规则：只有一个可识别的 `index.html` 入口，保留许可证；文件大小、解包大小、文件数量均有限制。不支持预发布版本、额外权限或原生可执行代码更新；这些需要主程序升级并审核运行时支持。

生成已签名清单（从仓库根目录，在安装了项目依赖的 Python 环境运行）：

```sh
python -m scripts.sign_plugin_update --slug wordcraft --version 0.8.0 \
  --archive /release/wordcraft.zip \
  --url https://github.com/OWNER/REPO/releases/download/v0.8.0/wordcraft.zip \
  --manifest-url https://github.com/OWNER/REPO/releases/latest/download/wordcraft-latest.json \
  --key /secure/plugin-ed25519.key --min-host 0.7.0 --max-host 0.9.0 \
  --output /release/wordcraft-latest.json
```

公司地址添加 `--kind company` 并改为公司同源 URL。密钥文件是原始 32 字节 Ed25519 私钥，不是 Tauri minisign 私钥；公钥会随工具输出，但仍必须通过可信渠道配置。公司后台直接返回生成的完整 JSON，包返回 ZIP 字节即可。

## 安全切换与恢复

插件包先下载到临时目录，验证签名、宿主兼容范围、大小、SHA-256 及所有解包文件，之后才原子更新活动版本记录。保留旧版本与启用/禁用设置。更新失败保持旧版；下次启动发现新包文件损坏，尝试验证并恢复上一版。设置页可手动恢复，恢复后坏版本被隔离，不会无限重装。

Web 包的健康验证是入口与文件完整性验证，并不等价于所有 JavaScript 功能的端到端验证。发布前仍应运行编辑、保存和导出验收。

本地 API 均通过既有 `local_control` 认证：

- `GET /api/local/plugin-updates`
- `GET /api/local/plugin-updates/readiness`
- `POST /api/local/plugin-updates/{id}/configure`
- `POST /api/local/plugin-updates/{id}/check`
- `POST /api/local/plugin-updates/{id}/apply`
- `POST /api/local/plugin-updates/{id}/rollback`

配置存在节点数据目录 `plugin-updates.json`；活动/上一版记录存在 `craft-plugins.json`；代码位于原插件外置缓存目录。更新过程不删除插件用户数据。

## 本地插件服务套件自动更新（`ryn.service.release`）

由安装包提供的本地插件服务（如三合一公司插件共用的后台运行时）通过通用服务包更新器自动保持最新，不影响上面的桌面本体或静态 Craft 插件流程。

### 本机信任与源配置

安装包在安装目录写入 `plugin-services/updates.json`（Ryn 主程序与前端代码中不硬编码业务名称、域名或公钥）：

- `name`：中英文显示名称（如 `{"zh-CN": "公司插件", "en": "Company plugins"}`）。
- `plugins`：共用该本地服务的插件 ID 列表。
- `url`：HTTPS 清单地址（如 `https://<origin>/ryn/updates/packs/company/latest.json`）。
- `public_key`：发布者 32 字节 Ed25519 公钥（Base64），**仅从本机 `updates.json` 读取**，绝不采信远程清单自报公钥。
- `publisher`：用于更新页来源说明的发布方名称。
- `auth`：`peer_provider` 与账号标签；仅当设备已登录且令牌签发域与 `url` 完全同源时，才会携带 `Bearer` 令牌请求清单和文件块，任何跨域下载或重定向立即拒绝。

未安装 `plugin-services/updates.json` 的 Ryn 在「设置 → 软件更新」页面整段隐藏「已安装的插件」。

### 检查、增量下载与切换规则

- **检查周期**：跟随桌面「自动下载更新」开关；每小时检查一次，失败后 15 分钟重试。
- **增量下载**：清单为 `SignedPayload`（`kind: "ryn.service.release"`, `schema: 1`），列出运行时文件、插件声明（`ryn-declarations.json`）及外置技能包（`ryn-skills/`）的大小与 SHA-256。本机活动版本或已暂存目录中哈希相同的文件直接硬链接/复制复用，只下载哈希变化的 `blobs/<sha256>`（支持 HTTP `Range` 断点续传），逐文件校验字节数与 SHA-256。
- **版本与宿主保护**：
  - 清单 `min_host` 高于当前 Ryn 版本时**不下载任何文件**，卡片显示「需要先更新 Ryn」。
  - 已装版本不低于清单版本时永不降级（包括用旧版整包重装后，自动保留已更新的新版运行时）。
- **无感暂存与切换时机**：
  - 新版下载到独立的版本目录，绝不改动正在运行的目录。
  - 切换时机：Ryn 启动时、退出时（跟随「退出 Ryn 时自动安装」开关）、用户点击「立即更新」时。
  - 若插件或 Agent 有任务正在运行，后台自动切换延后，卡片显示「等任务结束」；用户仍可主动点击「立即更新」立即切换。
  - 点击「立即更新」时，桌面壳切换 `services.json`、同步更新插件声明与外置技能，并立即调用 `restart_plugin_service` 重启该插件服务；打开中的插件页显示「正在更新」并在新进程就绪后自动恢复，更新完成后右下角提示一次。
- **状态保留**：切换前后保留登录态、插件本地用户数据、插件启用状态及安装包授予的只读工具信任（不会重复弹出只读工具确认提示）。
- **健康检查与自动回退**：切换后轮询本地服务健康检查并核对返回的 `revision`；若 180 秒内未就绪或修订号不匹配，自动将运行时、插件声明和外置技能一并回退到上一正常版本，并将该失败 `revision` 永久隔离，后续检查不再重试，卡片显示「已退回旧版」。

本地控制 API（均受 `local_control` 保护）：

- `GET /api/local/service-updates`
- `POST /api/local/service-updates/check`
- `POST /api/local/service-updates/{id}/check`
- `POST /api/local/service-updates/{id}/apply`
- `POST /api/local/service-updates/{id}/verify`
- `POST /api/local/service-updates/{id}/retry`
- `POST /api/local/service-updates/{id}/acknowledge`
- `POST /api/local/service-updates/quit`

### 服务端接口与构建发布

后端（`gshopper-ryn-node`）仅暴露两个认证接口，由环境变量 `RYN_UPDATES_DIR` 指定包根目录，并复用 `RynRateLimitService` 限流：

- `GET /ryn/updates/packs/:id/latest.json`（`Cache-Control: private, no-cache`）
- `GET /ryn/updates/packs/:id/blobs/:sha256`（`Cache-Control: private, max-age=31536000, immutable`，支持 `Range` `206`/`416`）

请求必须携带有效的 `peer.connect` 登录令牌；未登录或令牌无效一律先返回 `401`，不泄露包是否存在或任何版本信息。

发布私钥在发布机器上生成并保存在所有代码仓库之外（仓库仅存对应的 `.pub` 公钥）：

```sh
# 1. 首次在仓库外安全目录生成发布私钥（输出公钥写入安装包配置）
python scripts/publish_service_pack.py --generate-key ~/.rynmesh-release-keys/publisher.key

# 2. 在插件仓库一条命令构建服务、打包声明与技能并生成签名清单及去重文件块（不自动上传）
python integrations/company-app/scripts/release_service_pack.py \
  --ryn-repo /path/to/rynmesh \
  --key ~/.rynmesh-release-keys/publisher.key \
  --previous /path/to/last/latest.json \
  --notes "更新说明" \
  --output /path/to/ready-to-upload/company
```

版本号采用单调递增的 UTC `YYYY.M.D.HHMMSS` 规则（若与 `--previous` 相同或更早则自动递增尾号）。输出目录经人工确认后，先同步 `blobs/` 再原子替换服务器 `$RYN_UPDATES_DIR/packs/company/latest.json`。
