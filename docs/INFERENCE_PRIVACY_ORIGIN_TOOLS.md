# 推理隐私与发起端工具执行

2026-09-24。本文替代旧版 Codex 持久化会话、会话 ID 展示及续接设计。

## 调用边界

```text
zcode ──请求、工具定义、完整上下文──> A 的 Ryn API ──签名加密任务──> B ──> Codex App Server
zcode <──标准工具调用响应───────── A <──签名加密结果────────── B <── 模型的工具请求
zcode 在自己的权限范围内执行，再携带工具结果发起下一轮。
```

A、B 不解析路径为本地路径，也不执行返回的 shell、文件或安装操作。工具名必须来自本次请求，参数必须符合提供的 JSON Schema；外部 schema 引用禁用。发起端仍须自行授权工具、限定目录和资源。Ryn 不替 zcode 授予执行权限。

工具通过 Chat Completions `tool_calls`、Responses `function_call` 或 Messages `tool_use` 返回。支持工具结果回传；Responses 必须 `store=false`，每轮携带完整历史，不支持 `previous_response_id`。Codex 的 SSE 是缓冲后输出的兼容格式，不是逐 token 实时输出。每轮最多返回一个 Codex 工具请求，然后销毁提供端临时会话。

当前 API 仍仅接受 A 本机访问及独立 API Key。如果 zcode 运行在另一台机器，不应直接把本机管理接口暴露到公网；这需要另行配置受控的访问方式。

## Codex 与会话隐私

- 使用 `thread/start(ephemeral=true, environments=[])` 和 `turn/start(environments=[])`，没有提供端任务执行环境。
- 使用独立临时 CODEX_HOME，仅复制必要的本地文件型认证；不复制个人配置、插件、MCP、规则、历史。禁用 shell、exec、浏览器、图像、hooks、子代理、记忆和遥测等能力。认证副本与目录一起清理，不回写原认证文件。
- 注册客户端动态工具，使用内部名称避免与内置工具冲突；返回时恢复原工具名。收到未注册工具、执行审批或异常执行事件时失败，不回退到 B/A 执行。
- 当前 Codex 仍附带无执行环境的 `skills` 和 `request_user_input`。询问用户等服务端交互请求会被 Ryn 拒绝；没有命令、文件、浏览器执行工具。实际二进制的工具目录和恶意命令请求由本地假模型测试核验。
- 启动前检查该二进制导出的协议是否支持临时线程、禁用环境和动态工具。旧版本不支持时直接失败，不退回旧 `codex exec` 路径。
- 移除原 `conversation()` 持久化实现、thread/name/set、thread/resume、会话映射文件创建、前端 Codex 会话 ID 展示及续接参数。携带旧 `conversation_id` 的请求返回错误。
- Ryn 自己的聊天记录仍仅在发起端浏览器加密保存，并随请求携带上下文。不存在提供端 Codex 对话列表/读取接口。
- 已由旧版本写入个人 Codex 的历史不会被这个版本擅自删除；如需清理，在 Codex 中由所有者处理。原有 Ryn 会话映射不会再被读取或续接。

文件型登录已支持。只存在于系统钥匙串的登录、凭据自动刷新及不同 Codex 版本仍需实机验收。临时凭据、系统交换文件、进程内存和云厂商留存不属于“绝对无痕”保证。

## 保存与清理

- 保留现有 A/B 签名认证与端到端加密，不把推理正文放进注册中心。
- B 的任务结果不再写进订单文件；仅保留最多 8 份加密结果用于短暂重试，60 秒自动失效，结算可提前清除。重启后不可恢复正文，不重新执行过期工具请求。
- A 的 API 转发强制不保存结果，与界面里既有的保留设置无关。普通聊天订单默认保留期改为 0；用户以前明确保存的设置仍有效。
- 异步界面结果仅在有界内存中短暂等待领取，领取即移除，后台定时清理。订单元数据仍用于认证、额度和结算，文件数量有上限。
- 请求、输出不进入 Ryn 日志；禁止开启 manifest 正文调试日志；错误不暴露 CLI 原始 stderr。推理 HTTP 响应使用 `Cache-Control: no-store`。
- 正常、异常、超时和取消均回收 CLI 进程树及私有临时目录。进程被强杀/机器断电来不及清理时，下次调用会清理私有根目录下已标记且超过 1 小时的残留；不扫描删除个人 Codex 历史。

## 资源保护与边界

