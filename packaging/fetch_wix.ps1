<#
.SYNOPSIS
    Install the WiX Toolset v5 .NET tool and its UI extension from their NuGet
    packages, each pinned by version and SHA-256, into a folder of their own.

.DESCRIPTION
    packaging/build_msi.ps1 builds the MSI with them: the tool, and the UI
    extension whose WixUI_Minimal dialogs show Lumi's terms on the license
    page. The release job runs this in the job that holds the Azure sign-in
    for Authenticode, after lumi.exe is signed, so nothing unverified may run
    there. Each package is downloaded from nuget.org and checked against its
    SHA-256 below before anything is installed:

      - the tool: `dotnet tool install` gets a NuGet configuration whose only
        source is a folder holding that one file, and an empty package folder
        of its own, so it can't resolve anything else (the wix package has no
        dependencies);
      - the UI extension: only its WixToolset.UI.wixext.dll is extracted, and
        build_msi.ps1 loads it by path (`wix build -ext <dll>`), so nothing
        is installed from NuGet at build time (`wix extension add`).

    A mismatch fails the release. A package kept from an earlier run is
    checked again every time, and both are installed afresh.

    Like packaging/fetch_artifact_signing.ps1 and packaging/fetch_ripgrep.ps1.
    The tool runs on the .NET 6 runtime or later (GitHub's Windows runners
    have .NET 8 and later); it builds the MSI and is never shipped with Lumi.

    Returns an object with Wix (wix.exe) and UiExtension (the extension's
    DLL), for build_msi.ps1 -Wix and -UiExtension. To upgrade: change the
    version and both hashes, take the SHA-256 of each package NuGet serves and
    check it against the SHA-512 NuGet publishes for that version (its catalog
    entry's packageHash), and read WiX's release notes. The extension's
    version must match the tool's.

.PARAMETER Destination
    Where the packages are kept and the tool and extension installed.
.PARAMETER PackagePath
    The tool's package, downloaded already (a computer without the internet):
    checked the same way, never trusted as it is.
.PARAMETER UiExtensionPackagePath
    The UI extension's package, downloaded already: checked the same way.

.EXAMPLE
    $wix = ./packaging/fetch_wix.ps1 | Select-Object -Last 1
    ./packaging/build_msi.ps1 -Wix $wix.Wix -UiExtension $wix.UiExtension
#>
param(
    [string]$Destination = (Join-Path $PSScriptRoot "wix"),
    [string]$PackagePath = "",
    [string]$UiExtensionPackagePath = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Windows PowerShell downloads far slower with a progress bar

$Version = "5.0.2"
$Sha256 = "f30ef0c74e2a986126539c5780be93ac24e8136eaf723b1937b26272703ae173"
$UiExtensionSha256 = "5ef2c707614b9f70b6bbadd2d4abcb4124efee215e9b16bfbc80113079a604c7"

$source = Join-Path $Destination "source"                    # the verified tool package, and nothing else
$extensionSource = Join-Path $Destination "extension-source"  # the verified extension package
$tool = Join-Path $Destination "tool"                        # where the tool is installed
$extensions = Join-Path $Destination "extensions"            # the extension's DLL
$packages = Join-Path $Destination "packages"                # NuGet's package folder, for this install alone
$config = Join-Path $Destination "nuget.config"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

# The package in $Folder, checked against $Expected; $Folder holds nothing else.
function Get-CheckedPackage([string]$Name, [string]$Id, [string]$Expected, [string]$Folder, [string]$Given) {
    $file = "$($Id.ToLowerInvariant()).$Version.nupkg"
    $kept = Join-Path $Folder $file
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    Get-ChildItem -LiteralPath $Folder -Force | Where-Object { $_.Name -ne $file } | Remove-Item -Recurse -Force
    if ($Given) {
        Copy-Item -LiteralPath $Given -Destination $kept -Force
    } elseif (-not ((Test-Path -LiteralPath $kept) -and (Get-Sha256 $kept) -eq $Expected)) {
        Write-Host "Downloading $Name $Version"
        $url = "https://api.nuget.org/v3-flatcontainer/$($Id.ToLowerInvariant())/$Version/$file"
        Invoke-WebRequest -Uri $url -OutFile $kept -UseBasicParsing
    }
    $actual = Get-Sha256 $kept
    if ($actual -ne $Expected) {
        Remove-Item -LiteralPath $kept -Force
        throw "$Name $Version SHA-256 mismatch (expected $Expected, got $actual); nothing was installed."
    }
    return $kept
}

$toolPackage = Get-CheckedPackage "WiX" "wix" $Sha256 $source $PackagePath
$extensionPackage = Get-CheckedPackage "WiX UI extension" "WixToolset.UI.wixext" $UiExtensionSha256 `
    $extensionSource $UiExtensionPackagePath

# Afresh: nothing from an earlier run, or from NuGet's usual package folder, is used as it is.
foreach ($folder in $tool, $extensions, $packages) {
    if (Test-Path -LiteralPath $folder) { Remove-Item -LiteralPath $folder -Recurse -Force }
}
$sourceXml = [Security.SecurityElement]::Escape((Resolve-Path -LiteralPath $source).Path)
$configXml = @"
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="lumi-wix" value="$sourceXml" />
  </packageSources>
</configuration>
"@
[IO.File]::WriteAllText($config, $configXml, (New-Object System.Text.UTF8Encoding $false))

$saved = @{}
foreach ($setting in "NUGET_PACKAGES", "DOTNET_CLI_TELEMETRY_OPTOUT", "DOTNET_NOLOGO") {
    $saved[$setting] = [Environment]::GetEnvironmentVariable($setting)
}
try {
    # --configfile: only this file's settings apply, so its one source is the only one.
    $env:NUGET_PACKAGES = $packages
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = "1"
    $env:DOTNET_NOLOGO = "1"
    & dotnet tool install wix --version $Version --tool-path $tool --configfile $config | Write-Host
    if ($LASTEXITCODE -ne 0) { throw "dotnet tool install failed with exit code $LASTEXITCODE" }
} finally {
    foreach ($setting in $saved.Keys) { [Environment]::SetEnvironmentVariable($setting, $saved[$setting]) }
}

$wix = Join-Path $tool "wix.exe"
if (-not (Test-Path -LiteralPath $wix)) { throw "dotnet tool install left no wix.exe in $tool" }
$reported = "$(& $wix --version)".Trim()
if ($LASTEXITCODE -ne 0 -or $reported -notmatch "^$([regex]::Escape($Version))(\+|$)") {
    throw "The installed WiX reports version '$reported', not $Version"
}

# The UI extension: the one DLL WiX v5 loads, taken from the checked package.
New-Item -ItemType Directory -Path $extensions | Out-Null
$uiExtension = Join-Path $extensions "WixToolset.UI.wixext.dll"
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($extensionPackage)
try {
    $entry = $zip.Entries | Where-Object { $_.FullName -eq "wixext5/WixToolset.UI.wixext.dll" } | Select-Object -First 1
    if (-not $entry) { throw "The WiX UI extension package has no wixext5/WixToolset.UI.wixext.dll" }
    [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $uiExtension, $true)
} finally {
    $zip.Dispose()
}

Write-Host "WiX $reported (package sha256 $(Get-Sha256 $toolPackage)) in $tool"
Write-Host "WiX UI extension $Version (package sha256 $(Get-Sha256 $extensionPackage)) at $uiExtension"
[pscustomobject]@{ Wix = $wix; UiExtension = $uiExtension }
