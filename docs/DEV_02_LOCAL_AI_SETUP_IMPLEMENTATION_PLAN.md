# DEV-02：一键启用本地 AI——完整开发与验收文档

状态：已批准，implementation-ready  
产品需求：[USER_LOCAL_AI_SETUP_WORK_PLAN.md](USER_LOCAL_AI_SETUP_WORK_PLAN.md)  
优先级：P0  
前置依赖：DEV-01 提供入口；后端核心可独立开发  
阻断后续：DEV-03 默认本地助手闭环

## 1. 开发结果

把已有 LLM setup、hardware detection、managed GGUF lifecycle 和 Ollama detection
收敛成普通用户可以在应用内完成的一条路径：

```text
检测设备 → 推荐一个档位 → 用户确认 → 可恢复下载 → 校验 → 启动 → 真正问答测试
```

默认不要求安装 Ollama、运行 Docker、使用终端或提供云 API Key。高级路径保留，但
从默认向导折叠出去。

## 2. 当前实现基线

### 已有后端

- `rynmesh/llm_package/hardware.py`：硬件探测和模型建议基础；
- `rynmesh/llm_package/lifecycle.py`：managed download、SHA-256、GGUF、runtime、
  self-test、update、uninstall；
- `rynmesh/llm_package/routes.py`：
  - `GET /api/local/llm/hardware`；
  - `POST /api/local/llm/setup/async`；
  - `GET /api/local/llm/setup/status`；
  - `POST /api/local/llm/setup/{job_id}/cancel`；
  - setup job 原子写入 `llm/setup-job.json`；
  - node 重启会把遗留 running job 标记为失败；
  - service status/actions 和 local order API 已存在。
- `rynmesh/services/model_provider.py`：Ollama/OpenAI-compatible Provider 解析。

### 已有 Webapp

- `webapp/src/screens/Services.tsx`：完整但偏运营/Provider 的 setup UI；
- `webapp/src/screens/components/LocalModelPicker.tsx`：只能选择已有 Ollama 模型；
- `webapp/src/domain/nodeClient.ts`：已有 `LLMSetupJob`、setup/status/cancel/action；
- 当前无 Ollama 时仍提示访问外站和执行 `ollama pull`。

## 3. 范围与平台决策

### 3.1 v1 支持模式

1. 已运行 Ollama：复用已安装模型；
2. Ryn managed：下载固定 GGUF 并由 Ryn 管理运行时；
3. 导入 GGUF：高级路径；
4. 连接 loopback OpenAI-compatible/Ollama：高级路径。

### 3.2 桌面运行时

开发前必须确定每个平台的受管 runtime：

- runtime 二进制/容器来源；
- 架构矩阵；
- 签名或 SHA-256；
- GPU/CPU backend；
- 许可证展示；
- 数据目录和进程生命周期。

普通用户路径不能把 Docker 作为隐含依赖。如果当前 managed 实现仍需要 Docker，
该模式只作为开发者预览；正式“一键”路径必须使用随应用审核的 sidecar runtime，
或明确只在已支持的平台开放。

### 3.3 推荐档位

后端返回 `light`、`balanced`、`quality`，每个包含：

- model ID/alias/version；
- 下载大小；
- 估算运行内存；
- context window；
- backend；
- `supported` 与不支持原因；
- 推荐原因。

前端默认只突出一个 `recommended_profile`。

## 4. 后端改造

### 4.1 硬件 API 规范化

扩展 `GET /api/local/llm/hardware`：

```json
{
  "version": "ryn.local-ai-hardware.v1",
  "platform": "windows-x86_64",
  "memory_available_mib": 12288,
  "disk_available_mib": 45678,
  "accelerators": [{"kind":"cuda","memory_mib":8192}],
  "runtime_support": {"managed":true,"ollama":true},
  "recommended_profile": "balanced",
  "profiles": []
}
```

所有数值为保守估计。无法可靠获取 GPU 时退回 CPU 评估，不声称 GPU 可用。

### 4.2 安装任务协议 v2

保留旧字段兼容，新增：

```json
{
  "version": "ryn.local-ai-install.v2",
  "job_id": "setup_...",
  "state": "running",
  "stage": "download_model",
  "progress": 43,
  "bytes_done": 123,
  "bytes_total": 456,
  "resumable": true,
  "cancelable": true,
  "selected_profile": "balanced",
  "attempt": 1,
  "started_at": "...",
  "updated_at": "...",
  "safe_error": null
}
```

状态转移：

