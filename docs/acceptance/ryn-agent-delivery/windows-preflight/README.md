# Windows 冻结后端开发预检

2026-10-09，开发快照，非安装器验收、非真实模型成绩、非最终同快照回归。

发现并修复：

1. Windows sidecar 构建环境没有 pypdf；构建脚本原本仅安装基础依赖。Windows/macOS sidecar 脚本现在显式安装 documents extra 并收集 pypdf，Windows 增加构建前导入检查。
2. 文档解析父进程使用 sys.executable -m 在冻结 exe 中无法进入 Python 模块。新增 --ryn-document-worker 专用入口，冻结父进程调用此入口；普通 Python 保留 -m。Windows 子进程隐藏窗口，文件路径仍只经 stdin 传入。

使用现有本地 .venv 的 PyInstaller 6.22.3、pypdf 6.19.0，离线构建实际生产入口为隔离 rynmesh-agent-check.exe；未替换已安装 Ryn 或原发布包，也未运行下载依赖的完整 installer 脚本。二进制 SHA-256：`5c621b95c04b8e4b8d380dae9dc93a0c9cff8afe662450e2ebd706e445918950`。

- 直接执行实际 exe 文档子进程，PDF 提取并严格截到 50 字符，stderr 0，未创建节点 home。
- 实际 exe 启动新临时节点，使用启动期 local token，HTTP Agent 初始 disabled；通过实际来源/工具验证/资料授权 API 创建合成任务。
- Agent 经真实 WebDAV 读取 PDF，并从冻结父进程启动冻结文档子进程，得到 pdf-extracted-text 和预期文本；DAV 无写入。
- 规划模型为本机 19004 的确定性 UI fixture，不计真实模型或完整目标成功。这里只核对实际 PDF 工具读取，未把任务最终回答作为验收结论。

首轮 115520 的功能检查通过，但测试控制器只终止 onefile 外层，留下内层节点；已按精确本地测试 executable 路径清理。测试控制器改为挂起启动→Windows Job 归属→恢复，最终关闭整个进程树。115650 再跑全部检查通过，随后按精确 executable 路径查询确认存活进程数为 0。首轮记录保留，不删除失败清理证据。

文档解析既有回归 50 passed、7 skipped（Windows 不适用的 POSIX 限制测试）；新增 worker/父进程入口 3 passed；ruff 通过。Windows 现有解析器没有 POSIX 地址空间上限，仍由输入量、输出量与超时约束；本批不声称实现了内存沙箱。

最终 Windows 安装、升级、正常桌面启动和原任务恢复仍未执行。其他模块同时开发，必须在最终代码快照重新构建并完整验收。

后续真实冻结复验：121211 增加页级解析覆盖信息与Windows Job清理，worker直接结果有pdf_pages，但Agent普通read投影漏传extraction_pages（search已传）。保留该记录，补普通read白名单与真实PDF父链回归后，130812再构建/运行：实际Agent工具结果包含total=1/processed=1/failed=0/without_text=0，全部4项预检通过，进程退出后精确exe路径存活数0。最新二进制hash `a2e6c9c8b88921c23b7363be2454091b348023dcfe3943ab8ba4c844b69f4e2b`。

解析器部分页异常现在保留有效正文并truncated，空白/扫描页数单独记录，无OCR。新增真实Windows子孙进程超时测试证明清理整棵进程树；Job在本批仅作生命周期隔离，不设置内存限额，不能据此宣称Windows内存沙箱。文档合计56 passed/7平台skip；后续资源链回归98项包含实际PDF覆盖信息传播，数量有交集不相加。
