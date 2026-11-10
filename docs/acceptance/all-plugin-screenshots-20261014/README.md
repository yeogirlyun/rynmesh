# 17 项插件实际打开截图 · 2026-10-14

本机 Windows、Chrome、1440 × 1000。使用独立临时节点与真实 HTTP API，运行当前前端生产构建。12 个 Craft Web 构建均按固定版本下载并通过校验，缓存位于 Git 外；没有变更真实节点的数据、默认安装选择或已有服务。浏览器与临时监听器在截图完成后关闭。

截图范围是启动界面与工作区查看；尚未完成“新建 → 修改 → 导出 → 重新打开”的文件往返验收。PPT、调色、音频、特效中的内容来自上游自带样例。

|插件|版本|真实页面与限制|PNG 原图|
|---|---|---|---|
|PDF / PdfCraft|v0.7.1|新建空白 PDF|[查看](pdfcraft.png)|
|Word / WordCraft|v0.6.1|空白文档；中文标签缺字|[查看](wordcraft.png)|
|Excel / GridCraft|v0.6.1|空白工作簿|[查看](gridcraft.png)|
|PPT / DeckCraft|v0.6.1|上游 Northern Lights 示例|[查看](deckcraft.png)|
|CADCraft|v0.6.2|空白图纸|[查看](cadcraft.png)|
|DesignCraft|v0.7.1|新建 A4 排版页面|[查看](designcraft.png)|
|FigCraft|v0.5.1|空白设计画布|[查看](figcraft.png)|
|PhotoCraft|v0.8.2|新建 1920 × 1080 图像；字体清单 404|[查看](photocraft.png)|
|LightCraft|v0.7.1|上游照片示例；浏览器未授予持久存储|[查看](lightcraft.png)|
|VectorCraft|v0.10.0|新建 A4 画板；部分中文标签缺字|[查看](vectorcraft.png)|
|EffectCraft|v0.9.2|上游 EffectCraft Intro 合成工程；本机需临时软件 GPU 截图参数|[查看](effectcraft.png)|
|SoundCraft|v0.6.1|上游 Midnight Groove 工程|[查看](soundcraft.png)|
|Resume|1.0.0|真实空工作区与创建表单|[查看](resume.png)|
|Recruit|1.0.0|真实空工作区与创建表单|[查看](recruit.png)|
|Desk|1.0.0|真实空工作区与创建表单|[查看](desk.png)|
|NAS|1.0.0|真实空状态；未配置外部存储连接|[查看](nas.png)|
|FilmCraft|v0.7.2|仅版本与来源说明；固定版本无 Web 构建，未打开原生程序|[查看](filmcraft.png)|

普通编辑器使用 Chrome 默认 GPU。LightCraft 的本次独立捕获使用 `--enable-unsafe-webgpu --use-angle=d3d11`。EffectCraft 在 D3D11 截图时卡住；使用 `--enable-unsafe-webgpu --use-angle=swiftshader --enable-unsafe-swiftshader` 等待实际画面绘制后完成截图。这些参数仅用于临时测试浏览器，没有写入产品启动配置，没有关闭 CSP。

PhotoCraft 的 `fonts/manifest.txt` 返回 404，未补造文件。Word、Vector 的缺字保留在原始截图中。

安装结果见 [install-result.json](install-result.json)，逐项截图状态见 [screenshot-result.json](screenshot-result.json)。PNG 是无改绘的浏览器截图，JPEG 为同一界面直接捕获的相册预览；两个 GPU 编辑器的 PNG/JPEG 使用同次浏览器会话的 CDP 捕获。
