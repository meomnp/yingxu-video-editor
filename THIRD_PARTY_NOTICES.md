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

The app also bundles Qt for Python (PySide6/Qt). Qt for Python Community Edition
uses LGPLv3/GPLv3 licensing; individual Qt components and third-party code can
have additional terms. Exact bundled Qt module versions, license texts, notices,
and source/relinking materials still need a package-level audit before public
distribution. See [Qt for Python licenses](https://doc.qt.io/qtforpython-6/licenses.html)
and [Qt licensing](https://doc.qt.io/qt-6/licensing.html).

## Source-only repository

The Git repository itself does not contain the FFmpeg binaries. Optional local
vision, speech, separation, and model runtimes are not bundled or promised. If
installed separately, each runtime and model retains its own license and
redistribution terms. Ordinary local editing does not upload user media.

This notice is informational, not legal advice. A candidate is not cleared for
redistribution merely because this file is present; use the release status
document and the exact contents of each archive as the source of truth.
