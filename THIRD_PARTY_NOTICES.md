# 映序 — Third-Party Notices

The source repository depends on PySide6/Qt. Follow the license and redistribution terms for the exact Qt components used by any build: [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html).

The application invokes FFmpeg and ffprobe as separate local processes. The source repository does not include FFmpeg binaries. If a future installer or archive redistributes FFmpeg, its exact build configuration and corresponding license/source obligations must be reviewed and documented; see [FFmpeg legal considerations](https://ffmpeg.org/legal.html). Do not assume every FFmpeg build has the same license or codec support.

Optional local vision, speech, separation, and model runtimes are not included in this source repository or promised application package. If separately installed, each runtime and model retains its own license and redistribution conditions. No user media is uploaded by ordinary local editing.
