# 资源与依赖

新 MAA 工作流使用 LangGraph、langgraph-checkpoint、langgraph-checkpoint-sqlite、langchain-core 与 comtypes。保留现有 DeepSeek 传输适配；LangChain 组件按需使用。随包许可见 `licenses/`；本地任务默认不开启云端 tracing。MAA 为用户另行安装的外部工具，不随本包分发，不代表本项目重新实现了其游戏能力。

“黍”是本桌面助手的项目名称。本公开版本仅提供用户新设计的青芽毛衣女款、男款，动作图集使用 AI 辅助制作；不包含此前以游戏角色为参考的桌宠皮肤或图标。游戏名称、商标及独立安装的游戏内容归相应权利方，项目与游戏官方没有隶属关系。

桌面界面使用 Qt for Python / PySide6 与 Shiboken6，版本 6.12.0，由 The Qt Company 等贡献者提供，采用其分发包附带的开源许可。独立包保留动态 Qt 库，相关许可文本见 `licenses/`。

本公开包对适用的Qt/PySide6组件采用LGPLv3选项动态使用，完整许可与对应源码/替换信息见`licenses/`和`docs/DEPENDENCY_SOURCES.md`，不以商业许可选项作为本项目的授权依据。

打包工具为 PyInstaller 6.22.3，其 bootloader 使用 GPL 许可及允许分发所生成程序的例外。Python 运行时随程序分发，许可文本见 `licenses/`。

早期基线没有包含 MaaFramework、MAA 或 DeepSeek Harness 运行时。0.3.17-live 显式带入 MaaFramework 5.15.0 动态 SDK，用于图片 OCR 和后台窗口输入；它不调用游戏或 MAA 的内部账户接口。原 SDK 动态库与 Python 包位于 `_internal/optional-runtime/5.15.0`，保留 LGPL-3.0 许可，可替换为接口兼容构建进行调试。上游源码：https://github.com/MaaXYZ/MaaFramework/tree/v5.15.0 。SDK自身的LGPL许可在该目录的dist-info/licenses保留；补齐的native依赖版权/SPDX在licenses/native/MaaDeps-v3.0.2，Demo插件MIT通知在licenses/native/MaaPluginDemo；SDK未修改。StrEnum 0.4.15 的 MIT 许可同目录保留。

OCR 资源来自 MaaCommonAssets 固定提交 `dabcd4681ac990dc4361de26416d986abd80e4aa` 的 PP-OCRv4 zh_CN 模型，来源：https://github.com/MaaXYZ/MaaCommonAssets/tree/dabcd4681ac990dc4361de26416d986abd80e4aa 。上游 MIT 与 PaddleOCR Apache-2.0 许可位于 `_internal/assets/maa-framework/licenses`。模型和 SDK 文件 SHA-256 随独立资源清单保存。后台窗口输入使用 SDK 的 AnchoredTouch 等已实现适配，并由黍的 GUI worker 与动作账本管理。

后台窗口捕获使用 windows-capture 2.0.1（MIT），其 Python 依赖包含 NumPy 2.5.4 与 opencv-python 5.0.0.93。发行包收集上游 wheel 中提供的许可证文本，见 `licenses/`。本项目使用 HWND 对应的 Windows Graphics Capture，不通过游戏注入或内存读取获取画面。


## Optional local GPU speech runtime (not bundled in application ZIP)

Kokoro-82M v1.1-zh and voices: https://huggingface.co/hexgrad/Kokoro-82M-v1.1-zh (Apache-2.0). ONNX conversion: https://github.com/thewh1teagle/kokoro-onnx ; models from its model-files-v1.1 release. Misaki: https://github.com/hexgrad/misaki (Apache-2.0). ONNX Runtime: https://github.com/microsoft/onnxruntime (MIT). NumPy: https://numpy.org (BSD-3-Clause). CUDA and cuDNN wheels are subject to NVIDIA license terms; installed runtime contains their supplied license files. See `.shu-tts` installation ledger for exact versions, upstream licenses and checksums. No game-character voice recordings or voice cloning are used.


## 本次公开分发复核

完整的Python/native依赖版权和NOTICE见licenses/及THIRD_PARTY_INVENTORY.json。为避免多个LICENSE同名覆盖，许可材料保留上游目录结构。部分依赖适用MPL/LGPL，源码及替换信息见docs/DEPENDENCY_SOURCES.md。未使用的Qt Virtual Keyboard、其QML/Quick链及Qt PDF图片插件不随本公开包分发；两张来源未登记且未被程序调用的队列按钮截图也已移除。系统运行中从使用者本机MAA读取的模板并非本仓库分发的游戏资源。

公开人物素材由使用者提供新设计并经AI辅助生成动作；项目保留其本地制作证据。本说明不代表已对所有可能存在的第三方权利作法律鉴定。开源依赖可用和游戏运营方允许自动化是不同问题，见docs/PUBLICATION_REVIEW.md。
