# Screenshot Translation

一款面向 Linux GNOME/X11 的截图翻译工具。按下快捷键框选屏幕区域后，程序会自动完成 OCR、段落翻译和图片内排版，并尽量将译文放回原段落附近。
默认使用本地英→中模型和机器人／机械专业术语表，安装完成后可断网使用。

## 功能特点

- `Ctrl+Alt+O` 快速框选截图，`Esc` 随时取消或关闭窗口
- 冻结桌面画面后再显示选框，避免半透明选框导致黑屏
- 双屏/多屏时，只在启动截图时鼠标所在的屏幕框选，不合并其他屏幕；进度和结果窗口也显示在该屏幕
- 基于 Tesseract，中英混合识别，只将英文段落翻译成简体中文；已有中文保持原样
- OCR 前自动进行 2 倍高质量缩放，提高小字体识别率
- 自动处理深色背景并添加小边距；小选区未识别到文字时切换文本块模式重试
- 按 OCR 块、段落和位置组织文字，拆开图表中相距较远的标签
- 保留代码标识、函数名、驼峰类名和大写缩写，英文句子中的这些术语也不会被改写
- 自动连接英文断词、中文折行，并清理截图边缘的竖线误识别
- 只覆盖实际变化的英文段落，背景跟随原图；长译文可向下扩展，遮挡其他段落时显示独立译文区
- 全图使用统一字号；空间不足时统一从 18 调整到 16 或 14 像素
- 本地 CPU 翻译，模型在进程中复用，长段落按句子切分
- 内置 199 条机器人、机械与控制术语及专名，另含 81 个明确的英文词形变体，支持编辑 JSON 扩充和修改译法
- 结果内容区按截图尺寸显示，紧凑工具栏保留重新框选和复制，其余操作放在菜单中
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

安装本地翻译模型和独立运行环境：

```bash
bash scripts/setup_local_translation.sh
```

模型为 Argos English→Chinese 1.9，下载约 70.7 MB。轻量运行时只需要
CTranslate2 与 SentencePiece，避免额外安装整套 NLP 工具链。
模型安装到 `models/translate-en_zh-1_9/`，依赖安装到 `.venv-local/`；
GTK 程序继续使用系统 Python，翻译工作进程自动使用该独立环境。
下载脚本校验模型 SHA256。模型和环境不提交到 Git。

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

双屏或多屏时，先把鼠标移到要截图的屏幕，再按快捷键。选框只覆盖这块
屏幕，拖动超出屏幕边缘时会自动限制选区；本次进度和结果窗口也留在这块
屏幕。再次截图时会重新判断鼠标位置。复用固定区域（`--capture`）时，若
原区域位于其他屏幕或超出当前屏幕，会自动要求重新框选。

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

## 本地翻译与专业术语

默认 `SCREENSHOT_TRANSLATOR_BACKEND=local`：翻译使用 CPU，不请求网络接口，
不需要 GPU、API Key 或翻译服务账号，也不会在本地失败后偷偷切换在线服务。
首次截图会加载模型，同次运行的后续段落复用工作进程。
本地翻译固定为英文→简体中文。

### 机器人／机械术语包

官方 Argos 模型目录按语言对组织，未发现独立的机器人／机械专用模型包。
本项目提供 `glossaries/robotics_mechanics.json`，覆盖运动学、动力学、机械传动、
四足机器人步态、控制与优化、强化学习、机械材料和有限元等领域。
例如 `inverse kinematics → 逆运动学`、`harmonic drive → 谐波传动`、
`joint torque → 关节力矩`、`model predictive control → 模型预测控制`。

术语增强在完整句子翻译后，根据原文出现的术语规范已知译法，并在模型词对齐
可信时修正对应的译文片段。它是可编辑的专业词汇扩展，不是重新训练的领域模型；
小模型对复杂长句仍可能误译。`compliance`、`torque` 等多义词采用本领域习惯译法，
其他领域的截图可以关闭或更换术语表。

在 JSON 的 `terms` 数组中加入如下条目即可扩充：

```json
{
  "source": "inverse kinematics",
  "target": "逆运动学",
  "aliases": ["反向运动学", "逆向运动学"]
}
```

- `source`：英文术语，大小写不敏感，较长的完整词组优先；
- `target`：推荐的中文译法；
- `aliases`：希望统一为推荐译法的已知中文变体，可为空数组。

词形变体和已确认的 OCR 错字分别使用 `source_variants`、`ocr_variants`：

