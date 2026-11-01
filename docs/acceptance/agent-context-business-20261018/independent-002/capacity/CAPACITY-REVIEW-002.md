> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`40e144bec410782d117ec6401e15add7f1123dce2b839fa71fe3790d4df33afa`。

# 独立容量与来源复验 002

结论：**FAIL**。首轮 IR1-C01/C02/C03/C04 的原 5 个未改预期探针全部通过；另发现已观察容量 A→B→A 时旧 destination 恢复的防 ABA 缺口 IR2-C01。最终去重执行 **88 case：86 PASS / 2 FAIL**，两个失败证明同一个问题。全部推理回调为隔离 fixture，没有真实 CLI 推理或收费调用，没有改生产源码、已有测试、owner 配置、真实凭证或旧聊天。

## 未关闭项

### IR2-C01 [P2] 容量被元数据读取路径观察为 A→B→A，旧绑定与旧 Agent 授权仍恢复有效

位置：`rynmesh/llm_package/cli_binding.py:48` 的 `current_models()` 接受并返回刷新后的 metadata；`:64` 容量读取只返回 metadata；`:93` 和 `:109` 仅在 `_binding_for_models()` 被调用时比较 signature 并更新随机 model generation。

独立探针先在 model-a=400000 时取得 binding 及 Agent destination A。随后使真实 CLI metadata cache TTL 过期，把 fixture listing 声明改为 16000，通过容量读取观察该值；再次过期并读到恢复的 400000。期间没有调用中间状态的 `binding_for_model()`。最终 `binding_for_model("model-a")` **逐字段等于最初 A**。

第二个探针进一步通过认证的真实 `GET /v1/models` FastAPI 路由观察这两次容量变化，响应分别明确为 16000 和 400000。其 manager 的容量方法按生产相同路径委托 `context_metadata_for_model()`，没有请求推理。然后运行生产 `AgentModelBridge.complete(..., expected_destination=旧A)`，fixture `inner.chat` 实际被调用 **1 次**并成功返回；旧 destination 没有被拒绝。

因此这不只是只读行显示相同：旧授权实际可再次进入模型调用。它是在**已恢复为 400000**时调用；本探针**没有证明窗口仍为 16000 时越界派发**，也没有证明账号权限绕过。始终维持 16000 的缩容场景和原 C03 阻断均已通过。这里的缺口是约定的“已观察容量变化不复活旧绑定”未覆盖容量读取/公开 listing 路径；现有随机世代测试只覆盖每个中间状态均先查询新绑定的情况。

建议在接受新模型 metadata 时记录受影响模型的已观察 signature 并更新世代，而非仅在按需签发绑定时发现差异；兄弟模型应继续保持独立。未替主开发修改实现。

失败证据：`independent.log`、`independent.junit.xml`；自有探针 `test_capacity_independent_002.py` 的前两例。该问题在本轮冻结最终源码复现，不把它归因为首轮的旧缓存快照问题。

## 已确认关闭及通过的范围

