# 映序 — Third-party notices

The application source code is licensed under the MIT License in `LICENSE`.
That license does not replace the licenses of bundled third-party software.

## Portable candidates

The Windows and macOS portable candidates produced by the scripts in `scripts/`
bundle FFmpeg and ffprobe as separate local command-line programs. The candidates
currently audited are GPL builds, not LGPL-only builds:

- Windows: FFmpeg 9.0.1 Gyan “full” static build, GPLv3-or-later, with GPL
  encoders including `libx264`. Its exact build configuration is recorded in
  `FFMPEG_BUILD_INFO.txt` inside the candidate.
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
(including Windows `avcodec-61.dll` and macOS `libavcodec.61.dylib`,
`libavformat.61.dylib`, `libavutil.59.dylib`, `libswresample.5.dylib`, and
`libswscale.8.dylib`). These are distinct from the standalone FFmpeg/ffprobe
executables described above and need their own exact version, license, notice,
and corresponding-source review. Other Qt runtime libraries/plugins are
present in the frozen packages and have not yet all been mapped to application
imports.

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

The Git repository itself does not contain the FFmpeg binaries. Optional local
vision, speech, separation, and model runtimes are not bundled or promised. If
installed separately, each runtime and model retains its own license and
redistribution terms. Ordinary local editing does not upload user media.

This notice is informational, not legal advice. A candidate is not cleared for
redistribution merely because this file is present; use the release status
document and the exact contents of each archive as the source of truth.
