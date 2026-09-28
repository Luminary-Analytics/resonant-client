<#
.SYNOPSIS
    Build dist/installer/lumi-X.Y.Z.msi from the PyInstaller bundle with WiX v5.

.DESCRIPTION
    Run after the bundle exists (scripts/build_clean.ps1). Needs the WiX v5
    .NET tool: packaging/fetch_wix.ps1 installs the pinned package, checked
    against its SHA-256, and prints the wix.exe to pass as -Wix (the release
    does this). Without -Wix, a wix on PATH is used.

    The MSI's version is the release's three numbers; a pre-release or dev
    suffix can't be expressed in an MSI version, so betas get no MSI.
    packaging/lumi.wxs documents what the package installs.

.EXAMPLE
    $wix = ./packaging/fetch_wix.ps1 | Select-Object -Last 1
    ./packaging/build_msi.ps1 -Version 0.21.0 -Wix $wix
#>
param(
    [string]$Version = "",
    [string]$Wix = "",
    [string]$Bundle = "dist/lumi",
    [string]$OutDir = "dist/installer"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot

if (-not $Version) {
    $init = Get-Content (Join-Path $root "lumi/__init__.py") -Raw
    if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw "No __version__ in lumi/__init__.py" }
    $Version = $Matches[1]
}
if ($Version -notmatch '^(\d+)\.(\d+)\.(\d+)') { throw "Can't make an MSI version from '$Version'" }
$msiVersion = "$($Matches[1]).$($Matches[2]).$($Matches[3])"

if ($Wix) {
    if (-not (Test-Path -LiteralPath $Wix)) { throw "No WiX at $Wix" }
} else {
    $onPath = Get-Command wix -ErrorAction SilentlyContinue
    if (-not $onPath) {
        throw "WiX v5 isn't installed. Run packaging/fetch_wix.ps1 and pass the wix.exe it prints as -Wix."
    }
    $Wix = $onPath.Source
}
$bundlePath = (Resolve-Path (Join-Path $root $Bundle)).Path
if (-not (Test-Path (Join-Path $bundlePath "lumi.exe"))) {
    throw "No lumi.exe in $bundlePath; build the bundle first (scripts/build_clean.ps1)"
}

# Files that exist only in the MSI: the marker that leaves updates to device management.
$extra = Join-Path ([IO.Path]::GetTempPath()) ("lumi-msi-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $extra | Out-Null
try {
    Set-Content -Path (Join-Path $extra "lumi-install.json") -Value '{"installer": "msi"}' -Encoding ascii -NoNewline

    $outDirPath = Join-Path $root $OutDir
    New-Item -ItemType Directory -Force -Path $outDirPath | Out-Null
    $out = Join-Path $outDirPath "lumi-$Version.msi"
    # No .wixpdb beside it: Lumi ships no MSI patches, and the release checks
    # that dist/installer holds only what it publishes.
    & $Wix build (Join-Path $root "packaging/lumi.wxs") -arch x64 -d "Version=$msiVersion" `
        -bindpath "bundle=$bundlePath" -bindpath "extra=$extra" `
        -bindpath "brand=$(Join-Path $root 'lumi/gui/static')" -pdbtype none -o $out
    if ($LASTEXITCODE -ne 0) { throw "wix build failed with exit code $LASTEXITCODE" }
    Write-Host "MSI: $out ($([math]::Round((Get-Item $out).Length / 1MB, 1)) MB, version $msiVersion)"
} finally {
    Remove-Item -Recurse -Force $extra -ErrorAction SilentlyContinue
}
