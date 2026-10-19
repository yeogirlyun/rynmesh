> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`572ec4029617e4a99ba0b5ceb9fa69558afb728a326248cf2ea44d2e87b0588b`。

# 独立 usage 与账务验收 003

结论：PASS。**IR2-B01 在当前受核快照下关闭**；本轮没有发现尚未关闭的 usage 产品缺陷。原 002 探针原样复跑仍有 2 个失败，两者均为实报 `0/0` 后错误要求零金额，与既有默认底价 `0.001` 冲突；失败没有改成 xfail、删除或藏掉。本报告判断该价格断言有瑕疵，依据及独立替代合同如下。

本子审查先读取 001、002 和原始 usage 探针，再阅读当前实现，独立运行测试并新增反例。生产源码、原探针和既有 tests 未作修改。所有新文件均位于本目录；仅使用本机 loopback HTTP、隔离临时节点/密钥及开发模拟账本，未付费推理、启动真实 Claude/Codex、读取真实 owner 认证/配置/旧聊天、部署或向其他用户聊天发消息。没有适用的根/上级 AGENTS.md。

## 原 002 失败的实际复验及底价判断

`original-002.junit.xml`：**13 执行，11 PASS / 2 FAIL**，日志为 `original-002.log`。三个兼容 API 的 stream/nonstream 共 6 例现在均保留未知计数及 output quota reserve；Provider 的 missing/partial 4 例返回成功、unknown/partial 与 `amount=null/pending_usage`；真实 Consumer 缺失 usage 链路 1 例保留 hold、无 settlement/earning 且不重复 infer。它们共 11 例通过。

剩余原探针 `zero-infer`、`zero-chat` 均先通过 `(input_tokens,output_tokens)==(0,0)`，之后在原脚本第 143 行 `amount == 0` 失败：实测为 `0.001`、`billing_status=priced`。这是本轮才走到的后续断言；没有将它倒写为 002 当时已测通过的合同。

独立依据来自 002 已签署源码 ZIP，而非采用开发说明作为裁决：归档 `independent-002/reviewed-production-source.zip` 的 SHA256 为 `d059c8520e656b763722da22e45dbd0166a6ccec8e8028182d4770d3dab54ae6`，其中 `Pricing.minimum: float = 0.001`；当时 `_price()` 已经返回 `round(max(manifest.pricing.minimum, value), 8)`。原 fixture 没配置 minimum，因此 0/0 的已报告实际用量仍按该底价计费。详见 `prior-price-contract.json`。实报零应保留为零计数，不能据此擅自取消公开配置的底价。

本轮自写 `test_usage_independent_003.py`，真实 OpenAI adapter → sealed Provider infer/chat 各验证三个合同，共 **6/6 PASS**：

| 公开价格与上游报告 | 独立金额预期 | 实测 |
| --- | ---: | --- |
| 默认 `minimum=.001`，报告 0/0 | .001 | 计数0/0、usage_complete=true、priced、amount=.001 |
| 显式 `minimum=0`，报告 0/0 | 0 | 计数0/0、usage_complete=true、priced、amount=0 |
| 报告100000/100000，授权 `max_amount=.002` | .002 | 沿用授权 ceiling，amount=.002 |

预期值直接写自公开价格合同，不调用生产 `_price()` 作 oracle。每例重送同一密文结果完全一致、只 infer 1 次。

## 当前实现与额外反例

`adapters.py:19` 的 stdlib `actual_usage()` 先按 **key 是否存在**选取 reported_usage；显式 None/非 dict/部分 unknown 不回退顶层估算。未带 marker 的 legacy adapter 仅接受 `type(value) is int and value>=0`，排除 bool、负数、float、NaN/字符串。OpenAI infer/chat/infer_stream 和 Claude decoder 输出 reported/unknown，不再按字符合成 actual；0/0 不被 `or` 改写。

已复跑的新 adapter 65 例与 boundary 34 例合计 **99/99 PASS**。包括 malformed marker 对抗冲突顶层 120/80、各 OpenAI 入口与 Claude decoder 的 absent/null/empty/partial/zero/invalid，Bridge→Agent budget/store reopen，3 个 API × stream/nonstream 的 canonical final usage 与 quota，sealed old-peer、local CLI terminal metadata/retry/restart、pending checkpoint、status/list projection。API stream 只输出与 canonical completion 一致的最终 usage，冲突的上游 usage frame 不泄露出来。历史公开投影不回写既有审计历史。

