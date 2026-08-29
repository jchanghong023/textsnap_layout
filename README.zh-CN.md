# TextSnap Layout

TextSnap Layout 是仅支持 Windows 11 x86-64（x64）、Intel Core i7-13700 的离线
截图 OCR 工具。它常驻系统托盘，
通过全局快捷键截取鼠标所在显示器的一块区域，使用包内
`PP-OCRv6_small_det` 和 `PP-OCRv6_small_rec` 识别，并输出保留二维间距的
纯文本。

当前仓库已经包含应用、测试、原生启动器、精确依赖锁和 Windows x64 原生便携
构建流水线。项目只支持 Windows 11 x86-64（x64）、Intel Core i7-13700，不支持 Linux、
Windows ARM64 或其他 ARM 环境；只用于个人多台电脑，不面向外部发布。

## 运行方式

完成构建后，交付物为
`TextSnapLayout-0.1.0-win-x64.zip` 及同名 `.sha256`。

1. 核对 SHA-256，将 ZIP 完整解压到当前用户可写目录。
2. 双击根目录 `TextSnapLayout.exe`；无需系统 Python、VC++ 运行库或联网下载。
3. 默认按 `Ctrl+Alt+O`，在冻结画面上拖动选区。`Esc` 或右键取消；任一边小于
   8 个物理像素的选区也按取消处理。
4. 从托盘打开设置，可修改快捷键、开机启动并查看或重试模型状态。
5. 结果窗口支持标准选择、`Ctrl+C`、`Ctrl+A` 和“复制全部”，窗口缩放不会改变
   原始换行；点击“复制全部”后结果窗口自动关闭。

程序只允许用户明确保存的 `data/settings.json` 发生变化；不上传、不遥测、
不检查更新、不下载模型，也不主动保存截图、OCR 文本、历史或默认运行日志。
隐私保证是“不上传、不主动持久化”，不声称对已释放内存执行安全擦除。

无控制台的 GUI 启动或模型加载需要排障时，可显式启用包外 JSONL 阶段日志：

```powershell
$env:TEXTSNAP_DIAGNOSTIC_LOG = Join-Path $env:TEMP 'TextSnapLayout-diagnostic.jsonl'
.\TextSnapLayout.exe
```

日志逐行记录进程、单实例、离线保护、Qt、托盘、热键、模型加载、OCR 任务和退出
阶段的 UTC 时间、累计耗时及成功/失败状态。它不记录截图、OCR 正文、异常消息或
用户路径，也不会写入程序目录。完成复现并从托盘退出后，可执行
`Remove-Item Env:TEXTSNAP_DIAGNOSTIC_LOG` 恢复默认无日志模式。若日志中连
`process.start` 都不存在，应先确认目标为包外绝对路径、父目录存在且当前用户可
写；这些条件均满足时，故障才发生在嵌入式 Python 进入应用代码之前。

若要移动或删除程序目录，请先关闭开机启动。保持开机启动后移动目录时，运行新
位置会更新本应用自己的 HKCU Run 值。

UAC 安全桌面、DRM/受保护视频和系统返回的全黑表面不受支持。程序未签名，
SmartScreen 可能显示“未知发布者”。

## 开发验证

以下命令均从仓库根目录运行。基础纯 Python/静态测试不假定宿主已安装 Qt 或
OCR 运行时；相应测试会明确跳过：

```powershell
$env:PYTHONPATH = 'src'
py -3.13 -B -m unittest discover -s tests -v
```

Qt 联测需要 PySide6 Essentials 6.11.1 和 NumPy 2.2.6：

```powershell
$env:QT_QPA_PLATFORM = 'offscreen'
$env:PYTHONPATH = 'src'
py -3.13 -B -m unittest `
  tests.test_qt_instance tests.test_qt_worker `
  tests.test_ui_widgets tests.test_controller -v
```

真实 OCR 回归只在 Windows 11 x64、Intel i7-13700 上运行，需要 ONNX Runtime
CPU 1.28.0、发布锁指定的其余依赖、锁定模型和本地字体：

```powershell
$env:PYTHONPATH = 'src'
$env:TEXTSNAP_RUN_REAL_OCR = '1'
$env:TEXTSNAP_TEST_MODEL_ROOT = 'C:\absolute\path\to\models'
$env:TEXTSNAP_TEST_FONT = 'C:\absolute\path\to\local-font.otf'
py -3.13 -B -m unittest tests.integration.test_real_ocr -v
```

## 锁定资源与便携 staging

