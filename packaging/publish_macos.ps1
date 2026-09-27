<#
.SYNOPSIS
    Publish a macOS release to the Pages site: EdDSA-sign the disk image and the
    macOS feeds, check both with the key the app trusts, and lay them out.

.DESCRIPTION
    release.yml's publish-macos job runs this with the EDDSA_PRIVATE_KEY secret
    written to a file it deletes afterwards; build-macos.yml runs it with a
    throwaway key on a scratch copy of the gh-pages branch, as a dry run. It
    never pushes: the caller does, when this succeeds.

    1. Signs the disk image with winsparkle-tool (the tool and key that sign
       the Windows installer) and verifies the signature with -PublicKey, the
       app's SUPublicEDKey, so a mismatched key fails here, not on every Mac.
    2. Copies it (and, for a stable release, the PKG) into downloads/v<version>/
       beside the Windows installer (packaging/publish_pages.py).
    3. Adds it to the macOS feeds (packaging/update_appcast.py --platform macos).
    4. Signs each macOS feed it wrote (Sparkle's SURequireSignedFeed;
       packaging/feed_signature.py), again verifying with -PublicKey first.
    5. Verifies every macOS feed on the site, earlier releases' too.
    The Windows feeds aren't touched.

.PARAMETER Site
    The gh-pages checkout.
.PARAMETER Release
    The folder holding lumi-<version>.dmg and lumi-<version>.pkg.
.PARAMETER PagesUrl
    The Pages site's address, e.g. https://luminary-analytics.github.io/resonant-client
#>
param(
    [Parameter(Mandatory = $true)] [string]$Site,
    [Parameter(Mandatory = $true)] [string]$Release,
    [Parameter(Mandatory = $true)] [string]$Version,
    [Parameter(Mandatory = $true)] [string]$PagesUrl,
    [Parameter(Mandatory = $true)] [string]$PrivateKeyFile,
    [Parameter(Mandatory = $true)] [string]$PublicKey,
    [switch]$Notarized,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$tool = Join-Path $root "packaging/winsparkle/WinSparkle-0.9.2/bin/winsparkle-tool.exe"
$feedSignature = Join-Path $root "packaging/feed_signature.py"

function Get-Signature([string]$Path) {
    $output = & $tool sign --private-key-file $PrivateKeyFile $Path
    if ($LASTEXITCODE -ne 0) { throw "winsparkle-tool sign failed for $Path (exit $LASTEXITCODE)" }
    $signature = ("" + $output).Trim()
    if (-not $signature) { throw "winsparkle-tool sign printed no signature for $Path" }
    return $signature
}

function Assert-Signature([string]$Path, [string]$Signature, [string]$What) {
    & $tool verify --public-key $PublicKey --signature $Signature $Path | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$What doesn't verify with the app's key ($PublicKey): the private key doesn't match lumi/updater.py"
    }
}

$dmg = Join-Path $Release "lumi-$Version.dmg"
if (-not (Test-Path -LiteralPath $dmg)) { throw "No disk image at $dmg" }

# 1. The disk image's final bytes (after notarization and stapling).
$signature = Get-Signature $dmg
Assert-Signature $dmg $signature "The disk image's signature"
Write-Host "Disk image signed: $($signature.Substring(0, [Math]::Min(20, $signature.Length)))..."

# 2. The Pages copy, beside the Windows installer. Stable releases host the
#    PKG for administrators, as they do the MSI.
$extras = @()
$pkg = Join-Path $Release "lumi-$Version.pkg"
if (-not $Version.Contains("-") -and (Test-Path -LiteralPath $pkg)) { $extras = @("--extra", $pkg) }
$notarizedFlag = @()
if ($Notarized) { $notarizedFlag = @("--notarized") }
& $Python (Join-Path $root "packaging/publish_pages.py") --site $Site --installer $dmg --version $Version `
    --platform macos @extras @notarizedFlag | Out-Host
if ($LASTEXITCODE -ne 0) { throw "publish_pages.py failed" }

# 3. The macOS feeds.
$notes = "<p>Lumi $Version for macOS. See <a href=`"$PagesUrl/`">$PagesUrl/</a>.</p>"
& $Python (Join-Path $root "packaging/update_appcast.py") --version $Version --installer $dmg `
    --signature $signature --notes $notes --site $Site --download-base "$PagesUrl/downloads" `
    --platform macos | Out-Host
if ($LASTEXITCODE -ne 0) { throw "update_appcast.py failed" }

# 4. The feeds just written are unsigned; sign each over its bytes as written.
$feeds = @(Get-ChildItem -LiteralPath $Site -Filter "appcast-macos*.xml" | Sort-Object Name)
if ($feeds.Count -eq 0) { throw "No macOS feed in $Site" }
foreach ($feed in $feeds) {
    if (Select-String -LiteralPath $feed.FullName -SimpleMatch "<!-- sparkle-signatures:" -Quiet) { continue }
    $feedSig = Get-Signature $feed.FullName
    Assert-Signature $feed.FullName $feedSig "$($feed.Name)'s signature"
    & $Python $feedSignature attach $feed.FullName $feedSig | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Couldn't attach the signature to $($feed.Name)" }
}

# 5. Every macOS feed a Mac could read verifies with the app's key.
foreach ($feed in $feeds) {
    $content = [System.IO.Path]::GetTempFileName()
    try {
        $feedSig = ("" + (& $Python $feedSignature split $feed.FullName $content)).Trim()
        if ($LASTEXITCODE -ne 0) { throw "$($feed.Name) has no valid signing block" }
        Assert-Signature $content $feedSig "$($feed.Name)'s signing block"
    } finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $content
    }
}
Write-Host "Signed and verified: $dmg and $($feeds.Count) macOS feeds"
