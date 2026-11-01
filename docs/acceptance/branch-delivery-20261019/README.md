# 2026-10-19 推送前验证证据

完整判定、失败与范围见 [验证记录](../../changes/2026-10-19-verification.md)，功能说明见 [交付说明](../../changes/2026-10-19-upstream-delivery.md)。

本目录只提交通用测试日志、源码测试选择和构建输出；本机仓库路径已规范化。不包含 pytest 临时节点、密钥、数据库、用户会话、工作簿原件或安装包。历史独立模型与安装验收不由本轮离线自动化重判。

后端按唯一节点统计为 1,090 passed、3 skipped、0 未关闭失败。前端日志保留初跑的唯一超时及不放宽断言的复跑；不同批次的重复用例不相加。

| 范围 | 证据 |
| --- | --- |
| 后端唯一节点汇总与排除项 | [汇总](backend-validation-summary.json)、[节点表](backend-validation-nodeids.json)、[63 文件选择](backend-scope.txt) |
| 前五个完整后端文件 | [130 项结果](backend-first-five.log)；中断的 browser 结果未计入 |
| priority 修复前 / 后 | [保留初始失败](backend-priority-initial-failures.log)、[最终完整复跑](backend-priority-final-tests.log) |
| runtime / process | [runtime 含旧夹具失败](backend-runtime-tests.log)、[process](backend-process-tests.log) |
| 定向修复与筛选补漏 | [14 项修复](codex-repair-tests.log)、[7 项补漏](backend-selection-supplement.log) |
| 前端主组 / 超时复跑 | [初跑](frontend-focused.log)、[Sending 原断言复跑](frontend-sending-recheck.log) |
| 本地化初跑 / 修复 | [保留失败](frontend-localization.log)、[修复复跑](frontend-localization-recheck.log) |
| Node 脚本及入口串联 | [native](frontend-native-test.log)、[npm test 参数 smoke](frontend-script-smoke.log) |
| 最终构建与 lint | [生产构建](frontend-build-final.log)、[Ruff](ruff-all-final.log) |

对应 JUnit XML 同目录保留，可按 `(classname, name)` 去重核对；后跑的相同节点结果覆盖先前结果。前五个完整文件从中断批日志提取，因此单列其 130 项。XML/日志已规范化本机路径，原始文件本机留存。

常规复现入口：

```powershell
.venv/Scripts/python.exe -m ruff check rynmesh/ tests/
# 后端按 backend-scope.txt 选择 tests 中的文件；本轮排除 reference 和桌面用例。
# Windows 本轮使用单目录收集与临时选择 hook，避免多参数路径比较。
cd webapp
npm run test:native
npx vitest run --pool=threads --maxWorkers=3 <对应前端文件>
npm run build
```

`npm test` 已同时串联 native 和 Vitest。threads/worker 参数仅是本轮执行选择，没有写成项目默认配置。
