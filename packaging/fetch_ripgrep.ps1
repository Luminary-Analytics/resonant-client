<#
.SYNOPSIS
    Fetch the pinned ripgrep binary for bundling into the Windows build.

.DESCRIPTION
    The `grep` agent tool prefers ripgrep and falls back to `findstr`, whose
    regex dialect has no alternation, no `+`, and no groups — ordinary
    model-written patterns match nothing and return "(no matches)", which reads
    to an agent as "not in this codebase". Developers have `rg` on PATH and
    never see it; every shipped install without it silently got the weak path.
    Bundling closes that gap.

    Downloaded at build time rather than vendored: a 4 MB binary committed to
    git is permanent weight, and every version bump adds another copy forever.

    The version and hash below are pinned deliberately. An unpinned download
    into a signed installer is a supply-chain hole — the archive is verified
    against a known SHA-256 BEFORE anything is extracted, and the script fails
    loudly rather than silently producing a bundle without search.

    Re-running is cheap: an already-extracted, correct binary short-circuits.

.NOTES
    ripgrep is dual-licensed MIT / Unlicense. Redistributing the binary
    requires shipping its license text, so COPYING / LICENSE-MIT / UNLICENSE
    are extracted alongside rg.exe and bundled by packaging/resonant.spec.
#>
param(
    [string]$Destination = (Join-Path $PSScriptRoot "ripgrep")
)

$ErrorActionPreference = "Stop"

# Pinned release. To upgrade: bump both, and take the hash from the release's
# published .sha256 asset — never from a local download alone.
$Version = "15.2.0"
$Sha256 = "71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5"

$archiveName = "ripgrep-$Version-x86_64-pc-windows-msvc.zip"
$url = "https://github.com/BurntSushi/ripgrep/releases/download/$Version/$archiveName"

# rg.exe plus the license files redistribution requires.
$wanted = @("rg.exe", "COPYING", "LICENSE-MIT", "UNLICENSE")
$rgPath = Join-Path $Destination "rg.exe"

function Test-AlreadyFetched {
    if (-not (Test-Path -LiteralPath $rgPath)) { return $false }
    foreach ($name in $wanted) {
        if (-not (Test-Path -LiteralPath (Join-Path $Destination $name))) { return $false }
    }
    # Confirm it actually runs and is the pinned version, so a truncated or
    # half-extracted copy from an interrupted build is replaced rather than
    # bundled.
    try {
        $reported = & $rgPath --version 2>$null | Select-Object -First 1
    } catch {
        return $false
    }
    return ($reported -match [regex]::Escape($Version))
}

if (Test-AlreadyFetched) {
    Write-Host "ripgrep $Version already present at $Destination"
    exit 0
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$temp = Join-Path ([IO.Path]::GetTempPath()) "resonant-ripgrep-$PID"
New-Item -ItemType Directory -Force -Path $temp | Out-Null

try {
    $zip = Join-Path $temp $archiveName
    Write-Host "Downloading ripgrep $Version..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

    $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLower()
    if ($actual -ne $Sha256) {
        throw @"
ripgrep archive hash mismatch — refusing to bundle it.
  expected $Sha256
  actual   $actual
Either the pinned hash is wrong or the download was tampered with. Do not
"fix" this by updating the constant without checking the release's published
.sha256 asset.
"@
    }
    Write-Host "SHA-256 verified."

    $extract = Join-Path $temp "extracted"
    Expand-Archive -LiteralPath $zip -DestinationPath $extract -Force

    foreach ($name in $wanted) {
        $found = Get-ChildItem -LiteralPath $extract -Recurse -File -Filter $name |
            Select-Object -First 1
        if (-not $found) { throw "Expected '$name' in $archiveName but it was not there." }
        Copy-Item -LiteralPath $found.FullName -Destination (Join-Path $Destination $name) -Force
    }

    $reported = & $rgPath --version 2>$null | Select-Object -First 1
    if ($reported -notmatch [regex]::Escape($Version)) {
        throw "Extracted rg.exe reports '$reported', expected version $Version."
    }
    Write-Host "ripgrep ready: $reported"
} finally {
    if (Test-Path -LiteralPath $temp) {
        Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# Explicit, so the caller can trust the exit code rather than inheriting
# whatever the last native command left in $LASTEXITCODE.
exit 0
