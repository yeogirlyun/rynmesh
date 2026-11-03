# 插件 Agent 接入实施记录 — 2026-11-03

状态：开发中；未安装、未部署、未真机验收。

## 基线

本记录组件：ryn；隔离检出 `D:/code/_build-tools/ryn-plugin-agent-20261103`；起始提交 `77aa6bf76d8025642d2e1dcb59707dbafac5b9ae`。
主程序 develop：77aa6bf76d8025642d2e1dcb59707dbafac5b9ae。
本机插件 chat-backend develop：edc89604e308401e463e40f06ad8ced7e659bd9f。
中心 gshopper-ryn-node develop：1b48bf1549446d1f74503b364d2e8e8f8261ba1b。
原工作区存在其他人的改动，均保留；不复制工作区脏文件。中心只修改正式中心仓库，本机插件只修改 chat-backend 的 integrations/company-app。

已核对源码：本机 wecom_tools.py 定义 5 工具，Schema 暴露 login_token 和任意附件路径；routes.py tools/list 混合财务及企微，存在读取宿主私钥/OAuth 并选择最新连接的代理。wecom_delivery.py 已有 SQLite 原子入队、unknown 恢复及消息记录核验，继续复用。中心 rynMcp.service.ts 有 36 工具及全范围管理员保守门禁，sanitize 静默删除未知字段。主程序 prepare_full_package.py 白名单丢弃发现及能力字段，full_package_install.py 只导入 missing 插件。

现有身份链：宿主 OAuth → 本机 routes actor → 旧本机 token 代理 → 中心 OAuth → chatbot permission → 业务工具。目标链：宿主指定连接 → 本机实例认证/用户上下文 → 原发件箱；Dashboard 直接连接中心 OAuth。

运行时工具加载、当前安装版本、生产部署摘要尚待核验，不用上述源码提交替代运行证据。

## DEV 追踪

| 编号 | 范围 | 依赖 | 验收 |
|---|---|---|---|
| DEV01 | 完成版本、注册、身份及运行时基线 | 无 | 第 4 节基线文档；不能凭静态声明跳过 |
| DEV02 | 明确公司本机插件和中心正式源码归属 | DEV01 | 路径、仓库、分支、实际部署来源映射 |
| DEV03 | 建立规范工具注册表与完整 Schema | DEV01 DEV02 | tools/list 与分发一致；T01 至 T04 |
| DEV04 | 本机服务凭据和安全 MCP 入口 | DEV01 | 凭据生命周期与入口；T13 至 T20 |
| DEV05 | 移除旧宿主凭据读取并处理页面兼容 | DEV04 | 新旧路径安全一致；T19 T20 |
| DEV06 | 改造企微账号与目标引用、新增搜索 | DEV03 DEV04 | 目标解析及代次绑定；T21 至 T27 |
| DEV07 | 改造发送授权、幂等、状态与重试 | DEV06 | 复用现有发件箱；T28 至 T44 |
| DEV08 | 打通统一目录和正确插件连接 | DEV03 DEV04 | 可发现、可执行、版本失效；T01 至 T12 |
| DEV09 | 通用提示词、两个业务 Skill、点名引导 | DEV06 DEV08 | 自然语言插件选择轨迹；T05 至 T09 T30 |
| DEV10 | 六项 Dashboard 参数、权限、分页和结果 | DEV02 DEV03 | 查询映射与测试；T51 至 T61 |
| DEV11 | 安装声明生成及旧用户迁移 | DEV04 DEV05 DEV08 | 新安装及升级闭环；T73 至 T81 |
| DEV12 | 首期真实模型验收及组合版本集成 | DEV07 DEV09 DEV10 DEV11 | 第 20.11 节及首期发布证据 |
| DEV13 | 任务文件和企微附件闭环 | DEV07 DEV12 | T45 至 T50 |
| DEV14 | 中心完整工具盘点和逐项接入 | DEV10 DEV12 | 能力矩阵、逐工具测试、T62 |
| DEV15 | 通用桌面能力路由保护 | DEV08 DEV12 | 第 17 节及 T82 至 T86 |
| DEV16 | 日记账导出任务与文件访问 | DEV10 DEV12 | T63 至 T67 |
| DEV17 | 日记账刷新业务契约与回执 | DEV10 DEV12 | T68 至 T72 |

以上均为需求映射，不表示完成；各项提交和测试结果随实现追加。DEV01/02 基线调查进行中，其余待实施。

## 测试矩阵

