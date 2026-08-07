# 截图翻译（自制）

这是一个面向 GNOME/X11 的 Youdao 风格截图翻译工具：按 `Ctrl+Alt+O` 后框选区域，按 `Esc` 可随时取消。翻译会绘制回截图中，工具栏提供“提取中文”“提取英文”“复制翻译”。

项目目录已改名为 `Screenshot translation`（注意目录名中的空格）。


依赖：GTK3/PyGObject、Pillow、Tesseract。中文 OCR 模型和英文模型放在 `tessdata/`。翻译后端使用开源 Mozhi 的 DuckDuckGo 引擎（`GET /api/translate`，query 传参），并带三个备用节点，每段最多尝试 3 次。可用环境变量 `MOZHI_NODES`（逗号分隔）覆盖节点列表。

网络：程序会自动读取 GNOME“网络代理”中的手动 HTTP/HTTPS 代理（例如 Clash Verge 的混合端口）。也可通过 `SCREENSHOT_TRANSLATOR_PROXY=http://127.0.0.1:7897` 显式覆盖。

速度：未缓存的短段落会合并为一次批量翻译请求，再按标记拆回原段落；标记异常或批量失败时自动回退到逐段翻译。

长段落：论文摘要等超过约 650 字符的单段文字会先按句子边界拆分，并行翻译后重新合并，避免 Mozhi/Bing 因单次 GET 文本过长返回 HTTP 500。PDF 行末断词（例如 `demon-` 换行接 `strated`）会在翻译前自动还原。
