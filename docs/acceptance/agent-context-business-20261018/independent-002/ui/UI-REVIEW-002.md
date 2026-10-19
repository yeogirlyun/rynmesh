> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`b2b4f7adc1fe69f33184150e7e20858a5ef1eff8a6d5e1c1169d240227d97881`。

# Independent UI / retained settings review 002

结论：**PASS（本片段范围）**。首轮 IR1-U01 已关闭。实际执行 **46 个 distinct cases，46 passed，0 failed**；Agent UI 另有 68 项因定向筛选而 skipped，不计入已测数。此片段不为容量、usage、财务或真实办公文件单独签署整体结论。

只读检查生产代码；所有新增探针、配置、报告在本 `ui/` 目录。没有修改生产或原有测试，没有读取真实 owner 配置、凭证和旧聊天，没有发送外部模型请求。HTTP 测试使用新建临时节点和测试密钥，Agent 测试使用真实加密 store、fixture model 和 fixture tools；模型 complete 未执行。原 `independent-001/boundary-ui-probe.test.tsx` 直接原样复跑，未放宽断言。

| 检查 | 独立结果 |
| --- | --- |
| 来源 12 / 10000.5 / 2000001 | 实际打开来源弹窗，输入各值；保存与保存并测试均 disabled，无 PUT/测试请求。纠正为 75000 后可保存且发送整数/manual。原首轮 12/10000.5 断言现均通过。 |
| 输出及 auto 闲置字段 | 0、4096.5、256001、手动上下文60000下输出60001均禁用两保存按钮。auto 模式不受闲置32768数值卡住，128000输出可发保存请求；此 UI 测试不替代后端 metadata 容量验证。 |
| 后端严格数值 | 经真实 FastAPI 来源 PUT 路由测试12种非法 context/output（含整数外观 float、bool、小值、超限和小数字符串）；全部 HTTP400，已保存来源完整公开记录保持不变。源码 `sources.py:107` 仅接受 `type(value) is int` 或保留兼容的十进制整数字符串；不再 `int(float)` 截断。 |
| 旧来源 | 原首轮 control 测试证明无 mode 的 60000 保持手动且未伪造新mode；显式勾选 auto 才发送auto，并展示未知/保守回退提示。 |
| Agent auto/manual | 原UI测试实际从null进入manual，3000禁保存、250000保存，再明确切回auto发送null。原未知容量UI测试保持“unconfirmed”提示，未显示成已知32768模型窗口。 |
| 旧值/旧会话权限 | 自有探针分别迁移120000、600000，保留 `legacy_ambiguous_manual`、消息、limits、read_only、scope、approved、pending_approval、confirmed receipt；二次迁移完全幂等。只更新context null或600000不改任何已有task，也不改permission_mode/allowed_write_tools/workspace_root/desktop_access。 |
| 无效Agent设置不写入 | 自有12、10000.5、2000001、600000.0、true均抛agent_invalid_settings；整个加密store逻辑状态保持不变。原UI安全重启测试保留既有task记录的read_only权限。 |

源码核对：`AISources.tsx:82` 的整数/范围关系、`:91` save guard 和 `:170` 两按钮禁用相互一致；`RynAgent.tsx:835-840` 以null/整数判auto/manual，manual校验4000..2000000 UTF-8字节，仅发送明确变化；`runtime.py:702-703` 后端使用确切整数类型，`:714-717` 保留owner_auto/manual provenance；`state.py:86-98` 不按120000/600000数值猜自动意图。上下文设置改变本身未走permission_changed分支。

## 实际运行与计数

| 最终日志 / XML | 用例 |
| --- | --- |
| `original-ui-repro.log` / `.xml` | 4 passed，原001全部来源探针原样复跑。 |
| `independent-ui-final.log` / `.xml` | 8 passed，自有来源UI探针。 |
| `existing-source-ui.log` / `.xml` | 10 passed，实际运行AISources.test.tsx 9项 + aiSources.context.test.ts 1项。 |
| `existing-agent-ui.log` / `.xml` | 3 passed / 68 skipped，定向manual context limit、unconfirmed model capacity、recorded permission mode。 |
| `backend-independent.log` / `.xml` | 21 passed：12种来源HTTP非法值、2种旧手动迁移、2种context改动不碰权限/任务、5种Agent非法值。 |

`independent-ui.log/.xml` 为早期组合配置尝试：自有8项通过，但旧目录测试入口被Vite文件范围拒绝，0项旧测试执行；这是harness故障，不归为生产失败，也不重复计入8项。随后原001配置直接运行旧4项成功；自有配置仅保留本目录后最终8项成功。未扩大全仓测试，没有使用先前开发通过数加总。

`reviewed-file-hashes.json` 记录7个生产文件、3个原有测试和原001探针SHA256。7个生产文件及3个原有测试均与父审查 `independent-002/source-start.json` 一致。`original-probe-stability.json` 另核对原001 `evidence-index.json` 与当前原探针SHA256，证明原探针自001归档至本次原样复跑后未变。此子审查没有首次全源码快照职责；最终全部679项前后冻结核对由父审查汇总。

限制：UI为JSDOM + MSW，证明控件行为与请求payload；来源后端测试独立经过真实FastAPI保存路由。无真实桌面浏览器、真实凭据或真实模型metadata在线请求。保留旧整数数字字符串兼容；不把未知模型回退、估计token/lifetime或费用称为严格actual hard bound。未发现本片段未关闭缺陷。