```json
{
  "source": "quadruped robot",
  "source_variants": ["quadruped robots"],
  "target": "四足机器人",
  "aliases": ["四重机器人"]
}
```

例如词表中的 `Unitree A1` 使用 `"ocr_variants": ["Unitree Al", "Unitree A l"]`。
OCR 修复只处理明确列出的词和型号，不做通用的字母／数字替换；已有中文、
反引号代码和行内公式保留原样。匹配仍采用完整词边界，避免误改代码标识。
已正确翻译的术语不会再次被短别名替换；专名按完整名称保护，避免只保留其中的缩写。

保存后下次截图会自动重新读取，翻译缓存也会随术语表内容变化而更新。
自定义词表可以通过环境变量指定，关闭术语增强可以设置 `off`：

```bash
SCREENSHOT_TRANSLATOR_GLOSSARY=/absolute/path/my-terms.json python3 screenshot_translator.py --select
SCREENSHOT_TRANSLATOR_GLOSSARY=off python3 screenshot_translator.py --select
```

缓存区分本地／在线后端、模型版本、语言、术语表内容和整理后的原文。
本地环境或模型缺失时会明确显示本地错误，不会显示“翻译服务无法连接”。

## 可选在线翻译

旧在线后端保留为显式选项，默认不会启用：

```bash
SCREENSHOT_TRANSLATOR_BACKEND=remote python3 screenshot_translator.py --select
```

在线后端使用公共 Mozhi 实例的 `GET /api/translate` 接口，参数通过 query 传递。使用 DuckDuckGo 引擎：

```text
duckduckgo
```

默认只请求 DuckDuckGo，避免公共节点故障时额外等待第二个引擎。若确实需要 Google 回退，可显式设置：

```bash
SCREENSHOT_TRANSLATOR_BACKEND=remote SCREENSHOT_TRANSLATOR_ENGINES=duckduckgo,google \
  python3 screenshot_translator.py --select
```

（`duckduckgo` 由 Bing 提供支持；`google` 需要目标语言代码 `zh-CN`。）

节点依次尝试：

```text
https://mozhi.adminforge.de
https://mozhi.ducks.party
https://translate.projectsegfau.lt
```

节点选择是自适应的：最近一次成功的节点自动排到最前；连续连接失败（2 次）的节点进入 30 秒冷却期。首选节点失败后，剩余节点会并行竞速，不再逐个等待连接超时；连接超时为 4 秒，读取超时为 15 秒。若全部 Mozhi 节点都不可用，自动切换到无需 API Key 的 MyMemory 备用服务。翻译线程与 HTTP 会话在进程内常驻复用，部分段落失败时保留原文并继续渲染，全部失败才弹出提示。程序不会在用户框选前发送预热请求。

批量请求同时受两个限制约束：Bing 单次约 1000 字符（超出返回 HTTP 500），公共节点前的反向代理对 URL 长度有限制（中文每字约 9 个编码字节，过长返回 HTTP 431/414）。程序按「字符数 ≤ 950 且编码后 URL ≤ 3500 字节」自动分块，合并后的标记和换行也计入长度。单个段落直接翻译，不添加分隔标记；多个段落的返回值会检查标记顺序及重复，避免译文错配。

默认显式指定源语言 `en`，目标语言为简体中文，避免短句和缩写依赖自动语言识别。
MyMemory 备用接口使用 `en|zh-CN`，按每段最多 500 个 UTF-8 字节切分；备用服务仍可能因公共额度或限流暂时不可用。
翻译缓存按语言、引擎和整理后的原文区分，换行/空白差异不必重复翻译；旧版自动识别语言的缓存不再直接复用。

可用环境变量 `MOZHI_NODES`（逗号分隔）覆盖节点列表：

```bash
SCREENSHOT_TRANSLATOR_BACKEND=remote MOZHI_NODES=https://example-one.invalid,https://example-two.invalid \
  python3 screenshot_translator.py --select
```

## 翻译与排版策略

OCR 默认使用 `eng+chi_sim` 区分英文和中文，防止中文被英文模型误认成乱码后送去翻译。
只翻译英文段落，含中文的段落及其中的英文术语保留原样；数字、图标、纯代码标识也保留原图。
短界面截图采用较小的标签间距阈值，将相邻按钮分别翻译（如 `Sign in → 登录`、`Sign up → 注册`）。
同一 OCR 行中相距较远的图表标签会拆开，中文词块按 OCR 词序连接，避免重叠的坐标导致字序颠倒。
若截图始终是纯英文，可手动改用英文识别模型：

