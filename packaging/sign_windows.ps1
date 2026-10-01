<#
.SYNOPSIS
    Authenticode-sign release files with the signer that's configured, and
    verify each signature.

.DESCRIPTION
    Windows SmartScreen and many company policies block unsigned installers.
    Lumi's update feed already verifies each installer with an EdDSA signature
    (WinSparkle); Authenticode is the separate, publisher-level signature
    Windows itself checks. The release workflow signs lumi.exe before the
    installer is built, the MSI, and the installer before its EdDSA signature
    is computed, so the update signature covers the signed file. It runs this
    through .github/actions/authenticode-sign, the same way each time.

    This script decides how to sign, from the environment, and checks the
    result. At most one signer may be configured:

      Azure Artifact Signing: ARTIFACT_SIGNING_ENDPOINT, ARTIFACT_SIGNING_ACCOUNT
      and ARTIFACT_SIGNING_PROFILE (variables; none is secret)
          Microsoft's signing service (formerly Trusted Signing). Its
          certificates last a few days and their keys never leave Azure. The
          signer is a short-lived Azure sign-in with GitHub's OIDC token, made
          by the action's azure/login step, which sets ARTIFACT_SIGNING_SIGNED_IN
          to true; no secret is stored anywhere. signtool signs through
          Microsoft's dlib (packaging/fetch_artifact_signing.ps1, pinned by
          SHA-256), which may use only that Azure CLI sign-in, and timestamps
          each signature with Microsoft's server. To sign on your own computer,
          sign in with `az login` and set ARTIFACT_SIGNING_SIGNED_IN=true.

      WINDOWS_SIGN_PFX_BASE64 + WINDOWS_SIGN_PFX_PASSWORD (secrets)
          A code-signing certificate exported as PFX, base64 encoded. Signed
          with signtool and an RFC 3161 timestamp.

      WINDOWS_SIGN_COMMAND
          A command line for another cloud or hardware signer, with {file}
          where the file path goes, for example DigiCert KeyLocker or SSL.com
          eSigner, installed and authenticated by an earlier step.

    With none configured, the files are left unsigned and a warning says so;
    the release continues, unless WINDOWS_SIGNING_REQUIRED is "true" (a
    variable, set once signing works): then the release fails instead of
    quietly shipping unsigned files. A signer that is configured only in
    part, or fails, always fails the release, as do two signers at once.

    WINDOWS_SIGN_EXPECTED_SUBJECT (a variable, set with the signer's) is the
    subject of the certificate that must sign, exactly as Windows shows it,
    for example "CN=Luminary Analytics, O=Luminary Analytics, L=..., S=...,
    C=US"; a signer needs it. Each file must carry no signature before it is
    signed, and afterwards a signature that is Valid (Get-AuthenticodeSignature),
    timestamped and by that subject. So a signer that exits 0 without
    signing, on an unsigned file or on one signed already, or that signs with
    another certificate, fails the release; and an unstamped signature, which
    stops being valid when its certificate expires (in days, for Artifact
    Signing), fails it too.

    With WINDOWS_SIGN_RECORD set (the action sets it), each file's path,
    SHA-256 and whether it was signed are added there as a line of JSON.
    -Verify checks files against that record: unchanged since, and signed as
    recorded (packaging/check_release_files.ps1 runs it just before the
    release publishes anything, and publishes only files it recorded).

    WINDOWS_SIGN_TIMESTAMP_URL overrides the PFX signer's timestamp server
    (default http://timestamp.digicert.com). Outside CI, WINDOWS_SIGNTOOL names
    the signtool to use and ARTIFACT_SIGNING_DLIB a dlib instead of the pinned
    one; in GitHub Actions (GITHUB_ACTIONS=true) either stops the script, and
    only the Windows SDK's signtool, validly signed by Microsoft, and the
    pinned client sign. -CheckTools finds and checks both, and signs nothing
    (build-check.yml's dry run).
#>
[CmdletBinding(DefaultParameterSetName = "Sign")]
param(
    [Parameter(Mandatory = $true, ParameterSetName = "Sign")]
    [Parameter(Mandatory = $true, ParameterSetName = "Verify")]
    [string[]]$Files,
    [Parameter(Mandatory = $true, ParameterSetName = "Verify")][switch]$Verify,
    [Parameter(Mandatory = $true, ParameterSetName = "CheckTools")][switch]$CheckTools
)

$ErrorActionPreference = "Stop"

# Microsoft's timestamp server, as Artifact Signing's documentation and its
# GitHub action use.
$MicrosoftTimestamp = "http://timestamp.acs.microsoft.com"
# The dlib authenticates with DefaultAzureCredential; everything but the Azure
# CLI's sign-in (azure/login, with GitHub's OIDC token) is excluded, so no
# stored secret, cached token or other identity can sign.
$ExcludedCredentials = @(
    "EnvironmentCredential", "WorkloadIdentityCredential", "ManagedIdentityCredential",
    "SharedTokenCacheCredential", "VisualStudioCredential", "VisualStudioCodeCredential",
    "AzurePowerShellCredential", "AzureDeveloperCliCredential", "InteractiveBrowserCredential"
)
$AzureSettings = @("ARTIFACT_SIGNING_ENDPOINT", "ARTIFACT_SIGNING_ACCOUNT", "ARTIFACT_SIGNING_PROFILE")
$Overrides = @("WINDOWS_SIGNTOOL", "ARTIFACT_SIGNING_DLIB")

function Get-Setting([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value.Trim() }
    return ""
}

function Get-Sha256([string]$Path) {
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Assert-MicrosoftSigned([string]$Path, [string]$What) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne "Valid" -or "$($signature.SignerCertificate.Subject)" -notmatch "(^|, )O=Microsoft Corporation(,|$)") {
        throw "$What at $Path isn't validly signed by Microsoft ($($signature.Status): $($signature.SignerCertificate.Subject))"
    }
}

function Find-SignTool([switch]$X64) {
    # An override is the person's own choice, and never reaches CI (refused below).
    $chosen = Get-Setting "WINDOWS_SIGNTOOL"
    if ($chosen) {
        if (-not (Test-Path -LiteralPath $chosen)) { throw "WINDOWS_SIGNTOOL names no file: $chosen" }
        return $chosen
    }
    $found = $null
    if (-not $X64) {
        $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if ($onPath) { $found = $onPath.Source }
    }
    if (-not $found) {
        $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
        if (Test-Path $kits) {
            $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
                Where-Object { $_.FullName -match '\\x64\\' } |
                Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
        }
    }
    if (-not $found) { throw "signtool.exe was not found. Install the Windows SDK signing tools." }
    # The SDK's signtool is Microsoft's; anything else answering to the name doesn't sign.
    Assert-MicrosoftSigned $found "signtool.exe"
    return $found
}

function Get-ArtifactSigningDlib([string]$Work) {
    $dlib = Get-Setting "ARTIFACT_SIGNING_DLIB"
    if (-not $dlib) {
        # Checked against its pinned SHA-256, and as signed by Microsoft, before it's used.
        $dlib = & (Join-Path $PSScriptRoot "fetch_artifact_signing.ps1") -Destination (Join-Path $Work "lumi-artifact-signing") |
            Select-Object -Last 1
    }
    if (-not (Test-Path -LiteralPath $dlib)) { throw "No Artifact Signing dlib at $dlib" }
    return $dlib
}

function Assert-Unsigned([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne "NotSigned") {
        throw ("$Path already carries a signature, or can't be checked ($($signature.Status): " +
               "$($signature.StatusMessage)). Only unsigned files are signed, so a signer that signs nothing " +
               "can't pass for one that did.")
    }
}

function Assert-Signature([string]$Path, [string]$Subject) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne "Valid") {
        throw "The signature on $Path is $($signature.Status): $($signature.StatusMessage)"
    }
    if (-not $signature.TimeStamperCertificate) {
        throw "The signature on $Path has no timestamp, so it would stop being valid when its certificate expires."
    }
    $signer = "$($signature.SignerCertificate.Subject)"
    if (-not $Subject -or $signer -cne $Subject) {
        throw "$Path is signed by `"$signer`", not by WINDOWS_SIGN_EXPECTED_SUBJECT (`"$Subject`")."
    }
    Write-Output "Signed $Path by $signer, timestamped by $($signature.TimeStamperCertificate.Subject)"
}

