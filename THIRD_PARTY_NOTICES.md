# Local Slice Assistant — Third-Party Notices

The Windows release bundles FFmpeg/ffprobe and PySide6/Qt runtime dependencies. The optional local vision runtime and model are not bundled with the release; run `install_local_vision.cmd` beside the EXE when that feature is wanted.

- FFmpeg: [license information](https://ffmpeg.org/legal.html).
- PySide6 / Qt: [license information](https://doc.qt.io/qtforpython-6/licenses.html).
- llama.cpp build 11029 CUDA 12.4 x64: [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp), MIT.
- SmolVLM2-2.2B-Instruct GGUF: [ggml-org model page](https://huggingface.co/ggml-org/SmolVLM2-2.2B-Instruct-GGUF), Apache-2.0.

The installer downloads the tested model and projector to shared D-drive locations and verifies these SHA-256 values:

- `SmolVLM2-2.2B-Instruct-Q4_K_M.gguf`: `0cf76814555b8665149075b74ab6b5c1d428ea1d3d01c1918c12012e8d7c9f58`
- `mmproj-SmolVLM2-2.2B-Instruct-Q8_0.gguf`: `ae07ea1facd07dd3230c4483b63e8cda96c6944ad2481f33d531f79e892dd024`

Chinese project-specific details are in `第三方组件说明.md` in the source project. No user media is uploaded by this application.
