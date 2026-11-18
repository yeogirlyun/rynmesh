# 节点网页与做图：两台真实 Windows 电脑验收（2026-11-16）

## 范围与身份

A 为老板电脑，发布人 ziling；B 为 LAN Windows 82（真实独立 Windows 电脑）。保留现有账号，两台公司插件均登录 ziling。因此覆盖同账号跨设备，**不代表已验证两个不同成员之间的历史隔离**。未修改中心生产发布人名单，未推送 Git。

A 安装位置 `C:/Users/子陵/AppData/Local/Ryn`；B 安装位置 `C:/Users/ziling/AppData/Local/Ryn`。A 更新前等用户保存并关闭未保存的 Excel，收到“已保存”后才重启。只停止已核实路径的本项目进程；保留账号、节点身份、配置与历史。

## 构建与备份

- 独立构建目录：`D:/code/_build-tools/ryn-package-20261113`；公司运行包源为独立目录 `D:/code/_build-tools/company-package-20261113`，正式源仓库 `D:/code/gshopper-ryn-node`。
- 公司 develop：`5a38b8328c3d7250e6051653abe70857dc93b4e0`，本次无公司代码修改。
- 首轮主程序 develop：`a8ec8d814f0d611621b8c5c44b711a16a436679a`；核心 `0e90f8c7c014356a3d49307473ce5973beff8030`。安装包 SHA-256 `1FAC4A886476A74F75765748E6A02E2BD9A0B14FF7853C124085D521F0E691B9`，140401011 字节，已安装 B。
- 安装修复包 develop：`0b7445f282f7bb5c61b4d2cb46870cc29a599327`；核心同上。SHA-256 `F78F03844DDB3DAF6EA1A987AA25B50C0393A4FD2A103EF7FC5DCD4018F74684`，178909446 字节，已安装 A。
- 第一轮离线修复的主程序与核心均为 develop `815230e2daea89c9bcf72e02f7f9e8ccf6e87b5d`。完整包 `releases/node-sites-two-device-20261116/Ryn-0.9.1-company-815230e2-setup.exe`，178912053 字节，SHA-256 `61E3A2F0FFF154AF6D9FC856FDB8D0C3C7DE15256C3100C325C1EFF3BD739152`。新装与重复装隔离验收通过；A/B 静默安装退出码均为 0。
- A/B 安装后的节点程序 SHA-256 均为 `D19CA10994801BFCF0958F0B783AB7EE8A3D8F86AA8D75386A9556921ABBB38B`。A 主程序与构建输出仅差 Tauri 打包标记的 3 字节（UNK→NSS），已逐字节核对。两台公司绑定保留，A 发布权限和 Codex ready 均为 true。
- A 原运行路径另在 Codex LocalCache 中；更新前备份活动程序 919 个文件，规范安装目录备份 986 个文件，逐文件核对哈希。B 备份 2980 个非日志文件，逐文件核对哈希。
- 备份、安装器、原始日志和截图仅保留本地 `artifacts/node-sites-two-device-20261116/` 与 B 的 `Ryn-install-backups/node-sites-two-device-20261116/`，不提交凭据或日志原文。

## 已实测结果

