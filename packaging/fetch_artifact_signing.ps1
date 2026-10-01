<#
.SYNOPSIS
    Fetch Microsoft's Artifact Signing client, the dlib signtool loads to sign
    with Azure Artifact Signing, pinned by version and SHA-256.

.DESCRIPTION
    packaging/sign_windows.ps1 runs this when Azure Artifact Signing is
    configured: signtool signs through Azure.CodeSigning.Dlib.dll, which asks
    the Artifact Signing service to sign each file's digest with the
    certificate profile's short-lived certificate.

    Like packaging/fetch_ripgrep.ps1 and packaging/fetch_sparkle.sh: fetched
    at build time from the NuGet package Microsoft publishes
    (Microsoft.ArtifactSigning.Client, formerly Microsoft.Trusted.Signing.Client),
    and checked against the SHA-256 below before anything is extracted. A
    mismatch fails the release; nothing unverified runs. A package kept from
    an earlier run is checked again every time, and the client is always
    extracted afresh.

    Only bin/x64 is extracted: the dlib must match signtool's architecture,
    and sign_windows.ps1 uses the Windows SDK's x64 signtool. It needs the
    .NET 8 runtime, or later (GitHub's Windows runners have it). The client
    is used to sign in CI and is never shipped with Lumi.

    Prints the dlib's path. To upgrade: change both values, take the SHA-256
    of the package NuGet serves and check it against the SHA-512 NuGet
    publishes for that version (its catalog entry's packageHash), and read the
    package's CHANGELOG.md.

.PARAMETER Destination
    Where the package is kept and the client extracted.
.PARAMETER PackagePath
    A package downloaded already (a computer without the internet): checked
    the same way, never trusted as it is.
#>
param(
    [string]$Destination = (Join-Path $PSScriptRoot "artifact-signing"),
    [string]$PackagePath = ""
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"  # Windows PowerShell downloads far slower with a progress bar

$Version = "1.0.128"
$Sha256 = "74bd7d27e6ce1051409c38d9b46bc8df0400ecd643d51ffbf2ac00869061e40b"

$name = "microsoft.artifactsigning.client.$Version.nupkg"
$url = "https://api.nuget.org/v3-flatcontainer/microsoft.artifactsigning.client/$Version/$name"
$kept = Join-Path $Destination $name
$bin = Join-Path $Destination "bin"
$dlib = Join-Path $bin "x64/Azure.CodeSigning.Dlib.dll"

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null
if ($PackagePath) {
    Copy-Item -LiteralPath $PackagePath -Destination $kept -Force
} elseif (-not ((Test-Path -LiteralPath $kept) -and (Get-Sha256 $kept) -eq $Sha256)) {
    Write-Host "Downloading Microsoft.ArtifactSigning.Client $Version"
    Invoke-WebRequest -Uri $url -OutFile $kept -UseBasicParsing
}
$actual = Get-Sha256 $kept
if ($actual -ne $Sha256) {
    Remove-Item -LiteralPath $kept -Force
    throw "Microsoft.ArtifactSigning.Client $Version SHA-256 mismatch (expected $Sha256, got $actual); nothing was extracted."
}

# Afresh: nothing from an earlier run is used as it is.
if (Test-Path -LiteralPath $bin) { Remove-Item -LiteralPath $bin -Recurse -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
$zip = [System.IO.Compression.ZipFile]::OpenRead($kept)
try {
    foreach ($entry in $zip.Entries) {
        if (-not $entry.FullName.StartsWith("bin/x64/") -or -not $entry.Name) { continue }
        $target = [System.IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
        if (-not $target.StartsWith([System.IO.Path]::GetFullPath($bin) + [System.IO.Path]::DirectorySeparatorChar)) {
            throw "The Artifact Signing package names a file outside its folder: $($entry.FullName)"
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
    }
} finally {
    $zip.Dispose()
}
if (-not (Test-Path -LiteralPath $dlib)) { throw "The Artifact Signing package has no bin/x64/Azure.CodeSigning.Dlib.dll" }
# And Microsoft signed what signtool will load.
$signature = Get-AuthenticodeSignature -LiteralPath $dlib
if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notmatch "(^|, )O=Microsoft Corporation(,|$)") {
    throw "Azure.CodeSigning.Dlib.dll isn't validly signed by Microsoft ($($signature.Status): $($signature.SignerCertificate.Subject))"
}
Write-Host "Microsoft.ArtifactSigning.Client $Version (sha256 $actual) in $Destination"
Write-Output $dlib