```text
idle → queued → running → succeeded
                   ├→ cancelling → cancelled
                   └→ failed → queued(retry)
```

规则：

- 同时只能有一个 setup job；
- `job_id` 和 attempt 防止旧请求覆盖新任务；
- 每个阶段持久化，写入原子化；
- node 重启后验证临时文件和已完成阶段，安全恢复而非一律失败；
- 不可恢复阶段可以回退到最近安全 checkpoint；
- setup job 不保存 API Key、Prompt、输出或原始本地文件路径到公开响应。

### 4.3 可恢复下载

修改 `rynmesh/llm_package/lifecycle.py`：

- 下载到固定 `.part`；
- 只有服务端支持并验证 Range 时才续传；
- ETag/Last-Modified 或固定摘要变化时丢弃旧 partial；
- 下载前检查 `Content-Length`、上限和磁盘余量；
- 下载后流式计算摘要，原子 rename 到最终文件；
- 校验失败隔离/删除 Ryn 自有的损坏 partial，不触碰用户导入文件；
- timeout、低速和重试次数有界；
- progress 回调节流，避免频繁写盘。

### 4.4 Runtime supervisor

在 `rynmesh/llm_package/` 增加平台无关 supervisor seam，或复用 Tauri sidecar 管理：

- start/stop/restart；
- stdout/stderr 大小轮转且正文敏感信息过滤；
- 健康检查；
- 崩溃计数与退避；
- node/App 退出策略；
- 更新时停止并恢复；
- 只监听 loopback；
- 独占 package/port lock。

不得通过 shell 拼接用户输入启动命令。

### 4.5 真实 ready check

`succeeded` 必须满足：

1. runtime health 成功；
2. ModelProvider 能列出/选择目标模型；
3. local node 发出固定短测试请求；
4. 返回非空、在大小/超时范围内的结果；
5. Browser 能查询到 `ready=true`。

测试 Prompt 使用产品固定字符串，不进入正式会话和推荐画像。日志只记录成功/耗时/
token 数，不记录正文。

### 4.6 API

保留现有 API，新增/调整：

```http
GET  /api/local/llm/recommendations
POST /api/local/llm/setup/async
GET  /api/local/llm/setup/status
POST /api/local/llm/setup/{job_id}/cancel
POST /api/local/llm/setup/{job_id}/retry
GET  /api/local/llm/readiness
```

`setup/async` 默认请求只需 `{profile:"balanced", accept_license:true}`。高级 mode 的
旧请求继续支持。所有路径由 local-control auth 保护。

## 5. Webapp 改造

### 5.1 新页面与组件

- `webapp/src/screens/LocalAISetup.tsx`；
- `webapp/src/screens/LocalAISetup.module.css`；
- `webapp/src/screens/LocalAISetup.test.tsx`；
- `webapp/src/screens/components/ModelProfileCard.tsx`；
- `webapp/src/screens/components/InstallProgress.tsx`。

默认向导步骤：设备检测、推荐确认、许可/空间确认、安装进度、第一次回答、完成。

### 5.2 入口收敛

- DEV-01 成功页的“启用本地 AI”；
- Ask Ryn 无模型状态；
- Settings → AI；
- Services/manage 继续保留 Provider 运营能力，但调用同一套 API 和状态机。

`LocalModelPicker` 不再显示 terminal 命令作为首选恢复，而改为“安装推荐模型”；已有
Ollama 用户仍看到模型列表。

### 5.3 状态同步

- setup 进行中每 1 秒查询；页面隐藏后降低到 5 秒；
- App 重启后读取 active job 并回到进度页；
- 使用 job_id 过滤陈旧响应；
- 成功后停止轮询，刷新 ModelProvider 和 settings；
- 取消按钮只在后端返回 `cancelable=true` 时启用；
- 关闭页面不取消任务。

## 6. 安全和数据边界

- 只下载 manifest 白名单中的 HTTPS 资源；
- model/runtime 使用固定 SHA-256 和许可证元数据；
- 不执行仓库中的自定义代码；
- managed runtime 只监听 loopback；
- 非 loopback API 需要高级模式、显式风险确认和 allowlist；
- API Key 只允许环境变量引用或系统密钥存储，不通过普通 JSON 持久化；
- model 文件、partial、日志有磁盘上限；
- 导入模型只读使用，卸载默认不删除；
- 受管模型删除需要独立确认并验证路径位于 managed root；
- node 无模型时所有非 AI 功能保持可用。

## 7. 数据迁移

