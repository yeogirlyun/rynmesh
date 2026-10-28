# 2026-10-19 开发分支验证记录

对应 [本次交付说明](2026-10-19-upstream-delivery.md)。验证在 Windows 开发工作区完成，使用项目 `.venv/Scripts/python.exe`（Python 3.12.10）、Node.js 22.22.1、npm 10.9.4 与已安装依赖；没有以测试通过替代生产、跨平台或真实提供商验收。

## 最终结果

| 检查 | 结果与范围 |
| --- | --- |
| Python lint | `python -m ruff check rynmesh/ tests/` 全部通过。 |
| 后端核心回归 | 按唯一 `(classname, name)` 去重后 **1,090 passed、3 skipped、0 未关闭失败**；1,093 个不同节点来自 63 个实际执行文件。保留各批初始结果与相同节点的修复复跑，不宣称同一次全仓运行。 |
| 前端重点回归 | 21 个文件、260 个不同用例都有通过记录；初跑 259 passed / 1 timeout，完整 Sending 文件原断言复跑 23/23 passed。不是同一次全组全绿。 |
| 本地化回归 | 3 个文件、11/11 passed；修复 3 条缺失中英文翻译及 PDF 预览无障碍标签。 |
| Tauri drop 协议脚本 | `npm run test:native`：纯 Node 用例 5/5 passed；不等于 Rust 编译或真实编辑器验收。 |
| 测试入口串联 | `npm test -- src/domain/agentFilePreview.test.ts --pool=threads --maxWorkers=1`：先运行 native 5 项，再运行指定 Vitest 1 项，exit 0。 |
| 前端类型与生产构建 | 最终 `npm run build`（TypeScript + Vite）exit 0。 |
| Git whitespace | 最终源码暂存检查及相对交付基线 `c74813d` 的完整交付检查通过。 |

实际日志与测试文件选择见 [本轮证据目录](../acceptance/branch-delivery-20261019/README.md)。回归批次之间的重复用例不累加，早期中断、收集和单独探针不计为新通过数量。

后端 3 项跳过分别为 POSIX 真实长命令 1 项、需显式启用的真实 Codex binary 2 项。原 64 文件核心选择中的 browser 文件共 14 项未完整执行，另有 2 项实际 reference 用例排除；它们不计入通过或跳过数。一次过宽的 `reference` 名称筛选另误排了 2 项普通参数/Schema 用例，已定向补跑并按唯一节点纳入，不重复加算其他复跑。

## 整理中修复的问题

- 对本轮新增和已有 Agent/CLI 文件做最小 lint 整理：导入顺序、等效局部函数、保留调用的未使用赋值清理、pytest fixture 导入声明及 `zip(strict=False)`。没有删除或放宽断言。
- 清理相对远端交付基线新增的 EOF 多余空行。历史验收的失败事实保持不变。
- GridCraft 固定 hash 的 `.patch` 保留原始字节及必要的单空格上下文行；仅该路径在 `.gitattributes` 声明 `-text` 和 `whitespace=-blank-at-eol`。普通源码、测试和日志仍执行完整 whitespace 检查。
- Vitest 原配置会收集 `src-tauri/tests/document_drop.test.mjs` 的 Node 测试；现在限定为 `src` 测试，并用 `test:native` 单独运行原生协议脚本。普通 `npm test` 先运行 native，再运行 Vitest；附加参数仍正确传给 Vitest。
- 本地化守卫最初报告 4 条错误，补齐文案后 11 项复跑通过，守卫未弱化。
- 后端回归发现 Codex `_turn` 的 `dict.get` 默认表达式会无条件读取 `messages`，使只传 prompt 的底层调用出现 `KeyError`。改为惰性读取已预解析图片或可选消息体；新增图片不重复解析回归，异常 usage 的原断言保留。
- 一项既有 Codex 能力测试把 Python 当作 CLI executable，却未模拟新的模型清单发现。补固定模型清单夹具，保留原能力断言和生产代码对失效元数据的拒绝。定向复核 14 项通过；不把这 14 项重复加到最终核心范围。

## 失败、中断与本机执行条件

前端主组的一项 Sending 用例在 3 worker 并发时达到原 5 秒测试时限。保持原代码断言与测试时限，单 worker 下完整 23 项通过；保留首次失败日志，不能由此证明在所有负载下都不会超时。默认 pool/worker 数量没有为本机改写。

最初全量 Vitest forks 运行长时间未完成，已停止；本轮改用 threads 跑重点范围。没有完成全仓前端套件，也不宣称全仓前端全绿。

Windows 上大量显式 pytest 文件参数触发较慢的路径比较/收集。使用单一 `tests` 收集根和本机选择 hook，禁用外部自动插件，按明确文件清单执行；产品代码和测试断言没有为收集问题改写。广泛组与 browser 组中断前的部分通过数不作为整组结果。具体最终分组与范围以本轮证据为准。

priority 初跑为 366 passed、2 skipped、8 failed，命中同一个 Codex 图片默认值缺陷后停止；修复后完整重跑通过。runtime 初跑 284 passed、1 夹具失败，该同一节点定向修复后通过。process 为 126 passed、1 skipped；另 5 个完整文件为 130 passed。最后的 14 项修复组和 7 项补漏组与主批有重叠，最终数量由唯一节点表核对，不直接相加。

## 保留的提示与未测范围

生产构建仍有 notification 模块静态/动态混合导入和超过 500 kB chunk 的提示。PDF 预览引入的独立 chunk 约 641 kB，主入口约 1.27 MB；当前构建成功，尚未在本轮做首次加载性能验收。

本轮没有运行全仓后端/前端、完整 headless browser 组、真实 Windows 键盘/桌面写入、外部 reference worker 对照、Rust/Tauri 编译、新安装包、收费真实模型或跨公网验收。这些范围不能由本轮重点回归推导为已通过。

原生 OpenAI Responses 真实提供商验收、Claude thinking/adaptive 参数、旧 Office 的 LibreOffice 依赖、Word/LibreOffice 跨引擎兼容、GridCraft 空字符串公式问题和 WordCraft/PDFCraft 兼容包完整重建流程，仍以交付说明及各历史验收的边界为准。
