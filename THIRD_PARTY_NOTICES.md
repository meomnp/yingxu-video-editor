# 映序 — Third-party notices

The application source code is licensed under the MIT License in `LICENSE`.
That license does not replace the licenses of bundled third-party software.

## Portable candidates

PyInstaller's bootloader is embedded in the generated launcher executable and
is distributed under GPL-2.0-or-later with the PyInstaller Bootloader
Exception. Candidate build scripts copy the exact `COPYING.txt` shipped with
the installed PyInstaller version into `PYINSTALLER_COPYING.txt`; this license
applies to the bootloader, not to the 映序 application source.

The Windows and macOS portable candidates produced by the scripts in `scripts/`
bundle FFmpeg and ffprobe as separate local command-line programs. The candidates
currently audited are GPL builds, not LGPL-only builds:

- Windows: FFmpeg 9.0.1 Gyan “full” static build, GPLv3-or-later, with GPL
  encoders including `libx264`. The packaged `ffmpeg.exe` and `ffprobe.exe`
  SHA-256 hashes match the corresponding executables in Gyan's
  `ffmpeg-9.0.1-full_build.zip` release asset. That upstream asset has SHA-256
  `2e8e28af97c2ae338ccef92e36da9b2a4cd21d0cad9dde093545606cb07f5b00`, and its
  README identifies FFmpeg source commit `bf1b838f2a`:
  [Gyan FFmpeg 9.0.1 release](https://github.com/GyanD/codexffmpeg/releases/tag/9.0.1).
  The exact build configuration is recorded in `FFMPEG_BUILD_INFO.txt` inside
  the candidate. The upstream README lists enabled external libraries but not
  their exact source revisions; corresponding-source materials for those
  statically included libraries remain to be assembled.
- macOS: FFmpeg 9.0.1_1 from Homebrew, GPL-3.0-or-later, configured with
  `--enable-gpl`, `--enable-libx264`, and `--enable-libx265`. The candidate
  bundles its non-system dynamic-library dependencies beside FFmpeg. The exact
  formula metadata, versions, and build configuration are recorded in
  `FFMPEG_BUILD_INFO.txt` inside the candidate.

The corresponding source materials and build inputs for these exact FFmpeg
builds and their external dependencies have not yet been collected and verified.
Until that work is complete, these archives are internal test candidates and are
not cleared for public redistribution. The FFmpeg build configurations can be
different from one release to another; do not infer codec support or licensing
from the application source alone. See [FFmpeg legal considerations](https://ffmpeg.org/legal.html).

The app also bundles Qt for Python (PySide6/Qt). The Windows and macOS
candidates inspected here were built with PySide6 6.11.2 / Qt 6.11.2. The
Windows package contains the PySide bindings `QtCore`, `QtGui`, `QtMultimedia`,
`QtMultimediaWidgets`, `QtNetwork`, and `QtWidgets`; macOS additionally contains
`QtDBus`. Both packages include Qt runtime libraries and plugins. In
particular, their multimedia plugins (`ffmpegmediaplugin.dll` and
`libffmpegmediaplugin.dylib`) use separately bundled Qt FFmpeg runtime libraries
(Qt 6.11.2's official attribution identifies FFmpeg 7.1.3; the package contains
Windows `avcodec-61.dll` and macOS `libavcodec.61.dylib`,
`libavformat.61.dylib`, `libavutil.59.dylib`, `libswresample.5.dylib`, and
`libswscale.8.dylib`). Qt's attribution page lists LGPL-2.1-or-later and BSD,
ISC, MIT, and MPL-2.0 notices for this Qt FFmpeg build, and identifies the
source subtree as `qtmultimedia/src/3rdparty/ffmpeg`. These are distinct from
the standalone FFmpeg/ffprobe executables described above. Exact build flags,
per-platform source snapshots, license texts, notices, and build/relink
materials for this Qt-bundled copy still need to be matched to the shipped
binaries. Other Qt runtime libraries/plugins are present in the frozen
packages and have not yet all been mapped to application imports. See the
[Qt 6.11.2 FFmpeg attribution](https://doc.qt.io/qt-6.11/qtmultimedia-attribution-ffmpeg.html).

The PySide6 Community Edition wheels declare LGPL-3.0-only OR GPL-2.0-only OR
GPL-3.0-only. The wheel metadata in the build environment supplied only a Qt
commercial-license reference file, not a complete redistribution compliance
set. Qt's published documentation says licenses can differ by module and
third-party component. Exact per-platform modules, third-party notices, Qt
Multimedia's bundled FFmpeg provenance, license texts, source materials, and
the practical replacement/relink path for the frozen application still need
package-level review before public distribution. The upstream Qt 6.11.2 source
archive is available from [Qt's official download archive](https://download.qt.io/archive/qt/6.11/6.11.2/single/),
but merely linking that general archive does not establish that every packaged
binary and embedded third-party component is covered. See [Qt for Python
licenses](https://doc.qt.io/qtforpython-6/licenses.html), [Qt licensing](https://doc.qt.io/qt-6/licensing.html),
and the [Qt Multimedia licensing documentation](https://doc.qt.io/qt-6/qtmultimedia-index.html).

## Source-only repository

The inspected older candidates also contain unused Qt Virtual Keyboard
artifacts. Qt documents this module as GPLv3 or commercial-only, not LGPL;
the old candidates are not cleared for redistribution. The app source does not
import or enable Qt Virtual Keyboard. The Windows and macOS candidate build
scripts now exclude its PySide module and run
`scripts/prune_qt_virtualkeyboard.py` to remove the module libraries, plugins,
and framework payloads before the packaged startup smoke test. The exclusion
has passed unit fixtures and a startup smoke test on a temporary copy of the
old Windows candidate; fresh Windows and macOS application bundles still need
to be built and inspected to verify no dependent artifacts remain. See [Qt
Virtual Keyboard licensing](https://doc.qt.io/qt-6/qtvirtualkeyboard-index.html).

The Git repository itself does not contain the FFmpeg binaries. Optional local
vision, speech, separation, and model runtimes are not bundled or promised. If
installed separately, each runtime and model retains its own license and
redistribution terms. Ordinary local editing does not upload user media.

This notice is informational, not legal advice. A candidate is not cleared for
redistribution merely because this file is present; use the release status
document and the exact contents of each archive as the source of truth.
