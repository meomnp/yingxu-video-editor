# 映序便携应用的第三方组件

映序源码采用 MIT 许可证：https://github.com/meomnp/yingxu-video-editor 。
普通使用者无需下载或安装源码。本 ZIP 不包含使用者的工程、台词、截图、API 密钥或本机配置。

应用动态使用 Qt/PySide6/Shiboken 6.11.2 社区版，适用 LGPLv3 等上游许可；
不包含 Qt Virtual Keyboard。允许按其许可修改、替换相应动态库并调试修改后的程序，
本应用不对这些权利施加限制。替换前备份应用目录，保持对应平台、Python ABI 和 Qt 主版本一致。
Windows 动态库在 `_internal/PySide6`，Mac 在 `映序.app/Contents`，应用无需联网激活。

Qt 官方源码：https://download.qt.io/archive/qt/6.11/6.11.2/single/ 。
PySide/Shiboken 官方源码：https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-6.11.2-src/ 。
Qt Multimedia 随包 FFmpeg 为 7.1.5（与独立命令行 FFmpeg 不同）：https://github.com/FFmpeg/FFmpeg/tree/n7.1.5 。
完整对应源码、许可和构建材料与应用分开保管；通过上述项目的 Issues 联系维护者获取，
不会要求普通使用者将源码放入应用才能运行。上游许可文本随本包保留。

独立 FFmpeg/ffprobe 从固定源码构建，禁用 GPL/nonfree 和 x264/x265，使用平台 H.264 编码器。
FFmpeg：https://github.com/FFmpeg/FFmpeg/commit/bf1b838f2ab88b4f8fd83443325c782ea0e0f7fa 。
FreeType：https://github.com/freetype/freetype/commit/0a0221a1347e2f1e07c395263540026e9a0aa7c7 。
HarfBuzz：https://github.com/harfbuzz/harfbuzz/commit/7497c4147469fd4102a7229222586ad5c743c5a1 。
构建脚本在源码仓库 `scripts/build_ffmpeg_lgpl_validation.sh`；第三方对应源码不是映序的私人数据。

CPython、OpenSSL 和 PyInstaller 启动器许可分别随包提供；PyInstaller 启动器许可含 Bootloader Exception，
不将映序应用代码变更为 GPL。Mac 包未公证，维护者没有 Mac 实机，欢迎反馈问题。
