> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`3197265857f7435d5d829dcbfe2a81ba09ad17a36746b62146cc74f234e8ec85`。

# 上下文跟随模型与办公业务文件：独立验收 003

结论：PASS。本轮冻结快照在已约定的完整本批合同范围内通过；IR2-B01、IR2-C01 已用原始探针及新增反例确认关闭，未发现仍未关闭的产品缺陷。正式测试去重 **430 case：428 PASS / 2 FAIL**；两项 FAIL 是原002探针对现有默认底价的错误金额预期，以下单列判断，不冒称原探针全绿。

签署于 **2026-10-18 17:25（Asia/Hong_Kong）**，只签署下述指纹和归档，不追认未来修改。先读001、002两份独立报告，再审当前dirty源码、复跑必要聚焦测试和新增独立反例；不以开发summary代替测试。001、002的FAIL报告、原探针及原失败证据均原样保留。没有修改生产源码或已有测试，没有reset/clean/stage/提交/部署、真实owner认证/配置/旧聊天读取、新收费推理、新用户聊天或向其他用户聊天发消息。新增文件限本报告及 `independent-003/` 证据；Vitest初次把JUnit写到webapp/docs的新临时位置，随后已移入该证据目录。

## 两项阻断问题的复验

| 项目 | 本轮判断及实际证据 |
| --- | --- |
| IR2-B01：OpenAI字符估算冒充actual并结算 | **关闭。** 原002兼容API stream/nonstream、Provider缺失/partial与Consumer闭环均已满足unknown合同；原零用量两例先通过0/0计数断言，再在错误底价预期处失败。新独立探针通过真实loopback OpenAI适配器、真实API路由及seal/sign订单，验证缺失/坏类型/partial usage、API output quota、默认底价/显式零底价/授权ceiling。Provider和Consumer均接signed ledger的3例，实际成功但amount=null、pending_usage，原quote hold逐字节事件不变；重复请求、status/list和两次启动恢复不再infer、不追加hold、不settle、不earning，infer=1、settlement=0。 |
| IR2-C01：metadata-only容量A→B→A复活旧destination | **关闭。** 原002两例原样通过：metadata-only及认证GET /v1/models实际观察400000→16000→400000，中间不读binding，旧destination不再相等且生产Agent Bridge实际chat=0。自拟反例还覆盖known→unknown→known跨adapter重启、同容量仅default resolved ID A→B→A、配置CLI的models和tools probe HTTP路径观察、落盘失败与恢复；旧默认/确切模型destination不能派发，未变兄弟保持有效。显示名/排序/重复读取不会无端旋转世代或重写snapshot。 |

usage源码核读与实际复验包含：`reported_usage`按字段存在优先，None/非dict/部分None不回退顶层；无marker的可信legacy adapter仅收非bool的非负int；OpenAI infer/chat/infer_stream不合成actual，Claude decoder缺失保留unknown且实报0保留。Bridge、completion、三个兼容API、quota、Provider/Consumer、本地CLI订单、sealed返回、公开status/retry/list及恢复链路按该语义执行；流式仅输出canonical final usage，坏上游usage事件不能替代最终统计。own/free的金额0明确basis=own_request，tokens仍可unknown。有完整actual沿用既有底价及已授权quote ceiling。

容量源码核读与复验包含：成功非空listing接受前，单次file_transaction记录整份default/确切模型signature，仅变化者转随机世代；成功listing里的消失tombstone、重现、default resolved ID切换、兄弟隔离和重启持久化通过。冷/过期passive catalog不启动CLI discovery；TTL发现失败、空listing、落盘失败不能发布旧authority或放行旧destination。

完整细节及可复跑脚本见 [USAGE-REVIEW-003.md](independent-003/usage/USAGE-REVIEW-003.md)、[CAPACITY-REVIEW-003.md](independent-003/capacity/CAPACITY-REVIEW-003.md)，对应正式JUnit与机器证据均在同目录。

## 原002的两处底价测试瑕疵

