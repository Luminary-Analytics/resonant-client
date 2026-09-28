<#
.SYNOPSIS
    Install the WiX Toolset v5 .NET tool from its NuGet package, pinned by
    version and SHA-256, into a folder of its own.

.DESCRIPTION
    packaging/build_msi.ps1 builds the MSI with it. The release job runs this
    in the job that holds the Azure sign-in for Authenticode, after lumi.exe
    is signed, so nothing unverified may run there: the package is downloaded
    from nuget.org and checked against the SHA-256 below before anything is
    installed, and `dotnet tool install` then gets a NuGet configuration
    whose only source is a folder holding that one file, and an empty package
    folder of its own. It can't resolve anything else, and the wix package
    has no dependencies. A mismatch fails the release. A package kept from an
    earlier run is checked again every time, and the tool is always installed
    afresh.

    Like packaging/fetch_artifact_signing.ps1 and packaging/fetch_ripgrep.ps1.
    The tool runs on the .NET 6 runtime or later (GitHub's Windows runners
    have .NET 8 and later); it builds the MSI and is never shipped with Lumi.

    Prints the path of wix.exe, for build_msi.ps1 -Wix. To upgrade: change
    both values, take the SHA-256 of the package NuGet serves and check it
    against the SHA-512 NuGet publishes for that version (its catalog entry's
    packageHash), and read WiX's release notes.

.PARAMETER Destination
    Where the package is kept and the tool installed.
.PARAMETER PackagePath
    A package downloaded already (a computer without the internet): checked
    the same way, never trusted as it is.

.EXAMPLE
    $wix = ./packaging/fetch_wix.ps1 | Select-Object -Last 1
    ./packaging/build_msi.ps1 -Wix $wix
#>
param(
    [string]$Destination = (Join-Path $PSScriptRoot "wix"),
    [string]$PackagePath = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Windows PowerShell downloads far slower with a progress bar

$Version = "5.0.2"
$Sha256 = "f30ef0c74e2a986126539c5780be93ac24e8136eaf723b1937b26272703ae173"

$name = "wix.$Version.nupkg"
$url = "https://api.nuget.org/v3-flatcontainer/wix/$Version/$name"
$source = Join-Path $Destination "source"      # the verified package, and nothing else
$tool = Join-Path $Destination "tool"          # where the tool is installed
$packages = Join-Path $Destination "packages"  # NuGet's package folder, for this install alone
$config = Join-Path $Destination "nuget.config"
$kept = Join-Path $source $name

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

New-Item -ItemType Directory -Force -Path $source | Out-Null
# The folder is the tool install's only package source: nothing but the package stays in it.
Get-ChildItem -LiteralPath $source -Force | Where-Object { $_.Name -ne $name } | Remove-Item -Recurse -Force
if ($PackagePath) {
    Copy-Item -LiteralPath $PackagePath -Destination $kept -Force
} elseif (-not ((Test-Path -LiteralPath $kept) -and (Get-Sha256 $kept) -eq $Sha256)) {
    Write-Host "Downloading WiX $Version"
    Invoke-WebRequest -Uri $url -OutFile $kept -UseBasicParsing
}
$actual = Get-Sha256 $kept
if ($actual -ne $Sha256) {
    Remove-Item -LiteralPath $kept -Force
    throw "WiX $Version SHA-256 mismatch (expected $Sha256, got $actual); nothing was installed."
}

# Afresh: nothing from an earlier run, or from NuGet's usual package folder, is used as it is.
foreach ($folder in $tool, $packages) {
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
Write-Host "WiX $reported (package sha256 $actual) in $tool"
Write-Output $wix
