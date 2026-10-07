#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This script must run on macOS." >&2
  exit 2
fi

FFMPEG="$(command -v ffmpeg)"
FFPROBE="$(command -v ffprobe)"
DYLIBBUNDLER="$(command -v dylibbundler)"
OUT_ROOT="$ROOT/dist"

args=(
  --noconfirm --clean --windowed --onedir
  --name "映序"
  --osx-bundle-identifier "com.meomnp.yingxu"
  --paths "$ROOT/src"
  --add-binary "$FFMPEG:."
  --add-binary "$FFPROBE:."
  --add-data "$ROOT/assets/branding/local-slice-v2.ico:assets/branding"
  --add-data "$ROOT/THIRD_PARTY_NOTICES.md:."
  "$ROOT/src/local_slice_assistant_app.py"
)
if [[ -d "$ROOT/assets/stickers" ]]; then
  args+=(--add-data "$ROOT/assets/stickers:assets/stickers")
fi
python -m PyInstaller "${args[@]}"

APP="$ROOT/dist/映序.app"
FFMPEG_IN_APP="$(find "$APP/Contents" -type f -name ffmpeg -print -quit)"
FFPROBE_IN_APP="$(find "$APP/Contents" -type f -name ffprobe -print -quit)"

test -d "$APP"
if [[ -z "$FFMPEG_IN_APP" || ! -x "$FFMPEG_IN_APP" ]]; then
  echo "Could not locate executable ffmpeg inside $APP" >&2
  find "$APP/Contents" -maxdepth 5 -type f -print >&2
  exit 1
fi
if [[ -z "$FFPROBE_IN_APP" || ! -x "$FFPROBE_IN_APP" ]]; then
  echo "Could not locate executable ffprobe inside $APP" >&2
  find "$APP/Contents" -maxdepth 5 -type f -print >&2
  exit 1
fi
if [[ "$(dirname "$FFMPEG_IN_APP")" != "$(dirname "$FFPROBE_IN_APP")" ]]; then
  echo "Packaged ffmpeg and ffprobe are not in the same directory." >&2
  printf 'ffmpeg: %s\nffprobe: %s\n' "$FFMPEG_IN_APP" "$FFPROBE_IN_APP" >&2
  exit 1
fi
LIBS="$(dirname "$FFMPEG_IN_APP")/libs"
mkdir -p "$LIBS"

# Homebrew FFmpeg links to non-system dylibs. Bundle their dependency closure
# beside the app binaries and rewrite load paths for an isolated app bundle.
"$DYLIBBUNDLER" -b \
  -x "$FFMPEG_IN_APP" \
  -x "$FFPROBE_IN_APP" \
  -d "$LIBS" \
  -p "@executable_path/libs/"

# Verify the relocated executables resolve without relying on the build PATH.
env -u DYLD_LIBRARY_PATH "$FFMPEG_IN_APP" -hide_banner -version >/dev/null
env -u DYLD_LIBRARY_PATH "$FFPROBE_IN_APP" -hide_banner -version >/dev/null
QT_QPA_PLATFORM=offscreen "$APP/Contents/MacOS/映序" --smoke-test

ARCH="$(uname -m)"
PACKAGE="$OUT_ROOT/映序-macOS-${ARCH}-便携测试候选"
mkdir -p "$PACKAGE"
ditto "$APP" "$PACKAGE/映序.app"
cp "$ROOT/LICENSE" "$ROOT/THIRD_PARTY_NOTICES.md" "$PACKAGE/"
{
  echo "Product: 映序 macOS arm64 portable test candidate"
  echo "Source commit: $(git rev-parse HEAD)"
  echo "Built at (UTC): $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
  python --version
  python -m PyInstaller --version
} > "$PACKAGE/BUILD_INFO.txt"
cat >> "$PACKAGE/THIRD_PARTY_NOTICES.md" <<'NOTICES'

This macOS test candidate bundles FFmpeg and ffprobe from Homebrew as separate local processes. The exact version, build flags, and Homebrew formula metadata are recorded in FFMPEG_BUILD_INFO.txt. This candidate is not cleared for redistribution while corresponding-source materials are being checked.
NOTICES
{
  echo "映序 macOS 便携测试候选版"
  echo "架构：${ARCH}；未签名、未公证。此包是在 GitHub macOS runner 上构建，维护者没有 Mac 电脑，尚未进行实机验收。"
  echo "首次打开如被 Gatekeeper 拦截，可在系统设置→隐私与安全性中查看允许打开选项；不要关闭系统安全保护。"
  echo "欢迎反馈 bug；请附 Mac 型号、macOS 版本、复现步骤和脱敏报错。"
  echo "注意：随包 FFmpeg 的确切源码与再分发材料仍在核验。本候选版仅供测试，核验完成前请勿公开转发。"
} > "$PACKAGE/使用说明.txt"
{
  echo "FFmpeg/ffprobe version and build configuration"
  "$FFMPEG" -version
  "$FFMPEG" -L
  "$FFMPEG" -buildconf
  brew info --json=v2 ffmpeg
} > "$PACKAGE/FFMPEG_BUILD_INFO.txt"

OUT="$OUT_ROOT/映序-macOS-${ARCH}-便携测试候选.zip"
ditto -c -k --sequesterRsrc --keepParent "$PACKAGE" "$OUT"
shasum -a 256 "$OUT" | tee "$OUT.sha256"
echo "Built unsigned macOS candidate: $OUT"
