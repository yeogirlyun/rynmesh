# R7 企业微信发送修复验收（2026-11-03）

82 的 R6 公司插件在 Windows 10 19045 / 企业微信 5.0.13.6005 上复现发送准备失败。Windows 记录 Frida 17.25.1 的 frida-helper-x86.exe 崩溃（0xc0000409）；不加载发送逻辑的独立冻结程序也在 attach 阶段复现 TransportError: the connection is closed。

同机同进程改用 17.24.3 后，两次连接、脚本加载、RPC、卸载通过。本机同样通过。固定兼容版本后，40 项发送、权限和队列测试通过；冻结插件的三次离线启动/重启通过，密钥和加密设置保留。公司仓库已合并 develop，构建提交 `3a01dc90f5d4e0e998b64091a551dc6117550fa7`。

## 真实发送

82 通过 Ryn 对原先明确失败的请求执行一次重试，获得原生消息编号和服务器回执，状态 confirmed。用户随后确认指定收件人已收到，完成端到端验收。未自动重发未知状态消息，也未在本机额外发送消息。阿九电脑尚未直接验收；不能把 82 的结果表述为已验证他的电脑。

## 安装与回滚

本机 `C:/Users/子陵/AppData/Local/Ryn` 与 82 `C:/Users/ziling/AppData/Local/Ryn` 均保留 R6 已验收核心，更新至 `plugin-services/company-3a01dc90f5d4e0e9/company-service.exe`；两机健康检查 ready。节点身份、消息密钥、桌面配置和公司密钥均与升级前一致。原插件目录保留，服务注册表备份为 `services.json.before-wecom-*`，现有环境配置原样保留。

82 的四项共享 AI 服务仍 ready、绑定验证通过。本次未重复执行 R6 模型长时验收；相关证据见 `../shared-ai-r6-20261103/README.md`。

## 完整安装包

- 文件：`output/full-package-20261103-r7/Ryn_0.8.0_Full_7Plugins_R7_x64.exe`
- 大小：681522006 bytes
- SHA256：`60BF81703BC03BCC4D9115007AAAED694DA4BB92CA69F779A036339F30B8A337`
- Ryn 打包提交：`ceeb6599ce617cc68b8a75d12ee893be3e5f06dd`；核心构建提交：`efd9fa13f8c2b501f689b6b04deec754cd79b8fb`。
- 完整包离线新装、重复安装保留身份/禁用插件状态、四项办公插件、三个公司页面及健康检查通过；公司验收进程禁止外网。
- R6 的 169 个 Office/CJK 文件逐项哈希相同，菜单与主程序代码未变。
- 两台实机采用保留数据的插件更新；完整 EXE 的系统安装向导未在本轮重复执行，完整载荷由隔离安装验收覆盖。

构建收据和脱敏检查见 result.json。账户、联系人标识、登录令牌、数据库与原始日志均未提交。临时远端含登录令牌的诊断响应已删除。