| 检查 | 独立结果 |
| --- | --- |
| IR1-C01 已保存 auto 来源 1M metadata 配置 128000 输出 | 原探针 PASS；按各 effective 模型校验，未知 fallback 仍会拒绝不适配的输出，不受闲置 32768 数字误拦。 |
| IR1-C02 容量变化转精确模型 binding | 原探针 PASS；完整 catalog 或绑定查询路径会旋转受影响模型及 default，兄弟模型保持不变；中间状态均查询 binding 时 A→B→A 不复用。IR2-C01 是剩余未覆盖路径。 |
| IR1-C03 已选模型 TTL 与切小阻断 | 原两探针 PASS；selected 与 default 都走 `current_models()`，过期刷新失败和空 listing 均 fail closed；旧大窗口 expected destination 不可在持续小窗口下派发。 |
| IR1-C04 认证兼容 API 的 CLI 默认容量 | 原探针 PASS；400000 installed window 下 150000 ASCII 请求被接纳；缩容后同请求被拒绝。API 请求自报 context_window/model_metadata 被不支持参数校验直接 400 拒绝，回调 0 次。 |
| 来源值严格校验 | 自有真实 PUT 的 12、10000.5、2000001 均 HTTP400，fixture SourceStore 完全未变；既有 bool/NaN/Inf/浮点输入亦通过严格拒绝回归。这里未单独复跑前端交互，UI 结论由主审另记。 |
| 来源容量真实性及兼容 | 未采纳 caller metadata；listing 的确切模型 metadata 才生效，endpoint/credential 隔离；manual、旧无 provenance 数值、每模型 override 优先、重启保持均通过。未读取真实凭证。 |
| CLI passive metadata | `cached_binding_for_model()` 在冷启动/过期时不启动 discovery；离线旧 snapshot 只显示且无 destination authority；可执行文件和配置变化刷新、无效容量标 unknown fallback、default 切换和兄弟独立均通过。 |
| Agent 自动/手动与请求估计 | 新默认无 600000 或 2M 字节顶帽；4000..2M 手动整数和旧 120000/600000 ambiguous 限额保留；UTF8 messages/tools 序列化与 framing 的 heuristic 估计、完整输出预留、小窗口拒绝、重启接线均通过。 |
| Provider metadata 与防伪 | 既有 sealed task fixture 通过，provider 对确切 CLI 模型使用 installed metadata，caller 的 context_window 不被用于容量准入。 |

## 执行证据和计数

使用仓库 `.venv/Scripts/python.exe`（Python 3.12.10），所有正式测试使用此审查目录内的独占 basetemp。`capacity-evidence.json` 记录受核相关源码、原探针、正式 XML 和自有探针的 SHA-256 及每文件 case 计数。整体生产快照首尾一致性由主审统一记录，本片段不虚构独立的全仓初始 hash。

| 最终执行 | Case | 结果 | 证据 |
| --- | ---: | --- | --- |
| 原独立 `independent-001/test_capacity_edges.py` | 5 | 5 PASS | 合入 `focused.junit.xml`，原文件 SHA-256 保持 `741257db874f4d9f146b519aaf0b75b43d9cc5006d93a7230e6674783f1de607`。 |
| `test_agent_cli_models.py`、`test_model_context_discovery.py`、`test_ai_sources.py`、`test_agent_model_context_budget.py`、`test_cli_capacity_freshness.py`、`test_agent_local_model_discovery.py` | 77 | 77 PASS | `focused.log`、`focused.junit.xml`、`focused.exit.txt=0`；聚焦容量/来源回归，不是全仓绿。 |
| 自有 `test_capacity_independent_002.py` | 6 | 4 PASS / 2 FAIL | `independent.log`、`independent.junit.xml`、`independent.exit.txt=1`。 |

启动环境诊断不计 case：默认 Python 3.12 没有 pytest；系统 pytest.exe 是 Python 3.10，不能导入既有 `datetime.UTC`；先用仓库 .venv 执行的相同 82 case 过程中全部通过，但公共 temp 的既有 `pytest-current` junction 在 session cleanup 时发生 Windows 拒绝访问，故换独占 basetemp重新完整执行，正式 XML 无 error。相关 `startup-*`、`temp-cleanup-diagnostic.log` 保留。

自有探针首版误限定伪造参数必须返回“context window”文案，但实现正确提前返回“unsupported parameters”；已把**自有**断言修正为实际安全合同的 HTTP400 + unsupported parameters + dispatch=0，并保留初版诊断。未改首轮原 5 例的断言，也不把探索重复执行计为新增 case。

限制：未复跑大 HTTP/P2P 边界、真实收费大窗口推理或办公业务生成；未证明 heuristic token 估计/累计限额是 tokenizer 精确 actual hard bound。source draft `_model_probe` 的旧口径、未知窗口 32768/120000B fallback 以及业务受核快照与最终修复快照的区别由主审范围说明。
