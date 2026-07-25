# TextSnap Layout 项目协作指南

## 项目状态与事实来源

- 本仓库用于实现 Windows 11 x64 离线截图 OCR 工具 TextSnap Layout。
- 当前已完成计划范围内的应用代码、测试、依赖锁和跨平台静态构建流水线；最终
  发布仍受下述合规门禁及 Windows 11 x64 实机验收约束。
- 开始任何实现前，完整阅读根目录的 `IMPLEMENTATION_PLAN.md`。该文档是当前产品范围、架构、接口、实施顺序和验收要求的事实来源。
- 本文件只提炼协作约束，不替代实施计划。若二者出现实质冲突，停止实施并向用户确认，不得自行改写既定产品决策。
- 不因初始化、整理文档或处理单个阶段而提前创建无关代码和配置。
- 2026-07-25 已确认并修订版本兼容性：CPython 3.13.14 对应的 NumPy 固定版本为 2.2.6；不得恢复为没有 cp313 Windows x64 wheel 的 NumPy 1.26.4。
- 当前存在发布合规门禁：PaddleX 3.7.2 的传递依赖 `aistudio-sdk` 0.3.8 尚无可核验的再分发许可。可以继续不包含该发布物的代码和测试工作，但在获得发布者授权或用户批准改变依赖边界前，不得将其放入最终 ZIP，也不得声称许可证收集已通过。

## 已确定的交付边界

- 目标产物是从 ARM64 Linux 构建的 `TextSnapLayout-0.1.0-win-x64.zip`。
- 产物解压后应能在 Windows 11 x64 上直接运行，且不依赖系统 Python、额外运行库或在线模型下载。
- 应用采用单进程 PySide6；Paddle 模型在专用 `QThread` 中创建、预热并常驻，同时最多执行一个 OCR 任务。
- OCR 只使用计划指定的本地 `PP-OCRv6_medium_det` 和 `PP-OCRv6_medium_rec`，不得擅自更换模型、增加语言模型或启用在线解析。
- 运行时必须离线：不上传、不遥测、不检查更新、不下载模型，也不主动持久化截图、OCR 文本、历史或日志。
- 除用户明确保存的 `data/settings.json` 外，正常运行不得在程序目录产生可变文件。
- 不使用 PyInstaller、Nuitka、Wine、单文件自解压包或 Windows 安装器。
- Windows 原生行为必须由 Windows 11 x64 实机验收；ARM64 Linux 上的测试和静态检查不能替代该结论。

## 预期代码边界

按实施计划建立目录，不随意改成按页面或通用“工具类”组织：

- `src/textsnap/`：状态、Windows 集成、界面、OCR 工作线程、布局及隐私策略。
- `native/`：Unicode Win32 GUI launcher、图标和资源脚本。
- `scripts/`：依赖锁定、资源获取、Windows staging、许可证收集、静态验证和确定性打包。
- `tests/`：纯单元测试、真实 OCR 回归、固定样本和 Windows 验收脚本。
- `vendor-lock/`：资源 URL、版本、许可证和 SHA-256；不直接提交大型第三方二进制。

必须维持以下模块边界：

- 布局算法是与 Qt、Paddle 和 Win32 解耦的纯 Python 模块。
- `CaptureFrame`、`DetectionCandidate`、`RecognizedSpan`、`LayoutResult` 和 `TaskOutcome` 是明确的数据边界；跨层传递结构化对象，避免松散字典。
- 模型状态与任务状态分别建模，禁止用隐式布尔组合表达并发状态。
- Windows 专用导入和系统调用应隔离，使纯布局、分块、去重、状态机和配置测试可在 Linux 运行。
- 截图仅以内存图像/数组传递；失败对象、诊断信息和异常消息不得携带截图、OCR 文本或用户路径。

## 实施规则

