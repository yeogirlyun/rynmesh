> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`fa1f87a7974a7c9fb522c4386dd154c74ac34b35c913aaad11a989074b7238b0`。

# 上下文跟随模型与办公业务文件 独立验收 002

结论：FAIL

签署于 **2026-10-18 17:03（Asia/Hong_Kong）**。首轮6项源码问题的原始失败探针现已通过，但本轮独立发现 **2项仍阻断完整合同的缺陷**：OpenAI来源估算用量仍被API/账务当作actual，以及metadata读取路径观察容量A→B→A后旧destination恢复有效。最终三份办公成品通过有范围限制的业务文件核验。

本审查先读 `INDEPENDENT-REVIEW-001.md` 和原探针，再读取当前冻结dirty源码、实际复跑测试并自写探针。原 `test_capacity_edges.py`、`test_budget_independent.py`、`boundary-ui-probe.test.tsx` 的SHA256均与001归档完全相同，未改预期、未加xfail。生产源码和已有测试未由本审查修改；未reset/clean/stage/提交/部署，未重新付费推理、未读取或操作真实owner配置/凭证/旧聊天，未创建更多用户聊天或向其他用户聊天发消息。

## 未关闭项

| ID | 严重度 | 实际触发及结果 |
| --- | --- | --- |
| **IR2-B01** | P2 | 真实 `OpenAICompatibleAdapter` 接收本机HTTP fixture的缺失/partial/0 usage，`adapters.py:250–251,377–385,535–536` 仍合成顶层计数；`chat.py:211–212`、`api.py:311–313`、`routes.py:689–695` 把它们当actual。三个兼容API各stream/nonstream的6例均暴露合成计数，output quota由reserve30退成12。真实加密Consumer订单实测425/3、amount=.001、priced；consumer **signed**账本hold后settle，held=0，发送1次settlement，Provider账本新增earning=.001。应保留unknown/null、pending_usage、原hold和quota reserve。重复请求仍只有1次infer，不能以幂等通过抵消错误结算。 |
| **IR2-C01** | P2 | `cli_binding.py` 只在 `_binding_for_models()` 比较signature并更新随机世代；`current_models()`/`context_metadata_for_model()` 接受刷新结果时不记世代。认证真实`GET /v1/models`已先后观察model-a从400000变16000再回400000，中间不查询binding；最终旧binding逐字段复用，生产`AgentModelBridge.complete(expected_destination=旧A)`实际调用fixture adapter 1次。违反已观察容量变化后旧绑定不得复活的合同。**当前已经回到400000，本探针未证明仍为16000时越界，也未证明账号权限绕过。** |

IR2-B01有13个失败断言，IR2-C01有2个；分别归并为1个源码问题。所有上游、节点、密钥、推理回调和账本均为隔离fixture，不涉及真实收费或真实资金。Consumer闭环的Provider账本为standalone（ledger_backed=false），不能冒称该例也测了Provider signed ledger；nullable signed ledger重放由另一聚焦组覆盖。

直接复验与细节：[`usage/USAGE-REVIEW-002.md`](independent-002/usage/USAGE-REVIEW-002.md)、`usage/{own,chain}.junit.xml`、`usage/{own,chain}-summary.json`、`usage/test_usage_independent_002.py`；[`capacity/CAPACITY-REVIEW-002.md`](independent-002/capacity/CAPACITY-REVIEW-002.md)、`capacity/independent.junit.xml`、`capacity/test_capacity_independent_002.py`。没有替主开发修复实现。

## 已确认关闭及通过的范围