| 用例 | 当前结果 |
|---|---|
| T01 | 未执行 |
| T02 | 未执行 |
| T03 | 未执行 |
| T04 | 未执行 |
| T05 | 未执行 |
| T06 | 未执行 |
| T07 | 未执行 |
| T08 | 未执行 |
| T09 | 未执行 |
| T10 | 未执行 |
| T11 | 未执行 |
| T12 | 未执行 |
| T13 | 未执行 |
| T14 | 未执行 |
| T15 | 未执行 |
| T16 | 未执行 |
| T17 | 未执行 |
| T18 | 未执行 |
| T19 | 未执行 |
| T20 | 未执行 |
| T21 | 未执行 |
| T22 | 未执行 |
| T23 | 未执行 |
| T24 | 未执行 |
| T25 | 未执行 |
| T26 | 未执行 |
| T27 | 未执行 |
| T28 | 未执行 |
| T29 | 未执行 |
| T30 | 未执行 |
| T31 | 未执行 |
| T32 | 未执行 |
| T33 | 未执行 |
| T34 | 未执行 |
| T35 | 未执行 |
| T36 | 未执行 |
| T37 | 未执行 |
| T38 | 未执行 |
| T39 | 未执行 |
| T40 | 未执行 |
| T41 | 未执行 |
| T42 | 未执行 |
| T43 | 未执行 |
| T44 | 未执行 |
| T45 | 未执行 |
| T46 | 未执行 |
| T47 | 未执行 |
| T48 | 未执行 |
| T49 | 未执行 |
| T50 | 未执行 |
| T51 | 未执行 |
| T52 | 未执行 |
| T53 | 未执行 |
| T54 | 未执行 |
| T55 | 未执行 |
| T56 | 未执行 |
| T57 | 未执行 |
| T58 | 未执行 |
| T59 | 未执行 |
| T60 | 未执行 |
| T61 | 未执行 |
| T62 | 未执行 |
| T63 | 未执行 |
| T64 | 未执行 |
| T65 | 未执行 |
| T66 | 未执行 |
| T67 | 未执行 |
| T68 | 未执行 |
| T69 | 未执行 |
| T70 | 未执行 |
| T71 | 未执行 |
| T72 | 未执行 |
| T73 | 未执行 |
| T74 | 未执行 |
| T75 | 未执行 |
| T76 | 未执行 |
| T77 | 未执行 |
| T78 | 未执行 |
| T79 | 未执行 |
| T80 | 未执行 |
| T81 | 未执行 |
| T82 | 未执行 |
| T83 | 未执行 |
| T84 | 未执行 |
| T85 | 未执行 |
| T86 | 未执行 |


## 开发检查点：通用发现与本机实例授权（尚未完成首期）

实现：导入插件统一目录、通用插件优先提示；宿主原生服务启动前生成一次性待激活凭据，健康检查后原子轮换加密保存；共享服务内禁用插件不被重新启用；普通模型参数不能携带授权。托管 MCP tools/prepare 返回可读通用字段，界面显示真实预览，摘要改变后原审批不执行。原持久化账本在 dispatch 前保存完整授权 envelope；本机服务回调核验原 envelope、连接、任务/配置版本和当前停用/暂停/取消状态。管理服务的 OAuth 只允许已安装插件声明的 HTTPS 身份域，不放开任意跨域端点。

安装源生成保留 local_service 元数据；升级仅迁移已知默认 workspace 地址，保留用户地址、静态鉴权、连接实例和禁用状态；重复迁移幂等。workspace 资源改变必须单独重新授权，旧中心 Dashboard 连接不受影响。此处只修改安装器源码，未运行安装。

验证：test_agent_local_services、test_agent_managed_mcp、test_agent_extensions_runtime、test_agent_extensions_store、test_full_package_install、test_agent_tool_dispatch 合计 63 passed（隔离 .test-tmp/integration-boundaries-01）。前端 tsc -b --noEmit 通过；Rust cargo check --offline --tests 通过，使用移除 bundle externalBin/resources 的源码检查配置，未打包，有既存 Recovery.check 未使用警告。

已知基线失败：test_agent_plugin_discovery 的 12KiB 场景在原 develop 与本分支均 StopIteration（额外上下文压缩耗尽脚本模型），未改弱断言、未计为通过。新增 imported catalog 用例已单独通过。此处测试均为确定性合成测试，不是真实模型/真实发送验收。

未完成：完整输出 Schema/错误 taxonomy、重试与附件、队列重启后重新授权、真实模型验收、安装包验证、DEV13–17 等后续项。不得按本检查点部署或宣称全部完成。
