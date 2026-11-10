# Ryn 公司三个插件安装交付（2026-11-10）

- 用户范围：Ryn 本体 + 公司工作台、Dashboard、联盟营销；不包含 Excel/Word/PPT/PDF 等 Office 插件载荷。
- 安装包：`Ryn_0.8.0_74bda6f_company-f2a07a1_company-only-setup.exe`，144941671 字节；SHA-256 `7758872A0031EF2B31E2FDB8E4C7735A056BCB28E8B3BA8764C8BC79197E91FB`。
- 打包源码 develop `74bda6f953a236eb3c76568ae8e8e6cd3fa499e9`；本体二进制来自已验收 develop `22783aa24406ff3bf19f63d70fe528146a40d96b`，确认相关源码目录无差异、原始安装器哈希一致；公司插件 develop `f2a07a1f2b99cd19d503c4a9a90426a67fa66226`。两仓正式远端均已核对。
- 安装器补齐外部技能资源：9 个文件，模板拆分后逐字节还原核对通过；首次安装绑定资源，重复安装保留已有导入包和自定义技能。
- 验证：安装相关 pytest 36 通过；公司服务断网冷启动/重启 3 次通过；冻结安装器隔离首次安装/重复安装通过，身份密钥及关闭状态保留，三个页面正常，无 Office 载荷。
- 本机实际执行完整 NSIS 安装器退出 0；程序二进制哈希、公司运行时全部文件哈希、身份密钥和数据路径验证通过，已启动 Ryn。
- 重启后公司 health ready；Dashboard 60 个 installer 只读工具、候选 0；workspace 7 个 installer 只读工具、候选 0，无写工具误标。
- 原有插件开关、服务器实例和 Dashboard 外部 PPT 技能目录/manifest 完全保留；联盟营销保持安装前 NeedsAuth，没有声称其已登录。
- 安装位置：`C:\Users\子陵\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\Ryn`。
- 回滚备份：`D:\code\rynmesh\artifacts\company-only-release-20261110\rollback-20261110-132913`；私有配置及原始证据仅本地留存，不入库。
- 本地构建及安装回执：`artifacts/company-only-release-20261110/`；安装包及 SHA 回执位于 `releases/`，未提交二进制。