原 `independent-002/usage/test_usage_independent_002.py` 本轮实际执行 **13例=11通过/2失败**，没有改断言、xfail或删除。`zero-infer`、`zero-chat` 的实际0/0、usage_complete和priced均已正确，最后仍要求amount==0，而fixture没有配置minimum。独立读取002已归档源码ZIP确认：当时 `Pricing.minimum=0.001`，原定价函数已经是 `max(minimum, token_price)`；所以金额0.001符合已有合同，取消底价才会改变产品要求。这一判断有旧冻结源码依据，不仅依赖开发说明。

本轮新增的6个真实adapter/sealed价格case用字面金额作期望：infer/chat分别验证默认minimum时0/0→0.001、显式minimum=0时0/0→0，以及完整actual受已授权quote ceiling限制。6/6通过。证据为 `usage/prior-price-contract.json`、`usage/own.junit.xml` 和原失败的 `usage/original-002.junit.xml`；原两份FAIL报告的历史结论不回写。

## 本批其他合同与回归

| 合同 | 当前证据及边界 |
| --- | --- |
| Agent自动容量与手动UTF-8限额 | nullable auto；新默认无600000B或2MB自动顶帽；手动仍为4000..2M整数UTF-8字节。正常Agent完整messages/tools按ceil(UTF8 bytes/3)+512估计并预留完整output，真实大于600k历史、小窗口阻断和实际循环已用隔离fixture复验。 |
| metadata/未知fallback/来源设置 | 已认证listing、endpoint+credential缓存隔离、确切模型override、自动/手动来源和UI非法值阻断通过。未知模型明确unknown_fallback，使用32768窗口/120000B保守规则；不把它当模型声明。 |
| 旧设置与授权 | 旧120000/600000保留legacy_ambiguous_manual，不按值猜意图；可明确切auto。迁移幂等、旧task limits/消息/批准/回执/read_only权限不静默改变；后端21例及store重开通过。 |
| Agent预算、应用及批准 | 完整序列化、全output预留、actual/unknown预算持久化、精确批准和真实应用HTTP接线通过；业务批准策略2、Agent批准2、App HTTP1共5例通过。没有以checker代替业务文件写入。 |
| 原001六项 | 原容量5、预算8和来源UI4探针均原样通过；与002已关闭结论衔接。原始001/002关键7份探针hash均匹配其既有归档索引。 |
| 传输边界 | 沿用001已独立通过的HTTP16MiB/P2P32MiB及真实seal/sign/chunk边界证据；本轮订单仍实际运行seal/sign，但未重复百万token收费推理。 |

## 真实Excel、Word、PDF业务来源

本轮重新只读解析三份最终成品、核对保存回执和原始result/snapshot/Bridge/提案/精确批准/宿主结果，复看002归档的全部三张最终PNG。三文件解析前后hash不变；未新运行模型、重渲染、native dispatch或写业务文件。

| 成品 | 本轮独立结果与SHA-256 |
| --- | --- |
| XLSX，4846B | PASS；15精确公式、输入、缓存值、格式及无错误单元格通过。`bd907d8e0b58c0070a2599176f1e2e09624cc9a4b9b22e649ad87b4711824869` |
| DOCX，6999B | PASS，保留渲染范围；OOXML正文、产品、模拟/期间/币种及业务数字完整。`8ae130de8360ec528b6a891875252a76bce806ac6314ce60c75266f6b63ac650` |
| PDF，42164B | PASS；pypdf独立重开一页，中文/数字/SIMULATED DATA完整，输入逐字节匹配Word输出。`8f91e0fae1e79c82cde20d13c8dae7cc58989e0bcae53779c4162239b2b485e7` |

独立由输入复算销售5520、成本3380、毛利2140、费用500、利润1640；002已实际执行的GridCraft副本North数量12→13，结果为5670/3470/2200、利润1700，副本未保存，原件不变。本轮复核该旧证据，不冒称本轮再次引擎重算。

5份原始运行重新配对后仍为 **45次模型调用、81提案/81配对结果、77次confirmed宿主执行、39次精确批准、2次额外人工业务纠正**；4个not_executed不算dispatch。Excel首轮GLM12轮和修复10轮两次失败保留，最终Gemini5轮/19工具/8批准成功；Word/PDF各GLM9轮成功。002业务分报告Markdown逐任务表的Word及GLM修复计数有叙述瑕疵，本轮按原始result更正为Word9提案=8confirmed+1not_executed、GLM修复22提案=21confirmed+1not_executed；合计81/77/4不变，原档不改。

