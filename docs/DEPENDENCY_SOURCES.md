# 依赖源码、许可与替换

本程序动态使用Qt/PySide6和MaaFramework。GNU LGPLv3/GPLv3全文在licenses/；逐依赖版权、许可和NOTICE保留原相对路径，目录见THIRD_PARTY_INVENTORY.json。闲置的GPL-only Qt Virtual Keyboard及PDF/QML插件链已从本公开包移除。本声明不将整个应用改授为上游许可证。

## 对应版本源码入口

- PySide6/Shiboken6 6.12.0：[完整源包](https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.12.0-src/pyside-setup-everywhere-src-6.12.0.tar.xz)。
- QtBase 6.12.0：[源包](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/qtbase-everywhere-src-6.12.0.tar.xz)。
- QtSvg 6.12.0：[源包](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/qtsvg-everywhere-src-6.12.0.tar.xz)。
- QtImageFormats 6.12.0：[源包](https://download.qt.io/official_releases/qt/6.12/6.12.0/submodules/qtimageformats-everywhere-src-6.12.0.tar.xz)。
- MaaFramework v5.15.0：固定提交`172ec7b97df3e5b10adf69e36df01f8dc495b6f6`，[仓库](https://github.com/MaaXYZ/MaaFramework/tree/v5.15.0)。取源码应使用`git clone --recursive --branch v5.15.0 https://github.com/MaaXYZ/MaaFramework.git`，保持子模块，不能把不含子模块的GitHub源码tar包当作完整构建输入。
- MaaFramework依赖的MaaDeps v3.0.2：[构建配方](https://github.com/MaaXYZ/MaaDeps/tree/v3.0.2)，固定提交`cec43f04c295054e0aa3dce6e5f685b7b64f5be8`，vcpkg子模块`c5a15727ee70fddf0296f0d8aafc3f58916fefac`。本包native许可目录内保留逐组件SPDX来源/版本记录；SDK中的依赖并不由本项目声称重新实现。
- MaaUtils子模块固定提交`d76f93ead7f75494d8d5a939d920c4b524d75326`，以MaaFramework的.gitmodules登记来源递归取出。
- certifi 2026.10.23（MPL-2.0）：[源码标签](https://github.com/certifi/python-certifi/tree/2026.10.23)。
- orjson 3.13.0：含MPL-2.0组件及Apache-2.0/MIT选择，见其完整版权文本；[固定源码](https://github.com/ijl/orjson/tree/3.13.0)。
- ZeroMQ 4.3.5（MaaDeps port 4.3.5#2，MPL-2.0）：[固定源码](https://github.com/zeromq/libzmq/tree/v4.3.5)，还应保留MaaDeps/vcpkg对应port配方与补丁；逐组件SPDX来源见`licenses/native/MaaDeps-v3.0.2`。
- PP-OCRv4中文模型：MaaCommonAssets固定提交`dabcd4681ac990dc4361de26416d986abd80e4aa`，详见assets/maa-framework/resource-manifest.json及其MIT/Apache-2.0许可。

上述Qt完整源包的下载地址已检查可达；源码版本、二进制来源及子模块仍须由实际发布者持续保持一致。上游链接失效时，应补充等价源码访问或自行托管，不能用失效链接取代许可证义务。本次目录整理本身不代表所有发布条件已经获得法律确认。

## 替换与调试

可为修改LGPL库和调试这些修改的目的进行所需的逆向工程、替换及重新链接；本项目不对这些法定/许可权利额外加限制。Qt动态库、pyd和插件位于`_internal/PySide6`，替换时保留Python312/Win64 ABI与同一Qt插件版本，并按上游构建说明提供对应文件；应用源码可用于必要的兼容调整。MaaFramework动态库与Python适配位于`_internal/optional-runtime/5.15.0`，可替换接口兼容的5.15.0构建。源码的版本核对用于设备输入兼容验证，不能视为禁止使用或调试修改版。

实际上传Release时，应同时提供对应的应用源码、上述许可和明确的对应依赖源码访问方式。若二进制对应源码或版权材料不能维持可得，先不分发该二进制，待补齐后再发。参考[Qt官方LGPL义务](https://www.qt.io/development/open-source-lgpl-obligations)。
