#!/usr/bin/env bash
set -euo pipefail

# Build a small, pinned FFmpeg + x264 toolchain for source/compatibility
# validation. This does not package the application and intentionally uploads
# no build artifacts. Keep these commits and configure flags in sync with the
# release source index and THIRD_PARTY_NOTICES.md.
TARGET="${1:?usage: build_ffmpeg_source_validation.sh windows|macos}"
case "$TARGET" in
  windows|macos) ;;
  *) echo "Unsupported target: $TARGET" >&2; exit 2 ;;
esac

FFMPEG_REVISION="bf1b838f2ab88b4f8fd83443325c782ea0e0f7fa"
X264_REVISION="0480cb05fa188d37ae87e8f4fd8f1aea3711f7ee"
FREETYPE_REVISION="0a0221a1347e2f1e07c395263540026e9a0aa7c7"
HARFBUZZ_REVISION="7497c4147469fd4102a7229222586ad5c743c5a1"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_ROOT="${YINGXU_FFMPEG_BUILD_ROOT:-$ROOT/build/ffmpeg-source-validation/$TARGET}"
SOURCE_ROOT="$BUILD_ROOT/sources"
PREFIX="$BUILD_ROOT/install"
FFMPEG_SOURCE="$SOURCE_ROOT/FFmpeg"
X264_SOURCE="$SOURCE_ROOT/x264"
FREETYPE_SOURCE="$SOURCE_ROOT/freetype"
HARFBUZZ_SOURCE="$SOURCE_ROOT/harfbuzz"

mkdir -p "$SOURCE_ROOT" "$PREFIX"

fetch_revision() {
  local repo_url="$1" revision="$2" destination="$3"
  if [[ -e "$destination" ]]; then
    echo "Refusing to reuse existing source path: $destination" >&2
    exit 3
  fi
  git init "$destination"
  git -C "$destination" remote add origin "$repo_url"
  git -C "$destination" fetch --depth 1 origin "$revision"
  git -C "$destination" checkout --detach FETCH_HEAD
  local actual
  actual="$(git -C "$destination" rev-parse HEAD)"
  if [[ "$actual" != "$revision" ]]; then
    echo "Pinned source mismatch: expected $revision, got $actual" >&2
    exit 4
  fi
}

fetch_revision https://github.com/FFmpeg/FFmpeg.git "$FFMPEG_REVISION" "$FFMPEG_SOURCE"
fetch_revision https://code.videolan.org/videolan/x264.git "$X264_REVISION" "$X264_SOURCE"
fetch_revision https://github.com/freetype/freetype.git "$FREETYPE_REVISION" "$FREETYPE_SOURCE"
fetch_revision https://github.com/harfbuzz/harfbuzz.git "$HARFBUZZ_REVISION" "$HARFBUZZ_SOURCE"

JOBS="${YINGXU_BUILD_JOBS:-}"
if [[ -z "$JOBS" ]]; then
  if [[ "$TARGET" == macos ]]; then
    JOBS="$(sysctl -n hw.ncpu)"
  else
    JOBS="$(nproc)"
  fi
fi

X264_CONFIGURE=(--prefix="$PREFIX" --enable-static --disable-shared --disable-cli --disable-opencl)
FFMPEG_CONFIGURE=(
  --prefix="$PREFIX"
  --bindir="$PREFIX/bin"
  --pkg-config-flags=--static
  --enable-gpl
  --enable-libx264
  --enable-libfreetype
  --enable-libharfbuzz
  --enable-static
  --disable-shared
  --disable-autodetect
  --disable-ffplay
  --disable-doc
  --disable-debug
  --extra-cflags="-I$PREFIX/include"
  --extra-ldflags="-L$PREFIX/lib"
)

if [[ "$TARGET" == windows ]]; then
  # Run inside MSYS2 UCRT64 so both configure scripts detect the native
  # MinGW-w64 toolchain. Static-link its runtime into the standalone tools.
  FFMPEG_CONFIGURE+=(--extra-ldexeflags=-static)
  FONTFILE="/c/Windows/Fonts/arial.ttf"
else
  X264_CONFIGURE+=(--enable-pic)
  FONTFILE="/System/Library/Fonts/Supplemental/Arial.ttf"
fi
test -f "$FONTFILE"