| 项目 | 当前限制 |
| --- | --- |
| 整个 Ryn 进程的推理并发 | 4，另受服务自身上限限制 |
| 推理 HTTP 连接 / 请求体读取时间 | 32 / 15 秒 |
| 请求体 | 本地/API 1 MiB；加密信封 4 MiB |
| CLI | 最长 300 秒，累计 stdout 8 MiB，事件队列 256 条 |
| Windows CLI 进程树 | Job Object：2 GiB 内存、25% 主机 CPU、16 个进程；退出/取消关闭整个 Job |
| Linux CLI | 独立进程组、2 GiB 地址空间、CPU 时间上限、单文件 64 MiB、禁止 core dump、低优先级 |
| macOS CLI | 进程组、CPU 时间/文件限制；不宣称有等价的 Windows 内存/CPU 百分比硬限制 |
| 临时目录 | 定期检查 64 MiB 上限；这不是文件系统硬配额 |
| 资源紧张 | 磁盘剩余不足 512 MiB、可测可用内存不足 256 MiB时拒绝新任务；可读取 NVIDIA 温度且达到 85°C 时拒绝新任务，5 秒缓存 |
| Ryn 管理的 Docker 模型 | CPU、内存及 swap 限制，临时盘限制，禁用容器日志/core dump |

外部已经运行的模型/API 进程仍由 B 的所有者管理：Ryn 可以限制发往它的请求，无法替外部进程实现 GPU 显存硬隔离或保证取消后立刻停止计算。温度保护是新任务准入检查，不能替代散热、驱动或硬件保护，也不会擅自修改显卡功耗。未提供温度指标的设备不宣称已受到温度监控。

CLI 的 max_tokens 是生成长度要求，真正的硬保护是时间、进程资源及输出字节上限；在工具调用处提前结束时，若 Codex 尚未提供 usage，使用估计用量。CLI 云账号费用仍受供应商计费规则影响。

Claude Code 保留纯文本模式，禁用工具/MCP及个人设置，使用独立配置目录与相同进程限额；其真实登录和运行测试待安装该 CLI 的机器验收。

## 自动验证

```powershell
.venv/Scripts/python.exe -m pytest tests/test_inference_privacy.py tests/test_cli_agent.py tests/test_llm_package.py tests/test_llm_hardening.py tests/test_inference_api.py tests/test_llm_auto_connect.py tests/test_ai_sources.py -q
$env:RYNMESH_TEST_CODEX_BINARY='1'
.venv/Scripts/python.exe -m pytest tests/test_codex_isolation_live.py -q
npm --prefix webapp run test -- src/screens/PrivateAIChat.test.tsx
npm --prefix webapp run lint
npm --prefix webapp run build
```

2026-09-24 本机验证结果：相关后端测试合计 138 项通过，聊天界面测试 11 项通过，Ruff、前端 lint 和生产构建通过。真实 Codex 二进制的 2 项测试已在补充临时文件正文检查后再次通过。

后续打包更新：Windows 桌面程序和 NSIS 安装包已重建，本机桌面快捷方式指向的程序已更新并重启。新安装包为 `release/windows/Ryn-0.6.2-privacy-windows-x64-setup.exe`，SHA256 为 `71070f130f035f3b197496fce541e74704cc3e826a5844fb821b88a7c40fefff`。新版冻结后台独立启动及实际桌面启动均通过健康检查，并通过隔离 CLI 读取到 7 个 Codex 模型；没有为打包验收发起云端推理。实际运行的后台文件哈希与构建产物一致，SHA256 为 `e53727fb258a4c8571e410f949f9ddbe15cf8523651b0cec8089a7765e2f298b`。安装器的干净机器安装/卸载仍未验证。

Mac Apple Silicon 包已于 2026-09-24 在 ARM64 macOS 26.2 上原生构建完成：`release/macos/Ryn-0.6.2-privacy-macos-arm64.dmg`，31,424,752 字节，SHA256 为 `0de77602c146a19ee4fb2dfbcad51aad33547bcab13965a4932f2a85557cdc0c`。已放到构建 Mac 的桌面，并取回 Windows 工作区核对校验和。

构建入口为 `sh webapp/src-tauri/scripts/build-macos-arm64.sh`，可用 `RYNMESH_BUILD_PYTHON` 指定原生 Python 3.12+。本次使用独立 Python 3.12.14、Node 22.22.0、Rust 1.98.1、Xcode 26.2。固定签名 XML 和 shell 文件使用 LF，避免 Windows 换行导致 macOS 签名解析失败；同时修复 macOS 本机地址发现等待主机名/mDNS 解析的问题，改为读取本地网卡地址。

Mac 最终相关测试 140 项通过，覆盖新增地址发现回归测试、桌面默认配置及原有隐私/CLI/API/真实 HTTP 和本机 ICE 测试。初轮有 1 项连接回退失败，单独复测及两轮完整重跑均通过（分别 136、140 项）；不据此宣称跨网络稳定性验收完成。ARM64 架构、ad-hoc 签名完整性、DMG 完整性检查通过；只读挂载最终 DMG 后，包内后台在独立测试端口启动并通过健康检查，冻结后的 CLI 隔离子进程入口返回成功。构建 Mac 未安装 Codex CLI，未测试其真实账号/模型调用和原生界面交互。此为未进行 Apple 公证的测试包，首次打开可能受到 Gatekeeper 拦截。

