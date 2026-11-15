# Agent 改造分支增量同步（2026-11-16）

## 交付范围

- 仓库：<https://github.com/yeogirlyun/rynmesh>。
- 分支：[`feature/agent-overhaul`](https://github.com/yeogirlyun/rynmesh/tree/feature/agent-overhaul)。
- 上次已推送提交：`f2350f899ed8866eb39fa414098ff846ae30dc0c`，产品代码来源快照为 `10d8d694`。
- 本次来源快照：`claude/agent-overhaul-plan-20261114` 的 `710dc9e71e3f35ee4e5925b12e45125a7cec8bf6`。
- 本次合入 14 个后续提交（含 5 个合并提交），产品与测试共 32 个文件变更，新增 3,869 行、删除 243 行；不含本次交付说明的变更。
- 使用普通合并保留提交历史，同时保留上次交付的说明和敏感文件忽略规则。

本次新增内容均围绕 Agent 执行和依赖链路。节点网页、生图工作台是早于 Agent 改造基线的功能，不属于本次新增修复。587 项仍是规划总量，不能作为已完成数量。

## 本次新增修复

| 方案项 | 改动及效果 |
|---|---|
| S01-I3：重试与连接恢复 | 对明确可恢复的断连原因持续重连，并使用指数退避；未知原因的连接失败保留有限重试，避免无限重试。按来源配置流式重试次数，完善状态及前端展示；证书、永久 DNS、额度等错误不按临时断连反复重试。 |
| S04-I5：Shell 登录环境 | 捕获 POSIX 登录 Shell 环境、刷新 Windows PATH，增加 PowerShell 登录参数；登录环境预热最多等待一轮，并将 PSSecurityException 的回退限制在配置文件加载场景。 |
| S07-I7：子 Agent 工具和指引 | 完善各角色职责、共享工作区与写入范围、后续任务复用及等待行为的说明；任务提示词和消息长度上限提升至 32,000 字符，保持工具定义与运行时校验一致。 |
| S08-I4：MCP stdio 通信 | 改为空闲超时，按方法设置单行大小限制；缓慢传输的超大行能够排空处理，避免直接终止整个会话。 |
| S08-I12：MCP 工具参数格式 | 增加工具输入 JSON Schema 的规范化与压缩，改善复杂或不标准参数定义的兼容性，并补充插件界面提示及测试。 |

## 敏感文件和工作区保护

- 扫描来源快照相对上次推送新增可达历史中的全部 47 个 Git blob，包括中间提交版本。
- 检查凭据文件路径、私钥、常见服务令牌、JWT、带密码的连接 URL、敏感字段字面量及超过 5 MiB 的文件；未检出匹配项或风险路径。
- `.weagent/`、`artifacts/`、`releases/`、环境凭据、数据库、缓存和构建产物沿用现有忽略规则。本次没有添加这些本地文件。
- 原始扫描输出和测试临时数据保留在本机，不提交；原工作区的未提交改动保持不动。

## 验证与状态

本次在合并后的工作树执行回归，均退出码 0：

- 后端：本次变更涉及的全部 12 个测试文件，**191 passed in 261.82s**；使用项目现有 Python 虚拟环境及独立 `.pytest-tmp/sync-20261116` 临时目录。
- 前端：`ovh-S01-I3.test.tsx`、`ovh-S08-I12.test.tsx`、`AgentExtensions.test.tsx`，**3 个文件、27 项测试通过**，耗时 12.89 秒。
- `git diff --check` 通过；忽略规则检查通过；确认本次来源快照是交付分支的祖先，合并时无冲突，产品代码与来源快照一致。

复核命令（在独立工作树执行，前端命令在 `webapp/` 执行）：

```powershell
$reviewTests = git diff --name-only 10d8d694..710dc9e7 -- tests
python -m pytest @reviewTests -q --tb=short --basetemp=<独立工作树>/.pytest-tmp/sync-20261116
vitest run src/screens/ovh-S01-I3.test.tsx src/screens/ovh-S08-I12.test.tsx src/screens/AgentExtensions.test.tsx
```

本次为开发分支阶段性同步，未执行真实模型或双设备端到端验收，未构建安装包、更新本机程序或合入 `develop`。完成整体开发和验收后仍须按项目约定集成到 `develop`。
