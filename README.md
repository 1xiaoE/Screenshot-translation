# Screenshot Translation

一款面向 Linux GNOME/X11 的截图翻译工具。按下快捷键框选屏幕区域后，程序会自动完成 OCR、段落翻译和图片内排版，并尽量将译文放回原段落附近。

## 功能特点

- `Ctrl+Alt+O` 快速框选截图，`Esc` 随时取消或关闭窗口
- 冻结桌面画面后再显示选框，避免半透明选框导致黑屏
- 基于 Tesseract 的中英文 OCR
- OCR 前自动进行 2 倍高质量缩放，提高小字体识别率
- 按 OCR 块和段落组织文字，而不是逐词翻译
- 自动连接英文断词、中文折行，并清理截图边缘的竖线误识别
- 译文按原图列和段落位置排版，自动换行并检测碰撞
- 全图使用统一字号；空间不足时统一从 18 调整到 16 或 14 像素
- 每个段落一次翻译请求；节点与引擎回退、连接复用，单段最多尝试 6 次
- 翻译缓存、进度提示和单任务锁
- 支持提取中文、提取英文、复制译文和重新框选

## 运行环境

- Linux
- GNOME 42 或兼容桌面
- X11 会话
- Python 3.10+
- GTK 3 / PyGObject
- Pillow
- Requests
- Tesseract OCR
- Noto CJK 字体

当前截图方式依赖 X11 根窗口读取接口，暂不支持 Wayland。

## 安装

克隆仓库：

```bash
git clone https://github.com/1xiaoE/Screenshot-translation.git
cd Screenshot-translation
```

在 Ubuntu/Debian 上安装运行依赖：

```bash
sudo apt update
sudo apt install \
  python3-gi \
  gir1.2-gtk-3.0 \
  python3-pil \
  python3-requests \
  tesseract-ocr \
  fonts-noto-cjk
```

仓库中的 `tessdata/` 已包含 `eng` 和 `chi_sim` OCR 模型。

## 快速开始

打开设置窗口：

```bash
python3 screenshot_translator.py
```

在设置窗口中确认快捷键并点击“保存快捷键”。默认快捷键为：

```text
Ctrl+Alt+O
```

之后按下快捷键，拖动鼠标框选要翻译的区域即可。

也可以直接启动框选：

```bash
python3 screenshot_translator.py --select
```

## 使用流程

```text
快捷键 → 框选区域 → OCR → 段落翻译 → 防重叠排版 → 显示结果
```

结果窗口提供以下操作：

- 提取中文
- 提取英文
- 重新框选
- 快捷键设置
- 复制翻译

## 网络与翻译

翻译后端使用公共 Mozhi 实例的 `GET /api/translate` 接口，参数通过 query 传递（不要使用 POST，三个公共节点对 POST 都返回 HTTP 500）。引擎按需回退：

```text
duckduckgo → google
```

（`duckduckgo` 由 Bing 提供支持；`google` 需要目标语言代码 `zh-CN`，仅在 duckduckgo 返回服务端错误或空结果时才会尝试，不影响正常速度。）

节点依次尝试：

```text
https://mozhi.adminforge.de
https://mozhi.ducks.party
https://translate.projectsegfau.lt
```

节点选择是自适应的：最近一次成功的节点自动排到最前；连续连接失败（2 次）的节点进入 30 秒冷却期，不会让每次请求都卡在它上面。每个段落独立翻译，单段总尝试次数最多 6 次（3 节点 × 2 引擎）；连接级失败快速切换到下一个节点。翻译线程与 HTTP 会话在进程内常驻复用，代理 TLS 握手只发生一次，后续请求走 keep-alive 连接，明显更快。部分段落失败时保留原文并继续渲染，全部失败才弹出提示。

批量请求同时受两个限制约束：Bing 单次约 1000 字符（超出返回 HTTP 500），公共节点前的反向代理对 URL 长度有限制（中文每字约 9 个编码字节，过长返回 HTTP 431/414）。程序按「字符数 ≤ 950 且编码后 URL ≤ 3500 字节」自动分块，中文内容不会因 URL 过长而失败。

