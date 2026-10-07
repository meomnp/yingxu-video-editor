"""Build clean, current portable applications on standard hosted runners.

Output is staged for verification, not automatically published as a release.
Only frozen runtime, licenses and a short user guide enter the ZIP.
"""
import argparse
import hashlib
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

from fetch_runtime_licenses import fetch_runtime_licenses


def run(args, **kwargs):
    subprocess.run([str(item) for item in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('target', choices=['windows', 'macos'])
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    os.chdir(root)
    target = args.target
    if sys.platform != ('win32' if target == 'windows' else 'darwin'):
        raise RuntimeError('Build on the target OS.')
    prefix = root / 'build' / 'ffmpeg-lgpl-validation' / target / 'install' / 'bin'
    tools = [prefix / ('ffmpeg.exe' if target == 'windows' else 'ffmpeg'),
             prefix / ('ffprobe.exe' if target == 'windows' else 'ffprobe')]
    run([sys.executable, root / 'src/local_slice_assistant/release_checks.py', target, *tools])
    output = root / 'dist/current-portable'
    separator = ';' if target == 'windows' else ':'
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
               '--name', '映序', '--exclude-module', 'PySide6.QtVirtualKeyboard',
               '--paths', root / 'src', '--distpath', output,
               '--workpath', root / 'build/current-portable', '--specpath', root / 'build/current-portable',
               '--add-data', str(root / 'assets/branding/local-slice-v2.ico') + separator + 'assets/branding']
    for tool in tools:
        command += ['--add-binary', str(tool) + separator + '.']
    if target == 'windows':
        command += ['--icon', root / 'assets/branding/local-slice-v2.ico',
                    '--add-data', str(root / 'scripts/read_timecode_windows.ps1') + separator + 'scripts']
    else:
        command += ['--osx-bundle-identifier', 'com.meomnp.yingxu']
    if (root / 'assets/stickers').is_dir():
        command += ['--add-data', str(root / 'assets/stickers') + separator + 'assets/stickers']
    command += [root / 'src/local_slice_assistant_app.py']
    run(command)
    bundle = output / ('映序' if target == 'windows' else '映序.app')
    run([sys.executable, root / 'scripts/prune_qt_virtualkeyboard.py',
         bundle / '_internal/PySide6' if target == 'windows' else bundle / 'Contents'])
    if target == 'windows':
        for name in ('icuuc.dll', 'icudt78.dll'):
            (bundle / '_internal' / name).unlink(missing_ok=True)
        executable = bundle / '映序.exe'
    else:
        executable = bundle / 'Contents/MacOS/映序'
    env = os.environ.copy()
    env['QT_QPA_PLATFORM'] = 'offscreen'
    # Explicitly test bundled tools, independent of host-installed ffmpeg.
    env.pop('LOCAL_SLICE_FFMPEG', None)
    env.pop('LOCAL_SLICE_FFPROBE', None)
    run([executable, '--smoke-test'], env=env, timeout=60)
    report = root / 'build/portable-export-check.json'
    run([executable, '--portable-export-check', report], env=env, timeout=180)
    result = json.loads(report.read_text(encoding='utf-8'))
    expected_encoder = 'h264_mf' if target == 'windows' else 'h264_videotoolbox'
    if result.get('export') != 'passed' or result.get('encoder') != expected_encoder:
        raise RuntimeError('Packaged native-encoder export test did not pass.')
    # Put documentation outside .app on macOS; only this staging directory is archived.
    staging = root / 'build/portable-upload' / '映序'
    staging.mkdir(parents=True, exist_ok=False)
    shutil.copytree(bundle, staging / bundle.name, symlinks=True)
    if target == 'windows':
        # A Windows ZIP should open directly to 映序/映序.exe, not two nested folders.
        staging = staging / bundle.name
    shutil.copy2(root / 'LICENSE', staging / 'LICENSE')
    shutil.copy2(root / 'PORTABLE_NOTICES.md', staging / '第三方组件说明.md')
    # Preserve upstream notices, without shipping development sources.
    source_root = root / 'build/ffmpeg-lgpl-validation' / target / 'sources'
    for component, patterns in (
        ('FFmpeg', ('COPYING*', 'LICENSE*')),
        ('freetype', ('LICENSE.TXT', 'docs/FTL.TXT', 'docs/GPLv2.TXT')),
        ('harfbuzz', ('COPYING',)),
    ):
        for pattern in patterns:
            for item in (source_root / component).glob(pattern):
                if item.is_file():
                    destination = staging / 'licenses' / component / item.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, destination)
    fetch_runtime_licenses(staging, python_version=platform.python_version(),
                          openssl_version=__import__('ssl').OPENSSL_VERSION)
    pyinstaller = distribution('PyInstaller')
    copying = next(pyinstaller.locate_file(item) for item in pyinstaller.files
                   if str(item).endswith('.dist-info/licenses/COPYING.txt'))
    shutil.copy2(copying, staging / 'PYINSTALLER_COPYING.txt')
    for package in ('PySide6', 'shiboken6'):
        dist = distribution(package)
        for item in dist.files:
            if '/licenses/' in str(item) and Path(dist.locate_file(item)).is_file():
                destination = staging / 'licenses' / package / Path(item).name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dist.locate_file(item), destination)
    guide = ('映序 Windows 版\n全部解压后，双击映序.exe。无需安装 Python 或下载源码。\n'
             if target == 'windows' else
             '映序 Mac 版（Apple Silicon / arm64）\n解压后打开映序.app。无需 Python 或源码。\n'
             '维护者没有 Mac，尚未实机验证。未公证，首次打开可能有系统安全提示；不要关闭整机安全保护。\n')
    guide += ('本地导入、剪辑、导出不调用付费 API。主动使用 API 分析会产生服务商费用。\n'
              '发现问题请在 https://github.com/meomnp/yingxu-video-editor/issues 反馈，勿提交密钥或私人素材。\n')
    (staging / '使用说明.txt').write_text(guide, encoding='utf-8')
    build_info = {'source_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
                  'python': platform.python_version(), 'PySide6': version('PySide6'),
                  'PyInstaller': version('PyInstaller'), 'platform': target,
                  'architecture': platform.machine(), 'export_acceptance': result}
    (staging / '版本信息.json').write_text(json.dumps(build_info, ensure_ascii=False, indent=2), encoding='utf-8')
    name = '映序-Windows.zip' if target == 'windows' else '映序-Mac.zip'
    archive = root / 'dist' / name
    if target == 'windows':
        with zipfile.ZipFile(archive, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for item in staging.rglob('*'):
                if item.is_file():
                    z.write(item, '映序/' + item.relative_to(staging).as_posix())
    else:
        run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', staging, archive])
    if archive.stat().st_size > 320 * 1024 * 1024:
        raise RuntimeError('Package unexpectedly exceeds the size safety limit.')
    with zipfile.ZipFile(archive) as z:
        if z.testzip():
            raise RuntimeError('Archive integrity verification failed.')
        if any('/src/' in n or '/tests/' in n or 'runtime.json' in n or 'API请求历史' in n for n in z.namelist()):
            raise RuntimeError('Private/development files entered the package.')
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (root / 'dist' / (name + '.sha256')).write_text(digest + '  ' + name + '\n', encoding='utf-8')
    print('Packaged export verified:', json.dumps(result), flush=True)
    print('Portable ZIP:', archive, flush=True)


if __name__ == '__main__':
    main()