```bash
SCREENSHOT_TRANSLATOR_OCR_LANG=eng \
  python3 screenshot_translator.py --select
```

此设置只改变识别语言；源语言另由 `SCREENSHOT_TRANSLATOR_SOURCE_LANG` 控制，默认 `en`。
代码标识（如 `actor_state_dict`）、函数调用（如 `load_state_dict()`）、驼峰类名
（如 `MLPModel`）和大写缩写（如 `PPO`）不会单独翻译。英文句子中会检查并保留
代码名、常见公式、单位和缩写；词对齐无法可靠保留代码时，局部翻译周围文本并保留
代码原文。旧版未保护术语的缓存不会复用。

送入 Tesseract 前，截图会在内存中用 Lanczos 放大 2 倍，使小字字形高于
Tesseract 的最小可信高度；OCR 返回的文字框随后映射回原图坐标，不影响译文
覆盖位置。识别输入会转为灰度，深色背景转为浅色，并增加 10 像素边距；
返回坐标会扣除边距。处理过程不生成额外的临时图片。通常只调用一次 OCR；
高度不超过 160 像素的小选区若没有识别到文字，会用文本块模式再尝试一次。
放大会增加大选区的瞬时内存占用；放大倍数是自适应的（小选区 2 倍、超大选区
自动回落到 1 倍左右，放大后的像素总量上限约 500 万），因此全屏截图不会出现
长达数秒的 OCR 等待。全屏或多显示器场景仍建议只框选包含文字的区域。

同一行内的中文词块会直接连接，英文词块保留空格；翻译前会整理英文行末断词、
软连字符和 PDF 连字，并保留 `self-supervised`、`state-of-the-art` 等明确的连字符组合。
中文跨行内容会无空格连接。长段落尽量在句末拆分，并将标点保留在原句，
避免下一段从句号开始；拼接中文译文时不添加多余空格。
若 Tesseract 缺少语言模型或执行失败，程序会明确报错，不再把故障显示成“未识别到文字”。

排版时会：

1. 中文、代码、图标和翻译失败的段落保留原始像素；
2. 以需要翻译的英文段落左上角为锚点，利用附近空白横向展开；
3. 按实际像素宽度换行，替换区域的背景跟随原图；
4. 检测译文框重叠、越界和遮挡其他原文；
5. 必要时统一降低全图字号；
6. 空间不足时，将已翻译的段落依次向下排布并扩展画布；若仍会遮挡保留的标签，
   在截图下方仅显示该段译文。已翻译的英文原文会擦除，不再重复展示。结果窗口优先定位到译文。

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
- 默认本地翻译不发送截图或识别文字到网络；仅显式启用在线后端时，英文段落文字会发送到配置的翻译服务。
- 原始截图不会发送给翻译节点。
- `translation_cache.json` 会在本地保存原文和译文。
- `screenshot_translator.log` 只记录节点/引擎/异常类型；仅在布局等罕见错误时，
  异常信息里可能包含当次 OCR 的片段文字（纯本地保存）。
- 上述运行文件均已被 `.gitignore` 排除，不会提交到 Git 仓库。

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

此提示仅适用于显式启用的在线后端。本地模式不依赖网络；本地环境或模型缺失时，
运行 `bash scripts/setup_local_translation.sh` 修复安装，术语表错误则检查 JSON 格式。

1. 确认网络或 Clash Verge 正常运行；
2. 查看 `screenshot_translator.log`；
3. 本工具会自动使用 GNOME 系统代理（Clash 等）。代理链路较慢时 TLS 握手可能耗时数秒；当前连接超时为 4 秒、读取超时为 15 秒，连接在本次进程内复用，不会在框选前预热。若仍失败请确认代理本身可用；
4. 必要时通过 `MOZHI_NODES` 更换节点。

## 项目结构

```text
Screenshot-translation/
├── screenshot_translator.py
├── local_translation.py
├── requirements-local.txt
├── scripts/
│   ├── setup_local_translation.sh
│   └── install_local_model.py
├── glossaries/
│   └── robotics_mechanics.json
├── README.md
└── tessdata/
    ├── eng.traineddata
    ├── chi_sim.traineddata
    └── configs/
```

## 说明

默认本地模型解决网络接口不可用的问题，重复内容优先使用缓存。
术语表可以提高专业词汇的一致性，重要论文中的复杂论证仍需结合原文核对。