- 已选择 `settings.ai_model` 且 Provider 健康：直接 ready，不启动安装；
- Ollama 正在运行且已有模型：展示复用选择；
- 旧 setup-job running：v2 恢复器检查 partial/runtime 后映射状态；
- 旧 managed manifest：验证版本、摘要和路径后注册；
- 不认识的 manifest/version 不自动启动，显示“需要重新验证”；
- personal data export 只导出 model ID、版本、状态，不导出 API Key 或完整路径。

## 8. 实施 PR 切片

1. `refactor: version local AI hardware and setup state`
2. `feat: resume verified managed model downloads`
3. `feat: supervise bundled local model runtime`
4. `feat: add user-facing local AI setup wizard`
5. `test: validate local AI install recovery and privacy`
6. `docs: publish model/runtime support matrix and licenses`

受管 runtime 的平台实现可以分 PR，但任何平台只有通过完整验收才在 UI 显示支持。

## 9. 测试矩阵

### Backend unit/integration

- hardware recommendation boundary；
- 小内存/小磁盘拒绝；
- 单任务并发锁；
- download exact size、oversize、timeout、断线、Range resume；
- checksum mismatch；
- partial 文件版本变化；
- cancel at every stage；
- restart at every checkpoint；
- runtime port conflict、crash、health timeout；
- first inference success/failure；
- uninstall managed vs imported；
- secrets/Prompt markers absent from job/log/export。

重点文件：

- `tests/test_llm_package.py`；
- `tests/test_llm_hardening.py`；
- `tests/test_model_provider.py`；
- `tests/test_model_selection.py`；
- 新增 `tests/test_local_ai_setup_recovery.py`。

### Webapp

- recommended profile；
- Ollama reuse；
- progress stage；
- cancel/retry；
- refresh/restart resume；
- insufficient resources；
- license confirmation；
- advanced modes hidden/revealed；
- completed readiness and navigation to Ask Ryn；
- keyboard/focus/screen reader。

### Physical acceptance

至少覆盖：

- Windows x86_64 CPU-only；
- macOS Apple Silicon；
- macOS Intel（若仍在支持矩阵）；
- 一个受支持 GPU 环境；
- 无 Ollama、已有 Ollama、网络中断、磁盘不足四类状态。

## 10. 验收条件

### 功能验收

- [ ] 无 Ollama、Python、Node、Docker 和终端操作可完成受管安装（标记支持的平台）。
- [ ] 已有 Ollama 用户可以复用模型，不重复下载。
- [ ] 用户只需选择推荐档位并确认许可证/空间即可开始。
- [ ] 下载有真实进度、取消、重试和跨应用重启恢复。
- [ ] 安装成功必须经过真实 local-node 问答测试。
- [ ] 完成后 Ask Ryn 立即识别所选本地模型。
- [ ] 模型故障不影响 For You、Reader、Library 等非 AI 功能。

### 安全验收

- [ ] 模型和 runtime 均通过固定摘要验证；未知版本拒绝启动。
- [ ] oversize、路径逃逸、非 HTTPS、校验失败和恶意 Range 响应 fail closed。
- [ ] managed runtime 只监听 loopback。
- [ ] API Key、Prompt、输出和用户本地文件路径不进入 setup job、普通日志或导出。
- [ ] 卸载不会删除导入/用户自有模型；受管模型删除路径经过 root 校验。

### 质量验收

- [ ] focused pytest、完整 pytest、ruff 通过。
- [ ] Webapp `npm test`、`npm run lint`、`npm run build` 通过。
- [ ] 每个宣称支持的平台完成一次干净安装、取消恢复、升级和卸载。
- [ ] 安装任务在强制终止 node/App 后能安全恢复或给出可执行错误。

### 验收证据

- 各平台从无模型到第一次回答的录屏；
- 下载大小、总耗时、峰值磁盘/内存和首次 token 时间；
- 网络中断、checksum mismatch、磁盘不足、runtime crash 的恢复记录；
- model/runtime manifest、摘要和许可证清单；
- 日志 marker 负面搜索；
- 实现 commit、构建产物、已知限制和回滚路径。

## 11. 发布与回滚

- 按平台 feature flag 开启 managed runtime；
- 不支持的平台继续提供 Ollama/高级连接，但 UI 不声称“一键安装”；
- runtime/model manifest 独立版本化，可停止分发某一有问题版本；
- 回滚 App 时保留模型文件，避免重新下载；
- 新版本失败时允许回到上一个已验证 runtime，不回退安全摘要校验；
- 不因回滚删除用户导入模型或现有 Ollama 数据。
