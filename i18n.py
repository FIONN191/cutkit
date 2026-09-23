# -*- coding: utf-8 -*-
"""界面多语言。

用中文原文当键，不另编 key —— 这样漏翻的串会自动回落成中文，
不会出现「界面上一堆 nav.tab.pairs 这种键名」的半成品状态。
前端拿到这张表后遍历文本节点直接替换，HTML 本身不用打任何标记。
"""

LANGS = [("zh", "中文"), ("en", "English")]

EN = {
    "顶部标题": "Top title",
    "留空": "Leave blank",
    "使用 text": "Use text",
    "Fotor 转场自带转场音效，随成片速度同步；「拖拽音效」单独控制拖动与落框声音。": "Fotor transitions include synchronized sound. Drag sound controls the drag and drop sounds separately.",
    # —— 导航 / 通用 ——
    "视频运营工具箱": "Video ops toolkit",
    "前后对比": "Before / After", "录屏步骤": "Screen Steps",
    "拖照片演示": "Drag Demo", "圆环箭头": "Ring Arrow",
    "录屏自动剪辑": "Auto Cut", "广告成片": "Ad Assembly", "历史记录": "History",
    "选择…": "Choose…", "清除": "Clear", "删除": "Delete", "刷新": "Refresh",
    "应用": "Apply", "保存当前": "Save current", "恢复默认": "Reset",
    "预设名": "Preset name", "秒": "s", "时长（秒）": "Duration (s)",
    "在 Finder 中显示": "Show in Finder", "打开文件夹": "Open folder",
    "取消生成": "Cancel", "同时准备": "Prepare", "2 组": "2 groups",
    "转场": "Transition", "转场时长": "Transition length",
    "版本": "Version", "动画": "Animation",
    # —— 分区标题 ——
    "① 上传图片": "① Upload images", "② 参数": "② Settings", "③ 生成": "③ Generate",
    "① 前后对比素材": "① Before / after material", "② 拖照片演示": "② Drag demo",
    "③ 文案与素材": "③ Copy & assets", "④ 生成": "④ Generate",
    "① 原始录屏": "① Source recording", "② 步骤字幕": "② Step captions",
    "① 照片": "① Photo", "① 原图": "① Source image", "② 输出": "② Output",
    "① 录屏文件": "① Recording", "② 自然语言指令": "② Plain-language instruction",
    "③ 参数": "③ Settings", "④ 叠加素材": "④ Overlay assets", "⑤ 生成": "⑤ Generate",
    "① 剪映素材夹": "① CapCut material folder", "② 生成历史": "② Render history",
    # —— 前后对比 ——
    "顶部字幕（留空则不加）": "Top caption (leave blank for none)",
    "字幕字号": "Caption size", "Before 标签": "Before label", "After 标签": "After label",
    "每段时长（秒）": "Seconds per pair", "对比展示方式": "Reveal style",
    "滑杆·中段放慢": "Slider · slow middle", "滑杆·来回扫": "Slider · sweep",
    "滑杆·滑到底": "Slider · once", "反向污染": "Reverse", "手指擦除": "Finger wipe",
    "硬切闪频": "Hard flicker", "进度条还原": "Progress bar", "评论区驱动": "Comment card",
    "九宫格多米诺": "Grid domino",
    "滑动方向": "Slide direction", "从右往左 ←": "Right to left ←", "从左往右 →": "Left to right →",
    "人物对齐": "Subject alignment", "自动对齐前后图人物": "Auto-align the subject",
    "比例不同 / 位移缩放都能对上": "Handles different ratios, shifts and scaling",
    "裁到共同区域": "Crop to shared area", "推荐，无边缘拉丝": "Recommended — no edge smear",
    "旋转模糊": "Spin blur", "直切": "Hard cut",
    "评论用户名": "Comment username", "评论内容": "Comment text",
    "进度条文案": "Progress label", "生成视频": "Generate video",
    "或：一次导入整个文件夹…": "Or import a whole folder…",
    "重新自动配对": "Re-pair automatically", "清空配对": "Clear pairs",
    "点框选文件，或直接把图拖进来。某一组两边都放好，就会自动加成一对进下面的列表并清空该组。":
        "Click a box to pick a file, or drop images in. When both sides of a group are filled it "
        "becomes a pair in the list below and that group clears.",
    "选择包含前后图片的文件夹，自动按画面相似度配对（同场景的前/后图会配到一起；文件名带 before/ChatGPT 等会自动识别方向）。":
        "Pick a folder of before/after images; they are paired by visual similarity "
        "(same scene pairs up, and names containing before/ChatGPT set the direction).",
    "手动配对：先点一张当": "Manual pairing: click one as",
    "，再点另一张配成": ", then click another to pair as",
    "（再点一次取消选中；已配对的图会变暗）": " (click again to deselect; paired images dim)",
    # —— 拖照片演示 ——
    "选择要上传演示的照片…": "Choose the photo to demo…",
    "选 AI 结果图（可选）…": "Choose AI result (optional)…",
    "或把照片直接拖到这里": "or drop the photo here",
    "或把结果图直接拖到这里": "or drop the result here",
    "顶部标题（粉色药丸，留空则不加）": "Title (leave blank for none)",
    "标题样式": "Title style", "动画风格": "Motion style",
    "飞入落框（复刻参考片）": "Fly in and snap (matches reference)",
    "光标拖拽（任意比例自适应）": "Cursor drag (fits any ratio)",
    "输出与效果": "Output & effects",
    "透明底 MOV（可直接盖在自己的视频上）": "Transparent MOV (overlay on your own footage)",
    "叠 Fotor 转场": "Fotor transition", "鼠标指针": "Cursor", "拖拽音效": "Drag sound",
    "生成演示视频": "Generate demo",
    "粉色药丸": "Pink pill", "白色药丸": "White pill", "黄色药丸": "Yellow pill",
    "青色药丸": "Cyan pill", "白色方块": "White block", "纯白大字": "Plain white",
    "粉字白描边": "Pink + white outline", "白字粉描边": "White + pink outline",
    "复刻「上传一张照片」的教程演示：虚线上传框 → 照片飞入落框回弹 → Fotor 彩色转场（进度环 + AI Generated 徽章，自带压暗）。选了结果图的话，徽章出现时会淡入成结果。":
        "Recreates the “upload one photo” demo: dashed drop box → photo flies in and settles → "
        "Fotor transition (progress ring + AI Generated badge, with its own dim). "
        "With a result image, the badge cross-fades into it.",
    "透明 MOV 不含黑底和标题药丸，只有虚线框 + 照片卡片 + 光标，方便在剪映/CapCut 里叠到任意画面上。":
        "The transparent MOV drops the black backing and the title, leaving just the dashed box, "
        "photo card and cursor, so it can sit over any footage in CapCut.",
    # —— 圆环箭头 ——
    "任意比例都行，自动圆形裁切": "Any ratio — cropped to a circle automatically",
    "选择原图…": "Choose image…", "或把图片直接拖到这里": "or drop an image here",
    "画面裁切 · 横向": "Crop · horizontal", "画面裁切 · 纵向": "Crop · vertical",
    "圆环大小": "Ring size", "位置 X": "Position X", "位置 Y": "Position Y",
    "画布尺寸": "Canvas size", "生成透明底 MOV": "Generate transparent MOV",
    "开头 0.45s 弹入（不勾选＝全程静止，与竞品一致）":
        "Pop in over the first 0.45s (unchecked = static throughout)",
    "默认位置/大小与竞品一致（左下角）。棋盘格代表透明区域。":
        "Default position and size match the reference (bottom left). Checkerboard is transparency.",
    # —— 录屏步骤 ——
    "选择录屏文件…": "Choose recording…",
    "或把录屏文件直接拖到这里": "or drop the recording here",
    "添加步骤字幕": "Add step captions", "滤镜名（自动填入第 2 条）": "Filter name (fills caption 2)",
    "BGM（可选，复用左侧已选）": "Music (optional, reuses the one on the left)",
    "字幕 1 · 选照片": "Caption 1 · pick photo", "字幕 2 · 选滤镜": "Caption 2 · pick filter",
    "字幕 3 · 等待": "Caption 3 · wait", "字幕 4 · 结果（自动带 ❤️）": "Caption 4 · result (adds ❤️)",
    "结尾效果": "Ending", "选结果原图…": "Choose result image…",
    "结尾放大最终结果图（原图放大铺满 + 定格 1.8s，需选原图）":
        "End on the result zoomed to fill, held 1.8s (needs the result image)",
    "剪辑生成": "Cut and generate",
    "选滤镜 App 的完整操作录屏，自动剪掉中间的等待时间（AI 生成排队等），压成 ~15 秒节奏。":
        "Give it a full screen recording of the app; the waiting stretches (queueing, generating) "
        "are cut out and it is paced down to about 15 seconds.",
    # —— 录屏自动剪辑 ——
    "选择文件…": "Choose file…", "开始剪辑": "Start cutting",
    "目标时长（秒，留空=自动节拍）": "Target length (s, blank = auto)",
    "涂抹段时长（秒）": "Brush segment (s)", "打字加速倍数": "Typing speed-up",
    "等待节拍（秒）": "Wait beat (s)", "末尾等待节拍（秒）": "Final wait beat (s)",
    "等待节拍": "Wait beat",
    "自然流（去 logo + 叠加素材）": "Natural (logo removed + overlays)",
    "广告版（保留 fotor logo）": "Ad version (keeps the Fotor logo)",
    "— 选择预设 —": "— Choose preset —", "— 选择参数预设 —": "— Choose settings preset —",
    "— 选择素材预设 —": "— Choose asset preset —", "— 仅自然流": "— natural version only",
    "叠加预合成动画 + fotor 水印": "Overlay the precomp animation + Fotor watermark",
    "预合成动画": "Precomp animation", "fotor 水印": "Fotor watermark",
    "预合成动画宽度（% 画面宽）": "Precomp width (% of frame)",
    "水印宽度（% 画面宽）": "Watermark width (% of frame)",
    "末拍": "Final beat", "未选素材": "No asset",
    "动画铺在除末拍外的等待节拍，末拍只放水印，都居中。":
        "The animation covers every waiting beat except the last, which carries only the "
        "watermark. Both are centred.",
    "预览按 9:16 成片比例，素材大小即实际占比；底图取自当前录屏。":
        "Preview is at the 9:16 output ratio, so the asset size is its real proportion. "
        "The backdrop is a frame from the current recording.",
    "例：目标 15 秒，打字快一点，等待再短些":
        "e.g. target 15 seconds, type faster, shorten the waits",
    "离线解析；填了 Anthropic API key 会走 Claude 理解更灵活的说法。":
        "Parsed offline; with an Anthropic API key it goes through Claude and understands "
        "looser phrasing.",
    "Anthropic API key（可选）": "Anthropic API key (optional)",
    "Fotor 录屏自动剪辑：识别「涂抹 / 打字 / 等待」三种状态，按节拍重排时长、自动裁剪，自然流版本还会把等待段换成无 logo 画面并叠加预合成动画与水印。":
        "Detects brushing, typing and waiting, re-times them to the beat and crops "
        "automatically. The natural version also swaps the waiting stretches for logo-free "
        "footage and lays the precomp animation and watermark over them.",
    # —— 广告成片 ——
    "每组放一对前后图。组数越多，成片里对比段就越多、每段越短。":
        "One before/after pair per group. More groups means more comparison segments, each shorter.",
    "选原图…": "Choose source photo…", "选 AI 结果图…": "Choose AI result…",
    "或把原图直接拖到这里": "or drop the source photo here",
    "或把结果图直接拖到这里 —— 它也是结尾定格用的那张":
        "or drop the result here — it is also what the ending holds on",
    "顶部字幕（自动折行，支持 emoji）": "Top caption (wraps, emoji supported)",
    "演示段加速": "Demo speed-up", "2×（推荐）": "2× (recommended)", "原速": "Original speed",
    "对比片段之间": "Between comparison clips", "段落之间的转场": "Between segments",
    "无（硬切）": "None (hard cut)", "闪黑": "Flash black", "闪白": "Flash white",
    "叠化": "Dissolve", "淡入淡出": "Fade", "模糊": "Blur", "放大": "Zoom in",
    "圆形展开": "Circle open", "径向": "Radial", "像素化": "Pixelize",
    "左滑": "Slide left", "左擦除": "Wipe left",
    "选品牌结尾 MOV…": "Choose branded ending MOV…",
    "带 alpha 的结尾素材，会叠在结果定格上擦入；留空则不加结尾":
        "An ending clip with alpha; it wipes in over the held result. Leave blank for no ending.",
    "选 BGM…": "Choose music…",
    "必填 —— 成片总长跟着它走，切点会吸附到它的节拍":
        "Required — the film's length follows it and the cuts snap to its beat",
    "生成广告成片": "Generate ad",
    # —— 历史 / 剪映 ——
    "生成后自动放入剪映素材文件夹": "Copy finished videos into the CapCut material folder",
    "换文件夹…": "Change folder…",
    "BGM（可选）": "Music (optional)",
    "或拖图片到这里": "or drop an image here",
    "或拖音频到这里": "or drop audio here",
    # —— 带 {} 占位符的模板（JS 拼串用）——
    "· 第 {} 组": "· group {}",
    "渲染中 {}/{} 帧": "Rendering {}/{} frames",
    "分析中 {}/{}": "Analysing {}/{}",
    "读取中… {}% ({})": "Reading… {}% ({})",
    "这里只能放{}文件": "Only {} files here",
    "共 {} 条": "{} items",
    "已保存预设：{}（下次打开默认选它）": "Saved preset: {} (selected by default next time)",
    "已保存：{}": "Saved: {}", "已删除预设：{}": "Preset deleted: {}",
    "已删除：{}": "Deleted: {}", "已套用预设：{}": "Applied preset: {}",
    "已套用：{}": "Applied: {}",
    "移除": "Remove", "读取中…": "Reading…",
    "快捷入口": "Shortcuts",
    "橙": "Orange", "蓝": "Blue", "紫": "Violet", "绿": "Green",
    "玫红": "Rose", "灰": "Mono", "取消中…": "Cancelling…",
    "把常用的一套参数（含对齐开关）存下来，换片型时一键切回。":
        "Save a set of settings (alignment switches included) and switch back in one click.",
    "把常用的素材 + 大小存成预设，下次打开自动选最近保存的那份。":
        "Save assets and sizes as a preset; the most recent one is selected next time.",
    "生成时会先把两张图的人物对到一起；逐对可点「◎ 对齐」查看并手动微调。":
        "The subject is aligned across both images before rendering; use ◎ Align on a pair to "
        "check it and nudge by hand.",
    "已关闭：两张图按原样直接用，不做任何位移缩放，逐对微调也不生效。":
        "Off: both images are used as they are, with no shifting or scaling, and per-pair "
        "nudges have no effect.",
    "转场加在「对比段 → 演示」和「演示 → 定格」两处；成片总长会自动补偿，仍然贴合 BGM。":
        "Transitions sit between the comparison clips and the demo, and between the demo and "
        "the held result. The length is compensated so the film still matches the music.",
    "段落之间直接硬切。": "Segments cut straight from one to the next.",
    # —— JS 生成的列表/提示 ——
    "播放": "Play", "在 Finder 显示": "Show in Finder",
    "放入剪映素材夹": "Copy to CapCut folder", "删除记录": "Delete entry",
    "✅ 已在剪映素材夹": "✅ In the CapCut folder",
    "未找到，将跳过叠加": "not found — overlay will be skipped",
    "内置": "bundled", "用内置": "Use bundled",
    "两个素材已随 CutKit 内置，换机器、换路径都不会丢；想用自己的那份就点「选择…」。":
        "Both assets ship inside CutKit, so they survive a new machine or a moved "
        "folder. Choose… swaps in your own copy.",
    "改回随 CutKit 内置的那份素材": "Switch back to the copy bundled with CutKit",
    "还缺：": "Still needed: ", "、": ", ",
    "至少一组完整的前后图": "at least one complete before/after pair",
    "演示原图": "the demo source photo", "BGM": "music",
    "梳齿预览：人物边缘接不上就是还没对齐": "Comb preview — mismatched edges mean it is not aligned yet",
    "对齐分析中…": "Analysing alignment…",
    "滑到底，但两头快、中间慢 —— 时间花在画面中段（人脸所在），边缘一带而过；前后各停一拍。":
        "Sweeps once, fast at the edges and slow through the middle where the face is, "
        "holding a beat at each end.",
    "AI 视频 · 模板": "AI Video · Templates",
    "AI 视频 · 新建": "AI Video · Create",
    "AI 视频 · Magic Sync": "AI Video · Magic Sync",
    "AI 图片创作": "AI Image Create",
    "钉钉文档": "DingTalk Doc", "钉钉表格": "DingTalk Sheet",
    "给这个入口起个名字（留空恢复默认）": "Name this shortcut (blank restores the default)",
    "右键改名": "Right-click to rename",
    "顶部标题（留空则不加）": "Title (leave blank for none)",
    "字体": "Font", "系统": "System", "黑体": "Heiti",
    "标题竖直位置": "Title height",
    "标题位置预览": "Title position preview",
    "（真实字体与样式）": "(actual font and style)",
    "速度预设": "Speed preset", "成片时长（秒）": "Length (seconds)",
    "默认速度（1.9 秒）": "Default (1.9s)", "更快（1.4 秒）": "Faster (1.4s)",
    "原速（5.0 秒）": "Original (5.0s)", "放慢（2.6 秒）": "Slower (2.6s)",
    "图片": "image", "视频": "video", "音频": "audio",
}

TABLE = {"zh": {}, "en": EN}


def table(lang):
    return TABLE.get(lang) or {}