可用环境变量 `MOZHI_NODES`（逗号分隔）覆盖节点列表：

```bash
MOZHI_NODES=https://example-one.invalid,https://example-two.invalid \
  python3 screenshot_translator.py --select
```

## 翻译与排版策略

OCR 文字首先按 `block_num + par_num` 合并为段落，再以段落为单位翻译。

送入 Tesseract 前，截图会在内存中用 Lanczos 放大 2 倍，使小字字形高于
Tesseract 的最小可信高度；OCR 返回的文字框随后映射回原图坐标，不影响译文
覆盖位置。处理过程不生成额外的临时图片，也不会增加第二次 OCR 调用。
放大会增加大选区的瞬时内存占用；放大倍数是自适应的（小选区 2 倍、超大选区
自动回落到 1 倍左右，放大后的像素总量上限约 500 万），因此全屏截图不会出现
长达数秒的 OCR 等待。全屏或多显示器场景仍建议只框选包含文字的区域。

同一行内的中文词块会直接连接，英文词块保留空格；翻译前会去除英文行末断词
连字符，并把中文跨行内容无空格连接，避免排版换行破坏原句。
若 Tesseract 缺少语言模型或执行失败，程序会明确报错，不再把故障显示成“未识别到文字”。

排版时会：

1. 识别不同文本列；
2. 以原段落左上角为锚点；
3. 按实际像素宽度换行；
4. 检测译文框重叠和越界；
5. 必要时统一降低全图字号；
6. 空间仍不足时扩展结果画布，而不是让文字重叠。

## 生成文件

程序运行时会在项目目录生成：

| 文件 | 用途 |
| --- | --- |
| `cache.png` | 最近一次原始截图 |
| `translated.png` | 最近一次翻译结果 |
| `state.json` | 固定截图区域 |
| `translation_cache.json` | 本地翻译缓存 |
| `screenshot_translator.log` | 运行和错误日志 |
| `task.lock` | 防止同时运行多个截图任务 |

这些文件包含本机运行状态或截图文字，不建议提交到 Git 仓库。

## 隐私说明

- OCR 在本机通过 Tesseract 完成。
- 识别出的段落文字会发送到配置的公共 Mozhi 翻译节点。
- 原始截图不会发送给翻译节点。
- `translation_cache.json` 会在本地保存原文和译文。
- `screenshot_translator.log` 只记录节点/引擎/异常类型；仅在布局等罕见错误时，
  异常信息里可能包含当次 OCR 的片段文字（纯本地保存）。
- 上述运行文件均已被 `.gitignore` 排除，不会提交到 Git 仓库。
- 如需处理敏感内容，建议自建 Mozhi 服务，并通过 `MOZHI_NODES` 指向私有节点。

## 故障排查

### 快捷键没有反应

重新打开设置并保存快捷键：

```bash
python3 screenshot_translator.py --settings
```

检查 GNOME 当前注册的命令是否指向本仓库中的脚本。

### 截图框或截图结果为黑色

确认当前会话为 X11：

```bash
echo "$XDG_SESSION_TYPE"
```

预期输出：

```text
x11
```

### 翻译服务无法连接

1. 确认网络或 Clash Verge 正常运行；
2. 查看 `screenshot_translator.log`；
3. 注意：本工具会自动使用 GNOME 系统代理（Clash 等）。代理链路较慢时 TLS 握手可能耗时数秒，程序已使用宽松的超时设置（连接 10s / 读取 25s）并在启动时后台预热连接，因此**每次启动后的第一次截图翻译会稍慢**，之后明显更快；若仍失败请确认代理本身可用；
4. 必要时通过 `MOZHI_NODES` 更换节点。

## 项目结构

```text
Screenshot-translation/
├── screenshot_translator.py
├── README.md
└── tessdata/
    ├── eng.traineddata
    ├── chi_sim.traineddata
    └── configs/
```

## 说明

公共翻译实例可能临时不可用或受到速率限制。重复内容会优先使用本地缓存；对于长期稳定使用，建议部署自己的 Mozhi 实例或后续接入正式翻译 API。
