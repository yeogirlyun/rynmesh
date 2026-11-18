// Ryn passes its own UI language (?lang=); otherwise follow the browser.
const langParam = new URLSearchParams(location.search).get("lang");
export const zh = langParam ? /^zh/i.test(langParam) : !/^en\b/i.test(navigator.language || "zh");
export const T = zh ? {
  title: "做图", loading: "正在加载…", newImage: "新建一张图", purpose: "用途和尺寸", custom: "自定义",
  output: (w, h) => `输出 ${w} × ${h} PNG`, brief: "想要什么样的图", briefPh: "双十一保温杯主图，白色背景，杯子放中间，右上角红色角标",
  words: "图上要写的字", wordsHint: "（可选，会一字不差地放上去）", wordsPh: "限时 5 折",
  refs: "参考图", refsHint: "（可选，最多 5 张）", refsTip: "商品照片、旧海报都行，可以直接拖进来或粘贴",
  add: "添加", remove: "移除参考图", count: "张数", submit: "提交", submitting: "正在提交…",
  ahead: (n, m) => n ? `前面还有 ${n} 个任务，大约 ${m} 分钟后开始做你的` : "现在提交马上开始做",
  queue: (n) => n ? `排队 ${n} 个` : "现在没人排队", each: (s) => `每张约 ${Math.max(1, Math.round(s / 60))} 分钟`,
  mine: "我的作品", all: "全部", active: "进行中", done: "已完成", empty: "还没有作品。左边写好需求就可以提交。",
  queued: (p) => `排队中 · 第 ${p} 位`, wait: (s) => s > 0 ? `大约 ${Math.max(1, Math.round(s / 60))} 分钟后开始` : "下一个就做你的", running: (t) => `正在生成 · ${t}`, runningTip: "在对方电脑上做",
  finished: "完成", failed: "没做出来", cancelled: "已取消", cancel: "取消", retry: "重试",
  download: "下载", again: "再来一张", edit: "改这张", back: "我的作品", downloadPng: "下载 PNG", copy: "复制描述",
  copied: "已复制", info: { brief: "描述", words: "图上文字", size: "尺寸", refs: "参考图", made: "生成" },
  editTitle: "在这张基础上改", editPh: "背景换成浅红色，加一点雪花，角标改成「买一送一」",
  editHint: "这张图会作为参考图，尺寸不变", editSubmit: "提交修改", sent: "已提交，开始排队", notReady: {
    codex_missing: "对方电脑没有安装 Codex，现在提交的任务会失败。",
    codex_signed_out: "对方电脑的 Codex 没有登录，现在提交的任务会失败。" },
  errors: { image_bad_brief: "请添加图片、文案或补充说明。", image_too_many_active: "你进行中的任务太多了，等前面的做完再提交。",
    image_upload_too_large: "图片太大了，单张不能超过 8 MB。", image_upload_type: "只支持 PNG、JPG、WebP 图片。",
    image_bad_size: "自定义尺寸要在 256 到 2048 像素之间。", site_offline: "对方电脑离线了。", network: "连不上，稍后再试。",
    default: "没成功，稍后再试。" },
  ago: { now: "刚刚", min: (n) => `${n} 分钟前`, yesterday: "昨天" }, seconds: (s) => s < 60 ? `${Math.round(s)} 秒` : `${Math.floor(s / 60)} 分 ${Math.round(s % 60)} 秒`,
  refsCount: (n) => n ? `${n} 张` : "无",
} : {
  title: "Image studio", loading: "Loading…", newImage: "New image", purpose: "Use and size", custom: "Custom",
  output: (w, h) => `Output ${w} × ${h} PNG`, brief: "What should it look like", briefPh: "Product shot of a thermos on white, red corner badge",
  words: "Text on the image", wordsHint: " (optional, placed exactly)", wordsPh: "50% off",
  refs: "Reference images", refsHint: " (optional, up to 5)", refsTip: "Product photos or old posters. Drag, drop or paste.",
  add: "Add", remove: "Remove reference", count: "Count", submit: "Submit", submitting: "Submitting…",
  ahead: (n, m) => n ? `${n} ahead of you, starts in about ${m} min` : "Starts right away",
  queue: (n) => n ? `${n} in queue` : "No queue", each: (s) => `about ${Math.max(1, Math.round(s / 60))} min each`,
  mine: "Your images", all: "All", active: "In progress", done: "Finished", empty: "No images yet. Describe one on the left and submit.",
  queued: (p) => `Queued · #${p}`, wait: (s) => s > 0 ? `starts in about ${Math.max(1, Math.round(s / 60))} min` : "you're next", running: (t) => `Generating · ${t}`, runningTip: "On their computer",
  finished: "Done", failed: "Didn't work", cancelled: "Cancelled", cancel: "Cancel", retry: "Retry",
  download: "Download", again: "Another", edit: "Edit this", back: "Your images", downloadPng: "Download PNG", copy: "Copy brief",
  copied: "Copied", info: { brief: "Brief", words: "Text", size: "Size", refs: "References", made: "Made" },
  editTitle: "Change this image", editPh: "Light red background, some snowflakes", editHint: "This image is the reference; the size stays.",
  editSubmit: "Submit change", sent: "Submitted", notReady: {
    codex_missing: "Codex isn't installed on that computer; jobs will fail.",
    codex_signed_out: "Codex isn't signed in on that computer; jobs will fail." },
  errors: { image_bad_brief: "Add an image, copy or instructions first.", image_too_many_active: "Too many jobs in progress. Wait for some to finish.",
    image_upload_too_large: "Images must be 8 MB or smaller.", image_upload_type: "Only PNG, JPG and WebP.",
    image_bad_size: "Custom sizes must be 256–2048 px.", site_offline: "That computer is offline.", network: "Can't connect. Try again later.",
    default: "That didn't work. Try again later." },
  ago: { now: "just now", min: (n) => `${n} min ago`, yesterday: "yesterday" }, seconds: (s) => s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`,
  refsCount: (n) => n ? `${n}` : "none",
};
export const FAIL_EN = { codex_missing: "Codex isn't installed there.", codex_signed_out: "Codex isn't signed in there.",
  quota_exhausted: "Image quota used up. Try later.", content_rejected: "Blocked by content review. Rephrase it.",
  timeout: "Timed out.", cancelled: "Cancelled.", interrupted: "That computer restarted.", generation_failed: "Generation failed." };
document.documentElement.lang = zh ? "zh-CN" : "en";
document.title = T.title;


export const F = zh ? {
  scene: "做什么", images: "图片", copy: "文案", title: "主标题", subtitle: "副标题", badge: "角标",
  style: "风格", size: "尺寸", brief: "补充说明", material: "素材", reference: "风格参考", source: "上一版成品",
  tip: "素材里的商品会出现在成品里。风格参考只用来参考配色、构图和氛围。",
  width: "宽", height: "高", image: "图片", drop: "上传、拖入或粘贴图片", optional: "可选"
} : {
  scene: "Scene", images: "Images", copy: "Copy", title: "Headline", subtitle: "Subtitle", badge: "Badge",
  style: "Style", size: "Size", brief: "Additional instructions", material: "Material", reference: "Style reference", source: "Previous result",
  tip: "Materials show what to include. Style references guide only the colors, composition and mood.",
  width: "Width", height: "Height", image: "Image", drop: "Upload, drop or paste images", optional: "Optional"
};
const english = { banner: "Banner", main: "Product image", poster: "Poster", detail: "Detail header", social: "Social post",
  minimal: "Minimal", tech: "Tech", festive: "Festive", promo: "Promotional", premium: "Premium", fresh: "Fresh" };
export const presetLabel = (id, preset) => (!zh && english[id]) || preset?.label || id;
export const roleLabel = role => role === "style" ? F.reference : role === "source" ? F.source : F.material;

export const A = zh ? {
  choose: "从素材库选", manage: "管理素材", saveTo: "存到素材库", save: "保存", saved: "已存入素材库",
  mine: "我的素材", public: "公共素材", publicSave: "存为公共素材", search: "搜索素材", name: "素材名称",
  kind: "类型", product: "商品", logo: "Logo", style: "风格图", close: "关闭", remove: "删除",
  confirm: "确认删除", cancel: "取消", empty: "这里还没有素材。", noMatches: "没有匹配的素材。",
  loading: "正在读取素材…", limit: "每次最多可用 5 张图，请先移除一张。", emptyName: "请填写素材名称。",
  image_asset_limit: "这个素材库已满（最多 100 张），请先删除不用的素材。",
  image_bad_asset: "请填写 1 至 80 字的名称并选择素材类型。",
  image_asset_not_found: "这份素材已被删除，请重新选择。", image_asset_public_forbidden: "只有主人可以管理公共素材。"
} : {
  choose: "Choose from library", manage: "Manage assets", saveTo: "Save to library", save: "Save", saved: "Saved to library",
  mine: "My assets", public: "Public assets", publicSave: "Save as public", search: "Search assets", name: "Asset name",
  kind: "Type", product: "Product", logo: "Logo", style: "Style image", close: "Close", remove: "Delete",
  confirm: "Confirm deletion", cancel: "Cancel", empty: "No assets here yet.", noMatches: "No matching assets.",
  loading: "Loading assets…", limit: "You can use up to 5 images. Remove one first.", emptyName: "Enter an asset name.",
  image_asset_limit: "This library is full (100 assets). Delete an unused asset first.",
  image_bad_asset: "Enter a name of 1–80 characters and choose an asset type.",
  image_asset_not_found: "This asset was deleted. Choose another.", image_asset_public_forbidden: "Only the owner can manage public assets."
};
