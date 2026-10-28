# 角色契约 v2 提案（未生效）

状态：draft，2026-10-10，供独立审查。本文件不激活新输入，不修改生产或控制器。事实与引用见 [角色审计](role-contract-audit.json)；v1、历史失败和原分数保持不变。

修正执行方将“所有者本机管理”与“接收节点消费”统一绑定到 node-b 的矛盾。采用现有本机管理能力，**不为分数新增远端管理需求**。[既有协议](cases-protocol.md#文件与直接命令)允许修正时升 dataset_version，但禁止覆盖同版本或删除旧结果。

## 1. 最小角色差异

| 执行角色 | 用例 | v2 执行节点 | 相对 v1 |
| --- | --- | --- | --- |
| owner-admin：完整模型事实 | Q01、Q06 | node-a | 显式变更授权执行节点 |
| owner-admin：已有来源/默认/授权 | C02、C03、C04 | node-a | 同上 |
| owner-admin：分享审批/恢复/撤权 | R02、R04、P03 | node-a | 同上 |
| consumer：诊断、设备、历史、资料 | Q02、Q03、Q04、Q05 | node-b | 保留 |
| consumer：资料工作流 | F01–F08 | node-b | 保留 |
| consumer：新建自己的来源 | C01 | node-b | 保留 |
| consumer：资料/流式/上下文恢复 | R01、R03、R05、R06 | node-b | 保留 |
| consumer：权限与提供方隔离 | P01、P02、P04、P05、P06 | node-b | 保留 |

只迁移 8 题，其余 22 题不迁移。owner-admin 仍是一个本机 Agent，消费推理能力并执行自己的工具；它不是远端管理角色，也不自动增加写权限。Q01/Q06 仍只读。两组必须采用同一张角色表，不能依结果选择视角。

## 2. 三类节点与资源归属

新契约显式记录：

- **agent_execution_node**：实际主循环、AgentStore、工具网关所在节点，以真实身份/进程/存储核验。
- **model_provider**：逐业务模型的拥有/提供节点；另逐请求记录 primary_model 的实际提供者与来源绑定。
- **model_recipient**：逐模型真实授权接收者集合，保留原 node-b 身份，不事后改名。
- **execution_role、resource_owner、connection_host、history_owner**：任务角色、来源配置所属节点、连接器执行节点、历史任务所属 AgentStore；这些声明均需实际证据。

原 authorization.consumer_node 不暗中改义：它继续绑定实际任务执行节点。v2 对上述 8 题明确生成新的授权节点值，强制逻辑名映射后的身份满足：

`effective_case.authorization.consumer_node == agent_execution_node == actual_runtime.consumer_node_id`

不能保留 B 授权、另声明 A 执行就判符合。owner_id 相同不意味着存在跨节点管理授权。

| 对象 | 明确归属/范围 |
| --- | --- |
| source-alpha、model-a/model-b | node-a；保留 A→B 初始授权及 B 模型私有无 grant，原题覆盖除外 |
| source-other、model-c | node-a |
| source-broken、model-d | 默认 node-a，使 Q01 可检查完整本机模型池；**仅 Q02 的独立 fresh-world 明确属于 node-b** |
| C01 的 source-new | 初始不存在，由 node-b 创建；凭据引用只进入其隔离本机保管库 |
| docs-alpha/docs-beta/docs-offline | 相应用例的 B 连接执行环境，后端可独立；原目录/动作/egress 不变，连接存在不代表授权 |
| task-failed-read | B、owner-test 的真实历史任务，不从 A 复制；Q04 在 B 诊断 |
| R02 审批/恢复 | A 的实际 AgentStore 和执行进程 |
| R03/R05/R06 恢复 | B 的实际任务存储与连接上下文 |
| P04 两个消费会话 | B；提供者私有历史在 A，不能进入 B 请求 |
| Q03 设备证据 | B 的实际发现、HTTP 与 registry 观察，保留原节点状态和检查时间 |

Q02 的所属例外是 v2 **新增的显式部署绑定**，不是追认 v1 已指定；连接失败/凭据有效性/余额事实不变。不同题本就独立初始化，同一重复中禁止迁移或复制来源以取得答案。A/B/C 必须是不同身份。

primary_model 继续使用合法显式绑定：consumer 可用 A 授权给 B 的模型，owner-admin 可用 A 合法本机模型。修改来源导致 planner 不可用时，不自动换模型、补权限或跳过故障。生成参数映射须在首次业务快照前记录，候选与参考一致；不得伪造 B 不可见的目录行。

提供者不得执行别人的 Agent 工具；A 自己的 owner-admin 任务可以执行 A 工具，审计须按任务/身份区分。A 合法推理订单/计费日志不等于 B 的 Agent 状态。

## 3. 两个必须固定的语义

**C03：本机默认属于 A。** “新任务默认模型”及 enabled 指 A 的 AgentStore。A 私有 model-a 可供 A 使用；model-b 只分享给真实 B。独立回读 A 默认/开关/逐模型配置，并实际验证 B 只接收到且能调用 model-b、不能调用私有 model-a；B 默认值不应因此改变。原 all_models_private 只表达关闭分享，不暗中增加“清空已有 grant”效果；额外业务字段变化必须另列审查。

**C04：初始 grant 已存在，不清空。** 第一阶段是“确保已有授权并核验”，不能声称首次授予。保留原 pending grant 的精确批准阶段，可审批绑定对象/接收者/版本的幂等确保操作；实际写入/版本变化按原轨迹记录，不制造状态变化。若现有运行时无法到达原 barrier，记录未完成，不能跳过。随后保留 B 允许/C 拒绝探测、后续撤销要求、精确撤销批准、B 新请求拒绝和 model-b 不变。

## 4. 不可变项与证据边界

- 原 30 个 development 提示词、业务事实/结果断言、资源权限、干预、故障和预算不降低；所有子场景完整执行，固定 barrier 不可由提前拒绝代替。
- Q/F/C/R 仍 72 次、至少 58 次且达到固定参考组同题成功数的 80%；Q04/F02/C01 各 3 次全过，F02 至少 5 个有意义操作，R02–R06 各 3 次控制/恢复可复现，P 组 18 次全过。其他两机、安装升级、UI/同快照门均保留。[评分协议](cases-protocol.md#分数与放行)
- 真实模型仍只用用户原 src-7de9fb1d36c44bc8 的 GLM-5.3-Flash，migrate=False 读取且不改 owner 来源；两组生成策略、预算和权限相同。合成来源不算新增真实模型效果。
- 模型只能从真实授权工具取得事实；collector ground truth 不进 prompt，不教工具序列。P02/P06 的早期目标拒绝不能冒充更深授权分支的覆盖。
- 旧 owner 样本仍只是其实际视角下的开发证据；不追认为 v2 重复，不重写旧失败/normalizer，不把本地多进程提升为两物理节点。
- 条件性 51/72 上限仅是旧生产+严格 v1 角色下七题均无法满足时的可达性分析，不是实测分数；另有 P03 与恢复硬门。v2 不保证题目通过。

## 5. 带 hash 的版本化 overlay

建议新增 development 角色 overlay，引用原 cases-v1.json、world.json 及资料的精确 hash，不复制/重建冻结资料。拟用新 dataset_version（例如 ryn-agent-acceptance-2026-10-10.v2，激活时正式固定）。

overlay 包含：基版本/hash、新版本、全 30 题角色及资源归属、仅 8 题的 authorization.consumer_node 变更、Q02 归属例外、变更理由、合成算法版本和审查证据。运行前确定性生成并保存 effective_case/effective_world_binding 与 hash；原文件不动。除允许的版本、角色、归属与授权节点差异外，其他字段变化均拒绝。

grade/aggregate 必须验证有效输入，拒绝 v1 输入贴 v2 标签、缺 overlay、声明与实际身份不符、同次混用角色、v1/v2 混算或不同 overlay hash 混算。候选/参考须独立重置且角色、业务初态、故障、生成策略和预算一致。新版本重新运行，不能从旧样本补足次数。

**held-out 不读不改。** 本提案尚不定义其逐题角色。后续由隔离的独立盲评流程处理版本绑定，不向开发/提示词调试泄露内容；未完成前该依赖 pending，不宣称完整正式 v2 包就绪。

## 6. 实施与必须新增的验证

以下均是待实现要求，本轮没有生产/测试通过声明。

| 阶段 | 实施边界 | 必须验证 |
| --- | --- | --- |
| 独立审查/激活决定 | 本提案、审计、新版本决策记录 | 八题差异保持用户功能目标；不减门槛；文件存在不会自动生效 |
| 契约/判分 | 新 overlay、schema/合成器、必要 grade 支持与测试；原冻结文件不动 | 30 题唯一覆盖、仅八题改授权节点、Q02 例外不扩散；不可变字段 hash 相同；错版本/错 hash/缺 barrier/缺重复被拒绝 |
| 实际身份/归属 | collector 角色绑定和独立存储取证 | 错节点、A 任务复制到 B、两名同身份、授权与 executor 不一致拒绝；两机依赖仍验证不同 physical_host_ids |
| owner-admin controller 迁移 | Q01/Q06/C02/C03/C04/R02/R04/P03 本机路径 | 无远端管理绕路；只读仍禁止写；C03 只改 A 默认，C04 初始授权保留且全过程真实；R02 实际终止/重开 A 执行器、同批准、批准前零写、批准后一个有效写 |
| consumer controller 核对 | 保留 22 题 B 执行；显式 Q02/C01/Q04 归属 | B 不获得 A 私有模型/第三方 grant；资料/历史/恢复属于 B；连接和 egress 不扩权；P04/P05 正确隔离及真实路径 |
| 双组合成集成/独立复核 | 同有效契约、不同新环境 | 相同初态/权限/预算/策略；提供者零执行 B 工具；owner-admin 自己的工具独立归属；未达 barrier 不通过 |
| 新真实评测 | 激活并冻结后由主协调者安排 | 只用原 GLM；保存原来源密文前后 hash，缺历史基线不得追认；新重复、旧失败保留；两物理节点与其他交付门分别验收 |
| 独立盲评流程 | 后续隔离角色绑定 | 不在本轮查看内容，不以 development 迁移完成替代正式保留题依赖 |

controller 若暴露现有工具不能兑现原控制阶段，保留失败并单独处理真实生产缺口；不得由 harness 代做步骤。无任何步骤授权新增远端 owner 管理 API。完成独立审查、契约测试和明确激活记录前，本提案保持 draft。

