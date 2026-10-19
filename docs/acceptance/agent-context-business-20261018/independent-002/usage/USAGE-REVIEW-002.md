> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`441561bc9bf6fbd7159f603f94cab58c271c64485347fedff0a2a19a5d0e60fb`。

# 独立 usage 与账务验收 002

结论：**FAIL**。首轮 **IR1-B01（Codex decoder 缺失 usage 被合成）已关闭，限 Codex 路径**；本轮发现 **IR2-B01**：真实 OpenAICompatibleAdapter 仍将旧估算的顶层计数传给公共兼容 API 和 Provider 计费层。消费者会对缺失 actual usage 的成功结果结算并确认 Provider earning，违反本轮 `pending_usage` 合同。

本子审查只读取当前冻结源码，新增文件位于本目录。未修改生产代码或原探针预期，未启动任何真实 CLI、外部模型推理，未读取真实 owner 认证、旧聊天或修改真实配置。模型上游是本进程启动的 `127.0.0.1` HTTP fixture；订单使用临时 Ryn 节点和临时密钥，走真实 `seal_task/open_task`、签名、ProviderService、ConsumerCommands 与 consumer signed credit ledger。临时账本为开发模拟账本，不代表真实金钱交易。

## 阻断项 IR2-B01（P2）

`rynmesh/llm_package/adapters.py:250–251,377–385,535–536` 仍以 `reported or heuristic` 填充顶层 `input_tokens/output_tokens`。`chat` 同时保留准确的 `reported_usage`（缺失为 null），但 `chat.py:211–212`、`api.py:311–313` 和 `routes.py:689–695` 只使用顶层旧估算。缺失 actual count 因而被当成完整实际用量，连报告的整数 0 也被 `or` 覆盖。

| 独立触发 | 实测结果 | 证据 |
| --- | --- | --- |
| OpenAI HTTP 上游 `usage={}`，三个公共 API，各 stream/nonstream | 六例均成功返回合成实际计数；原 output quota reserve=30 被改成12。应为 null并保持30。 | `own.junit.xml` 前6例；`own-summary.json` 含完整脱敏 HTTP 结果和 quota读回 |
| 真加密 Provider infer/chat，上游 usage缺失 | infer给出24/3；chat给出32/12，且`reported_usage={null,null}`同时存在。两者都 `usage_complete=true,billing_status=priced,billing_basis=reported_usage,amount=0.001`。 | `own.junit.xml` missing-infer/missing-chat |
| Provider部分usage，仅prompt_tokens=7 | infer补造output=3、chat补造output=12，两者仍priced。 | `own.junit.xml` partial-infer/partial-chat |
| Provider明确报告0/0 | infer改成24/3、chat改成32/12；本次底价仍.001。 | `own.junit.xml` zero-infer/zero-chat |
| 真实Consumer加密direct订单，上游usage缺失；重复同请求 | result给出425/3，amount=.001、priced；consumer signed账本（ledger_backed=true）实际hold后settle，held变0；发送1次settlement，Provider standalone账本（ledger_backed=false）实际新增earning=.001。重复请求infer仍1次，幂等未额外失效。 | `chain.junit.xml`、`chain-summary.json` |

以上13个失败断言归并为同一根因，不把fixture初始签名/health错误计作生产失败。测试保留原应然断言，未改为xfail或“先适应实现”。修复应确保 downstream只把明确报告的合法计数当 actual：本轮至少应统一 OpenAI `infer/infer_stream/chat` 与 API/Provider 的取值口径，并保留 quota、hold 的unknown reservation语义。没有要求新建后续补用量/清算平台。

## 已独立确认的关闭范围

最终 `focused.junit.xml` **120/120通过**，其中包含原 `independent-001/test_budget_independent.py` 的全部8例（文件未编辑）。原 `test_missing_codex_usage_must_not_become_actual_runtime_tokens` 现在通过；Codex malformed/absent/partial/0计数、tool-before-usage、Bridge→AgentRuntime和reopen均由fixture实测。

Codex路径的三个兼容 API stream/nonstream保持 null、JSON可编码，缺失output usage保留quota reserve；Provider/Consumer缺失usage的成功结果保持`amount=null,pending_usage`，同请求和两次startup恢复不再infer、hold、charge或settle。Provider own/free返回0价并明确`own_request`，token未知。nullable settle/earning在standalone和signed ledger中重开、snapshot丢失重建、显式rebuild与去重仍为null；pending_usage的running/succeeded、explicit/legacy-marker、standalone/signed共8个恢复组合保留原hold。旧known usage订单结算、signed账本及普通中断释放路径也复跑通过。

上述结果证明新增null逻辑在Codex/Antigravity及直接nullable模型fixture下可用，不能外推为OpenAI来源已满足同一合同。Agent预算通过`reported_usage`避开chat顶层估算，并不自动保证API/Provider账务层也避开它。

## 计数与限制

唯一执行用例 **133：120pass / 13fail**，由最终focused120、独立own12、consumer闭环1组成。`case-manifest.json` 列每个唯一nodeid及状态，重复运行不重复累计。先前 collection文件名错误、fixture签名/health接线错误和一次pytest全局临时目录清理PermissionError日志均保留，排除于结论计数；最终focused使用本目录`--basetemp`并正常exit0。

聚焦组来自原budget独立探针、test_codex_usage_unknown、test_antigravity_usage_accounting、test_agent_budget_dispatch、test_agent_runtime_budget、test_task_balance_ledger、test_inference_api，以及原test_llm_package三个恢复/known结算case。不是全仓测试，也不与主开发其他回归组重复加总。

累计token admission仍是heuristic reservation：实际报告可高于估算，原独立测试保留并通过。未知费用仍unknown，不能将此称为精确actual token/费用硬上界。此次没有验证真实provider补交usage后的清算流程，源码也未建立该平台。`read-source-hashes.json` 记录本子审查关键文件hash；全生产源码验前验后冻结校验由总独立审查汇总。
