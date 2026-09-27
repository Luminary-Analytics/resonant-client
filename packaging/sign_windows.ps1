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

    Every signature is checked with Get-AuthenticodeSignature: it must be
    Valid and timestamped, since an unstamped signature stops being valid
    when its certificate expires (in days, for Artifact Signing).

    WINDOWS_SIGN_TIMESTAMP_URL overrides the PFX signer's timestamp server
    (default http://timestamp.digicert.com). WINDOWS_SIGNTOOL names the
    signtool to use (default: the Windows SDK's newest x64 signtool), and
    ARTIFACT_SIGNING_DLIB a dlib to use instead of the pinned one.
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Files
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

function Get-Setting([string]$Name) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ($value) { return $value.Trim() }
    return ""
}

function Find-SignTool([switch]$X64) {
    $chosen = Get-Setting "WINDOWS_SIGNTOOL"
    if ($chosen) {
        if (-not (Test-Path -LiteralPath $chosen)) { throw "WINDOWS_SIGNTOOL names no file: $chosen" }
        return $chosen
    }
    if (-not $X64) {
        $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
        if ($onPath) { return $onPath.Source }
    }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw "signtool.exe was not found. Install the Windows SDK signing tools."
}

function Assert-Signature([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne "Valid") {
        throw "The signature on $Path is $($signature.Status): $($signature.StatusMessage)"
    }
    if (-not $signature.TimeStamperCertificate) {
        throw "The signature on $Path has no timestamp, so it would stop being valid when its certificate expires."
    }
    Write-Output "Signed $Path by $($signature.SignerCertificate.Subject), timestamped by $($signature.TimeStamperCertificate.Subject)"
}

foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Nothing to sign at $file" }
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
    if ($env:WINDOWS_SIGNING_REQUIRED -eq "true") {
        throw ("WINDOWS_SIGNING_REQUIRED is true but no signer is configured (Azure Artifact Signing, " +
               "WINDOWS_SIGN_PFX_BASE64 or WINDOWS_SIGN_COMMAND); refusing to release unsigned files.")
    }
    Write-Output "::warning::Not Authenticode-signed: no signer is configured (see docs/release-pipeline.md)."
    return
}
if ($signers[0] -eq "Azure Artifact Signing" -and -not $signedIn) {
    throw ("Azure Artifact Signing is configured, but there was no Azure sign-in to sign with (the release " +
           "environment's AZURE_CLIENT_ID and AZURE_TENANT_ID variables, and the job's id-token permission).")
}

$work = if ($env:RUNNER_TEMP) { $env:RUNNER_TEMP } else { [IO.Path]::GetTempPath() }
$certPath = $null
$metadataPath = $null
try {
    if ($signers[0] -eq "Azure Artifact Signing") {
        $signtool = Find-SignTool -X64
        $dlib = Get-Setting "ARTIFACT_SIGNING_DLIB"
        if (-not $dlib) {
            $dlib = & (Join-Path $PSScriptRoot "fetch_artifact_signing.ps1") -Destination (Join-Path $work "lumi-artifact-signing") |
                Select-Object -Last 1
        }
        if (-not (Test-Path -LiteralPath $dlib)) { throw "No Artifact Signing dlib at $dlib" }
        $metadataPath = Join-Path $work ("lumi-artifact-signing-" + [guid]::NewGuid().ToString("N") + ".json")
        Write-Output ("Signing with Azure Artifact Signing: account $($azure.ARTIFACT_SIGNING_ACCOUNT), " +
                      "certificate profile $($azure.ARTIFACT_SIGNING_PROFILE), $($azure.ARTIFACT_SIGNING_ENDPOINT)")
    } elseif ($pfx) {
        $signtool = Find-SignTool
        $certPath = Join-Path $work ("lumi-sign-" + [guid]::NewGuid().ToString("N") + ".pfx")
        [IO.File]::WriteAllBytes($certPath, [Convert]::FromBase64String($pfx))
    }
    $timestamp = if ($env:WINDOWS_SIGN_TIMESTAMP_URL) { $env:WINDOWS_SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
    foreach ($file in $Files) {
        $full = (Resolve-Path -LiteralPath $file).Path
        if ($metadataPath) {
            # Named in the service's signing history, to trace a signature to its run.
            $correlation = if ($env:GITHUB_RUN_ID) {
                "$env:GITHUB_REPOSITORY run $env:GITHUB_RUN_ID/$env:GITHUB_RUN_ATTEMPT $(Split-Path -Leaf $full)"
            } else {
                "Lumi signing $(Split-Path -Leaf $full)"
            }
            $metadata = [ordered]@{
                Endpoint = $azure.ARTIFACT_SIGNING_ENDPOINT
                CodeSigningAccountName = $azure.ARTIFACT_SIGNING_ACCOUNT
                CertificateProfileName = $azure.ARTIFACT_SIGNING_PROFILE
                CorrelationId = $correlation
                ExcludeCredentials = $ExcludedCredentials
            }
            [IO.File]::WriteAllText($metadataPath, ($metadata | ConvertTo-Json), (New-Object System.Text.UTF8Encoding $false))
            & $signtool sign /v /fd SHA256 /tr $MicrosoftTimestamp /td SHA256 /dlib $dlib /dmdf $metadataPath $full
            if ($LASTEXITCODE -ne 0) { throw "signtool (Azure Artifact Signing) failed on $full with exit code $LASTEXITCODE" }
        } elseif ($pfx) {
            & $signtool sign /fd SHA256 /td SHA256 /tr $timestamp /f $certPath /p $env:WINDOWS_SIGN_PFX_PASSWORD $full
            if ($LASTEXITCODE -ne 0) { throw "signtool failed on $full with exit code $LASTEXITCODE" }
        } else {
            # The command is the operator's own configuration, run as written.
            $line = $command.Replace("{file}", '"' + $full + '"')
            & cmd.exe /d /c $line
            if ($LASTEXITCODE -ne 0) { throw "The signing command failed on $full with exit code $LASTEXITCODE" }
        }
        Assert-Signature $full
    }
} finally {
    if ($certPath -and (Test-Path -LiteralPath $certPath)) { Remove-Item -LiteralPath $certPath -Force }
    if ($metadataPath -and (Test-Path -LiteralPath $metadataPath)) { Remove-Item -LiteralPath $metadataPath -Force }
}