| 项目 | 观测与耗时 | 本地截图 |
|---|---|---|
| 前提 | A/B `bound=true`，A `can_publish_models=true`、`model.ready=true` | A-owner-open.png |
| 开放与打开 | A 全部同事开放，B 列表显示 A 在线；打开后 Ryn 顶栏显示来源电脑 | B-host-listed.png、B-open-trusted-frame.png |
| 1:1 纯文字 | 红色马克杯，成功 30.5 秒；1024×1024 | B-first-result.png |
| 详情及下载 | 大图可见；打开后 0.359 秒尚未显示，6.079 秒前已显示（观测区间）；PNG 实际保存于 B `C:/Users/ziling/Downloads/ryn-d44aee20.png`，867674 字节 | B-first-detail.png |
| 3:4 参考海报 | 从 B 已生成图片复制并粘贴参考图；文字“冬季新品 暖心上市”目视逐字一致；48.5 秒，1024×1365 | B-pasted-reference-batch2.png、B-poster-success-cancel.png |
| 批量与排队取消 | 一次提交 2 张，第二张排队第 1 位、约 1 分钟；B 取消排队项成功 | B-queue-position.png |
| 再来一张 | 详情按钮生成另一张主图，42.4 秒成功 | B-first-detail.png |
| 改这张 | 详情填写修改后提交；34.8 秒，1024×1024，红杯保留、浅蓝背景和柠檬符合要求 | B-edit-success.png |
| 未登录失败 | A 队列清空后暂时移走登录文件，ready=false/codex_signed_out；B 提交即失败并显示“这台电脑的 Codex 没有登录。”；finally 恢复文件，ready=true。未执行服务器端撤销会话或实际重新登录流程 | B-codex-signed-out.png |
| A 队列管理 | 显示 B 的公司用户名；两项排队时预计 40/79 秒；点击提前后顺序反转、优先级变为 1；两项取消成功 | A-owner-caller-queue.png |
| A 停止运行 | 专门提交一张任务，A 在运行中点击停止，25.3 秒后记录为 cancelled；另一次停止点击晚于完成，未计作通过 | A-stopped-early.png |
| A 用量 | 首轮 3 张成功，管理页显示调用者、今日 3 张与近 30 日 3 张 | A-owner-caller-queue.png |
| 指定人员与关闭 | A 本地接口切换 members 空名单，B 实际请求 403；关闭开关同样 403；页面显示访问撤销。恢复全部同事与开关后 200 并恢复作品 | B-members-denied.png |
| 线路 | B opened 状态 `route=lan`；未人为制造 P2P 线路 | B-open-trusted-frame.png |
| 首轮离线 | A 退出后 B 约 89.4 秒才出现离线，不达标；A 重开后 B 自动恢复，恢复前曾出现积压探测 | B-offline-before-fix.png、B-auto-recovery-before-fix.png |
| 新版重启历史 | 两台重装重启后，B 9 条任务记录全部保留，图片与失败记录仍可见；A 用量 4 张、失败 1、队列 0 | B-history-after-reinstall.png、A-final-owner-ready.png |
| 新版离线 | A 停止时间 18:54:20.625；18:54:33.019 截图已显示离线，即不超过 12.394 秒，符合约 15 秒要求 | B-offline-after-fix-12s.png |

首张图缩略图在提交后 51.124 秒时已见，生成耗时 30.5 秒；因为期间切换了窗口，这只能给出完成后约 20.6 秒的观测上界，不能当作精确传输延迟。

B 下载文件独立读取 PNG 头确认为 1024×1024；SHA-256 为 `29ED4AF3CA332C415130787108AEB53EA21E915DA256A24B74D75CF7034A9C43`。截至此处共 9 个任务，4 个成功、4 个取消、1 个未登录失败；取消中只有 1 个已开始生成。

另通过 B 的页面接口下载海报到 `C:/Users/ziling/Downloads/ryn-85d1ad6a-poster.png`，独立读取 PNG 头为 1024×1365，1261871 字节，SHA-256 `5294B9F261C0B0F7831B27C859DF535AC9636B42DA970FF85C12B838F655E23A`。

## 问题与修复

1. 同公司源码提交的运行包可能包含不同构建产物。原安装器复用仅按提交命名的旧目录，哈希验证失败。修复 `652763f6`：只有内容全部一致才复用；否则安装到新唯一目录并保留旧版回滚。合并 `0b7445f2`。安装器回归 38 项通过；完整包隔离验收覆盖新装、重复装、身份与禁用设置保留、公司三页面与精确 revision、已有 Office 不受影响。A 真实更新成功。
2. 离线探测等待长时间 NAT/P2P 建连，且页面轮询可能重叠。修复 `c642901e`、测试调整 `517578eb`，合并 `815230e2daea89c9bcf72e02f7f9e8ccf6e87b5d`：状态探测等待最多 4 秒、每个 peer 复用单个后台任务、最多 8 个后台任务；页面每 10 秒轮询并避免重叠。身份校验仍由原探测执行。后端 11 项通过，前端 4 项通过。新版两机离线时限复测通过。
3. `815230e2` 的离线时限虽已通过，但 A 重开后 B 页面长时间没有恢复；与此同时从 B 独立请求同一状态接口连续 8 次在线、耗时 0.081–0.302 秒。手动重连也未立即恢复，之后才恢复，因此该轮不能计作自动恢复通过。补充修复 `e2b8f283`，合并 `4452e0fa20e9a0c05d39cf9f6e044ec850ba888d`：浏览器状态请求整体设 6 秒截止时间并中止请求，覆盖响应头和响应体，防止请求阻塞轮询锁。新增两种阻塞与恢复回归测试，前端共 6 项通过。截图 `B-recovery-stalled.png`。

