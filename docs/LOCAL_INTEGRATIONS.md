# 可选本地工具配置

映序不会随仓库分发视觉模型、个人音色或配音工作台。未配置这些工具时，基础剪辑仍可使用；也可以直接导入自己生成的 WAV。

## 本地视觉辅助

安装兼容的 `llama.cpp` 视觉 CLI 与模型后，可通过环境变量指定位置：

- `LOCAL_SLICE_VISION_RUNTIME`：`llama-mtmd-cli` 可执行文件路径。
- `LOCAL_SLICE_VISION_MODEL_ROOT`：包含模型 GGUF 和 projector GGUF 的目录。

未设置时，Windows 默认查找 `%LOCALAPPDATA%\Yingxu\vision` 下的文件。

## 本地声音工作台（可选）

如需连接你自行安装的兼容工作台，可分别设置：

- `LOCAL_SLICE_VOICE_ROOT`：工作台根目录；默认 `%USERPROFILE%\.yingxu\voice`。
- `LOCAL_SLICE_VOICE_PYTHON`：覆盖根目录下默认的 `runtime\Scripts\python.exe`。
- `LOCAL_SLICE_VOICE_CLI`：覆盖根目录下默认的 `voice_bridge_cli.py`。
- `LOCAL_SLICE_VOICE_LAUNCHER`：工作台启动脚本；默认根目录下的 `Start-VoiceStudio.ps1`。

人声分离工具可使用 `LOCAL_SLICE_SEPARATION_ROOT`、`LOCAL_SLICE_SEPARATION_PYTHON` 和 `LOCAL_SLICE_SEPARATION_CLI` 设置。配置路径只保存在用户自己的环境中，不要把个人音色档案、运行环境或模型提交到公共仓库。