function Add-Record([string]$Path, [bool]$Signed) {
    $record = Get-Setting "WINDOWS_SIGN_RECORD"
    if (-not $record) { return }
    $entry = [ordered]@{ path = $Path; sha256 = (Get-Sha256 $Path); signed = $Signed }
    [IO.File]::AppendAllText($record, (($entry | ConvertTo-Json -Compress) + "`n"), (New-Object System.Text.UTF8Encoding $false))
}

$inCI = (Get-Setting "GITHUB_ACTIONS") -eq "true"
if ($inCI) {
    foreach ($name in $Overrides) {
        if (Get-Setting $name) {
            throw ("$name is set, but in GitHub Actions only the pinned tools sign: the Windows SDK's signtool, " +
                   "checked as signed by Microsoft, and the Artifact Signing client fetch_artifact_signing.ps1 " +
                   "verifies. Unset $name.")
        }
    }
}
$work = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$expectedSubject = Get-Setting "WINDOWS_SIGN_EXPECTED_SUBJECT"
$required = (Get-Setting "WINDOWS_SIGNING_REQUIRED") -eq "true"

if ($CheckTools) {
    $signtool = Find-SignTool -X64
    Write-Output "signtool: $signtool"
    $dlib = Get-ArtifactSigningDlib $work
    Write-Output "Artifact Signing dlib: $dlib"
    return
}

