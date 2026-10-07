# Windows 候选包的 FFmpeg 依赖追溯清单

核对日期：2026-10-07
对象：映序 Windows 候选包内 `ffmpeg.exe` / `ffprobe.exe`，对应 Gyan FFmpeg 9.0.1 full 静态构建。
上游发行页：[GyanD/codexffmpeg 9.0.1](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.1)

## 上游包 README 已确认

- FFmpeg 版本：9.0.1；源码提交：[`bf1b838f2a`](https://github.com/FFmpeg/FFmpeg/commit/bf1b838f2a)。
- 上游声明许可证：GPL v3；构建为静态 Windows x64 构建。
- 上游 full ZIP 大小：251,427,729 字节；GitHub Release 声明的 SHA-256 与本次下载核验值一致：`2e8e28af97c2ae338ccef92e36da9b2a4cd21d0cad9dde093545606cb07f5b00`。
- README 包含 configure/build configuration、内置解码器/编码器/滤镜清单、外部库清单和多数外部库的版本或源码修订号。
- 本项目候选包里的两个可执行文件已与该上游 full 压缩包逐项校验 SHA-256 一致。
- 这些信息比先前记录的“没有版本信息”更完整，但仍不等于已取得每个组件的源代码、许可证、补丁、依赖关系及重建脚本。因此不能据此放行再分发。

## README 中的外部库版本/修订号

以下为上游 README 原样核录的版本标识；它们是后续查找对应源码的索引，不表示源码已下载或校验。

```text
AMF v1.5.2-2-gc35f613
aom v3.14.1-147-gec0dedc1a2
aribb24 v1.0.3-5-g5e9be27
aribcaption 1.1.2
AviSynthPlus v3.7.5-362-gf4628d0a
bs2b 3.1.0
bzip2 1.0.8-3
cairo 1.18.5
chromaprint 1.6.1
codec2 1.2.0-108-g310777b1
dav1d 1.5.4
davs2 1.7-1-gb41cf11
dvdnav 7.0.0-16-g2ffc50b
dvdread 7.1.1-92-g50009a0
ffnvcodec n13.1.15.0-1-geddcea9
flite v2.2-55-g6c9f20d
fontconfig 2.18.3
freetype VER-2-14-3
frei0r v3.2.3
fribidi v1.0.16-5-g069a7e3
gmp 6.3.0-2
gnutls 3.8.13-1
gsm 1.0.24
harfbuzz 14.3.0-10-g9f2f0317
ladspa-sdk 1.17
lame 3.100
lc3 1.1.3
lcms2 2.16
lensfun v0.3.95-1996-g6804b5f5
libass 0.17.5-3-g89cc0f4
libcdio-paranoia 10.2
libgme 0.6.6
libiconv 1.19-1
libilbc v3.0.4-346-g6adb26d4a4
libjxl v0.12-snapshot-3-ge8ff0976
libopencore-amrnb 0.1.6
libopencore-amrwb 0.1.6
libplacebo v7.360.0-109-g4d82c68
libsoxr 0.1.3
libssh 0.12.0
libtheora v1.2.0
libwebp v1.6.0-199-g94d3c4a
libxml2 v2.15.0-122-gddcb79dc
openAL 1.25.2
openapv v0.3.0.0-9-ga5312e4
openjpeg2 2.5.4
openmpt libopenmpt-0.6.28-40-gefc11a27
opus v1.6.1-50-g3da9f7a6
qrencode 4.1.1
quirc 1.2
rav1e p20250624-3-g564ae3b
rist 0.2.20
rubberband v4.0.0
SDL release-2.32.0-228-ga2e7c76bd
shaderc v2026.3-9-g7060a66
shine 3.1.1
snappy 1.2.2
speex Speex-1.2.1-51-g0589522
srt v1.5.6-2-gfcae571
SVT-AV1 v4.2.0-72-gae2658e53
SVT-JPEG-XS v0.9.0-78-g8056642
twolame 0.4.0
uavs3d v1.1-50-g0e20d2c
VAAPI 2.25.0
vidstab v1.1.2-105-gc7a720a
vmaf v3.2.0-9-g4991d2b5
vo-amrwbenc 0.1.3
vorbis v1.3.7-37-g1b75110b
VPL 2.17
vpx v1.16.0-184-g0cfc6da39
vulkan-loader v1.4.359
vvenc v1.14.0-160-ga03b882
whisper.cpp 1.9.1
x264 v0.165.3223
x265 4.3-6-g9ddc216
xavs2 1.4
xevd 0.5.0
xeve 0.5.1
xvid v1.3.7
zeromq 4.3.5
zimg release-3.0.6-252-gf6cc75a
zvbi v0.2.44-8-g4e222f9
```

## 尚缺的发行审计证据

1. 对每项确认实际静态链接/编入的组件及其许可证，重点复核 GPL、LGPL、专利/非自由限制和来源义务；不能只按 README 列表假设全部依赖都进入两个程序。
2. 对每个编入的外部库取得与上面版本/提交一致的源代码，记录可信上游 URL、源码归档校验值、许可证文件及必要补丁；对存在间接依赖的组件继续追踪。
3. 取得或重建精确构建脚本、工具链版本、configure 参数和补丁，证明能从对应源码生成候选包里的二进制；目前 Gyan 发布说明只直接指向 FFmpeg 源码提交。
4. 将源码材料和构建说明放在与应用包分离的公开源码库/发布材料中，并在便携包保留适当的许可、版权与源码获取说明；发布前再次核对当时适用的完整许可证文本。
5. 此清单仅对应 Windows Gyan full FFmpeg，不覆盖 macOS Homebrew FFmpeg，也不覆盖 PySide6/Qt Multimedia 自带的 FFmpeg 运行库；它们必须各自单独建立对应清单。

## 不同二进制的后续源码构建路线

仓库新增 `scripts/build_ffmpeg_source_validation.sh`，固定官方 FFmpeg 提交 `bf1b838f2ab88b4f8fd83443325c782ea0e0f7fa`、VideoLAN x264 提交 `0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee` 和 FreeType 提交 `0a0221a1347e2f1e07c395263540026e9a0aa7c7`，构建 FFmpeg/ffprobe + libx264 + drawtext 所需的 FreeType，并运行同时覆盖文字滤镜与 H.264 编码的合成烟测；FreeType 的可选外部库全部关闭，减少依赖闭包。配套 GitHub Actions 手动工作流使用标准 Windows/macOS runner，不上传应用或二进制工件。

这是一条重建新二进制的路线，不是 Gyan full 二进制的对应源码，不能追溯性地解除上面 Gyan 包的发行限制。该工作流尚未在 GitHub 实际执行；Windows/macOS 构建可行性、Mac 编码结果、Qt 组件及最终应用回归都未验证。只有工作流成功并把该精确构建产物用于新候选包之后，才可以按其源码依赖闭环继续审计。

在这些证据齐全、法律要求经核对且编码行为通过回归测试前，Windows 及 macOS 便携包仍不获分享放行。此清单是工程审计记录，不构成法律意见。