真实二进制测试使用 Windows 的 `codex-cli 0.155.0-alpha.9.2`，模型服务器是测试进程中的本地 HTTP 假模型，不调用云端、不消耗模型额度。测试包括实际工具目录检查、强行返回 B 端命令时不执行、正文未出现在临时文件及结束后目录移除。

## 留给最终实机验收

1. 更新 A、B 的 Ryn 及依赖；B 配置 Codex 登录和个人空间授权。在 A 的 API 接入页面选 B 的 Codex 模型并创建 zcode 专用 Key。
2. zcode 发送一个只允许写入其测试目录的任务，要求生成文件、运行测试。确认文件和命令仅出现在 zcode 端。
3. 连续多轮“读取—修改—运行—修复”，核对工具调用 ID、参数、结果和上下文都能正确往返。
4. 测试 A/B 不同系统路径、恶意要求改写 B 文件、客户端不支持工具、断线、取消、超时。失败必须结束，不能自动在 A/B 执行。
5. 检查 B 的 Codex 对话列表没有新增 Ryn 对话，临时目录回收，订单仅有元数据。
6. 实测真实账号认证/续期、跨机 P2P、安装包干净安装与原生界面交互、Linux 进程限制、实际 GPU 温度与高负载；这些不算本机自动测试已经通过。macOS 基本进程终止、超时及输出限制已有上述自动测试覆盖。

接口依据：[Codex App Server](https://learn.chatgpt.com/docs/app-server)、[Codex 配置](https://learn.chatgpt.com/docs/config-file/config-reference)、[Claude CLI](https://code.claude.com/docs/en/cli-reference)。

## 2026-09-24 Mac 回答交付与局域网修复

用户报告的任务 `task_c15c7f2ba525433dab6619e7a45c3ca1` 在 Mac 消费端和 Windows 提供端均已成功。消费端先写入成功元数据，完成结算后才把不落盘的回答交给轮询接口；界面可能在这段空档读取到“成功但无正文”。现在后台尚未交付回答时继续返回 running，已领取或过期的回答明确报错，不增加正文留存。新增确定性回归测试主动暂停成功元数据提交后的工作线程，验证期间不得提前结束轮询。

聊天界面的 CLI 路由从强制 p2p 改为 auto，使用现有的局域网地址优先、签名加密任务传输。显式要求 P2P 的 API 行为保持原协议。原任务连接协商约 19.39 秒、模型处理 23.05 秒；二者不应混为局域网网络延迟。

本次 Mac 实际地址为 `172.16.8.117`，已在 `/Users/zhoulei/Applications/Ryn.app` 更新并启动。补丁应用于 Mac 上已有的设备启动修复版本，未打入另一个任务尚在开发的本地模型导入功能。旧应用备份在 `/Users/zhoulei/ryn-builds/privacy-20260924/Ryn-before-private-answer-fix.app`。

验证记录：

- Windows 相关后端 75 项通过，聊天界面 16 项通过，生产前端构建通过，真实 Codex 隔离测试 2 项通过。
- Mac 同组后端首轮 74 项通过、1 项 LAN 连接中断；该项单独复测及整组重跑通过，最终 75 项通过。Mac 当前界面版本 11 项通过。原生构建、ARM64、签名、冻结后台健康检查及隔离子进程入口通过。
- 已安装的 Mac 应用后台调用 Windows Codex，任务 `task_4a017ce37ef84b348c0eec112846699d` 在 9.661 秒内正确返回合成测试回答，其中模型处理 7.546 秒。证据为 `peer_http_direct`、`route=lan`、`relay_used=false`。Mac 到 Windows 健康请求三次为 9.9、11.5、10.1 毫秒。不同问题及模型负载不可直接作等条件性能对比。
- A/B 两端订单均未写入合成测试正文或加密回答，B 的临时 CLI 会话目录数为 0。本次没有采集用户历史对话正文。

测试包：`release/macos/Ryn-0.6.2-private-answer-fix-macos-arm64.dmg`，31,444,410 字节，SHA256 `141e8e39c6822aed8e8ebef4b2492c7cbd41551484b2429d2c467df8a4dac013`。同名文件已放到 Mac 桌面，Windows 工作区副本校验和一致。

本次实测通过后台 API 驱动已安装应用；Mac 原生界面手动发送和 zcode → A → B → Codex CLI 完整工具往返仍由用户最终验收。B 推理进程可接触明文，云厂商留存和操作系统交换文件不受上述“不保存正文”检查保证。