$full = @()
foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "No file at $file" }
    $full += (Resolve-Path -LiteralPath $file).Path
}

if ($Verify) {
    $recordPath = Get-Setting "WINDOWS_SIGN_RECORD"
    if (-not $recordPath -or -not (Test-Path -LiteralPath $recordPath)) {
        throw "Nothing was recorded as signed: WINDOWS_SIGN_RECORD names no record ($recordPath)."
    }
    $entries = @(Get-Content -LiteralPath $recordPath | Where-Object { $_.Trim() } | ForEach-Object { $_ | ConvertFrom-Json })
    foreach ($path in $full) {
        $entry = $entries | Where-Object { $_.path -eq $path } | Select-Object -Last 1
        if (-not $entry) { throw "$path wasn't signed by sign_windows.ps1 in this job: it isn't in $recordPath." }
        $hash = Get-Sha256 $path
        if ($hash -ne $entry.sha256) {
            throw "$path changed after it was signed (SHA-256 $hash; recorded $($entry.sha256))."
        }
        if ($entry.signed) {
            Assert-Signature $path $expectedSubject
        } else {
            if ($required) { throw "$path was left unsigned, but WINDOWS_SIGNING_REQUIRED is true." }
            Assert-Unsigned $path
            Write-Output "$path is unsigned, as recorded: no signer is configured."
        }
    }
    return
}

$azure = @{}
foreach ($name in $AzureSettings) { $azure[$name] = Get-Setting $name }
$azureCount = @($azure.Values | Where-Object { $_ }).Count
$signedIn = (Get-Setting "ARTIFACT_SIGNING_SIGNED_IN") -eq "true"
$pfx = $env:WINDOWS_SIGN_PFX_BASE64
$command = $env:WINDOWS_SIGN_COMMAND

if ($azureCount -gt 0 -and $azureCount -lt $AzureSettings.Count) {
    $missing = @($AzureSettings | Where-Object { -not $azure[$_] })
    throw "Azure Artifact Signing is configured only in part: set $($missing -join ', ') too, or none of them."
}
$signers = @()
if ($azureCount -eq $AzureSettings.Count) { $signers += "Azure Artifact Signing" }
if ($pfx) { $signers += "WINDOWS_SIGN_PFX_BASE64" }
if ($command) { $signers += "WINDOWS_SIGN_COMMAND" }
if ($signers.Count -gt 1) {
    throw "More than one signer is configured ($($signers -join ', ')); configure one."
}
if ($signedIn -and $azureCount -eq 0) {
    throw ("There was an Azure sign-in for signing (AZURE_CLIENT_ID and AZURE_TENANT_ID are set), but " +
           "$($AzureSettings -join ', ') aren't: set them too, or remove the sign-in's variables.")
}
if ($signers.Count -eq 0) {
    if ($expectedSubject) {
        throw ("WINDOWS_SIGN_EXPECTED_SUBJECT is set, but no signer is configured (Azure Artifact Signing, " +
               "WINDOWS_SIGN_PFX_BASE64 or WINDOWS_SIGN_COMMAND): configure it too, or unset the subject.")
    }
    if ($required) {
        throw ("WINDOWS_SIGNING_REQUIRED is true but no signer is configured (Azure Artifact Signing, " +
               "WINDOWS_SIGN_PFX_BASE64 or WINDOWS_SIGN_COMMAND); refusing to release unsigned files.")
    }
    foreach ($path in $full) {
        Assert-Unsigned $path
        Add-Record $path $false
    }
    Write-Output "::warning::Not Authenticode-signed: no signer is configured (see docs/release-pipeline.md)."
    return
}
if (-not $expectedSubject) {
    throw ("$($signers[0]) is configured, but WINDOWS_SIGN_EXPECTED_SUBJECT isn't: set it to the subject of the " +
           "certificate that signs, as Windows shows it (for example `"CN=Luminary Analytics, O=Luminary Analytics, " +
           "L=..., S=..., C=US`"), so a signature by any other certificate fails the release.")
}
if ($signers[0] -eq "Azure Artifact Signing" -and -not $signedIn) {
    throw ("Azure Artifact Signing is configured, but there was no Azure sign-in to sign with (the release " +
           "environment's AZURE_CLIENT_ID and AZURE_TENANT_ID variables, and the job's id-token permission).")
}
# Before anything is signed: a file signed already would let a signer that
# does nothing look as if it had signed.
foreach ($path in $full) { Assert-Unsigned $path }