在 Windows 11 x64、Intel i7-13700 主机安装 x86_64 MinGW 工具链，并准备
**精确 CPython 3.13.14** 宿主可执行文件，用于生成与嵌入运行时 magic 一致的
checked-hash 字节码。先执行 `git lfs pull`，确保 `vendor-models` 下两套 ONNX
模型不是 LFS 指针；缓存和 staging 必须放在仓库外的绝对短路径，避免第三方包
的深层目录触发 Windows 路径长度限制：

```powershell
py -3.13 -B -m scripts.build_release validate-lock `
  --lock-dir vendor-lock

py -3.13 -B -m scripts.build_release fetch `
  --lock-dir vendor-lock `
  --cache-dir C:\ts\cache

py -3.13 -B -m scripts.build_release validate-wheel-closure `
  --lock-dir vendor-lock `
  --cache-dir C:\ts\cache
```

生成个人使用的 staging：

```powershell
py -3.13 -B -m scripts.build_release stage `
  --profile private-use `
  --lock-dir vendor-lock `
  --cache-dir C:\ts\cache `
  --stage-dir C:\ts\b\TextSnapLayout `
  --python-for-bytecode C:\absolute\path\to\python-3.13.14.exe

py -3.13 -B -m scripts.build_release verify `
  --lock-dir vendor-lock `
  --stage-dir C:\ts\b\TextSnapLayout
```

随后生成 ZIP：

```powershell
py -3.13 -B -m scripts.build_release package `
  --lock-dir vendor-lock `
  --stage-dir C:\ts\b\TextSnapLayout `
  --output-dir C:\ts\a
```

流水线只使用锁中的精确 HTTPS URL、大小和 SHA-256，并只接受 Windows x64
平台产物。它检查 wheel RECORD、安全路径、PE 架构与导入、模型哈希、重复 DLL、
BUILD_MANIFEST 和确定性 ZIP。`package` 会重新读取当前锁并复验整个 staging，
不能只信任 staging 内自报的状态。

PE 静态检查只把与导入者位于同目录或明确运行时目录中的 DLL 记为静态可解析；
仅在其他目录发现同名 DLL 时会写入 `load_path_pending`，继续保留为
“待 Windows 加载器实机验证”，不会把全目录同名搜索误报为已验证。确定性范围是
相同源码、锁文件、CPython 3.13.14 字节码构建器、zlib 和清单中记录的 MinGW
工具链版本。

## GitHub Actions 发布

`.github/workflows/release.yml` 使用 GitHub 免费托管的 `windows-2025` x64
Runner 构建，不需要本地或自托管 Runner。工作流通过 `actions/setup-python`
取得精确 CPython 3.13.14，并使用托管镜像内的 x86_64 MinGW、Git LFS 和
GitHub CLI。可选仓库变量 `TEXTSNAP_BUILD_ROOT` 指定短路径构建根目录，默认
`C:\ts`；`TEXTSNAP_BUILD_CACHE` 指定缓存目录，默认 `C:\ts\cache`；工具链
命令带前缀时，通过 `TEXTSNAP_TOOLCHAIN_PREFIX` 指定。

发布支持两种入口：

- 在 GitHub 的 Actions 页面手动运行 **Build and publish release**。工作流从
  `scripts.release_pipeline.PRODUCT_VERSION` 读取版本，在所选提交上创建对应
  `vX.Y.Z` 标签和 Release。
- 推送与源码版本完全一致的标签，例如 `git push origin v0.1.0`。标签版本不一致
  时构建立即失败，不会发布。

工作流获取 Git LFS 模型，依次执行锁校验、资源获取、wheel 闭包校验、staging、
静态验证、确定性打包和 SHA-256 复验，并要求构建 ZIP 的哈希与仓库中同名 LFS
ZIP 完全一致。验证通过后同时保存 Actions Artifact，并把 ZIP 与 `.sha256` 上传
为 GitHub Release 附件。GitHub 托管环境是 Windows Server 2025，不替代计划
要求的 Windows 11/i7-13700 原生行为和 OCR 实机验收。

## Windows 实机验收

Win32 热键、DPI/GDI 截图、托盘、单实例、开机启动、DLL 加载和 SmartScreen
目前均为“待 Windows 11 x64 实机验证”。在新电脑上使用前可逐项执行
[Windows 验收清单](tests/windows/ACCEPTANCE.zh-CN.md)，并可用
`tests/windows/snapshot_bundle.ps1` 检查正常运行前后程序目录是否发生了未授权
变化。
