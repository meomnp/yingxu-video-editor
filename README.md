# 映序（Yingxu）

映序是一款 Windows 优先的本地视频粗剪与包装工具。它读取用户选择的视频和带时间戳的字幕/转写稿，按剪辑清单生成可编辑工程，并通过 FFmpeg 导出；原始媒体保持只读。

## 当前能力

- 导入单个视频、多个视频或素材文件夹，并按文件名顺序整理素材。
- 导入本地剪辑方案，查看片段顺序与源时间，人工调整入点/出点、重排和撤销。
- 预览、保存可继续编辑的工程，导出粗剪或字幕包装视频。
- 将转写稿和素材目录清单打包交给任意网页 AI；也提供可选 API 设计入口。API 入口仍属实验功能，可能产生费用且不保证模型返回完整结果。
- 可选字幕遮挡、用户提供的字幕贴纸和配音音频。

AI 方案需要人工审阅。预览不是正式导出；只有看到逐条导出成功并确认文件存在，才算导出完成。

## 从源码运行

需要 Windows、Python 3.12 或更高版本、FFmpeg/ffprobe（加入 `PATH`）以及可用的视频解码器。

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python -m local_slice_assistant
```

第三方依赖和通知见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。可选的本地视觉/声音工具不会随源码仓库提供；配置说明见 [docs/LOCAL_INTEGRATIONS.md](docs/LOCAL_INTEGRATIONS.md)。

## 隐私边界

普通剪辑在本机进行。网页 AI 任务包由用户自行上传；API 入口仅在用户确认后发送素材文件名、时间戳和转写文本，不上传视频/音频。API 服务商可能收费。不要把私人素材、API 密钥、工程文件或本地运行历史提交到 GitHub。

## 许可

本项目以 MIT License 发布，见 [LICENSE](LICENSE)。第三方依赖和素材仍受其各自许可约束。