$certPath = $null
$metadataPath = $null
try {
    if ($signers[0] -eq "Azure Artifact Signing") {
        $signtool = Find-SignTool -X64
        $dlib = Get-ArtifactSigningDlib $work
        $metadataPath = Join-Path $work ("lumi-artifact-signing-" + [guid]::NewGuid().ToString("N") + ".json")
        Write-Output ("Signing with Azure Artifact Signing: account $($azure.ARTIFACT_SIGNING_ACCOUNT), " +
                      "certificate profile $($azure.ARTIFACT_SIGNING_PROFILE), $($azure.ARTIFACT_SIGNING_ENDPOINT)")
    } elseif ($pfx) {
        $signtool = Find-SignTool
        $certPath = Join-Path $work ("lumi-sign-" + [guid]::NewGuid().ToString("N") + ".pfx")
        [IO.File]::WriteAllBytes($certPath, [Convert]::FromBase64String($pfx))
    }
    $timestamp = if ($env:WINDOWS_SIGN_TIMESTAMP_URL) { $env:WINDOWS_SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    foreach ($path in $full) {
        if ($metadataPath) {
            # Named in the service's signing history, to trace a signature to its run.
            $correlation = if ($env:GITHUB_RUN_ID) {
                "$env:GITHUB_REPOSITORY run $env:GITHUB_RUN_ID/$env:GITHUB_RUN_ATTEMPT $(Split-Path -Leaf $path)"
            } else {
                "Lumi signing $(Split-Path -Leaf $path)"
            }
            $metadata = [ordered]@{
                Endpoint = $azure.ARTIFACT_SIGNING_ENDPOINT
                CodeSigningAccountName = $azure.ARTIFACT_SIGNING_ACCOUNT
                CertificateProfileName = $azure.ARTIFACT_SIGNING_PROFILE
                CorrelationId = $correlation
                ExcludeCredentials = $ExcludedCredentials
            }
            [IO.File]::WriteAllText($metadataPath, ($metadata | ConvertTo-Json), (New-Object System.Text.UTF8Encoding $false))
            & $signtool sign /v /fd SHA256 /tr $MicrosoftTimestamp /td SHA256 /dlib $dlib /dmdf $metadataPath $path
            if ($LASTEXITCODE -ne 0) { throw "signtool (Azure Artifact Signing) failed on $path with exit code $LASTEXITCODE" }
        } elseif ($pfx) {
            & $signtool sign /fd SHA256 /td SHA256 /tr $timestamp /f $certPath /p $env:WINDOWS_SIGN_PFX_PASSWORD $path
            if ($LASTEXITCODE -ne 0) { throw "signtool failed on $path with exit code $LASTEXITCODE" }
        } else {
            # The command is the operator's own configuration, run as written.
            $line = $command.Replace("{file}", '"' + $path + '"')
            & cmd.exe /d /c $line
            if ($LASTEXITCODE -ne 0) { throw "The signing command failed on $path with exit code $LASTEXITCODE" }
        }
        Assert-Signature $path $expectedSubject
        Add-Record $path $true
    }
} finally {
    if ($certPath -and (Test-Path -LiteralPath $certPath)) { Remove-Item -LiteralPath $certPath -Force }
    if ($metadataPath -and (Test-Path -LiteralPath $metadataPath)) { Remove-Item -LiteralPath $metadataPath -Force }
}