| 要求或首轮问题 | 本轮独立结果 |
| --- | --- |
| IR1-B01 Codex缺失usage被合成 | **关闭，限Codex路径。** 原unknownusage测试原样通过；absent/malformed/partial/0、Bridge→runtime、store重开通过。不能外推到OpenAI适配器，见IR2-B01。 |
| IR1-C01 auto被闲置32768校验误拦 | **关闭。** 已保存1M metadata来源在auto下接受128000输出；未知/小窗口仍按有效窗口校验。 |
| IR1-C02 容量变更不转binding | **原触发关闭。** catalog/绑定查询感知中间状态时旋转精确模型世代，兄弟不误失效；仅metadata读取的ABA遗漏见IR2-C01。 |
| IR1-C03 selected CLI缓存不遵守TTL | **关闭。** selected/default遵守TTL；持续缩到16000、刷新失败和空listing均fail closed，旧大窗口destination被阻断。 |
| IR1-C04 /v1 CLI默认用旧manifest容量 | **关闭。** 认证API使用installed默认模型容量；大窗口接纳、缩小后拒绝。caller伪造context_window/model_metadata不能替代真实metadata，实际dispatch=0。 |
| IR1-U01 来源小值/浮点PUT与截断 | **关闭。** 原12/10000.5 UI探针原样通过；2000001也禁用两个保存按钮且不PUT。后端非法float/bool/小值/超限均400且不改记录，允许旧十进制整数字符串兼容。 |
| Agent auto/manual与旧状态 | null=auto，整数=manual UTF8字节；新默认不受旧600000B顶帽。旧120000/600000保留legacy_ambiguous_manual，不猜用户意图；迁移幂等，已有会话消息、批准、回执、read_only权限及task limits不变。 |
| 容量来源与请求预算 | authenticated listing/endpoint+credential缓存隔离、每模型owner override、manual/auto和未知fallback标记通过；完整messages/tools按ceil(UTF8 bytes/3)+512估计，预留完整output。passive signed catalog冷启动/过期不启动CLI discovery。 |
| Codex/nullable fixture usage新链路 | 三兼容API stream/nonstream保持null；output缺失保留quota；Provider/Consumer成功但pending_usage保留hold；重复请求及running/succeeded恢复不重复infer/hold/charge。own/free可0价且basis=own_request，token仍unknown。standalone/signed nullable ledger重开、snapshot丢失重建、rebuild/去重与旧knownusage结算均通过。 |
| Ryn应用与批准接线 | 业务批准策略、Agent精确批准合同、App真实HTTP接线共5/5通过；业务结果来源另由真实模型响应、Ryn提案、批准、回执和文件哈希核对。 |

详细UI范围为 [`ui/UI-REVIEW-002.md`](independent-002/ui/UI-REVIEW-002.md)。测试是JSDOM/MSW及真实FastAPI路由的隔离执行，不声称本轮使用真实owner浏览器配置。

## 真实业务成品

独立核对原result/snapshot/Bridge记录、批准及宿主保存回执，重新解析最终文件并查看全部最终PNG。5次尝试合计 **45次模型调用、81个提案/81个配对结果、77次confirmed宿主执行、39次精确批准、2次额外人工业务纠正**；4个not_executed不算host dispatch。GridCraft18、WordCraft7、PdfCraft35工具，复制native executable哈希仍与preflight一致。最终成品来自真实Ryn Agent调用现有Craft应用，checker没有代写业务文件。

| 最终成品 | 独立结果与SHA256 |
| --- | --- |
| XLSX，4846B | **PASS**，Gemini第二次人工纠正后5轮/19工具/8批准修复；15个精确公式、输入、缓存结果和格式正确。`bd907d8e0b58c0070a2599176f1e2e09624cc9a4b9b22e649ad87b4711824869`。 |
| DOCX，6999B | **PASS，保留渲染兼容限制**；GLM9轮/3批准，OOXML正文、产品和业务数字完整，另进程WordCraft重开/渲染。`8ae130de8360ec528b6a891875252a76bce806ac6314ce60c75266f6b63ac650`。 |
| PDF，42164B | **PASS**；GLM9轮/5批准，输入逐字节等于Word输出；真实PdfCraft添加SIMULATED DATA。独立pypdf/PDF.js重开和一页渲染正确。`8f91e0fae1e79c82cde20d13c8dae7cc58989e0bcae53779c4162239b2b485e7`。 |

