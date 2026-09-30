<#
.SYNOPSIS
    Build dist/installer/lumi-X.Y.Z.msi from the PyInstaller bundle with WiX v5.

.DESCRIPTION
    Run after the bundle exists (scripts/build_clean.ps1). Needs the WiX v5
    .NET tool and its UI extension: packaging/fetch_wix.ps1 installs both from
    their pinned packages, checked against their SHA-256, and returns them to
    pass as -Wix and -UiExtension (the release does this). Without -Wix, a wix
    on PATH is used. The extension is always loaded by path: nothing is
    installed from NuGet here.

    The MSI's version is the release's three numbers; a pre-release or dev
    suffix can't be expressed in an MSI version, so betas get no MSI.
    packaging/lumi.wxs documents what the package installs.

    Its license page shows Lumi's terms for -Version (packaging/legal_texts.py
    renders license.rtf), through the UI extension's WixUI_Minimal dialogs.

.EXAMPLE
    $wix = ./packaging/fetch_wix.ps1 | Select-Object -Last 1
    ./packaging/build_msi.ps1 -Version 0.21.0 -Wix $wix.Wix -UiExtension $wix.UiExtension
#>
param(
    [string]$Version = "",
    [string]$Wix = "",
    [string]$UiExtension = "",
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
        throw "WiX v5 isn't installed. Run packaging/fetch_wix.ps1 and pass its Wix as -Wix."
    }
    $Wix = $onPath.Source
}
# The license dialog (WixUI_Minimal) comes from WiX's UI extension, pinned like the tool.
if (-not $UiExtension -or -not (Test-Path -LiteralPath $UiExtension -PathType Leaf)) {
    throw ("WiX's UI extension isn't at '$UiExtension': run packaging/fetch_wix.ps1 and pass its UiExtension " +
           "as -UiExtension. The MSI's license page needs it.")
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
    # Lumi's terms for the license page: the EULA, and the test terms for a pre-release version.
    $legal = Join-Path $extra "legal"
    & python (Join-Path $root "packaging/legal_texts.py") rtf --out $legal --version $Version
    if ($LASTEXITCODE -ne 0) { throw "Rendering Lumi's terms for the license page failed" }
    $license = Join-Path $legal "license.rtf"

    $outDirPath = Join-Path $root $OutDir
    New-Item -ItemType Directory -Force -Path $outDirPath | Out-Null
    $out = Join-Path $outDirPath "lumi-$Version.msi"
    # No .wixpdb beside it: Lumi ships no MSI patches, and the release checks
    # that dist/installer holds only what it publishes.
    & $Wix build (Join-Path $root "packaging/lumi.wxs") -arch x64 -d "Version=$msiVersion" `
        -d "LicenseRtf=$license" -ext $UiExtension `
        -bindpath "bundle=$bundlePath" -bindpath "extra=$extra" `
        -bindpath "brand=$(Join-Path $root 'lumi/gui/static')" -pdbtype none -o $out
    if ($LASTEXITCODE -ne 0) { throw "wix build failed with exit code $LASTEXITCODE" }
    Write-Host "MSI: $out ($([math]::Round((Get-Item $out).Length / 1MB, 1)) MB, version $msiVersion)"
} finally {
    Remove-Item -Recurse -Force $extra -ErrorAction SilentlyContinue
}
