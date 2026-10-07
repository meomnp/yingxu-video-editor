param([string]$LocalRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot)))
$ErrorActionPreference = 'Stop'
$PublicRoot = Split-Path -Parent $PSScriptRoot
$LocalRoot = (Resolve-Path -LiteralPath $LocalRoot).Path
if ($LocalRoot -eq $PublicRoot) { throw 'Specify the separate local source directory.' }
$Differences = @()
foreach ($Area in @('src','tests','scripts')) {
    $Base = Join-Path $PublicRoot $Area
    foreach ($File in (Get-ChildItem -LiteralPath $Base -File -Recurse | Where-Object { $_.Extension -in @('.py','.ps1','.sh') -and $_.FullName -notmatch '[\\/]__pycache__[\\/]' })) {
        $Relative = $File.FullName.Substring($PublicRoot.Length + 1)
        $Other = Join-Path $LocalRoot $Relative
        if (!(Test-Path -LiteralPath $Other -PathType Leaf) -or
            (Get-FileHash -LiteralPath $File.FullName).Hash -ne (Get-FileHash -LiteralPath $Other).Hash) {
            $Differences += $Relative
        }
    }
}
if ($Differences.Count) {
    $Differences | Write-Output
    throw "Shared files out of sync: $($Differences.Count)"
}
Write-Output 'Shared source, tests and scripts are identical. Local-only files are preserved.'