独立复算销售5520、成本3380、毛利2140、费用500、利润1640、毛利率2140/5520。真实GridCraft副本把North数量12改13后，合计变5670/3470/2200、利润1700、毛利率2200/5670；副本未保存、原件哈希未变。Artifact Tool只读导入/计算/渲染，无XLSX导出。Word/PDF轻微单字续行，无缺字、遮挡或截断；本轮标准`render_docx.py`仍因缺LibreOffice soffice.exe失败，同WordCraft引擎另进程不等同MS Word/LibreOffice跨引擎兼容验证。

首轮Excel12轮及GLM修复10轮仍为**2次失败**：已保存文件有错误公式/覆盖输入，并到step limit；没有把最后未保存内存修改算成成品。Gemini成功不回写这两次结果。

业务生成冻结在第一批各295生产Python快照，Gemini逐项复用相同生产快照，均与原snapshot磁盘hash匹配。当前源码相比业务快照有**10个生产文件差异**，涉及models、CLI绑定/缓存、sources和usage/API/账务；办公node_apps及native引擎未变。**没有在最终修复hash下重新付费跑完整业务生成。** GLM业务为owner model override 1M/输出4096，Gemini为明确unknown fallback32768/输出4096，不当作大容量模型metadata声明或1M-token真实推理证据。详见 [`BUSINESS-REVIEW-002.md`](independent-002/BUSINESS-REVIEW-002.md)、`business-audit-002.json`及`native/`、`xlsx/`、`pdf/`。

## 实际测试计数与冻结

| 正式最终组 | 唯一执行case | PASS | FAIL |
| --- | ---: | ---: | ---: |
| 容量：原5例及focused77、自有6 | 88 | 86 | 2 |
| usage：含原budget8的focused120、自有12、consumer闭环1 | 133 | 120 | 13 |
| UI/旧设置：原UI4、自有UI8、来源10、Agent定向3、后端21 | 46 | 46 | 0 |
| 应用/批准 | 5 | 5 | 0 |
| 合计 | **272** | **257** | **15** |

`independent-002/case-manifest.json`由11份正式JUnit独立汇总，classname+name无重复；Agent另68项因定向选择skip，不计执行。上表不混入5次真实业务尝试、不加总开发189/193或初批244、UI78等可能重叠组。环境启动、旧pytest-current清理权限、fixture初始签名/health、相对QA路径和探索错误文案日志均保留，正式复跑已排除harness故障；不把开发旧诊断/计时失败改称全仓绿。没有扩大全仓测试。

独立验前/验后 **295生产Python+384webapp源文件共679/679一致**，changed=[]、added=[]。`source-start.json`和`source-end.json` SHA256均为 `9c9b3624eea34932f2a453a6634754b204ba07216e53891d8838227b1d2f2160`。production fingerprint为 `03f2a97dc0ac69847d58f905dc4b4a2713277c7d70be66eead5320ed1878c815`，逐项匹配开发最终冻结manifest。归档 `independent-002/reviewed-production-source.zip`：2264893B，SHA256 `d059c8520e656b763722da22e45dbd0166a6ccec8e8028182d4770d3dab54ae6`，每个成员hash再核对一致。此归档包含受核公开源码，不含owner状态、凭证或旧聊天。

## 范围限制

正常Agent的token估计与lifetime准入是heuristic reservation，实际报告可高于估计；不是精确tokenizer或费用actual hard bound。未知容量以明确32768窗口/120000B fallback规则处理，不编造模型容量。source draft `_model_probe`仍旧估算口径，不称所有入口已统一。HTTP16MiB/P2P32MiB及真实seal/sign/chunk边界沿用001既有独立证据，本轮未再大推理或重复计case。pending_usage尚无后续补用量/清算平台，本报告不要求或宣称已新建该平台。

完整合同需关闭IR2-B01与IR2-C01后再对新冻结快照复验。本报告只签署上述冻结快照，不提前追认后续修改。
