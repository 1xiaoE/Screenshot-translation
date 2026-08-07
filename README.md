# Screenshot Translation

一款面向 Linux GNOME/X11 的截图翻译工具。按下快捷键框选屏幕区域后，程序会自动完成 OCR、段落翻译和图片内排版，并尽量将译文放回原段落附近。

## 功能特点

- `Ctrl+Alt+O` 快速框选截图，`Esc` 随时取消或关闭窗口
- 冻结桌面画面后再显示选框，避免半透明选框导致黑屏
- 基于 Tesseract 的中英文 OCR
- 按 OCR 块和段落组织文字，而不是逐词翻译
- 译文按原图列和段落位置排版，自动换行并检测碰撞
- 全图使用统一字号；空间不足时统一从 18 调整到 16 或 14 像素
- 短段落合并为一次网络请求，降低翻译延迟
- 论文摘要等长段落按句子拆分并行翻译，再自动合并
- 自动修复 PDF 常见的行末断词，例如 `demon-` 换行接 `strated`
- 自动读取 GNOME 手动代理，可直接使用 Clash Verge 等本地代理
- 翻译缓存、节点回退、进度提示和单任务锁
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

截取上次保存的固定区域：

```bash
python3 screenshot_translator.py --capture
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

## 网络与代理

翻译后端默认使用公共 Mozhi 实例的 DuckDuckGo/Bing 引擎。程序会依次尝试以下节点：

```text
https://translate.projectsegfau.lt
https://mozhi.adminforge.de
https://mozhi.ducks.party
```

程序会自动读取 GNOME“网络代理”中的手动 HTTP/HTTPS 代理。例如 Clash Verge 使用 `127.0.0.1:7897` 时，无需额外修改代码。

也可以显式指定代理：

```bash
SCREENSHOT_TRANSLATOR_PROXY=http://127.0.0.1:7897 \
  python3 screenshot_translator.py --select
```

自定义 Mozhi 节点：

```bash
MOZHI_NODES=https://example-one.invalid,https://example-two.invalid \
  python3 screenshot_translator.py --select
```

## 翻译与排版策略

OCR 文字首先按 `block_num + par_num` 合并为段落。短段落会在不超过接口长度限制的前提下合并请求，并使用内部标记拆回对应位置。

超过约 650 字符的长段落会按句号、问号、分号或单词边界拆分，并行翻译后重新组成一个段落。这可以避免 Mozhi/Bing 因单次 GET 文本过长返回 HTTP 500。

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
2. 检查 GNOME 网络代理；
3. 查看 `screenshot_translator.log`；
4. 使用 `SCREENSHOT_TRANSLATOR_PROXY` 显式指定代理；
5. 必要时通过 `MOZHI_NODES` 更换节点。

### 论文长段落返回 HTTP 500

当前版本会自动拆分超过约 650 字符的段落。若仍出现问题，请检查是否正在运行最新代码，并查看日志中的时间戳。

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
