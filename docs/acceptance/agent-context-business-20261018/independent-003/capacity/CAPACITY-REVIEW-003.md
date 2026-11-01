> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`06b95955082344925e382849590b7ca81aa79ededd74fbb9da878b0ca96500aa`。

# 独立容量与来源复验 003

结论：PASS（容量/来源聚焦范围）。IR2-C01 的两个原样失败探针现均通过；本轮自拟反例未发现仍未关闭的容量/绑定缺陷。本片段不替 usage、真实办公业务或完整合同作结论。001、002 的 FAIL 报告和原探针保持原样。

## 直接复验与反例

`formal.junit.xml`、`formal.log`、`formal.exit.txt=0` 记录本轮正式执行 **101 个唯一 case，101 PASS / 0 FAIL / 0 ERROR / 0 SKIP**。独立汇总 `capacity-evidence.json` 逐 case 记录 classname/name 和相关文件 SHA-256，无重复 case；生成器 `summarize_capacity.py` 不改 XML。

| 正式组 | 唯一 case | 结果 |
| --- | ---: | --- |
| 原001 `test_capacity_edges.py` | 5 | 全部通过，未改原探针 |
| 原002 `test_capacity_independent_002.py` | 6 | 全部通过，其中前2项即 IR2-C01 原触发 |
| 已有容量/来源聚焦6文件 | 77 | 全部通过：CLI models 7、model context discovery 10、AI sources 8、Agent context budget 26、CLI freshness 20、local discovery 6 |
| 新实现 `test_cli_metadata_observation.py` | 6 | 全部通过 |
| 本轮独立新增 `test_capacity_independent_003.py` | 7 | 全部通过 |

原002 metadata-only 路径确实观察 400000→16000→400000，期间没有读取 B binding；最终旧 A binding 不再相等。原002另一个探针经认证真实 `GET /v1/models` 观察两次容量变化，然后用生产 `AgentModelBridge.complete(expected_destination=旧A)` 发起小请求，fixture `inner.chat` **0 次派发**。因此 IR2-C01 按原触发关闭，结论不依赖主开发自报。

本轮7个自拟反例进一步验证：

- known→unknown→known metadata-only 观察，中间重建 `CLIModelDispatchAdapter`；旧 Agent 默认 destination 和具体 model-a destination 都被拒绝，实际 chat=0，未变 sibling binding 保持。
- 两个不同确切模型均声明400000窗口，仅切 default resolved ID A→B→A，认证 `GET /v1/models` 的容量数值始终相同；旧默认 Agent destination 仍被拒绝、chat=0，两个确切子模型的 binding 都保持有效。
- 已配置 Antigravity 的真实 `GET /api/local/llm/cli-services/antigravity_cli/models` 与具体模型 `POST /api/local/llm/services/antigravity-cli/tools/test?cli_model=...` 分别观察 400000→16000→400000；每次返回前磁盘 signature 已更新。旧具体 binding 的 `call_bound` 被拒绝，chat=0，sibling 不失效。工具 probe 的付费演示只替换为 fixture，HTTP 路由、provider 收集和 metadata 观察未替换；该两项是直接 binding dispatch 检验，Agent expected_destination 检验由前述三项及原002另例覆盖。
- 原400000 snapshot 已持久化后，16000发现成功而 `atomic_write_json` 抛 OSError；旧 Agent destination 不能派发，passive 读取不会返回旧 binding（本探针得到 OSError并按不可发布处理），原磁盘字节不变。恢复写入能力并重建 adapter 后，16000新世代可持久化，旧 binding 不恢复，未变 sibling 保持。
- 只变显示名、排列顺序、重复读相同容量，不改变任何 binding，也不重写持久化 snapshot；避免以无必要的兄弟失效掩盖 ABA 修复。

开发新增6项在本轮实际复跑，覆盖metadata-only跨重启、消失tombstone与重现、同容量default ID ABA、全snapshot单次原子写与不变时零写、存储失败不发布新metadata、已配置CLI HTTP models观察。既有 freshness 20项及原001/002探针另覆盖TTL刷新失败/空listing fail closed、冷/过期 passive catalog不启动CLI discovery、持续缩容下旧授权和新小窗口均不能大请求派发、无效容量明确unknown fallback、配置/可执行文件变动与每模型隔离。未扩大全仓。

## 实现核读和边界

`rynmesh/llm_package/cli_binding.py:48` 的 `current_models()` 在接受成功非空listing前调用 `_observe_models()`，同一 `file_transaction` 提交default与全部确切模型snapshot。`:95` 的观察signature包含 resolved model ID、有效context_window、known标记；只有变化模型旋转随机generation，消失模型写tombstone，重现不复用旧世代。`:77` 的 passive binding只接受配置一致且仍新鲜的cached metadata，不运行发现。`routes.py:364` 的provider模型读取和`:2397`的已配置CLI models API走同一入口；具体模型工具probe的`:2318`同样调用该入口。

来源auto/manual、未知fallback、endpoint/credential缓存隔离、每模型override、manual整数校验及nullable Agent预算等沿上述聚焦测试验证。正常Agent的 `ceil(UTF8(messages+tools bytes)/3)+512` 与完整output预留仍是heuristic admission，不是精确tokenizer或actual费用硬上界；source draft `_model_probe` 旧估算口径不在本片段被宣称统一。

本轮仅使用隔离fixture、本地HTTP ASGI TestClient、临时模型清单/密钥/配置/目录；没有读取真实owner认证、配置、旧聊天，没有真实CLI模型调用或收费推理，没有修改生产源码/既有测试，没有reset/clean/stage/提交/部署或其他用户聊天消息。项目与祖先路径无AGENTS.md；全局 `<user-home>\.codex\AGENTS.md` 为空。

## 执行与快照说明

使用仓库 `.venv/Scripts/python.exe`。正式命令选择上表10个测试文件，`--basetemp=.../independent-003/capacity/tmp-formal-002`，耗时41.84秒。此前同一101组为100 PASS / 1自有harness错误，保留在 `harness-initial.*`：落盘失败安全分支由 `catalog.resolve()` 抛 `ValueError("agent_model_unavailable")`，我的自拟探针异常白名单只列AgentError/OSError，故在确认0派发之前被异常类型断言拦住。只扩充**本轮自有**探针接受ValueError后正式完整复跑；没有改变产品语义、原001/002预期或加xfail。实际202个attempts是101个唯一case的两次执行，不相加冒称202个case。最终落盘失败探针后续的0派发、磁盘未变、passive不发布与恢复持久化断言均实际通过。

主审先完成全源基线hash后才允许运行本片段；受核295生产Python fingerprint为 `c3ef74102482c31689716b6c9e5073cf9d017c6f29344a2bf552a084ddeacc12`，与最终冻结manifest逐项一致。完整webapp/测试/旧报告的前后hash守卫由主审统一签署；本片段的相关源、原probe、正式日志/XML逐项hash见 `capacity-evidence.json`。本片段不冒称另做全仓独立初始hash，也不追认任何未来修改。
