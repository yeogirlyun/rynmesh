> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`da27b7af1e21c821352d2e20e6ac7366d3007f779acaefd075fa6a3063c43ef1`。

# 安装、构建来源与用户数据保全独立验收

结论：**PASS（本报告限定范围）**。未发现阻断本轮普通 Windows 安装更新、代码进入冻结包、或现有用户数据/设置/应用授权保全目标的证据。原始更新脚本的失败记录真实保留；后续只读重试充分解释了当时的 API 超时误判。此结论不替代主报告对模型协议和本轮 Word/PDF 业务任务的检查。

本轮审查只读取代码、打包文件、摘要与脱敏哈希证据。新增内容仅在本验收目录；未安装、启动第二个 Ryn 节点、请求模型、修改权限/设置/凭据/业务原件、读取密钥或解密旧会话，也未执行 Git 写操作。采用 reverse-engineer-anything 的证据边界区分包内静态内容、保存的普通 Windows 运行记录和当前只读观察。

## 独立检查结果

1. **代码确实进入冻结包。** 独立脚本使用 PyInstaller 的只读 CArchiveReader 打开本轮 `dist/rynmesh-peer.exe`，提取 PYZ 代码对象，与当前源码重新 compile 后逐模块比较（只规范化代码对象的文件名，递归保留常量/字节码等内容）。295 个 Ryn 模块全部相符；31 个冻结 UI 文件逐字节 SHA 相符；打包前登记的 857 项源文件 SHA 全部无漂移。相较上轮 packaged-source-provenance，仅生产 Python 文件 `llm_package/antigravity_session.py` 改变，UI 清单无变化。

2. **安装文件身份链条闭合。** 重算完整 installer 和 sidecar SHA；二者与 release/frozen provenance、外部执行 status 和 installation-proof 一致。独立读取未安装 shell 构建产物，验证唯一 `__TAURI_BUNDLE_TYPE_VAR_UNK`、不存在 NSS marker；在偏移 9825930 只替换末尾三个字节为 NSS，恰有三字节差异，所得整个文件 SHA 精确等于外部安装 shell SHA。没有以忽略任意字节差异的方式放宽匹配。

3. **外部安装执行与进程证据一致。** outside-update-002 原始 status 的 installer_exit_code 为 0；普通 Windows SID、Session 1、物理 profile 根验证，以及三个 helper 的固定 SHA 均能重算匹配。identity 的服务端反向 ESTABLISHED tuple 指向 worker 42636，父链为 shell 42424 → bootstrap 48608 → worker 42636；worker 路径位于普通 Local/Ryn。当前独立 CIM 仍观察到同一父链和创建时间，当前 netstat 的多个服务端 ESTABLISHED 连接也归属 42636。当前观察没有重启产品或发送 HTTP 请求。

4. **数据保全证据自洽且保留例外。** 本轮备份摘要为 3,103 文件 / 4,374,391,587 字节，四个根统计总和一致，私有 manifest 的完整 SHA 重算匹配。检查了 backup_tree / inventory / full_userdata_comparison 算法：停止已固定的旧进程后，备份逐文件 SHA 验证，再次检查源库存；安装后启动前完整 node-home、插件、Local profile、Roaming profile 共 3,099 数据文件路径/长度/SHA 无变化。没有将旧备份回写到在线 profile。独立重新比较保存的 before-offline/after-offline 脱敏快照，没有变化或缺失。

5. **首次启动失败得到诚实恢复。** outside-update-002 的 status.json 与 W/installed-update-status.json 字节完全一致，仍是 `failed / old_public_ids_missing_after_restart`。第一次 after-online 快照的六个端点明确为 TimeoutError；比较器把缺少响应 items 的端点当作缺失条目，共产生 32 个 missing ID。45 秒只读 retry 中全部 10 个端点正常（health 的投影 status 是 `ok`，其余为 HTTP 200），旧任务共 28 个，API missing ID 为 0，节点/Agent 设置字段哈希变化为 0。新 resolution 固定绑定原失败 status、retry 快照和语义比较输出 SHA；没有另一次安装或迁移。after-online 的一个旧 task blob 换名及 sessions.json 改写被保留，没有被描述成全部在线字节不变。

6. **启动后的语义保全输出通过独立一致性检查。** 检查了只读语义算法源码和固定 SHA，没有执行解密。算法使用已有密钥，仅在内存解密，并验证 task blob 的承诺长度、SHA、ID，输出只保留计数/ID/语义哈希。其本轮输出包含 28 个旧任务，missing/added ID 均为 0；所有消息的前后数量和哈希一致、旧 events 为精确前缀；每任务 20 项权限/控制/审批/限制等字段的前后哈希一致。唯一变化的任务原本及之后均为 interrupted，只增加一个 interrupted 事件，changed_fields 仅 event_cursor/events/revision/updated_at；这与 blob 换名和公开 task revision 变化一致。root settings、personal-space 和 cli-model-bindings 语义哈希一致。

## 保留的边界与非阻断观察

