# Ryn 五项用户需求完整开发文档与验收索引

版本：v1.0  
日期：2026-09-04  
本地分支：`codex/user-requirements-suite`  
用途：产品、研发、测试和老板审核的统一入口

## 一、总体结论

五项需求的产品范围、详细开发方案、测试方案、回滚方法和逐条验收条件均已形成文档；
代码已在独立 worktree 实现，没有混入原工作树。Windows x86_64 上的后端、Web、
真实本地模型、双节点好友链路、冻结 sidecar、Tauri 桌面程序和 NSIS 安装器均已验证。

对外发布结论必须分平台：Windows 候选版本具备本轮证据；macOS 实机冷启动和文件
权限语义尚未在本轮 Windows 主机执行，因此不得把整体跨平台状态写成“全部验收通过”。

## 二、五项需求文档

| 编号 | 用户需求 | 产品需求 | 完整开发文档 | 验收材料 |
|---|---|---|---|---|
| DEV-01 | 首次启动三分钟获得价值 | [产品文档](USER_FIRST_SUCCESS_WORK_PLAN.md) | [开发与验收条件](DEV_01_FIRST_SUCCESS_IMPLEMENTATION_PLAN.md) | [验收记录](acceptance/dev-01-first-success/README.md) |
| DEV-02 | 一键启用本地 AI | [产品文档](USER_LOCAL_AI_SETUP_WORK_PLAN.md) | [开发与验收条件](DEV_02_LOCAL_AI_SETUP_IMPLEMENTATION_PLAN.md) | [验收记录](acceptance/dev-02-local-ai/README.md) |
| DEV-03 | 统一 Ask Ryn 助手 | [产品文档](USER_UNIFIED_ASSISTANT_WORK_PLAN.md) | [开发与验收条件](DEV_03_UNIFIED_ASSISTANT_IMPLEMENTATION_PLAN.md) | [验收记录](acceptance/dev-03-assistant/README.md) |
| DEV-04 | “我的内容”统一工作台 | [产品文档](USER_MY_CONTENT_WORK_PLAN.md) | [开发与验收条件](DEV_04_MY_CONTENT_IMPLEMENTATION_PLAN.md) | [验收记录](acceptance/dev-04-library/README.md) |
| DEV-05 | 两台设备简单配对与分享 | [产品文档](USER_SIMPLE_PAIR_AND_SHARE_WORK_PLAN.md) | [开发与验收条件](DEV_05_SIMPLE_PAIR_AND_SHARE_IMPLEMENTATION_PLAN.md) | [验收记录](acceptance/dev-05-friends/ACCEPTANCE_REPORT.md) |

总体顺序、共同安全门槛、跨需求 E2E 和 Definition of Done 见
[总体开发顺序](APPROVED_USER_REQUIREMENTS_DEVELOPMENT_SEQUENCE.md)，最终执行结论见
[总体验收报告](acceptance/FINAL_ACCEPTANCE_REPORT.md)。

## 三、验收材料结构

每项材料至少包括以下内容：

- 功能验收与安全/隐私验收；
- focused 和全量自动化结果；
- 真实环境或物理文件/进程验证；
- migration、rollback 或关闭 feature flag 的方法；
- 不含正文、绝对路径、token、invite、relationship secret 的隐私负面证据；
- 原始 UI 截图和一张带编号说明的标注总图。

带标注 UI 总图：

1. [DEV-01 首次成功](acceptance/dev-01-first-success/screenshots/dev01-ui-annotated.png)
2. [DEV-02 本地 AI](acceptance/dev-02-local-ai/screenshots/dev02-ui-annotated.png)
3. [DEV-03 Ask Ryn](acceptance/dev-03-assistant/screenshots/dev03-ui-annotated.png)
4. [DEV-04 我的内容](acceptance/dev-04-library/screenshots/dev04-ui-annotated.png)
5. [DEV-05 好友配对](acceptance/dev-05-friends/screenshots/dev05-ui-annotated.png)

## 四、审核建议

老板可先审核五份产品文档中的“用户问题、范围、不做什么”，再审核五份 DEV 文档中的
API、数据、风险、测试和验收条件。只有 [总体验收报告](acceptance/FINAL_ACCEPTANCE_REPORT.md)
中标为通过并附有证据的项目，才可写入对应平台的发布说明。

