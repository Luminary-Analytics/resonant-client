"""How the release decides to Authenticode-sign, and checks it (packaging/sign_windows.ps1).

The script runs as the release runs it, in PowerShell, with the signer
replaced: WINDOWS_SIGNTOOL names a fake signtool that records how it was
called (and the Artifact Signing metadata it was given), ARTIFACT_SIGNING_DLIB
a stand-in dlib, and a wrapper script shadows Get-AuthenticodeSignature (a
function outranks the cmdlet) with the signature a real signer would have
left. Nothing here signs a file or reaches Azure.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Authenticode signing runs on Windows")

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "packaging" / "sign_windows.ps1"
MICROSOFT_TIMESTAMP = "http://timestamp.acs.microsoft.com"
AZURE = {
    "ARTIFACT_SIGNING_ENDPOINT": "https://eus.codesigning.azure.net",
    "ARTIFACT_SIGNING_ACCOUNT": "luminary",
    "ARTIFACT_SIGNING_PROFILE": "lumi-public-trust",
}
# The release runs it in PowerShell 7 (pwsh); people may run it in Windows PowerShell.
SHELLS = ["powershell.exe", *(["pwsh"] if shutil.which("pwsh") else [])]
SIGNER_SETTINGS = {*AZURE, "ARTIFACT_SIGNING_SIGNED_IN", "ARTIFACT_SIGNING_DLIB", "WINDOWS_SIGN_PFX_BASE64",
                   "WINDOWS_SIGN_PFX_PASSWORD", "WINDOWS_SIGN_COMMAND", "WINDOWS_SIGNING_REQUIRED",
                   "WINDOWS_SIGNTOOL", "WINDOWS_SIGN_TIMESTAMP_URL", "GITHUB_RUN_ID"}
FAKE_SIGNTOOL = """\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
record = {"args": args}
if "/dmdf" in args:
    record["metadata"] = json.loads(Path(args[args.index("/dmdf") + 1]).read_text(encoding="utf-8"))
if "/f" in args:
    record["pfx"] = Path(args[args.index("/f") + 1]).read_bytes().hex()