- 按 `IMPLEMENTATION_PLAN.md` 第 4.3 节的阶段顺序推进；一个阶段只引入支撑该阶段所需的最小结构。
- 对计划已经固定的版本、阈值、路径、配置结构、热键行为和打包方式，精确实现，不以“更常见”的替代方案覆盖。
- 若依赖版本或官方资源已经不可用、互不兼容或与目标平台不符，先提供可复现证据并请求决策，不得静默升级或降级。
- 所有 Windows wheel、Python 嵌入包、模型、字体及传递依赖都必须锁定具体 URL、版本、许可证和 SHA-256。
- 许可证锁必须以发布者或上游项目的可核验材料为依据；元数据为 `UNKNOWN` 或缺少许可证文本时必须使可分发构建失败，不得用第三方聚合站点的标签补全。
- 构建脚本必须适用于 ARM64 Linux 主机和 win-x64 目标；不得在宿主机上错误解析或安装 Windows 平台依赖。
- 生产路径应有与风险相称的输入校验、异常处理、资源释放和取消检查；不要吞掉异常。
- 设置保存应采用可恢复的原子写入。配置损坏时仅回退到内存默认值，用户保存前不得覆盖原文件。
- 不新增网络功能、持久化、遥测、日志、缓存或隐私例外。确有调试需要时使用不含敏感数据的内存诊断，并确保发布路径默认关闭。
- 保留用户已有文件和未提交改动；只做当前任务必要的修改，不进行顺手重构。

## 测试与验证

- 每项实现都应附带与其风险相称的测试；优先使用构造坐标、固定 OCR 返回值和受控 HTML/CSS 样本，保证测试确定性。
- 纯 Python 测试至少覆盖布局、瓦片边界、检测去重/接缝合并、方向选择、状态机和设置读写。
- 隐私测试必须验证网络连接被拒绝，以及正常 OCR 流程除显式设置保存外不落盘。
- OCR 集成测试使用与 Windows 包相同版本、模型和参数，并在断网环境运行。
- 打包验证必须检查 PE 架构、导入依赖、模型哈希、许可证、ZIP 路径、重复 DLL 和确定性构建。
- 修改后先运行最窄的相关检查，再运行当前阶段可用的完整检查。检查失败时报告实际失败，不得以静态阅读代替执行结果。
- 涉及 Win32 热键、DPI/GDI 截图、托盘、单实例、开机启动、DLL 加载或 SmartScreen 的结论，应明确标记为“待 Windows 实机验证”，直到取得对应测试结果。
- 不使用真实用户截图或识别文本作为仓库测试夹具。

## 当前命令约定

- ARM64 Linux 纯 Python 与静态回归：
  `PYTHONPATH=src python3 -B -m unittest discover -s tests -v`
- 安装 PySide6 Essentials 6.11.1 与 NumPy 2.2.6 后的 Qt 回归：
  `QT_QPA_PLATFORM=offscreen PYTHONPATH=src python3 -B -m unittest tests.test_qt_instance tests.test_qt_worker tests.test_ui_widgets tests.test_controller -v`
- 真实 OCR 回归及所需环境变量见 `README.zh-CN.md`；ARM64 兼容性结果不能替代
  Windows x64 原生验收。
- 非交互构建入口为 `python3 -B -m scripts.build_release`，支持
  `validate-lock`、`fetch`、`validate-wheel-closure`、`stage`、`verify`、
  `package` 和 `all`；参数及执行顺序见 `README.zh-CN.md`。
- 当前许可证门禁未通过，只允许
  `stage --profile nonredistributable-test` 和后续 `verify`；`package` 必须失败且
  不得生成最终 ZIP。
- 命令依赖的 PySide6、Paddle、精确 CPython 3.13.14、MinGW、缓存、模型和字体
  不得假定已安装；执行前按 `README.zh-CN.md` 检查实际环境。

## 完成标准

一个实施任务只有在以下条件满足时才算完成：

1. 变更保持计划中的接口、离线、隐私和平台边界。
2. 已增加或更新必要测试，并实际运行适用检查。
3. 已检查差异，未引入无关文件、缓存、大型二进制或敏感数据。
4. 已区分 Linux 已验证项、静态验证项和待 Windows 实机验证项。
5. 交付说明包含完成内容、验证结果、剩余限制及用户需要执行的实机步骤（如适用）。
