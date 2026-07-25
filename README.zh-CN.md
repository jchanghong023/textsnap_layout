# TextSnap Layout

TextSnap Layout 是面向 Windows 11 x64 的离线截图 OCR 工具。它常驻系统托盘，
通过全局快捷键截取鼠标所在显示器的一块区域，使用包内
`PP-OCRv6_medium_det` 和 `PP-OCRv6_medium_rec` 识别，并输出保留二维间距的
纯文本。

当前仓库已经包含应用、测试、原生启动器、精确依赖锁和 ARM64 Linux 到
Windows x64 的便携构建流水线。**当前不能生成或分发最终 ZIP**：PaddleX 3.7.2
的传递依赖 `aistudio-sdk` 0.3.8 没有可核验的再分发许可，Qt/Paddle 原生
notices、VC runtime 条款、模型许可关联及部分原生依赖 notices 也仍在许可门禁
中。构建脚本没有忽略门禁的开关。

## 运行方式

发布门禁解除并完成 Windows 实机验收后，交付物应为
`TextSnapLayout-0.1.0-win-x64.zip` 及同名 `.sha256`。

1. 核对 SHA-256，将 ZIP 完整解压到当前用户可写目录。
2. 双击根目录 `TextSnapLayout.exe`；无需系统 Python、VC++ 运行库或联网下载。
3. 默认按 `Ctrl+Alt+O`，在冻结画面上拖动选区。`Esc` 或右键取消；任一边小于
   8 个物理像素的选区也按取消处理。
4. 从托盘打开设置，可修改快捷键、开机启动并查看或重试模型状态。
5. 结果窗口支持标准选择、`Ctrl+C`、`Ctrl+A` 和“复制全部”，窗口缩放不会改变
   原始换行。

程序只允许用户明确保存的 `data/settings.json` 发生变化；不上传、不遥测、
不检查更新、不下载模型，也不主动保存截图、OCR 文本、历史或日志。隐私保证是
“不上传、不主动持久化”，不声称对已释放内存执行安全擦除。

若要移动或删除程序目录，请先关闭开机启动。保持开机启动后移动目录时，运行新
位置会更新本应用自己的 HKCU Run 值。

UAC 安全桌面、DRM/受保护视频和系统返回的全黑表面不受支持。程序未签名，
SmartScreen 可能显示“未知发布者”。

## 开发验证

以下命令均从仓库根目录运行。基础纯 Python/静态测试不假定宿主已安装 Qt 或
Paddle；相应测试会明确跳过：

```bash
PYTHONPATH=src python3 -B -m unittest discover -s tests -v
```

Qt 联测需要 PySide6 Essentials 6.11.1 和 NumPy 2.2.6：

```bash
QT_QPA_PLATFORM=offscreen PYTHONPATH=src \
python3 -B -m unittest \
  tests.test_qt_instance tests.test_qt_worker \
  tests.test_ui_widgets tests.test_controller -v
```

真实 OCR 回归需要 PaddlePaddle 3.3.1、PaddleOCR 3.7.0、PaddleX 3.7.2、
NumPy 2.2.6、锁定模型和本地字体。ARM64 Linux 没有目标 MKL-DNN 等价环境，
因此只允许显式的 `paddle`/单线程兼容性冒烟测试：

```bash
PYTHONPATH=src \
TEXTSNAP_RUN_REAL_OCR=1 \
TEXTSNAP_ALLOW_ARM_COMPAT=1 \
TEXTSNAP_TEST_MODEL_ROOT=/absolute/path/to/models \
TEXTSNAP_TEST_FONT=/absolute/path/to/local-font.otf \
python3 -B -m unittest tests.integration.test_real_ocr -v
```

该 ARM64 结果不能替代 Windows x64 上 MKL-DNN、10 推理线程和 DLL 加载验证。

## 锁定资源与便携 staging

先安装 x86_64 MinGW 交叉工具链，并准备**精确 CPython 3.13.14** 宿主可执行
文件，用于生成与嵌入运行时 magic 一致的 checked-hash 字节码。缓存和 staging
必须放在仓库外的绝对路径：

```bash
python3 -B -m scripts.build_release validate-lock \
  --lock-dir vendor-lock

python3 -B -m scripts.build_release fetch \
  --lock-dir vendor-lock \
  --cache-dir /absolute/path/to/cache

python3 -B -m scripts.build_release validate-wheel-closure \
  --lock-dir vendor-lock \
  --cache-dir /absolute/path/to/cache
```

在当前许可阻塞期间，只能生成不可分发的测试 staging：

```bash
python3 -B -m scripts.build_release stage \
  --profile nonredistributable-test \
  --lock-dir vendor-lock \
  --cache-dir /absolute/path/to/cache \
  --stage-dir /absolute/path/to/build/TextSnapLayout \
  --python-for-bytecode /absolute/path/to/python-3.13.14

python3 -B -m scripts.build_release verify \
  --lock-dir vendor-lock \
  --stage-dir /absolute/path/to/build/TextSnapLayout
```

`nonredistributable-test` staging 含当前不可分发依赖，只能用于本机结构和静态测试；
`package` 子命令会拒绝为其创建 ZIP。许可全部通过后，使用 `release` profile
创建并验证 staging，再执行：

```bash
python3 -B -m scripts.build_release package \
  --lock-dir vendor-lock \
  --stage-dir /absolute/path/to/release/TextSnapLayout \
  --output-dir /absolute/path/to/artifacts
```

流水线只使用锁中的精确 HTTPS URL、大小和 SHA-256，不在 ARM64 Linux 上执行
Windows wheel 代码。它检查 wheel RECORD、安全路径、PE 架构与导入、模型哈希、
重复 DLL、许可证、BUILD_MANIFEST 和确定性 ZIP。`package` 会重新读取当前锁、
重新计算许可门禁并复验整个 staging，不能只信任 staging 内自报的通过状态。

PE 静态检查只把与导入者位于同目录或明确运行时目录中的 DLL 记为静态可解析；
仅在其他目录发现同名 DLL 时会写入 `load_path_pending`，继续保留为
“待 Windows 加载器实机验证”，不会把全目录同名搜索误报为已验证。确定性范围是
相同源码、锁文件、CPython 3.13.14 字节码构建器、zlib 和清单中记录的 MinGW
工具链版本。

## Windows 实机验收

Win32 热键、DPI/GDI 截图、托盘、单实例、开机启动、DLL 加载和 SmartScreen
目前均为“待 Windows 11 x64 实机验证”。发布前必须逐项执行
[Windows 验收清单](tests/windows/ACCEPTANCE.zh-CN.md)，并可用
`tests/windows/snapshot_bundle.ps1` 检查正常运行前后程序目录是否发生了未授权
变化。
