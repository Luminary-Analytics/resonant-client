<#
.SYNOPSIS
    Publish a macOS release to the Pages site: EdDSA-sign the disk image and the
    macOS feeds, check both with the key the app trusts, and lay them out. Or
    check, or sign again, the macOS feeds a site already has.

.DESCRIPTION
    Publishing: release.yml's publish-macos job runs this with the
    EDDSA_PRIVATE_KEY secret written to a file it deletes afterwards;
    build-macos.yml rehearses it with a throwaway key on a copy of the gh-pages
    branch (scripts/rehearse_pages_publish.py).

    1. Signs the disk image with winsparkle-tool (the tool and key that sign
       the Windows installer) and verifies the signature with -PublicKey, the
       app's SUPublicEDKey, so a mismatched key fails here, not on every Mac.
    2. Copies it (and, for a stable release, the PKG) into downloads/v<version>/
       beside the Windows installer (packaging/publish_pages.py).
    3. Adds it to the macOS feeds (packaging/update_appcast.py --platform macos),
       which writes the feeds it changes afresh, unsigned.
    4. Signs each unsigned macOS feed (Sparkle's SURequireSignedFeed;
       packaging/feed_signature.py), verifying with -PublicKey first. A feed
       whose signing block doesn't verify stops the publish: that needs a
       person (-ResignFeeds below), not a quiet new signature.
    5. Checks every macOS feed on the site, earlier releases' too.
    The Windows feeds aren't touched. It never pushes: packaging/push_pages.py
    does, once the staged blobs check out byte for byte.

    -CheckFeeds checks every macOS feed on the site, and changes nothing.

    -ResignFeeds is for a publish that changed a feed after signing it: it
    checks each macOS feed first and signs again only those that don't verify
    with -PublicKey. Commit and push the result with packaging/push_pages.py;
    see "Repairing the macOS feeds" in docs/release-pipeline.md (which also
    says why rotating the key needs a plan of its own).

.PARAMETER Site
    The gh-pages checkout.
.PARAMETER Release
    The folder holding lumi-<version>.dmg and lumi-<version>.pkg.
.PARAMETER PagesUrl
    The Pages site's address, e.g. https://luminary-analytics.github.io/resonant-client
#>
param(
    [Parameter(Mandatory = $true)] [string]$Site,
    [Parameter(Mandatory = $true)] [string]$PublicKey,
    [string]$Release,
    [string]$Version,
    [string]$PagesUrl,
    [string]$PrivateKeyFile,
    [switch]$Notarized,
    [switch]$CheckFeeds,
    [switch]$ResignFeeds,
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

function Get-MacFeeds {
    return @(Get-ChildItem -LiteralPath $Site -Filter "appcast-macos*.xml" | Sort-Object Name)
}

# "valid", "unsigned" or "invalid": the feed's bytes as Sparkle would check them.
function Get-FeedState([string]$Path) {
    if (-not (Select-String -LiteralPath $Path -SimpleMatch "<!-- sparkle-signatures:" -Quiet)) { return "unsigned" }
    $content = [System.IO.Path]::GetTempFileName()
    try {
        $signature = ("" + (& $Python $feedSignature split $Path $content)).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $signature) { return "invalid" }
        & $tool verify --public-key $PublicKey --signature $signature $content | Out-Null
        if ($LASTEXITCODE -ne 0) { return "invalid" }
        return "valid"
    } finally {
        Remove-Item -Force -ErrorAction SilentlyContinue $content
    }
}

# Sign the feed's bytes as they are now, without any earlier signing block.
function Set-FeedSignature([string]$Path) {
    if (Select-String -LiteralPath $Path -SimpleMatch "<!-- sparkle-signatures:" -Quiet) {
        & $Python $feedSignature strip $Path | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "Couldn't remove the signing block from $Path" }
    }
    $signature = Get-Signature $Path
    Assert-Signature $Path $signature "$(Split-Path -Leaf $Path)'s signature"
    & $Python $feedSignature attach $Path $signature | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "Couldn't attach the signature to $Path" }
}

function Assert-AllFeeds([object[]]$Feeds) {
    $failed = @($Feeds | Where-Object { (Get-FeedState $_.FullName) -ne "valid" } | ForEach-Object { $_.Name })
    if ($failed.Count -gt 0) {
        throw "These macOS feeds don't verify with the app's key: $($failed -join ', ')"
    }
}

if ($CheckFeeds -and $ResignFeeds) { throw "Choose -CheckFeeds or -ResignFeeds, not both" }

if ($CheckFeeds -or $ResignFeeds) {
    $feeds = Get-MacFeeds
    if ($feeds.Count -eq 0) { throw "No macOS feed in $Site" }
    $failing = @()
    foreach ($feed in $feeds) {
        $state = Get-FeedState $feed.FullName
        Write-Host "$($feed.Name): $state"
        if ($state -ne "valid") { $failing += $feed }
    }
    if ($CheckFeeds) {
        if ($failing.Count -gt 0) { throw "$($failing.Count) of $($feeds.Count) macOS feeds don't verify" }
        Write-Host "All $($feeds.Count) macOS feeds verify with the app's key"
        return
    }
    if (-not $PrivateKeyFile) { throw "-ResignFeeds needs -PrivateKeyFile" }
    foreach ($feed in $failing) {
        Set-FeedSignature $feed.FullName
        Write-Host "$($feed.Name): signed again"
    }
    Assert-AllFeeds $feeds
    Write-Host "Signed again: $($failing.Count); all $($feeds.Count) macOS feeds verify. Push with packaging/push_pages.py."
    return
}

foreach ($name in @("Release", "Version", "PagesUrl", "PrivateKeyFile")) {
    if (-not (Get-Variable -Name $name -ValueOnly)) { throw "Publishing needs -$name" }
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

# 3. The macOS feeds. (Single quotes in the notes' HTML: Windows PowerShell
#    drops double quotes inside an argument to a program.)
$notes = "<p>Lumi $Version for macOS. See <a href='$PagesUrl/'>$PagesUrl/</a>.</p>"
& $Python (Join-Path $root "packaging/update_appcast.py") --version $Version --installer $dmg `
    --signature $signature --notes $notes --site $Site --download-base "$PagesUrl/downloads" `
    --platform macos | Out-Host
if ($LASTEXITCODE -ne 0) { throw "update_appcast.py failed" }

# 4. The feeds just written are unsigned; sign each over its bytes as written.
#    One already signed must still verify.
$feeds = Get-MacFeeds
if ($feeds.Count -eq 0) { throw "No macOS feed in $Site" }
foreach ($feed in $feeds) {
    $state = Get-FeedState $feed.FullName
    if ($state -eq "unsigned") {
        Set-FeedSignature $feed.FullName
    } elseif ($state -eq "invalid") {
        throw ("$($feed.Name) has a signing block that doesn't verify with the app's key. Nothing was published: " +
               "see 'Repairing the macOS feeds' in docs/release-pipeline.md")
    }
}

# 5. Every macOS feed a Mac could read verifies with the app's key.
Assert-AllFeeds $feeds
Write-Host "Signed and verified: $dmg and $($feeds.Count) macOS feeds"