**业务真实生成属于首批295生产Python快照，并未在当前最终全部hash下重新付费生成。** 当前相对该快照有13个生产Python差异，涉及Bridge/models、CLI容量/绑定、来源及usage/API/账务；由本轮430个聚焦case桥接相关链路。18个node_apps源码不变，三native executable的源和复制件仍匹配preflight；没有完整历史payload目录manifest，不扩大为全目录证明。GLM当时为明确1M override、Gemini为unknown 32768 fallback，均非真实1M-token推理证据。

Word/PDF元信息末尾轻微单字续行，无缺字遮挡；标准render_docx缺LibreOffice的既有失败保留，同WordCraft另一进程渲染不等于MS Word/LibreOffice跨引擎验证。业务详情及来源更正见 [BUSINESS-REVIEW-003.md](independent-003/business/BUSINESS-REVIEW-003.md)、`business/business-audit-003.json`。

## 实际计数与冻结

| 正式最终组 | 正式唯一case | PASS | FAIL |
| --- | ---: | ---: | ---: |
| 容量：原001五、原002六、focused77、新metadata6、自拟7 | 101 | 101 | 0 |
| usage：adapter65+boundary34及Codex/ledger/API等聚焦回归 | 200 | 200 | 0 |
| usage：本轮独立真实价格/HTTP/双signed账本 | 13 | 13 | 0 |
| usage：原002不改探针 | 13 | 11 | 2 |
| Agent序列化/预算/输出/store、旧设置/权限、应用/批准 | 74 | 74 | 0 |
| 原001预算探针 | 8 | 8 | 0 |
| UI：原001四、原002八、现有来源九 | 21 | 21 | 0 |
| 合计 | **430** | **428** | **2** |

`independent-003/case-manifest.json`从7份正式JUnit按classname+name独立汇总，duplicates=[]、skip/error/xfail=0。容量初轮同101例有1个本轮自有harness异常白名单遗漏ValueError（实际已安全拒绝派发），只修本轮自拟probe后正式完整复跑101/101；初轮100pass/1harness失败和日志保留，不重复加总。3成品审计、5来源记录复核也不混入case数。开发201/281/旧189/193均可能与本轮重叠，不相加。未跑全仓，不改称初批244/1旧diagnostic或UI78/1计时组全绿。

验前和全部正式测试后的 **1058项守卫文件完全一致**：295生产Python、395个webapp/src文件、352个repository测试文件、14个既有独立probe/脚本及两份旧报告。395包含旧679口径里的384项webapp代码/测试及另11项SVG/资源说明。源/测试逐项匹配最终manifest，其中63项delta文件也全部匹配；changed=[]、added=[]。

- 295生产Python fingerprint：`c3ef74102482c31689716b6c9e5073cf9d017c6f29344a2bf552a084ddeacc12`。
- `source-start.json`、`source-end.json` SHA-256均为 `3defe789f07a8f3bd6ce4df9e8a1d114501af04ec57168401961a88baecaeb6e`。
- `reviewed-public-source.zip`：3436049B，1058成员逐一hash复核，SHA-256 `ba6ae0f52de66de4efbfd3ff51c058ed43a84bbfd715daa682fc4e4c4d222145`。
- 最终冻结索引来源：研究根 `work/context-business-final-delta/manifest.json` 与 `rewrite.patch`；开发usage summary最终SHA-256 `1b688cc7abac2743e81458dcf1bb5183b6f695903a377b91bc88e2b0d452dde2`仅作为交接索引。实际源码权威为本轮独立hash和归档。

完整机器清单为 `freeze-end-summary.json`、`original-probe-preservation.json`、`case-manifest.json` 和 `evidence-index.json`。归档只含公开源码、测试和既有probe/报告，不含owner状态或认证。

## 明确保留的范围

正常Agent的token估计和lifetime准入是heuristic reservation，actual可能超过估计；并非精确tokenizer或严格actual token/费用硬上界。source draft `_model_probe`仍旧口径，不称全部入口统一。metadata freshness受既有TTL约束，不保证上游实时变化立即可见。pending_usage尚无补用量/后续清算平台，本批未要求或声称新建。上述范围与业务旧快照、跨引擎渲染限制均保留；本报告不保证未测全仓及未来修改。
