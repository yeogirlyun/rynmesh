# 模型上下文与办公业务验收记录

本目录收录 2026-10-18 的三轮独立源码验收及后续本机安装版验收，2026-10-19 整理为公开交付副本。
报告中的源码/安装包 hash 和测试计数绑定各自历史快照，不自动证明后续源码或本次上传重新验收。

## 实际改动

- Agent 以 `max_context_chars=null` 自动跟随当前模型窗口；整数仍是 owner 选择的 UTF-8 请求字节上限。
- 完整 messages、工具 schema、输出及 framing 共同参与预算；保留旧数值来源，避免按 120000/600000 猜测用户意图。
- 完善来源容量发现、CLI 确切模型/default 元数据、TTL 和 destination binding，已观察容量变化不能复活旧绑定。
- 缺失、部分或无效 usage 保留 unknown；真实零用量保留零。API、预算和账务不以字符估算冒充 actual。
- 未知用量订单保留原 quote hold 和 pending_usage；现有应用与批准路径继续支持真实 Excel、Word、PDF 任务。

## 最终验收与历史失败

[第三轮独立验收](INDEPENDENT-REVIEW-003.md) 对当轮完整合同判定 PASS：正式去重 430 项，428 PASS / 2 FAIL。
两项 FAIL 是原探针对默认底价的错误金额预期，原断言和失败记录保留；实报 0/0 不等于必须取消既有底价。
第三轮另用默认底价、显式零底价和授权 ceiling 的独立探针确认实际价格合同，未将原探针改绿。

[第一轮 FAIL](INDEPENDENT-REVIEW-001.md) 发现用量、CLI 容量/缓存、API 容量和来源输入校验等六项缺口；
[第二轮 FAIL](INDEPENDENT-REVIEW-002.md) 继续发现 OpenAI 估算计费和 metadata-only 容量 A→B→A 绑定复活。
第三轮分别复验关闭；历史 FAIL 结论保留，测试计数不跨轮相加。[底价测试说明](REVIEW-002-TEST-NOTE.md) 是开发说明，独立判断以第三轮报告为准。

业务最终三份成品通过有范围限制的核验，Excel 首次及 GLM 修复两次失败仍保留。
五次业务尝试合计 45 次模型调用、81 对提案/结果、77 次 confirmed 宿主执行、39 次精确批准及两次额外人工业务纠正。
成品生成来自较早冻结快照，后续容量/用量修复由聚焦回归桥接，没有在最终全部 hash 下重新付费生成。
详见 [业务复核](independent-003/business/BUSINESS-REVIEW-003.md)、[用量复核](independent-003/usage/USAGE-REVIEW-003.md)
及 [容量复核](independent-003/capacity/CAPACITY-REVIEW-003.md)。

[本机安装版独立验收](INDEPENDENT-INSTALLED-REVIEW-001.md) 另行确认对应安装身份、监测范围内的数据/设置/授权保全和最终办公结果 PASS。
首次安装身份 guard、首次 Word 业务失败及后续人工干预均保留，不能写成一次无异常自动升级或全程无人介入。
安装版与源码验收口径独立，旧 430 项没有在安装验收中重复计数。

## 范围边界与公开内容

- token 准入为明确标注的启发式预留，不是精确 tokenizer 或 actual token/费用硬上界；未知容量 fallback 不是模型声明。
- CLI freshness 受 TTL 约束；本批没有新增 pending_usage 的补用量/后续清算平台，也未做真实百万 token 推理。
- Word 视觉检查来自 WordCraft 独立进程；缺少 LibreOffice 的标准渲染失败保留，未证明 Microsoft Word/LibreOffice 跨引擎兼容。
- 保全检查限定记录的文件与语义范围，不等于逐外部服务重新登录，也不宣称全仓测试全绿。
- 本目录 `.gitignore` 仅公开报告及说明；机器专用 harness、raw 状态、日志、业务/源码归档在本机留存。
- 公开报告保留证据名称与 hash，完整可维护自动化以仓库根 `tests/` 为准；后续源码变化需重新验证。
