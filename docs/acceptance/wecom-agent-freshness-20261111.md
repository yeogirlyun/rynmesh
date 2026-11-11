# WeCom Agent 查不到最新聊天、分析混用历史（2026-11-11）

现场问题：Agent 搜不到 11-10 之后的企业微信聊天；分析不准，把历史状态当成现状。诊断包的截图和原回答只用于本次私下诊断，未提交。本文不含业务内容。

## 根因（已由现场只读证据确认）

1. **Agent 查询从不触发同步。** 本机索引只在插件页面打开（状态、会话列表、会话消息）时增量同步，Agent 的搜索、读会话、查覆盖工具只读缓存。现场索引停在 2026-11-09 16:50:22，最后同步时间是 2026-11-09T08:50:56Z，此后源 `message.db-wal` 仍在持续写入。
2. **覆盖状态把旧数据报成完整。** `coverage.state=complete / verified=true` 只表示上一次快照逐条处理完，没有和当前源比较，所以已有的“区分完整未找到与覆盖不足”规则失效。同步失败只写日志，不进入状态。
3. **规则缺口 + 模型未遵守。** 已有规则要求 has_more 时继续翻页，但模型没有用 `next_cursor` 和 `wecom_read_message`；规则里也没有覆盖“历史与现状”“文件时间与发送时间”“附件读不出与没提供”这几类区分。
4. **Ryn 的潜在隐患（这次不是主因）。** 一个 MCP 结果同时以 `content`、`text`、`structuredContent` 三份交给模型。一页约 24 KB 的结果因此超过 20000 字节上限，中间被裁掉；这次关键元数据碰巧在保留的头尾里。

已排除：日期过滤正确（开始包含、结束不包含，按 +08 解释）。

未确认：11-09 之后是否有同步尝试失败。见下方现场核对第 1 项。

## 修复

插件（`yeogirlyun/gshopper-ryn-node`，`integrations/company-app`）：
- Agent 发起新查询和查覆盖时，先请求一次本账号的增量同步，与页面共用同一个后台任务（去重），最多等 15 秒，失败后 15 秒退避。翻页沿用第一页的边界，不刷新。
- 每个结果带机器可读的四项状态：快照（`snapshot_verified`）、源一致（`freshness.source_consistent`）、日期覆盖（`date_coverage.state / indexed_through`）、同步（`freshness.sync_state`：current / refreshing / failed / stale / source_unavailable，含失败码）。`verified` 只有在快照完整且请求的日期已覆盖时才为 true；`query_complete` 和 `incomplete_reasons` 汇总未查全的原因。
- 每页消息预算 24000 → 12000 字节；读单条正文 12000 → 4000，前后文 10000 → 8000。整页加状态信息不超过 Ryn 的单结果上限，不会被裁剪。代价是每页约 10 条短消息。
- 技能和工具说明补充取证规则：四种状态（已确认 / 附件待核验 / 未查全 / 明确未提供）、文件下载或修改时间不是发送时间、旧消息不当成现状、`query_complete=false` 时不得说没有。旧技能文本加入 `previous_defaults`，已安装的副本可以原地更新。

Ryn（本仓库）：
- 与 Codex 0.165.0 的 `CallToolResult::as_function_call_output_payload` 对齐：成功且带非空结构化内容的 MCP 结果，只把结构化内容交给模型，去掉重复的 MCP 文本块。图片、保存的文件、嵌入资源和下载提示保留；出错结果不变。截断算法、上限和通用提示词均未改。

已核对现有插件不会因此丢信息：企业微信工具的文本就是 `data` 的 JSON；Dashboard 的文本表格由结构化 `display` 生成；身份工具的文本只是一句说明。

## 验证（VM，合成数据）

- Ryn：`tests/test_agent_mcp_structured_only.py` 5 项，其中 3 项在修改前失败；MCP、扩展、插件视图相关的现有测试全部通过。
- 插件：`tests/test_wecom_agent_freshness.py` 7 项，覆盖报告第 7 节：索引过期时首次 Agent 查询自动刷新；刷新慢时可见进度且有界；源未变时不误报过期；刷新失败可诊断、可重试，且不报成完整；并发查询只刷新一次；账号隔离；日期边界与 UTC 等价；每页和读消息都在 Ryn 上限内；旧游标不吸收新数据。
- 插件：`tests/test_wecom_answer_evaluation.py` 和 `tests/wecom_answer_cases.json` 定义 6 类回答评测：混合历史与最新、扫描件无文字、只有缓存文件时间、索引过期返回空、同名群、has_more 未结束。每类都有正例和反例，按证据状态和引用的消息时间判定，而不是按固定措辞。`scripts/evaluate_wecom_answer.py` 也可以给现场真实回答打分。

## 现场核对（只读，由宿主 Codex 通过已有阿九 SSH 技能执行）

不发送企业微信消息，不关闭认证，不复制凭据。

1. 安装前，查插件服务日志里 2026-11-09T08:50Z 之后的 `wecom_sync_failed`：次数和 `error_type`。
2. 安装前后各记一次：本账号 `wecom_local_messages` 的 `COUNT(*)` 和 `MAX(timestamp)`；`wecom_local_sync_state` 的 `last_sync_at / last_sync_mode`；源 `message.db-wal` 的大小和修改时间。
3. 安装后，不打开插件页面，让 Agent 按原问题查同一日期范围（2026-11-10 到 2026-11-12），记录结果里的 `query_complete`、`freshness`、`date_coverage`，以及是否用了 `next_cursor` 和 `wecom_read_message`；再用 `scripts/evaluate_wecom_answer.py` 按最接近的用例给回答打分。
4. 主程序和插件的版本、健康状态、插件服务是否在线。

## 未验证

- VM 上没有真实模型，也没有现场企业微信数据。现场恢复闭环（第 3 项）完成前，不能说问题已在线上解决。
- 模型是否每次都遵守取证规则，只能靠现场评测观察。
- 插件 develop 无法从 VM 推送到私有仓库，需要宿主推送。
