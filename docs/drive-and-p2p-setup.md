# 网盘配置与 P2P AI 加速

## 网盘配置入口

更新前后端并重启 Ryn 节点后，打开左侧 **网盘 → 添加网盘**。
原有 NAS 配置会保留。支持 NAS、百度网盘、Google Drive 和 OneDrive。

P2P 连接复用需要通信双方使用相应版本。源码安装及桌面构建步骤见 [开发文档](RYN_INTEGRATED_WORKSPACE_DEVELOPMENT.md)。

### 百度网盘

1. 在 [百度网盘开放平台](https://pan.baidu.com/union/console/applist) 找到已有应用。
2. 在 Ryn 选择百度网盘，填入应用的 **App Key、Secret Key**，填写连接名称。
3. 选择是否允许上传、是否允许 AI 读取选中的文件，然后点击「保存并开始授权」。
4. 打开显示的百度官方链接，输入设备授权码。完成后，Ryn 会自动显示网盘。

应用必须具备所用接口的权限。可写目录、上传下载资格和速度由百度的应用权限、账号及网络决定；Ryn 不绕过平台限制。如果只能写入应用目录，请先进入该目录再上传。

### Google Drive

1. 在 [Google Cloud 控制台](https://console.cloud.google.com/apis/library/drive.googleapis.com) 新建项目，启用 **Google Drive API**。
2. 配置 Google Auth Platform 的品牌信息、受众。个人账号可选择 External，并在测试用户中加入自己的 Google 账号。
3. 创建 OAuth 客户端，应用类型选择 **Desktop app / 桌面应用**。
4. 将 **Client ID、Client Secret** 填入 Ryn 的 Google Drive 配置，选择权限并开始授权。
5. 在运行该 Ryn 节点的同一台电脑上，用系统浏览器完成 Google 登录。Ryn 自动接收本机回调，不需要手填回调端口。如果桌面端没有打开浏览器，可复制界面中的授权链接。

只读连接请求 `drive.readonly`；允许上传时请求 `drive`，因为要浏览用户已有文件。后续从只读切换为可写，需要重新授权。Testing 状态下，包含 Drive 权限的刷新令牌通常只有 7 天有效期，到期可在连接设置中重新授权。公开发布给其他用户时，应按 Google 的要求处理 OAuth 验证。

官方说明：[桌面应用授权](https://developers.google.com/identity/protocols/oauth2/native-app)、[授权配置](https://developers.google.com/workspace/guides/configure-oauth-consent)、[刷新令牌有效期](https://developers.google.com/identity/protocols/oauth2#expiration)。

## 当前可用范围

- 浏览目录、下载、上传、新建文件夹；每个文件最多 64 MiB。
- 文本和图片预览；Google 文档、表格、演示文稿可导出为文本或 CSV 后读取。
- AI 只读取用户明确选中的文件，默认关闭；上传权限默认关闭。
- 应用密钥和授权令牌沿用本机 NAS 配置的加密存储，不进入浏览器持久存储或节点公共目录。
- 重新授权会更新同一个连接；断开连接只删除本机配置，不删除网盘文件。
- 本版没有后台自动同步、跨网盘搬运、大文件断点续传或真实传输任务中心；设计预览中的进度、容量与文件都是样例。

## 网盘外部页面

桌面版的“打开开发者控制台”和“打开官方授权页”会调用系统默认浏览器。开始授权后，桌面版自动请求打开官方授权页；普通网页版本保留手动打开入口，以兼容浏览器的弹窗限制。打开失败会显示提示和可复制的网址，配置步骤仍留在 Ryn 内。Google 创建应用及填写配置需要用户在官方控制台完成。

## P2P AI 有哪些变化

双方运行新版后，第一次成功的 ICE 连接可被后续 AI 请求复用，减少反复交换地址和打洞。已有连接会优先于重新尝试失效的 HTTP 地址；使用前会检测连接是否存活，失效时重新建连。

首次连接仍可能慢：空闲时的信令轮询间隔最多 10 秒，之后还要交换地址和完成 ICE 检查；自动模式也可能先尝试直连地址。这一版没有消除所有首次调用等待，不能仅凭连接复用测试就认定真实公网调用已经足够快。

回复按小窗口连续发送，不再每发一段都等待一个完整网络往返。普通 AI 工作台和聊天流式回复均可使用；每个任务仍单独验证签名、设备权限、服务和序号。新会话协议中发送后发生故障不会自动重放该请求，避免重复生成。

资源限制：每节点最多 3 条出站缓存连接、8 条入站会话；每个目标最多 8 个进行中或排队请求。空闲 10 分钟回收，单个连接接收缓冲最多 8 MiB。保活使用现有 ICE consent 检测，没有添加高频注册中心轮询，也没有启动时向所有设备主动建连。

旧版节点自动退回原协议。配置了 `RYNMESH_P2P_BIND_PORT` 的固定单端口模式也使用原协议，避免空闲会话占用其他设备或文件传输所需的唯一端口。固定端口不必为了此功能强行取消，先按部署的防火墙和网络要求决定。

任务的 `transport_evidence` 会记录 `connection_reused`、`connection_ms`、`first_event_ms`、`total_ms`、`stream_events`。`first_event_ms` 是请求开始到首个回复事件，包含建连、排队和模型等待；它不是纯网络延迟。

## 验证边界与三机验收

自动测试覆盖模拟延迟及丢包、真实本机 ICE、跨请求事件循环复用、签名流式回复、撤销设备权限、OAuth 状态校验、令牌刷新、分页和加密存储。

多设备连接和网盘读写可以按以下流程检查：

1. A、B、C 都更新并重启节点；确认在同一私人空间且 A 允许共享 AI。
2. B 连续调用 A 两次，C 也连续调用 A 两次。第二次应显示 `connection_reused: true`（动态端口且双方新版）。
3. 比较首个事件耗时与总耗时，观察回复是否连续显示；A 本身模型生成时间不会因 P2P 复用而缩短。
4. A 重启或断网恢复后再次调用，确认能重新建连。撤销 B 的权限后，已有连接也不能继续调用。
5. 网盘授权后，用一个小文件验证浏览、下载及可写目录的上传；测试前不要用重要文件验证覆盖行为。


## 目录缓存与后台更新

网盘页面优先显示本机目录缓存，再在后台核对。30 秒内重新打开已完整加载的目录直接复用缓存；手动刷新始终请求最新目录。第一次浏览没有缓存的目录仍需连接网盘。百度和 Google 每批最多 200 项，首批先显示，随后继续加载，仍沿用每目录最多 5000 项的限制。

缓存只包含目录元数据，不保存文件正文、缩略图或账号令牌。后台使用现有节点密钥加密保存到 `plugins/nas/directories.enc`，与网盘连接按 source ID 隔离，最多 100 个目录、8 MiB 元数据，保留 7 天；断开连接及重新授权时清理该连接缓存。页面内存最多保存 100 个目录、20000 项。上次网盘、目录和路径显示名称随加密缓存保存，重启后恢复。

刷新失败时保留已缓存列表和更新时间；权限失效、目录不存在时不继续显示当前目录缓存。缓存中的项目可能已在云端发生变化，下载、预览和写入仍经过实时权限检查。上传、新建成功后更新当前列表，再重新核对目录。

各网盘使用独立操作锁，状态和缓存读取不等待远程 I/O。同一网盘复用连接，超过 60 秒未用的连接在下次访问时重建。重复列表请求合并；页面切换会取消不再使用的浏览器请求，但已发出的服务器/网盘请求可能执行到完成或超时。没有新增持续轮询或全盘扫描。


## OneDrive（2026-10-09）

在“网盘 → 添加网盘 → OneDrive”中配置。使用微软全球版 Microsoft Graph 和设备码授权，支持个人 Microsoft 账户及允许此登录方式的工作/学校账户。只连接登录者自己的 OneDrive，不枚举 SharePoint 站点或共享快捷方式。

1. 打开 [Microsoft Entra 应用注册](https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade)，新注册 `Ryn Desktop`，账户类型选择“任何组织目录中的账户和个人 Microsoft 账户”。如果当前账户没有应用注册权限，需要在有权限的 Entra 目录创建，或请目录管理员提供应用。
2. “身份验证 → 高级设置”启用“允许公共客户端流”。“API 权限”添加 Microsoft Graph **委托**权限 `Files.Read`；需要上传/新建文件夹时添加 `Files.ReadWrite`。授权请求自动包含 `offline_access`，用于后续刷新令牌。无需客户端密钥或重定向 URI。
3. 复制“概述”里的**应用程序（客户端）ID**到 Ryn；设置连接名称及是否允许上传、AI 读取明确选中的文件。AI 选项不会授权全盘扫描。
4. 点击“保存并开始授权”，在打开的微软官方登录页面输入 Ryn 显示的设备码，然后选择需要连接的 OneDrive 账号并授权。Ryn 在完成后自动添加连接。设备码请勿发送给其他人。

读权限升级为写权限时需要再次授权。组织管理员的条件访问策略可能禁止设备码登录或要求管理员同意；本版本不支持中国运营版及其他独立云的 Microsoft Graph 端点。连接失败不应通过反复创建新应用解决，先核对账户类型、Client ID、公共客户端设置和组织策略。

目录使用已有加密缓存和每批 200 项的分页；保留每目录最多 5000 项、单文件 64 MiB 的限制。下载/预览有大小限制，Graph 令牌不发往下载或上传地址。上传通过临时分块会话，冲突策略为 `fail`，不覆盖同名文件；暂不支持空文件上传，也不提供持久化断点续传。

官方依据：[设备码授权](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-device-code)、[桌面应用配置](https://learn.microsoft.com/en-us/entra/identity-platform/scenario-desktop-app-configuration)、[目录分页](https://learn.microsoft.com/en-us/graph/api/driveitem-list-children?view=graph-rest-1.0)、[分块上传](https://learn.microsoft.com/en-us/graph/api/driveitem-createuploadsession?view=graph-rest-1.0)。
