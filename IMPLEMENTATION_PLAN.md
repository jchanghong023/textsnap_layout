# TextSnap Layout Windows 11 离线截图 OCR 实施计划

## 1. 目标与交付边界

- 在 ARM64 Linux 开发机上构建 `TextSnapLayout-0.1.0-win-x64.zip`。
- 解压后双击根目录 `TextSnapLayout.exe` 即可运行，无需安装 Python、Paddle、VC++ 运行库或联网下载模型。
- 首版只保证 Windows 11 x64、Intel i7-13700、CPU/AVX2/MKL 环境。
- 使用原始 `PP-OCRv6_medium_det` 与 `PP-OCRv6_medium_rec`，不增加方向分类、版面分析或语言模型。PP-OCRv6 medium 统一支持中文、英文、日文及拉丁语种，官方也提供 Windows x64 CPU 推理支持。([PP-OCRv6 说明](https://www.paddleocr.ai/latest/en/version3.x/algorithm/PP-OCRv6/PP-OCRv6.html)，[Windows CPU 安装说明](https://www.paddlepaddle.org.cn/documentation/docs/zh/install/pip/windows-pip_en.html))
- 运行时完全离线：无上传、更新检查、模型下载、遥测、截图文件、OCR 历史或运行日志。
- 交付无数字签名，附 ZIP 的 SHA-256；接受首次运行出现 SmartScreen“未知发布者”提示。

## 2. 架构与稳定接口

### 2.1 运行时与依赖

固定核心版本并将所有直接、间接依赖锁定到具体 Windows wheel URL 与 SHA-256：

- CPython 3.13.14 Windows x64 embeddable package。官方提供对应的 64 位嵌入式包。([Python 3.13.14](https://www.python.org/downloads/release/python-31314/))
- PaddlePaddle CPU 3.3.1。
- PaddleOCR 3.7.0。
- PaddleX 3.7.2，仅安装 `ocr-core` 所需依赖。
- PySide6 Essentials 6.11.1，不携带 Qt Addons。
- OpenCV Contrib 4.10.0.84、NumPy 2.2.6。
- Noto Sans Mono CJK SC Regular，固定 `Sans2.004` 静态 OTF；ASCII 半宽、CJK 全宽，随包附 OFL 1.1。([字体发布页](https://github.com/notofonts/noto-cjk/releases)，[OFL 许可证](https://github.com/googlefonts/noto-cjk/blob/main/Sans/LICENSE))

版本兼容性决定（2026-07-25）：

- 原草案固定的 NumPy 1.26.4 没有 CPython 3.13 的 Windows x64 wheel，不能装入 CPython 3.13.14 嵌入运行时。目标锁必须使用 `numpy-2.2.6-cp313-cp313-win_amd64.whl`，不得让解析器自动选择其他版本。NumPy 2.2.6 同时满足 PaddlePaddle 3.3.1 的 `numpy>=1.21`、PaddleX 3.7.2 的 `numpy>=1.24,<2.4` 和 OpenCV Contrib 4.10.0.84 在 Python 3.12 及以上的 `numpy>=1.26.0` 约束。该结论已完成 wheel 元数据和目标标签检查；原生导入及 OCR 运行仍须按第 5 节验证。([NumPy 1.26.4 文件列表](https://pypi.org/project/numpy/1.26.4/)，[NumPy 2.2.6 文件列表](https://pypi.org/project/numpy/2.2.6/))
- PaddleX 3.7.2 无条件依赖 `aistudio-sdk>=0.3.5`。当前解析到的 `aistudio-sdk` 0.3.8 wheel 将许可证标为 `UNKNOWN`，包内没有许可证文本，发布页也没有可核验的源码仓库或再分发许可。因此它目前是发布合规阻塞项：在发布者提供可核验的再分发授权，或用户另行批准改变依赖边界之前，不得把该 wheel 放入可分发 ZIP，不得依据第三方聚合站点猜测其许可证，也不得把许可证收集步骤标记为通过。([aistudio-sdk 0.3.8 发布页](https://pypi.org/project/aistudio-sdk/0.3.8/))

应用采用单进程 PySide6。模型在专用 `QThread` 中创建、预热并常驻，主线程只处理热键、截图和窗口事件；同时只允许一个 OCR 任务。

### 2.2 稳定配置和启动接口

`data/settings.json` 采用版本化结构：

```json
{
  "schema_version": 1,
  "hotkey": {
    "modifiers": ["Ctrl", "Alt"],
    "key": "O"
  },
  "autostart": false
}
```

- 除该文件外，不在程序目录创建运行日志、截图、结果或缓存。
- 配置损坏时使用内存默认值并显示提示；只有用户保存设置时才覆盖文件。
- 程序目录不可写时阻止启动并提示移动到用户可写目录，不回退到注册表或 LocalAppData。
- 无参数启动：创建主实例；如果主实例已存在，则通知它打开设置窗口并退出。
- `--autostart`：后台启动，不显示首次启动通知。
- 开机启动使用当前用户的 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，关闭选项时删除且只删除本应用值。
- 单实例使用当前用户会话范围的 Win32 named mutex，加 `QLocalServer` 传递 `open-settings` 命令。

### 2.3 核心内部类型

建立明确的数据边界：

- `CaptureFrame`：屏幕物理像素、显示器标识、物理坐标原点和 DPI。
- `DetectionCandidate`：全局四边形、检测分数、来源瓦片和内部边缘距离。
- `RecognizedSpan`：四边形、文字、检测/识别分数、最终旋转角。
- `LayoutResult`：唯一的二维纯文本和非内容型统计数据。
- `TaskOutcome`：`Success | Empty | Cancelled | Failure`，失败对象不得携带截图或 OCR 文本。
- 应用状态分为模型状态 `Loading/Ready/Error` 与任务状态 `Idle/Capturing/Recognizing`，禁止隐式并发。

## 3. 实施内容

### 3.1 Windows 常驻、热键和截图

- 在创建 `QApplication` 前启用 `PerMonitorV2` DPI awareness。
- 默认注册 `Ctrl+Alt+O`；设置窗口通过按键录制器修改。新快捷键必须先成功注册，再释放旧快捷键；冲突时保留旧值并明确提示。
- 首次启动只驻留托盘，并显示一次“已启动，按 Ctrl+Alt+O 截图”；托盘菜单包含“截图识别、设置、开机启动、退出”。
- 模型在托盘出现后后台校验、加载并预热。模型尚未就绪时仍可截图，识别提示等待模型就绪。
- 热键触发后先隐藏本应用窗口、刷新桌面合成，再通过 Win32 GDI `BitBlt + CAPTUREBLT` 抓取鼠标所在显示器的物理像素；不支持跨屏框选、不包含鼠标指针。
- 在冻结图像上显示无边框遮罩和十字光标；支持任意拖动方向，`Esc` 或右键取消，松开左键立即提交。小于 8×8 像素的选区按取消处理。
- 仅抓取普通交互桌面；UAC 安全桌面、受保护视频、DRM 或被系统返回为黑色的表面明确列为不支持。
- 已有结果窗在新截图前隐藏但暂存于内存；新任务取消、为空或失败时恢复旧结果，成功后才替换。

### 3.2 OCR 管线

- 启动时验证两个本地模型目录及 SHA-256；缺失或损坏时直接失败，绝不调用远程模型解析。
- 使用 PaddleOCR 的 `TextDetection`、`TextRecognition` 本地模块，显式指定 CPU、MKL-DNN、10 个推理线程和本地模型目录。
- 文档方向、去畸变、文字方向分类全部关闭，只运行指定 det/rec。
- 小选区单次检测；任一边超过 1216 像素时，按 1216×1216、128 像素重叠生成瓦片，保证末端瓦片覆盖图像边界。
- 检测初始参数固定为：
  - `limit_type=max`
  - `limit_side_len=1216`
  - `thresh=0.3`
  - `box_thresh=0.5`
  - `unclip_ratio=1.5`
- 将检测框映射回选区全局坐标：
  - IoU ≥ 0.4 或交集/较小面积 ≥ 0.6 的候选归为重复框。
  - 优先保留不接触内部瓦片边缘、边缘距离更大、检测分数更高的候选。
  - 横跨瓦片接缝且垂直重叠 ≥ 0.6、文字高度相近的片段合并为一个全局框，再从原始选区重新裁剪，避免长行重复或断裂。
- 四边形透视矫正后批量识别，批大小固定为 8。
- 正常横排识别一次；高宽比超过 1.3 的疑似竖排框额外尝试 90°/270°；普通框首次分数低于 0.5 时尝试 180°，取最高置信结果。
- `text_rec_score_thresh=0.0`：仅丢弃空字符串，不因低置信度静默漏掉小字或代码符号。
- 不做拼写纠正、繁简转换、语言模型补全或字符替换。
- OCR 期间只显示“正在识别…”和取消按钮，`Esc` 同样取消。取消在当前不可中断的 Paddle 调用结束后，于瓦片或识别批次检查点生效。
- 识别中再次按热键只提示“正在识别”，不排队、不取消当前任务。
- 退出程序时先请求取消；等待当前推理调用安全返回。超过 10 秒后才提供显式“强制退出”。

### 3.3 二维纯文本布局

最终结果只生成一份“布局保真纯文本”，不提供阅读顺序模式。

1. 根据框的基线、中心和垂直重叠聚类成文字行；垂直重叠达到 0.45，或基线差不超过正文中位高度的 0.5 时允许归入同一行。
2. 同一行内严格按全局 x 坐标排序；跨分栏但 y 坐标相近的框仍处于同一输出行，以大量空格维持栏距。
3. 用 `框宽 ÷ Unicode 显示单元数` 的截尾中位数估算半角网格宽；CJK 计两个半角单元，ASCII 计一个。
4. 将全局 x 映射到半角网格列；检测框间距四舍五入为空格，碰撞时后一个框右移到不覆盖前一框的位置。
5. 使用正文中位行距作为 y 网格，将段落、图片区和不等高分栏产生的纵向距离按比例映射为空行。
6. 只裁掉所有文字共同的顶部和左侧外边距；保留行首缩进、栏距和内部空行。
7. 每行去掉无视觉意义的右侧尾随空格，保留所有内部空格；最终文本不添加多余结尾换行。
8. 对控制字符做安全处理：检测框内部的 Tab 和换行转换为空格，不修改普通 Unicode 字符。

布局核心必须是与 Qt、Paddle 解耦的纯 Python 模块，以便使用构造坐标和固定 OCR 结果进行严格单元测试。

### 3.4 结果和错误界面

- 结果窗在截图所在显示器工作区居中，初始宽高均为工作区约 80%，弹出时激活但不永久置顶。
- 使用只读 `QPlainTextEdit`、Noto Sans Mono CJK SC 12pt、`NoWrap`，水平和垂直滚动条按需显示。
- 支持标准鼠标选择、右键复制、`Ctrl+C`、`Ctrl+A`，并提供“复制全部”按钮；复制后不自动关闭。
- “复制全部”复制布局模块生成的原文，不二次格式化。
- 关闭结果窗时清空控件和内存引用；剪贴板内容由 Windows 管理，不自动清除。
- 空结果、截图失败或 OCR 失败只显示简短提示，不打开空白窗口、不覆盖旧结果。
- 不写错误日志。错误窗口可在内存中生成“复制诊断信息”，只包含应用版本、系统版本、错误类型和去敏后的应用内部堆栈，不包含截图、识别文字或用户路径。
- 设置窗口只暴露快捷键、开机启动、模型状态和“重试加载”；不暴露 OCR 阈值等高级参数。

### 3.5 离线和隐私约束

- 全部模型通过绝对本地路径加载；禁用 Hugging Face、ModelScope、PaddleX 遥测及在线模式。
- 在 Python 启动层拒绝 AF_INET/AF_INET6 连接；Qt 本地命名管道不受影响。
- 解释器必须以 `-B` 启动，并在应用入口设置 `sys.dont_write_bytecode = True` 作为纵深保护；构建阶段预生成所需字节码。不得只依赖 `PYTHONDONTWRITEBYTECODE=1`，因为 `-I` 隔离模式隐含 `-E`，会忽略 `PYTHON*` 环境变量。
- 对程序目录做运行前后文件快照测试，正常 OCR 流程只允许 `settings.json` 因用户操作变化。
- 截图只以 `QImage`/NumPy 数组存在，绝不传给临时文件 API；布局完成或任务终止后释放所有引用。
- 不声称对 Python/原生库的已释放内存执行安全擦除；保证范围是“不上传、不主动持久化”。

## 4. Linux 构建、打包与实施顺序

### 4.1 代码组织

按子系统组织为：

- `src/textsnap/`：应用状态、Windows 集成、PySide6 界面、OCR 工作线程、布局和隐私策略。
- `native/`：Unicode Win32 GUI launcher、图标和资源脚本。
- `scripts/`：依赖锁定、模型获取、Windows staging、许可证收集、静态验证和确定性 ZIP 构建。
- `tests/`：纯单元测试、端到端 OCR 测试、HTML/CSS 样本及 Windows 验收脚本。
- `vendor-lock/`：核心资源及所有 wheel/model/font 的 URL、版本、许可证和 SHA-256，不直接提交数百 MiB 二进制。

### 4.2 构建流程

1. 在 ARM64 Linux 安装 x86_64 MinGW 交叉工具链；编译 `TextSnapLayout.exe` 为 PE32+ x86-64 GUI subsystem，使用宽字符 API 定位自身目录并启动 `runtime/pythonw.exe -I -B app/main.py`。
2. 下载并校验固定的 CPython embeddable package、Windows wheels、两个模型和字体。
3. 通过完整的 win-x64 wheel lock，以 `--no-deps` 方式安装到 staging，避免在 ARM Linux 上错误解析平台条件。
4. 只保留 Qt Widgets 所需模块、`qwindows` 平台插件、必要图像插件和样式；不删除 Paddle/PaddleOCR 运行所需文件。
5. 配置 `python313._pth`，只允许包内标准库、`site-packages` 和应用源码，忽略系统 Python、用户 site 和环境变量。
6. 生成以下便携目录：

```text
TextSnapLayout/
├── TextSnapLayout.exe
├── app/
├── runtime/
├── models/
│   ├── PP-OCRv6_medium_det/
│   └── PP-OCRv6_medium_rec/
├── assets/fonts/
├── data/
├── LICENSES/
├── README.zh-CN.md
└── BUILD_MANIFEST.json
```

7. 收集 Python、Paddle、PaddleOCR、PaddleX、Qt/PySide、OpenCV、Noto 及所有传递依赖的许可证和版本；任何依赖缺少可核验的再分发许可都必须使构建失败。当前不得绕过 2.1 节记录的 `aistudio-sdk` 发布合规阻塞。
8. 静态验证所有 `.exe/.dll/.pyd` 都是 x86-64，检查 PE 导入依赖、模型文件完整性、ZIP 内路径和重复 DLL。
9. 使用固定文件顺序和时间戳生成确定性 ZIP，并输出同名 `.sha256`。
10. 不使用 PyInstaller、Nuitka、Wine、单文件自解压或 Windows 安装器。

### 4.3 实施阶段

1. 建立项目骨架、依赖锁和纯布局类型。
2. 实现布局算法、瓦片生成、检测去重和状态机，并完成纯单元测试。
3. 在 ARM64 Linux 安装同版本 Paddle CPU wheel，接入真实 det/rec 和固定样本回归。
4. 实现 Windows DPI、GDI 截图、遮罩、热键、托盘、单实例和开机启动。
5. 完成结果窗口、取消流程、旧结果恢复、隐私和离线保护。
6. 构建 win-x64 便携目录和 ZIP，执行静态包验证。
7. 交付 Windows 验收清单；根据实机失败截图和期望文字修正参数或坐标逻辑，不更换指定模型和总体架构。

## 5. 测试、验收与风险

### 5.1 自动化测试

- 状态机：预加载、截图、取消、空结果、失败恢复、成功替换、重复热键和退出。
- 设置：默认值、损坏配置、原子保存、热键冲突、开机启动启停。
- 瓦片：边界覆盖、重叠宽度、任意尺寸、接缝重复框及长行片段合并。
- 方向：横排、竖排、180°、低置信度重试和最高分选择。
- 布局：
  - 打乱输入框后仍按坐标排序。
  - 多框同一行正确合并。
  - 中文全角与 ASCII 半角对齐。
  - 代码缩进、明显空格和长行保留。
  - 双栏不串成单段。
  - 外边距裁剪、纵向空行、尾随空格清理。
  - 结果窗宽度变化不会改变真实换行。
- 隐私：模拟全部文件写入，断言正常任务除显式设置保存外不落盘；模拟网络连接必须立即失败。
- Linux 真实 OCR：使用与 Windows 包相同的模型、PaddleOCR 和参数，在断网环境运行。
- 固定测试语料由受控 HTML/CSS 页面生成，覆盖中文文档、英文网页、中英混排、代码、长行、缩进、双栏、反色文字和 4K 小字。
- 清晰高对比样本验收：
  - 不丢失文字行。
  - 4K 分块无接缝重复或截断。
  - 正文字符错误率目标不高于 2%；若指定模型自身无法达到，保留模型并报告真实基线。
  - 布局锚点与预期网格偏差不超过一个半角单元或一行。
- 连续执行两次构建，在依赖缓存和锁文件相同时 ZIP SHA-256 必须一致。

### 5.2 Windows 实机验收

由用户在指定 i7-13700 Windows 11 机器验证：

- 无系统 Python、断网状态下解压启动，无控制台窗口。
- 首次托盘通知、第二次启动打开设置、单实例、退出流程正确。
- `Ctrl+Alt+O`、修改热键、冲突提示和开机启动启停正确。
- 100%、125%、150%、200% DPI 下选区与实际截图像素一致。
- 热键时画面冻结；拖动、反向拖动、Esc、右键、小选区行为正确。
- 网页、文档、代码、长行、双栏样本的识别和二维排版符合预期。
- 局部复制、复制全部、水平滚动和窗口缩放不引入真实换行。
- 识别中重复热键、取消、失败和空结果均不覆盖旧结果。
- 程序目录中不出现截图、文字、日志或运行缓存；网络资源监视器无外连。
- 记录后台预加载时间、常驻内存、OCR 峰值内存、普通选区和 4K 全屏耗时；首版不设硬性延迟门槛，但界面必须保持响应且允许取消。
- 在包含空格和中文字符的解压路径中运行。
- 接受并记录无签名程序的 SmartScreen 行为。

### 5.3 已接受的代价与剩余风险

- 官方 Paddle、Qt、模型和字体会使 ZIP 达到数百 MiB，解压后可能接近或超过 1 GiB。
- 后台预加载和模型常驻会占用数百 MiB 内存。
- 4K 原分辨率分块可能需要十几秒或更久，精度优先于固定延迟。
- 纯文本只能近似像素布局，无法恢复真实 Tab、CSS、字体大小、表格线、HTML 或交互。
- 无代码签名，SmartScreen 无法从代码层消除。
- ARM64 Linux 可以验证 OCR、布局和包结构，但不能证明 Windows DLL 加载、全局热键、托盘、DPI/GDI 截图已经成功；这些结论必须以用户实机结果为准。
- 如果 QThread 中的 Paddle 原生调用导致主界面无法重绘，该问题会重新打开“单进程架构”决策；在出现实证前不擅自改为 Python 子进程或第二套运行时。
- 如果用户移动或删除程序目录前未关闭开机启动，HKCU 中可能留下失效路径；README 必须说明先关闭开机启动，重新运行已移动程序时则自动更新路径。
- PaddleX 3.7.2 的传递依赖 `aistudio-sdk` 0.3.8 尚无可核验的再分发许可；在 2.1 节的合规门禁解除前，可以继续进行不包含该发布物的代码和测试工作，但不能生成或发布声称许可证完整的最终 ZIP。
