#Requires -Version 5.1

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = $scriptDir
$specPath = Join-Path $projectRoot "source\launcher\gui_installer.spec"
$distRoot = Join-Path $projectRoot "dist"
$outerExe = Join-Path $distRoot "PlatformIO_Offline_Installer.exe"

Write-Host "Building offline installer from: $specPath"
Push-Location $projectRoot
try {
    python -m PyInstaller $specPath --noconfirm
    if (Test-Path -LiteralPath $outerExe -PathType Leaf) {
        Remove-Item -LiteralPath $outerExe -Force
        Write-Host "Removed redundant outer exe: $outerExe"
    }

    $innerExe = Join-Path $distRoot "PlatformIO_Offline_Installer\PlatformIO_Offline_Installer.exe"
    if (-not (Test-Path -LiteralPath $innerExe -PathType Leaf)) {
        throw "Expected installer entry not found: $innerExe"
    }

    Write-Host "Done. Use this installer:"
    Write-Host $innerExe
}
finally {
    Pop-Location
}
