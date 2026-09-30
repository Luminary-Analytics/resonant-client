<#
.SYNOPSIS
    Check the Windows release files just before they're published, and list
    what may be published.

.DESCRIPTION
    release.yml's release job runs this twice. With -Handover, right after it
    downloads the build job's artifact: dist must hold exactly the bundle
    (dist/lumi), the installers' license page (dist/legal), the SBOM and the
    third-party notices, so nothing else, an MSI for one, can come from the
    job that built them.

    Without it, just before each upload (the GitHub Release, then the Pages
    site), and publishing only what this job produced and signed:
      - dist holds the bundle, the license page, the SBOM, the notices and
        dist/installer;
        dist/installer holds the installer and, for a stable version, the MSI,
        and nothing else (a beta gets no MSI: an MSI version is three numbers);
      - packaging/sign_windows.ps1 -Verify checks each of those installers
        against the record it made as it signed them (WINDOWS_SIGN_RECORD):
        unchanged since, and signed Valid, timestamped and by
        WINDOWS_SIGN_EXPECTED_SUBJECT; or, recorded unsigned because no signer
        is configured, still unsigned and not required to be signed.

    The list goes to GITHUB_OUTPUT: `files`, one path a line, for the GitHub
    Release, and `msi`, the MSI or nothing, for the Pages site.

.EXAMPLE
    ./packaging/check_release_files.ps1 -Version 0.21.0
#>
param(
    [Parameter(Mandatory = $true)][string]$Version,
    [string]$Dist = "dist",
    [switch]$Handover
)

$ErrorActionPreference = "Stop"

function Assert-Holds([string]$Folder, [string[]]$Expected, [string]$Why) {
    $actual = @(Get-ChildItem -LiteralPath $Folder -Force | ForEach-Object { $_.Name })
    $extra = @($actual | Where-Object { $Expected -notcontains $_ })
    $missing = @($Expected | Where-Object { $actual -notcontains $_ })
    if ($extra.Count -or $missing.Count) {
        $found = if ($actual.Count) { $actual -join ", " } else { "nothing" }
        throw "$Folder holds $found; expected exactly $($Expected -join ', '). $Why"
    }
}

$stable = $Version -notmatch "-"
$sbom = "lumi-$Version-sbom.cdx.json"
$notices = "lumi-$Version-THIRD_PARTY_NOTICES.txt"
if (-not (Test-Path -LiteralPath $Dist -PathType Container)) { throw "No folder at $Dist" }

if ($Handover) {
    Assert-Holds $Dist @("lumi", "legal", $sbom, $notices) ("The build job hands over the bundle, the license " +
        "page, the SBOM and the notices, and nothing else.")
    Write-Output "The build job handed over dist/lumi, dist/legal, $sbom and $notices."
    return
}

$installers = Join-Path $Dist "installer"
$names = @("lumi-setup-$Version.exe")
if ($stable) { $names += "lumi-$Version.msi" }
Assert-Holds $Dist @("installer", "legal", "lumi", $sbom, $notices) ("Only this job's installers are published " +
    "beside the build's SBOM and notices.")
Assert-Holds $installers $names $(if ($stable) { "" } else { "A beta gets no MSI." })

$signed = @($names | ForEach-Object { Join-Path $installers $_ })
& (Join-Path $PSScriptRoot "sign_windows.ps1") -Verify -Files $signed
foreach ($name in $sbom, $notices) {
    if (-not (Test-Path -LiteralPath (Join-Path $Dist $name) -PathType Leaf)) { throw "$name in $Dist isn't a file" }
}

# Paths as the workflow names them (softprops/action-gh-release reads them as patterns: forward slashes).
$base = $Dist.Replace("\", "/").TrimEnd("/")
$files = @($names | ForEach-Object { "$base/installer/$_" }) + @("$base/$sbom", "$base/$notices")
$msi = if ($stable) { "$base/installer/lumi-$Version.msi" } else { "" }
Write-Output "Publishing:"
foreach ($file in $files) {
    Write-Output "  $file (sha256 $((Get-FileHash -Algorithm SHA256 -LiteralPath $file).Hash.ToLowerInvariant()))"
}
if ($env:GITHUB_OUTPUT) {
    $text = "files<<LUMI_RELEASE_FILES`n" + ($files -join "`n") + "`nLUMI_RELEASE_FILES`nmsi=$msi`n"
    [IO.File]::AppendAllText($env:GITHUB_OUTPUT, $text, (New-Object System.Text.UTF8Encoding $false))
}
