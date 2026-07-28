<#
.SYNOPSIS
    Fetch the pinned frontend libraries and fonts that index.html used to load
    from CDNs.

.DESCRIPTION
    Until v0.11.14 the page pulled five things from the network on every start:
    marked, highlight.js and its stylesheet, DOMPurify, and the Inter font via
    Google Fonts. All five sit in <head>, so all five are render-blocking.

    That is wrong for a desktop app in three ways. It puts a network round trip
    on the critical path of every launch (the Google Fonts stylesheet alone
    measured 273 ms locally); it makes startup depend on internet access and
    degrade badly without it; and it reports every launch to two third parties.

    Same reasoning as packaging/fetch_ripgrep.ps1: fetched at build time and
    SHA-256 verified rather than committed, because a binary in git is
    permanent weight, and pinned rather than floating, because an unverified
    download feeding a signed installer is a supply-chain hole. `marked` was
    previously loaded from an UNPINNED CDN path — whatever the CDN served that
    day ran inside the app.

.NOTES
    Licences: marked (MIT), highlight.js (BSD-3-Clause), DOMPurify
    (Apache-2.0 OR MPL-2.0), Inter (SIL OFL 1.1). All permit redistribution.
#>
param(
    # Fetched straight into the directory the GUI already serves, so a source
    # checkout and the packaged bundle resolve these the same way and there is
    # no copy step to forget. Gitignored.
    [string]$Destination = (Join-Path $PSScriptRoot "..\resonant_client\gui\static\vendor")
)

$ErrorActionPreference = "Stop"

# name = url, sha256
$Assets = @(
    @{ Name = "marked.min.js"
       Url  = "https://cdn.jsdelivr.net/npm/marked@12.0.2/marked.min.js"
       Sha  = "15fabce5b65898b32b03f5ed25e9f891a729ad4c0d6d877110a7744aa847a894" }
    @{ Name = "highlight.min.js"
       Url  = "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/highlight.min.js"
       Sha  = "837a6fa5b0c736b52bbde2b2b6190f305da3fc9ed41681db5321507057b5c846" }
    @{ Name = "github-dark-dimmed.min.css"
       Url  = "https://cdn.jsdelivr.net/gh/highlightjs/cdn-release@11.9.0/build/styles/github-dark-dimmed.min.css"
       Sha  = "bc1116bfba58ee83794d53b8bd08e5ab13cba81bf03454cf67d6cfe435033cae" }
    @{ Name = "purify.min.js"
       Url  = "https://cdn.jsdelivr.net/npm/dompurify@3.0.6/dist/purify.min.js"
       Sha  = "ea4b09082ca4ba0ae71be6431a097678751d0453b9c52a4d2c7c39a2166ed9fc" }
    @{ Name = "inter-latin-400-normal.woff2"
       Url  = "https://cdn.jsdelivr.net/npm/@fontsource/inter@5.0.17/files/inter-latin-400-normal.woff2"
       Sha  = "2301bb030a2bcaa9c763cc4771bd717aac16709c29eaba00673fcbe7cdf99a59" }
    @{ Name = "inter-latin-500-normal.woff2"
       Url  = "https://cdn.jsdelivr.net/npm/@fontsource/inter@5.0.17/files/inter-latin-500-normal.woff2"
       Sha  = "eebf14aba456b89b7e899584e076588a92e422a45b37fb5fa36ce17519a3e8c5" }
    @{ Name = "inter-latin-600-normal.woff2"
       Url  = "https://cdn.jsdelivr.net/npm/@fontsource/inter@5.0.17/files/inter-latin-600-normal.woff2"
       Sha  = "3022fadde78fd30c384797bcef8bebc18c96083527a850f62a58d8957a8b208f" }
    @{ Name = "inter-latin-700-normal.woff2"
       Url  = "https://cdn.jsdelivr.net/npm/@fontsource/inter@5.0.17/files/inter-latin-700-normal.woff2"
       Sha  = "7b43cb86a0e63bbb55376b4ea60d8cc9527a1421c367aa09962725e0c5140f5f" }
)

function Get-Sha256([string]$Path) {
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

New-Item -ItemType Directory -Force -Path $Destination | Out-Null

$fetched = 0
foreach ($asset in $Assets) {
    $target = Join-Path $Destination $asset.Name
    if ((Test-Path -LiteralPath $target) -and (Get-Sha256 $target) -eq $asset.Sha) {
        continue  # already present and correct
    }

    Write-Host "Fetching $($asset.Name)..."
    $temp = "$target.download"
    Invoke-WebRequest -Uri $asset.Url -OutFile $temp -UseBasicParsing

    $actual = Get-Sha256 $temp
    if ($actual -ne $asset.Sha) {
        Remove-Item -LiteralPath $temp -Force -ErrorAction SilentlyContinue
        throw @"
Hash mismatch for $($asset.Name) — refusing to bundle it.
  expected $($asset.Sha)
  actual   $actual
Do not "fix" this by pasting in the new hash. Check what changed upstream
first; these files execute inside the signed application.
"@
    }
    Move-Item -LiteralPath $temp -Destination $target -Force
    $fetched++
}

Write-Host "Web assets ready at $Destination ($fetched fetched, $($Assets.Count - $fetched) cached)"
exit 0