独立新增 **4/4 PASS** 的真实 loopback API 反例（不是只 stub adapter 返回值）：

| 上游 usage | API actual | 预留30后的 output quota |
| --- | --- | ---: |
| null（JSON） | null/null | 30 |
| 非dict list（SSE） | null/null | 30 |
| `prompt_tokens=false,completion_tokens=5`（SSE） | null/5 | 5 |
| `prompt_tokens=7,completion_tokens=-2`（JSON） | 7/null | 30 |

部分报告只允许已实报的 output 决定 output quota；缺失 output 保留整个预留。未知总量为 null。SSE 两例都恰好 1 个最终 usage frame。

独立新增 **3/3 PASS** 的真实 adapter→加密 Provider/Consumer 链路，**Provider 与 Consumer 两边都是 signed ledger（ledger_backed=true）**，补足 002 原 chain 的 Provider standalone 范围：分别报告 null、仅 input=7、input=false/output=5。成功结果保留对应 unknown/partial，`amount=null,usage_complete=false,billing_status=billing_basis=pending_usage`。Consumer 原始 hold 均精确保留 `.001`，available=99.999，events 仅原1个 hold；Provider earning=0、events=[]。重送、status、list 和实际重新安装路由触发的两次 startup recovery 后，账本 summary/events 均完全不变；共1次 infer、1次 task dispatch、0 settlement、0 release、0二次占款。证据存于 `own-summary.json`。

此外独立复跑 Codex unknown/partial/zero/nullable 持久化 55 例、signed Task Balance 7 例、Antigravity 7 例、兼容 API 29 例及原 known-settlement/restart 3 例，均通过。覆盖 own/free 的 `amount=0,basis=own_request` 与 token unknown，standalone/signed nullable reopen/rebuild/dedup、8个 pending恢复组合，以及已有明确用量结算和普通中断释放。

关键实现接线已阅读：Bridge `bridge.py:50`，completion `chat.py:211`，API quota `api.py:311`、stream `api.py:359`，Provider `routes.py:704`，sealed返回 `routes.py:249`，local CLI `routes.py:2856`，Consumer `routes.py:3480`，recovery `routes.py:1062`，公开投影 `routes.py:3729`。对应文件及原探针 hashes 在 `read-source-hashes.json`。

## 本轮实际计数与快照

| 正式组 | 唯一执行 | PASS | FAIL |
| --- | ---: | ---: | ---: |
| 原002 usage probe（保持原样） | 13 | 11 | 2，旧零底价断言 |
| focused：adapter65+boundary34+Codex55+ledger7+Antigravity7+API29+known/restart3 | 200 | 200 | 0 |
| 本轮自写：价格6+真实API反例4+双signed pending链路3 | 13 | 13 | 0 |
| 合计 | **226** | **224** | **2，均为探针预期瑕疵** |

三份正式 JUnit 分别为 `original-002.junit.xml`、`focused.junit.xml`、`own.junit.xml`；每个 case 的 classname+name 无重复，见 `case-manifest.json`。没有 skip/xfail/error。各组只正式执行一次，没有探索失败后改探针重跑。focused 200 耗时185.47s；pytest 输出缓冲期间临时目录持续前进，不是挂住后中断或删掉的测试。此计数不加总主开发281、以前189/193/244或原验收中的重叠组，也不含真实业务生成。

总验收在执行前完成当前295生产Python fingerprint核对：`c3ef74102482c31689716b6c9e5073cf9d017c6f29344a2bf552a084ddeacc12`，逐项匹配最终冻结 manifest。全生产/webapp/test 的验前验后守卫由主独立报告汇总；本子报告的关键文件hash由本进程另外记录。原002 usage probe SHA256仍为 `e8ebd62153ab65850ae4db51288423d3bd232e6dcee83ca986bf26145c3bf41a`。

本结论仅关闭当前快照的 IR2-B01，不替代容量与真实办公业务审查。没有要求或证明新建补用量/后续清算平台；unknown仍待用量，未承诺自动补缴。Agent估算及累计准入是 heuristic reservation，不是精确tokenizer或实际费用硬上界。未重跑付费业务生成，不把最早业务快照冒称为最终源码下全套实跑；没有宣称全仓绿或预先签署未来修改。
