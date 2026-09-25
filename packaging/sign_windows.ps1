<#
.SYNOPSIS
    Authenticode-sign release files when signing credentials are configured.

.DESCRIPTION
    Windows SmartScreen and many company policies block unsigned installers.
    Lumi's update feed already verifies each installer with an EdDSA signature
    (WinSparkle); Authenticode is the separate, publisher-level signature
    Windows itself checks. The release workflow calls this for lumi.exe before
    the installer is built, and for the installer before its EdDSA signature
    is computed, so the update signature covers the signed file.

    Credentials come from the environment (repository secrets), one of:

      WINDOWS_SIGN_PFX_BASE64 + WINDOWS_SIGN_PFX_PASSWORD
          A code-signing certificate exported as PFX, base64 encoded. Signed
          with signtool and an RFC 3161 timestamp.

      WINDOWS_SIGN_COMMAND
          A command line for a cloud or hardware signer, with {file} where the
          file path goes. Certificates issued since June 2023 keep their keys in
          hardware, so most new certificates use this form: for example Azure
          Trusted Signing through signtool's /dlib, DigiCert KeyLocker or
          SSL.com eSigner, installed and authenticated by an earlier step.

    With neither set, the files are left unsigned and a warning says so; the
    release continues. Signatures are verified after signing.

    WINDOWS_SIGN_TIMESTAMP_URL overrides the timestamp server
    (default http://timestamp.digicert.com).
#>
param(
    [Parameter(Mandatory = $true)][string[]]$Files
)

$ErrorActionPreference = "Stop"

function Find-SignTool {
    $onPath = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $kits = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $kits) {
        $found = Get-ChildItem $kits -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Where-Object { $_.FullName -match '\\x64\\' } |
            Sort-Object FullName -Descending | Select-Object -First 1
        if ($found) { return $found.FullName }
    }
    throw "signtool.exe was not found. Install the Windows SDK signing tools."
}

foreach ($file in $Files) {
    if (-not (Test-Path -LiteralPath $file)) { throw "Nothing to sign at $file" }
}

$pfx = $env:WINDOWS_SIGN_PFX_BASE64
$command = $env:WINDOWS_SIGN_COMMAND
if (-not $pfx -and -not $command) {
    Write-Output "::warning::Not Authenticode-signed: no signing credentials are configured (see docs/release-pipeline.md)."
    return
}

$timestamp = if ($env:WINDOWS_SIGN_TIMESTAMP_URL) { $env:WINDOWS_SIGN_TIMESTAMP_URL } else { "http://timestamp.digicert.com" }
$signtool = $null
$certPath = $null
try {
    if ($pfx) {
        $signtool = Find-SignTool
        $certPath = Join-Path ([IO.Path]::GetTempPath()) ("lumi-sign-" + [guid]::NewGuid().ToString("N") + ".pfx")
        [IO.File]::WriteAllBytes($certPath, [Convert]::FromBase64String($pfx))
    }
    foreach ($file in $Files) {
        $full = (Resolve-Path -LiteralPath $file).Path
        if ($pfx) {
            & $signtool sign /fd SHA256 /td SHA256 /tr $timestamp /f $certPath /p $env:WINDOWS_SIGN_PFX_PASSWORD $full
            if ($LASTEXITCODE -ne 0) { throw "signtool failed on $full with exit code $LASTEXITCODE" }
        } else {
            # The command is the operator's own configuration, run as written.
            $line = $command.Replace("{file}", '"' + $full + '"')
            & cmd.exe /d /c $line
            if ($LASTEXITCODE -ne 0) { throw "The signing command failed on $full with exit code $LASTEXITCODE" }
        }
        $signature = Get-AuthenticodeSignature -LiteralPath $full
        if ($signature.Status -ne "Valid") {
            throw "The signature on $full is $($signature.Status): $($signature.StatusMessage)"
        }
        Write-Output "Signed $full by $($signature.SignerCertificate.Subject)"
    }
} finally {
    if ($certPath -and (Test-Path -LiteralPath $certPath)) { Remove-Item -LiteralPath $certPath -Force }
}
