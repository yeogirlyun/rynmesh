# 同事安装包：Ryn 本体与三个公司插件（2026-11-12）

已生成 Windows x64 单文件安装包，用户接受保留读取无限重试已知问题后要求打包。该问题仍未修复，不能将本包描述为第 5 条验收通过。

- 文件：`D:/code/rynmesh/releases/colleagues-20261112/Ryn-0.9.0-company-20261112-setup.exe`。
- 大小：182,203,215 字节（约 182 MB），zlib 压缩。
- SHA-256：`762d0a427d955d0bc1eb9376e89ec288b85718a3a13b193bd2db5528a4cfdb97`。
- 本体：0.9.0，develop 已提交源码 `185fa22a98ecd395a97db9a85d4e8c25a464b407`；复用本聊天刚完成构建、安装及健康检查的同批主程序安装包。
- 安装辅助程序及包装源码：develop `5d61cb9ea45ed814a30bb20bb7eed8c5143d52cb`，与本体的产品源码无差异。独立干净克隆 `D:/code/_build-tools/ryn-distribution-20261112`，未混入原工作区未提交文档。
- 公司插件：老板仓库 `https://github.com/yeogirlyun/gshopper-ryn-node` 的 develop `410720e31c47400b7fa74877d02ebb609583cd2c`。声明和技能来自干净的 `D:/code/gshopper-wecom-review`；903 个运行文件按已验证 runtime-manifest 白名单复制并逐项核对哈希，未复制运行配置或用户数据。
- 精确范围：company-workspace、company-dashboard、company-affiliate，加 Ryn 主程序。没有打入 Office 编辑器。
- 已包含微软签名有效的 WebView2 引导安装程序；没有 WebView2 的电脑需要联网安装该组件。

## 验收

冻结安装辅助程序成功；隔离首次安装成功；三个公司插件页面验证通过，company health 为 ok=true/status=ready；重复安装保留身份密钥和插件禁用设置；已有 Office 注册及合成文档未改变。测试阻断公司服务出站网络，未操作真实业务。NSIS 合并安装包构建退出码 0，最终文件哈希和回执一致。

这是安装辅助程序及打包服务的隔离验收，未执行最终系统安装器，不代表已在同事电脑完成安装。没有再次替换本机正在运行的 Ryn。

分发目录另附 `安装说明.txt`、`SHA256SUMS.txt` 和 `.exe.json` 构建回执。已知重试问题和首次登录说明写入安装说明。安装包、原始日志与运行资料不提交；本记录提交 develop。用户自行分发，未代发消息或上传公开发布。