FREETYPE_BUILD="$BUILD_ROOT/freetype-build"
FREETYPE_CMAKE_ARGS=(
  -DCMAKE_POSITION_INDEPENDENT_CODE=ON
  "-DCMAKE_C_FLAGS=-I$FREETYPE_SOURCE/include"
)
if [[ "$TARGET" == windows ]]; then
  # CMake launched from MSYS can report the host as MSYS/UNIX even though the
  # selected compiler targets native Windows. Tell FreeType to use its Win32
  # system backend (the UNIX backend requires sys/mman.h, absent in MinGW).
  FREETYPE_CMAKE_ARGS+=(-DCMAKE_SYSTEM_NAME=Windows)
fi
cmake -S "$FREETYPE_SOURCE" -B "$FREETYPE_BUILD" \
  -DCMAKE_INSTALL_PREFIX="$PREFIX" \
  -DBUILD_SHARED_LIBS=OFF \
  -DFT_DISABLE_ZLIB=TRUE \
  -DFT_DISABLE_BZIP2=TRUE \
  -DFT_DISABLE_PNG=TRUE \
  -DFT_DISABLE_HARFBUZZ=TRUE \
  -DFT_DISABLE_BROTLI=TRUE \
  -DFT_DISABLE_HVF=TRUE \
  "${FREETYPE_CMAKE_ARGS[@]}"
cmake --build "$FREETYPE_BUILD" --target install --parallel "$JOBS"

HARFBUZZ_BUILD="$BUILD_ROOT/harfbuzz-build"
export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
meson setup "$HARFBUZZ_BUILD" "$HARFBUZZ_SOURCE" \
  --prefix="$PREFIX" \
  --libdir=lib \
  --default-library=static \
  --wrap-mode=nofallback \
  -Dtests=disabled \
  -Dutilities=disabled \
  -Ddocs=disabled \
  -Dintrospection=disabled \
  -Dglib=disabled \
  -Dgobject=disabled \
  -Dcairo=disabled \
  -Dchafa=disabled \
  -Dicu=disabled \
  -Dgraphite2=disabled \
  -Dfreetype=enabled
meson compile -C "$HARFBUZZ_BUILD" --jobs "$JOBS"
meson install -C "$HARFBUZZ_BUILD"

(
  cd "$X264_SOURCE"
  ./configure "${X264_CONFIGURE[@]}"
  make -j"$JOBS"
  make install
)

(
  cd "$FFMPEG_SOURCE"
  ./configure "${FFMPEG_CONFIGURE[@]}"
  make -j"$JOBS"
  make install
)

if [[ "$TARGET" == windows ]]; then
  FFMPEG="$PREFIX/bin/ffmpeg.exe"
  FFPROBE="$PREFIX/bin/ffprobe.exe"
else
  FFMPEG="$PREFIX/bin/ffmpeg"
  FFPROBE="$PREFIX/bin/ffprobe"
fi
test -x "$FFMPEG"
test -x "$FFPROBE"
"$FFMPEG" -hide_banner -version
"$FFMPEG" -hide_banner -encoders | grep -Eq '[[:space:]]libx264[[:space:]]'
"$FFMPEG" -hide_banner -loglevel error \
  -f lavfi -i 'testsrc2=size=1280x720:rate=25:duration=2' \
  -an -vf "drawtext=fontfile='$FONTFILE':text='Yingxu':fontsize=32:fontcolor=white:x=10:y=10" \
  -c:v libx264 -preset medium -crf 18 -pix_fmt yuv420p \
  -y "$BUILD_ROOT/smoke.mp4"
"$FFPROBE" -v error -select_streams v:0 \
  -show_entries stream=codec_name,profile,width,height,avg_frame_rate \
  -of default=noprint_wrappers=1 "$BUILD_ROOT/smoke.mp4"

printf 'FFmpeg source: https://github.com/FFmpeg/FFmpeg/commit/%s\n' "$FFMPEG_REVISION"
printf 'x264 source: https://code.videolan.org/videolan/x264/-/commit/%s\n' "$X264_REVISION"
printf 'FreeType source: https://github.com/freetype/freetype/commit/%s\n' "$FREETYPE_REVISION"
printf 'HarfBuzz source: https://github.com/harfbuzz/harfbuzz/commit/%s\n' "$HARFBUZZ_REVISION"
printf 'Target: %s; configure flags are recorded in this script.\n' "$TARGET"
