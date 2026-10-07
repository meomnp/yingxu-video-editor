param(
    [Parameter(Mandatory = $true)][string]$FfmpegPath,
    [Parameter(Mandatory = $true)][string]$FfprobePath,
    [switch]$Console
)
$ErrorActionPreference = 'Stop'

if ($env:YINGXU_LICENSE_AUDIT_CONFIRMED -ne '1') {
    throw 'Packaging blocked: complete the package-level third-party source/license audit first. Set YINGXU_LICENSE_AUDIT_CONFIRMED=1 only after the release gate in docs/发行状态.md is satisfied.'
}

$repoRoot = [System.IO.Directory]::GetParent($PSScriptRoot).FullName
$python = (Get-Command python -ErrorAction Stop).Source
$ffmpeg = (Resolve-Path -LiteralPath $FfmpegPath).Path
$ffprobe = (Resolve-Path -LiteralPath $FfprobePath).Path
foreach ($binary in @($ffmpeg, $ffprobe)) {
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "Media binary not found: $binary" }
}
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'scripts\read_timecode_windows.ps1'))) {
    throw 'The Windows OCR helper is missing from the source tree.'
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$distRoot = Join-Path $repoRoot "dist\windows-candidate-$stamp"
$workRoot = Join-Path $repoRoot "build\windows-candidate-$stamp"
$bundle = Join-Path $distRoot '映序'
New-Item -ItemType Directory -Path $distRoot | Out-Null
New-Item -ItemType Directory -Path $workRoot | Out-Null

Push-Location -LiteralPath $repoRoot
try {
    $pyInstallerArgs = @(
        '--noconfirm', '--clean', $(if ($Console) { '--console' } else { '--windowed' }), '--onedir',
        '--name', '映序', '--icon', (Join-Path $repoRoot 'assets\branding\local-slice-v2.ico'),
        '--paths', (Join-Path $repoRoot 'src'), '--distpath', $distRoot, '--workpath', $workRoot, '--specpath', $workRoot,
        '--add-binary', "$ffmpeg;.", '--add-binary', "$ffprobe;.",
        '--add-data', "$(Join-Path $repoRoot 'assets\branding\local-slice-v2.ico');assets\branding",
        '--add-data', "$(Join-Path $repoRoot 'scripts\read_timecode_windows.ps1');scripts",
        '--add-data', "$(Join-Path $repoRoot 'THIRD_PARTY_NOTICES.md');.",
        (Join-Path $repoRoot 'src\local_slice_assistant_app.py')
    )
    if (Test-Path -LiteralPath (Join-Path $repoRoot 'assets\stickers')) {
        $pyInstallerArgs += @('--add-data', "$(Join-Path $repoRoot 'assets\stickers');assets\stickers")
    }
    & $python -m PyInstaller @pyInstallerArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    # The host PATH contains Poppler ICU DLLs that are ABI-incompatible with Qt.
    # The project's established Windows spec excludes these incidental copies.
    foreach ($name in @('icuuc.dll', 'icudt78.dll')) {
        $path = Join-Path $bundle "_internal\$name"
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }

    foreach ($file in @('LICENSE', 'THIRD_PARTY_NOTICES.md')) {
        Copy-Item -LiteralPath (Join-Path $repoRoot $file) -Destination $bundle
    }
    $readme = @(
        '映序 Windows 便携测试候选版',
        '',
        '适用环境：Windows 10/11 x64。解压后运行“映序.exe”，无需安装 Python 或下载源码。',
        '此包为内部测试候选版，不代表已在所有 Windows 设备完成验收，请勿作为公开发行版转发。处理视频时请先备份工程与素材。',
        '',
        '第三方许可：本程序源码采用 MIT License；随包 FFmpeg/ffprobe 为独立的 GPLv3-or-later 构建，Qt 等组件也各受其自身许可约束，详见 THIRD_PARTY_NOTICES.md 和 FFMPEG_BUILD_INFO.txt。此候选版的对应源码材料仍在核验，公开分发前必须补齐。'
    ) -join "`r`n"
    Set-Content -LiteralPath (Join-Path $bundle '使用说明.txt') -Value $readme -Encoding utf8
    $revision = (& git -C $repoRoot rev-parse HEAD).Trim()
    $qtInfo = (& $python -c "import PySide6; from PySide6.QtCore import qVersion; print(f'PySide6 {PySide6.__version__}; Qt {qVersion()}')" 2>&1 | Out-String).Trim()
    $buildInfo = @(
        "Product: 映序 Windows x64 portable test candidate",
        "Source commit: $revision",
        "Built at: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz')",
        "Python: $(& $python --version 2>&1)",
        "PyInstaller: $(& $python -m PyInstaller --version 2>&1)",
        "Qt for Python: $qtInfo"
    ) -join "`r`n"
    Set-Content -LiteralPath (Join-Path $bundle 'BUILD_INFO.txt') -Value $buildInfo -Encoding utf8
    $ffmpegLicense = (& $ffmpeg -L 2>&1 | Out-String).Trim()
    $ffmpegBuild = (& $ffmpeg -buildconf 2>&1 | Out-String).Trim()
    $ffmpegInfo = @(
        'Packaged FFmpeg build details',
        'License declaration from ffmpeg -L:',
        $ffmpegLicense,
        '',
        'Build configuration:',
        $ffmpegBuild
    ) -join "`r`n"
    Set-Content -LiteralPath (Join-Path $bundle 'FFMPEG_BUILD_INFO.txt') -Value $ffmpegInfo -Encoding utf8

    $previousQtPlatform = $env:QT_QPA_PLATFORM
    $env:QT_QPA_PLATFORM = 'offscreen'
    $smoke = Start-Process -FilePath (Join-Path $bundle '映序.exe') -ArgumentList '--smoke-test' -PassThru -Wait
    if ($null -eq $previousQtPlatform) { Remove-Item Env:\QT_QPA_PLATFORM -ErrorAction SilentlyContinue }
    else { $env:QT_QPA_PLATFORM = $previousQtPlatform }
    if ($smoke.ExitCode -ne 0) { throw "Packaged GUI smoke test failed with exit code $($smoke.ExitCode)" }

    $zip = Join-Path $repoRoot "dist\映序-Windows-x64-便携测试候选-$stamp.zip"
    Compress-Archive -Path $bundle -DestinationPath $zip -CompressionLevel Optimal
    $hash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
    $checksumLine = "$hash  $([System.IO.Path]::GetFileName($zip))`r`n"
    [System.IO.File]::WriteAllText("$zip.sha256", $checksumLine, [System.Text.UTF8Encoding]::new($false))
    $size = [math]::Round((Get-Item -LiteralPath $zip).Length / 1MB, 1)
    Write-Host "Windows candidate ready: $zip ($size MiB)"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
