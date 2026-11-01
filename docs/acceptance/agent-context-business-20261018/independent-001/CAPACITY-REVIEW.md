> 公开交付副本（2026-10-19）：保留原验收结论、失败和范围；本机绝对路径已规范化，未提交的原始证据仅在本机留存。文中的旧快照/安装包 hash 不代表本次源码；原报告 SHA-256：`e7b712a882dd518201b308fccff6c403ac70a32b60b721a20edd0b866ac0ae22`。

# 独立容量/来源/CLI绑定审查片段（001）

结论：此分项 **FAIL / 本轮合同未完全关闭**。独立执行既有 focused 测试 41 个通过；额外 5 个合同边界探针失败，归为下面 4 项问题。没有更改生产源码、真实 owner 设置或授权，没有读取认证文件或用户旧聊天，没有启动真实 CLI 推理或新增付费调用。临时认证、SourceStore、CLI 元数据及推理回调全部位于新建 fixture 目录。

本片段只审核容量发现、传递及绑定；不对办公业务文件作结论。最终整体生产快照稳定性由主审报告核对。本片段受核文件的完整 SHA-256 位于 `capacity-hashes.json`。

## 已验证通过

| 项目 | 结果与范围 |
| --- | --- |
| 新来源 auto、明确来源 manual、旧无 mode 数值继续保留 | PASS。`tests/test_model_context_discovery.py` 包含默认自动、原 32768 数值不静默替换、显式每模型 override 优先。旧 provenance 的后端名称是 `legacy_source_configuration`，前端显示命名不在此分项下重验。 |
| 来源 metadata 真实性与端点隔离 | PASS。浏览器传入 `model_metadata` 被忽略；只采纳模型 listing 的 `context_window/context_length/max_model_len` 且严格整数 256..2M；缓存 key 绑定 kind/provider/base_url/api_key；更换地址或凭证不会继承旧容量。省略容量会保留既有已发现容量；这不是实时容量保证。 |
| 免费 listing →持久化→effective→AgentCatalog | PASS。真实测试 FastAPI 路由接线、fixture listing，无 `chat()` 推理；400000→1050000 来源容量刷新会改每模型 destination revision，AgentCatalog 随之更新。 |
| 已安装 CLI 默认及确切子模型容量 | PASS（静态新鲜 metadata 范围）。本地默认/子模型已读取 fixture installed CLI metadata；未知/bool/string/极大数字不会获得已知容量标记。 |
| ProviderService、local/remote order 选择 CLI 容量 | PASS（有可用 metadata 的选定模型范围）。`routes.py:349`、`:473`、`:2744`、`:2918` 各入口读取选定/default CLI metadata；sealed provider 请求中的 caller `context_window` 不被采纳。 |
| 权限及绑定回归 | PASS。`test_agent_peer_cli.py` 继续覆盖真实签名 catalog/sealed task、显式子模型、配置/可执行文件变化、权限重查；使用 fixture 模型回调。 |

## 具体问题

### IR1-C01 [P2] 自动来源已知 1M 窗口，来源输出设置仍被无效 32768 字段挡住

位置：`rynmesh/llm_package/sources.py:107-110`。

独立探针先 listing 发现 large 的 1048576 窗口，保存来源，读回 `model_metadata.large.context_window == 1048576`，再用相同 `source_id` 编辑 `{context_window_mode: "auto", max_output_tokens: 128000}`。`prepare()` 在加载/采用 metadata 前用旧的非生效 `context_window=32768` 校验 `output <= context`，直接抛 ValueError。自动模式 1M 窗口的来源无法按实际窗口配置 128k 输出；只有同时抬高这个应被 auto 忽略的数字或改用每模型 override 才可绕过。

该数值校验条件在 baseline HEAD 已存在；**auto 模式及 discovered capacity 是本轮新增，保留旧校验未适配新语义**。不能称旧手动模式本身有此回归。

### IR1-C02 [P2] 完整 CLI catalog 刷新容量后，精确模型 destination revision 未更新

位置：`rynmesh/llm_package/cli_binding.py:43-44`、`rynmesh/agent_runtime/models.py:39-47`。

独立探针从 selected installed model-a 的 400000 容量开始，改变 fixture 元数据为 16000、过期全局 CLI cache，并调用完整 `catalog.list_models()`。新的 `context_window` 已是 16000，但前后 `destination_revision` 与 binding 完全相同。绑定 fingerprint 只含 executable、manifest、kind；不含选中/默认模型对应的已发现容量。故“容量变化 destination revision/binding 正确”的合同未覆盖此入口。