log = Path(os.environ["FAKE_SIGNTOOL_LOG"])
calls = json.loads(log.read_text(encoding="utf-8")) if log.exists() else []
calls.append(record)
log.write_text(json.dumps(calls), encoding="utf-8")
sys.exit(int(os.environ.get("FAKE_SIGNTOOL_EXIT", "0")))
"""


@pytest.fixture(params=SHELLS)
def signing(request, tmp_path):
    shell = request.param
    fake = tmp_path / "fake_signtool.py"
    fake.write_text(FAKE_SIGNTOOL, encoding="utf-8")
    signtool = tmp_path / "signtool.cmd"
    signtool.write_text(f'@"{sys.executable}" "{fake}" %*\r\n', encoding="utf-8")
    dlib = tmp_path / "Azure.CodeSigning.Dlib.dll"
    dlib.write_bytes(b"stand-in")
    target = tmp_path / "lumi.exe"
    target.write_bytes(b"MZ")
    runner = tmp_path / "runner-temp"
    runner.mkdir()
    log = tmp_path / "signtool-calls.json"

    def run(settings: dict[str, str], *, signature: tuple[str, bool] | None = None) -> subprocess.CompletedProcess:
        """sign_windows.ps1 on the target; ``signature`` is the (status, timestamped) a signer would leave."""
        env = {key: value for key, value in os.environ.items() if key not in SIGNER_SETTINGS}
        env.update(WINDOWS_SIGNTOOL=str(signtool), FAKE_SIGNTOOL_LOG=str(log), RUNNER_TEMP=str(runner))
        env.update(settings)
        script = SCRIPT
        if signature is not None:
            status, stamped = signature
            stamper = ('[pscustomobject]@{ Subject = "CN=Microsoft Public RSA Timestamping CA" }' if stamped
                       else "$null")
            script = tmp_path / "with-signature.ps1"
            script.write_text(
                "param([string[]]$Files)\n"
                "function global:Get-AuthenticodeSignature {\n"
                "    param([string]$LiteralPath)\n"
                f'    [pscustomobject]@{{ Status = "{status}"; StatusMessage = "(stand-in)";\n'
                '        SignerCertificate = [pscustomobject]@{ Subject = "CN=Luminary Analytics" };\n'
                f"        TimeStamperCertificate = {stamper} }}\n"
                "}\n"
                f'& "{SCRIPT}" -Files $Files\n', encoding="utf-8")
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
                               "-Files", str(target)], capture_output=True, text=True, timeout=120, env=env)

    def calls() -> list[dict]:
        return json.loads(log.read_text(encoding="utf-8")) if log.exists() else []

    return SimpleNamespace(run=run, calls=calls, target=target, dlib=dlib, runner=runner)


AZURE_SIGNED_IN = {**AZURE, "ARTIFACT_SIGNING_SIGNED_IN": "true"}


def output(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


class TestNothingConfigured:
    def test_the_file_is_left_as_it_was(self, signing):
        result = signing.run({})
        assert result.returncode == 0, output(result)
        assert "Not Authenticode-signed" in result.stdout
        assert signing.target.read_bytes() == b"MZ" and signing.calls() == []

    def test_signing_required_fails_the_release(self, signing):
        # Once signing works, WINDOWS_SIGNING_REQUIRED turns losing it into a failed release.
        result = signing.run({"WINDOWS_SIGNING_REQUIRED": "true"})
        assert result.returncode != 0
        assert "WINDOWS_SIGNING_REQUIRED" in output(result)
        assert signing.target.read_bytes() == b"MZ" and signing.calls() == []


class TestAzureArtifactSigning:
    def test_signtool_signs_through_the_dlib_and_timestamps_with_microsoft(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                              "GITHUB_REPOSITORY": "Luminary-Analytics/resonant-client"}, signature=("Valid", True))
        assert result.returncode == 0, output(result)
        [call] = signing.calls()
        args = call["args"]
        assert args[0] == "sign" and args[-1] == str(signing.target)
        assert args[args.index("/fd") + 1] == "SHA256"
        assert args[args.index("/tr") + 1] == MICROSOFT_TIMESTAMP and args[args.index("/td") + 1] == "SHA256"
        assert args[args.index("/dlib") + 1] == str(signing.dlib)
        metadata = call["metadata"]
        assert (metadata["Endpoint"], metadata["CodeSigningAccountName"], metadata["CertificateProfileName"]) == (
            AZURE["ARTIFACT_SIGNING_ENDPOINT"], AZURE["ARTIFACT_SIGNING_ACCOUNT"], AZURE["ARTIFACT_SIGNING_PROFILE"])
        assert metadata["CorrelationId"] == "Luminary-Analytics/resonant-client run 123/2 lumi.exe"
        # Only the Azure CLI's sign-in (azure/login) may sign: no stored secret or other identity.
        assert "AzureCliCredential" not in metadata["ExcludeCredentials"]
        assert {"EnvironmentCredential", "ManagedIdentityCredential", "WorkloadIdentityCredential",
                "SharedTokenCacheCredential", "InteractiveBrowserCredential"} <= set(metadata["ExcludeCredentials"])
        assert "Signed " in result.stdout and "timestamped by" in result.stdout
        assert list(signing.runner.iterdir()) == []  # the metadata file is gone

    def test_a_signature_without_a_timestamp_fails(self, signing):
        # An Artifact Signing certificate lasts days; unstamped, the signature would die with it.
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)},
                             signature=("Valid", False))
        assert result.returncode != 0
        assert "no timestamp" in output(result)

    def test_a_signer_that_signs_nothing_fails(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)})  # the real cmdlet
        assert result.returncode != 0
        assert "The signature on" in output(result)
        assert len(signing.calls()) == 1

    def test_a_failing_signtool_fails(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "FAKE_SIGNTOOL_EXIT": "1"}, signature=("Valid", True))
        assert result.returncode != 0
        assert "signtool (Azure Artifact Signing) failed" in output(result)

    def test_signing_required_still_fails_a_failed_signing(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "FAKE_SIGNTOOL_EXIT": "1", "WINDOWS_SIGNING_REQUIRED": "true"}, signature=("Valid", True))
        assert result.returncode != 0

    def test_without_the_azure_sign_in_nothing_runs(self, signing):
        result = signing.run({**AZURE, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)}, signature=("Valid", True))
        assert result.returncode != 0
        assert "no Azure sign-in" in output(result)
        assert signing.calls() == []

    def test_an_account_configured_in_part_is_refused(self, signing):
        result = signing.run({"ARTIFACT_SIGNING_ENDPOINT": AZURE["ARTIFACT_SIGNING_ENDPOINT"],
                              "ARTIFACT_SIGNING_SIGNED_IN": "true"}, signature=("Valid", True))
        assert result.returncode != 0
        assert "ARTIFACT_SIGNING_ACCOUNT, ARTIFACT_SIGNING_PROFILE" in output(result)
        assert signing.calls() == []

    def test_a_sign_in_without_an_account_is_refused(self, signing):
        result = signing.run({"ARTIFACT_SIGNING_SIGNED_IN": "true"}, signature=("Valid", True))
        assert result.returncode != 0
        assert "sign-in" in output(result) and signing.calls() == []

    def test_two_signers_are_refused(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "WINDOWS_SIGN_COMMAND": "echo {file}"}, signature=("Valid", True))
        assert result.returncode != 0
        assert "More than one signer" in output(result)
        assert signing.calls() == []


class TestTheOtherSigners:
    def test_a_pfx_signs_with_its_timestamp_server_and_the_file_is_removed(self, signing):
        certificate = b"not really a certificate"
        result = signing.run({"WINDOWS_SIGN_PFX_BASE64": base64.b64encode(certificate).decode(),
                              "WINDOWS_SIGN_PFX_PASSWORD": "pw"}, signature=("Valid", True))
        assert result.returncode == 0, output(result)
        [call] = signing.calls()
        args = call["args"]
        assert args[args.index("/tr") + 1] == "http://timestamp.digicert.com" and "/dlib" not in args
        assert bytes.fromhex(call["pfx"]) == certificate
        assert list(signing.runner.iterdir()) == []

    def test_a_command_runs_as_written_for_each_file(self, signing, tmp_path):
        command = f"{tmp_path / 'signtool.cmd'} sign {{file}}"
        result = signing.run({"WINDOWS_SIGN_COMMAND": command}, signature=("Valid", True))
        assert result.returncode == 0, output(result)
        assert [call["args"] for call in signing.calls()] == [["sign", str(signing.target)]]


def test_the_signing_client_is_used_only_as_pinned(tmp_path):
    # packaging/fetch_artifact_signing.ps1 checks the package before extracting anything.
    package = tmp_path / "microsoft.artifactsigning.client.nupkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("bin/x64/Azure.CodeSigning.Dlib.dll", b"not Microsoft's")
    destination = tmp_path / "client"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(ROOT / "packaging" / "fetch_artifact_signing.ps1"), "-Destination", str(destination),
                             "-PackagePath", str(package)], capture_output=True, text=True, timeout=120)
    assert result.returncode != 0
    assert "SHA-256 mismatch" in output(result)
    assert not (destination / "bin").exists() and list(destination.iterdir()) == []