- 本审查没有再次读取私有备份的任务正文或密钥，也没有逐个外部服务重新登录。3,099 文件原始逐 SHA 比较依赖保留的外部执行结果及已审计算法；本次重算了其摘要/manifest/证据引用完整 SHA，而未重做包含密钥的备份扫描或旧会话解密。
- 本机应用仍标记 0.7.0，应以完整 SHA 和进程链识别此轮代码。
- **Codex 直接文件视图不能替代普通 Windows 安装视图。** 当前审查直接读取 Local/Ryn 得到 shell `f7a45f04f76e98944ed9b51c68aa63d761063b62dcd66e6e646032acc422e760`、backend `2869b2b10a0f4bbe796a2c9d1a9c7d99b94a0f143a9e6c087b6a5f3f2fefe6ec`，与外部部署固定 SHA 不同。这一差异被记录，不据此误判普通安装未更新；普通外部 pinned-file 校验、父链、精确 established socket 和当前 CIM/netstat 构成实际安装/服务身份依据。本审查不声称已定位所有 VFS 机制。
- WordCraft 的非敏感 receipt 与当前直接视图中 10 个 payload 文件逐 SHA 一致，canonical manifest `eef424bc102f11d9a3a9e5beb3c9b27c4abc088a915d34d9231aedb9244af467` 精确等于生产 catalog pin，CLI SHA 为 `74eff42097886095f106f662cd9a6eea77c10507be2b1698d80979c187368208`。然而在当前 Codex 进程中只调用 `installed_runtime` 的离线 preflight 仍返回 `craft_agent_runtime_not_verified`（exit 1，未启动 Craft）。receipt/spec 字段一致，拒绝发生在库存验证阶段；原因没有在本审查中继续诊断。不能武断归因 payload 损坏，也不能把当前 Codex preflight 代替普通 Windows 独立 QA 的结果。
- outside-update-001 保留 preflight endpoint 不可用失败，未记录 installer PID/exit 或 before_processes；早期 helper 的 ACL/health 检查问题和 launcher hash typo 均在 W 保留。outside-update-002 才有实际 installer PID 44168 与 NSIS exit 0。不能将原 orchestrator 整体退出码改述为 0。

## 关键文件 SHA-256

| 文件/对象 | SHA-256 |
|---|---|
| 本轮 installer | b5c6b9595f67d557a29e04dee51f55a164f8a4e74389180db50bfc205e1ce972 |
| frozen sidecar / 外部 installed backend | 8ad9968858ceeb1e5f43df2d993a211fc68c80389c3f9630574b23b6ac6f3a63 |
| 外部 installed shell / 独立推导完整 NSS shell | 436b13597ae1c206d3c8f58a9f48db8f46ac201a204b00de99b25cf8e7fca925 |
| 未安装 shell target | d5306bd61d34f31115cb06837da44443039f449710bed3a9abcfa1ce4f53a342 |
| 原失败 status.json | ce6653c14ded46cbb53f5a6791523d554db4fca2d86e7c4244b485d4aa4c82fe |
| outside-running-identity.json | 1b0ef8e40aafb016b459b4b42b19ca802a2555d9db7977f07eae94d7922b2187 |
| startup-verification-resolution.json | 6e67584f37e604b91ffc0086957aa665fcc36d345e62d7df2fa4ba13f120249e |
| 45 秒 retry 快照 | 766ed29d9b2c83d0725307452ad7368fbde1a1f383fd1303db6e2086aef41e62 |
| semantic-preservation-startup.json | aa5f8fadb4980b6ff86041db9dbfe182bbbd3f45cf29da442169edb3305e3c9b |
| private manifest（只重算 digest，未解析/复制备份） | c286e0f8f50b444c118d90d3bb8e548f46795fbf8358df833a73a6e89d2b2805 |
| 本审查 checks.json | e33e1efa272aaf9719d1c8410f3558bdb7cab1c54a4af2f372af2ecc4aa18859 |
| 本审查 current-context.json | 15e70d3b06fa021f1c5ea78f06a74d6a9b7b95dcd3a4fb7d3923901d99db683b |

## 实际检查命令与 exit

工作目录 `<repo>`；本轮新增脚本位于本报告同目录，读取路径固定，产物只写此目录。

```powershell
& '<repo>/.codex-tmp/windows-package/venv/Scripts/python.exe' -I '<repo>/docs/acceptance/gemini-tool-fix-20261018/independent-001/installation/verify_independent_installation.py'
# exit 0；checks.json PASS

& '<repo>/.codex-tmp/windows-package/venv/Scripts/python.exe' -I '<repo>/docs/acceptance/gemini-tool-fix-20261018/independent-001/installation/capture_readonly_context.py'
# exit 0；CIM、netstat 子命令均 exit 0；current-context.json

& '<repo>/.codex-tmp/windows-package/venv/Scripts/python.exe' -I '<repo>/docs/acceptance/gemini-tool-fix-20261018/independent-001/installation/word_runtime_preflight.py'
# exit 1；craft_agent_runtime_not_verified，未启动 Craft，结果保留

Get-FileHash -LiteralPath '<user-home>/AppData/Local/Ryn/Ryn.exe','<user-home>/AppData/Local/Ryn/rynmesh-peer.exe' -Algorithm SHA256
# exit 0；当前 Codex 直接视图的完整 SHA 差异已保留

& 'C:/Windows/System32/netstat.exe' -ano -p tcp | Select-String -Pattern ':8791\s'
# exit 0；8791 服务端 ESTABLISHED PID 42636；LISTENING 在当前上下文显示 PID 0

git rev-parse HEAD
# 返回 0146c0255fb056dcbb1b4939e27cd81c05073ef7；只读
```

其他读取采用 `Get-Content`、`Get-ChildItem`、`rg --files` 与 `rg -n` 检查固定来源文件；未调用构建/安装/迁移 helper 的 main，也未执行其 --execute-update。最初的 AGENTS 路径存在性查询没有找到祖先 AGENTS.md，组合命令因此 exit 1；HEAD 读取本身正常。当前上下文一次 Get-NetTCPConnection 的只读查询在 SilentlyContinue 下返回空，未作为无服务证据，后续 netstat ESTABLISHED 观察已独立保留。