这是**既有未关闭边界**：当前 `cli_binding.py` 与本轮前 dirty ZIP 字节完全一致，SHA-256 `1b7db55013d8b405625de7d775045779a6eb1d9d59a0958b2c4dcb9e59e27e2c`。本轮把 CLI discovered capacity 接进更多路径，但未给该变化增加绑定世代。

### IR1-C03 [P1] 已选 CLI 子模型容量快照不受 TTL 约束，本地 Agent 可按过时大窗口继续派发

位置：`rynmesh/agent_runtime/models.py:185-187`；实际 local Agent dispatch `rynmesh/agent_runtime/bridge.py:207-218`。

当已选具体子模型存在且 executable/manifest binding 未变，`get_model()` 无条件复用 `_cli_metadata` 快照，没有年龄限制。独立探针建立 400000 窗口快照，把 installed metadata 声明改为 16000，并使全局 CLI metadata TTL 过期：单模型 `get_model()` 仍返回 400000，未发现缩容。

第 5 个探针进一步运行真实 `AgentModelBridge.complete()`，使用先前精确 `expected_destination`，发送 60000 个 ASCII 内容字节（仅内容的统一估计已超过 16000 窗口）。调用仍抵达 fixture 本地 CLI adapter 并成功返回，未在 admission 阶段阻断。该探针是生产 bridge 接线加 fake upstream；**没有付费推理，也不宣称某真实提供商已缩容**。它证明一旦上述 metadata 缩容条件成立，本地 Agent 可按旧窗口派发。

该复用分支在 baseline dirty `models.py:175` 已存在；所以属于**本轮“跟随实际窗口/切小安全”合同未关闭的既有缓存策略**，不是本轮新引入该缓存分支。已有 `test_selected_known_cli_model_reuses_expired_metadata_without_changing_authority` 明确接受无限复用；它保障无需重启 CLI 的性能，却没有证明容量仍新鲜。

### IR1-C04 [P2] 认证兼容 API 的 CLI 默认模型仍按 manifest 32768 拒绝实际大窗口请求

位置：`rynmesh/llm_package/api.py:123`、`:135`、`:148`、`:244-247`。

`/v1` API 的独立 model catalog 直接读取 manager/peer service manifest 的 `context_window`，没有采用 `context_window_for_model()` 或 listing 的默认 CLI capacity。独立 FastAPI fixture 的 CLI default 已知 400000，manifest 保留默认 32768；认证 `/v1/chat/completions` 的 150000 ASCII 内容字节请求得到 HTTP 400 `estimated context window exceeded`，回调未执行。请求在实际 400000 窗口及 16MiB transport 上限内。

该 catalog 与 admission 代码与 baseline HEAD 相同；本轮 `api.py` 只把 1MiB body 检查改为公共 16MiB 上限。故这是**本轮大窗口合同在兼容 API 上的覆盖缺口，不是本轮新写坏的已有 API 容量逻辑**。Agent 自身的 `ModelCatalog` 路径不同；不能据此说所有 Agent 请求都仍限 32k。

## 实测及证据

| 执行范围 | 结果 | 新且短的 basetemp | JUnit |
| --- | --- | --- | --- |
| `tests/test_model_context_discovery.py` | 10 passed | `D:\tmp\ir1c4b82` | `capacity-discovery.junit.xml` |
| `tests/test_agent_cli_models.py tests/test_agent_peer_cli.py tests/test_model_pool_policies.py` | 31 passed | `D:\tmp\ir1c67e91` | `capacity-regression.junit.xml` |
| 本分项 `test_capacity_edges.py`（最终版本，含已保存来源更新） | 5 failed | `D:\tmp\ir1c4a6e0` | `capacity-edges-003.junit.xml` |

前两版探索证据也保留：`capacity-edges.junit.xml`（4 failed）、`capacity-edges-002.junit.xml`（5 failed）。它们不是额外独立测试数量，不与最终版本重复相加。没有扩大全仓测试。

最终探针源 SHA-256：`741257db874f4d9f146b519aaf0b75b43d9cc5006d93a7230e6674783f1de607`。

最终失败 JUnit SHA-256：`495e036117aa7b7ff3fc2eb2b295d4c86419ef900f639a3df4bbe9a873bd4b83`。

baseline 指定 HEAD 为 `0146c0255fb056dcbb1b4939e27cd81c05073ef7`；本轮前 dirty 文件优先取 `context-business-baseline-20261018/current-dirty-files.zip`，ZIP 未含的 tracked 文件取该 HEAD。只作源码归因，没有运行整套 before 环境或推断全仓绿。