B 首次运行安装器时可用提交内存约 30 MB，出现 NSIS 完整性提示；安装包哈希和 CRC 都正确。停止已确认属于 Ryn 的进程后释放到约 13 GB，同一安装包正常完成。未绕过安装完整性检查。

## 待完成记录

最后安装的主程序与核心为 develop `4452e0fa20e9a0c05d39cf9f6e044ec850ba888d`，完整安装包 `releases/node-sites-two-device-20261116/Ryn-0.9.1-company-4452e0fa-setup.exe`，178913168 字节，SHA-256 `CF6949A9A69BAF5579D2F11FC229BD4504B2170D1B2E2E67C397443B372BF42B`。隔离新装与重复安装检查通过，A/B 安装退出码均为 0；两台节点程序 SHA-256 均为 `627F5965DE535687485A46E8D79007F3D3F0D5F8C46FE1541F8B5101C3776E10`。

19:29 后接口复核曾确认 A/B 公司绑定保留，A 发布权限、Codex ready、开放开关均 true，范围 all，今日成功 4、队列 0。但随后 A 节点出现健康接口超时和间歇重启，B 的目录一度为空，已有页面请求出现 site_unknown_peer/离线。此轮稳定性仍在排查，不能以此前瞬时健康替代最终验收。

界面自动化原连接报 Transport closed，重置仍失败；备用连接报 failed to write kernel assets（系统找不到指定的路径）。因此 `4452e0fa` 的界面自动恢复、最终缩略图与大图显示耗时尚未复测。最后已通过 B 的页面接口提交第 10 个任务并成功，最终共 5 个成功、4 个取消、1 个失败。已有第 1 轮离线 12.394 秒结果仅属于 `815230e2`。

待完成：最终包长时间稳定性、B 界面自动恢复、最终缩略图/大图的实际绘制时延与最终历史/用量截图。不同公司成员隔离、P2P 线路、真实 OAuth 退出并重新登录均未验证。权限切换实际使用 A 的本地接口完成，不冒称已覆盖对应开关的点击交互。本文件是过程验收记录，**不代表全部通过**。

## 最终接口复核补充（19:43–19:46）

A 受控重启后，健康接口恢复；公司工作台 bound=true、can_publish_models=true，目录刷新成功。B 重新通过原页面接口读回此前全部 9 条历史。第 10 条任务通过 B 的原页面接口提交，45.8 秒成功；完成后约 1.052 秒内被轮询发现，缩略图 HTTP 传输 0.093709 秒、大图 0.427291 秒。**这些是接口传输值，不能替代界面实际显示时间。**

B 实际文件 `C:/Users/ziling/Downloads/ryn-final-image.png`，974653 字节，独立读取 PNG 头确认为 1024×1024，SHA-256 `6D6F8A66BBA6A55AA97C17DE340F0314FBADDAE47AA7C9A8D6C702D38C3C270C`。A 此时 ready=true、enabled=true、today=5、queue_length=0。

额外以 2 秒间隔调用 B 的 fresh 状态接口（此频率不同于 UI 的 10 秒轮询）：开始约 54 秒内出现离线/4 秒探测超时，随后连续在线、route=lan，多次响应约 80–305 毫秒。该观测没有主动退出 A，不能冒充退出/重开的自动恢复验收，也表明最终稳定性仍需持续验证。期间 A 主节点进程保持同一 PID，采样整机 CPU 曾达 100%；线程栈显示多个存储锁等待、目录/模型刷新，但尚未证明唯一根因。原始线程栈仅保留本地，不提交。