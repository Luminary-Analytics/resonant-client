"""How the release decides to Authenticode-sign, checks it (packaging/sign_windows.ps1),
and checks what it publishes (packaging/check_release_files.ps1).

The scripts run as the release runs them, in PowerShell, with the signer
replaced: WINDOWS_SIGNTOOL names a fake signtool that records how it was
called (and the Artifact Signing metadata it was given) and marks the file it
"signs", ARTIFACT_SIGNING_DLIB a stand-in dlib, and a wrapper script shadows
Get-AuthenticodeSignature (a function outranks the cmdlet) with a stand-in
that sees a marked file as signed and any other as unsigned. A few tests use
the real cmdlet on a real unsigned program. The wrapper stops on any error, so
a script that can't even be called never passes. Nothing here signs a file or
reaches Azure.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
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
CHECK = ROOT / "packaging" / "check_release_files.ps1"
UNSIGNED_PROGRAM = ROOT / "packaging" / "winsparkle" / "WinSparkle-0.9.2" / "bin" / "winsparkle-tool.exe"
MICROSOFT_TIMESTAMP = "http://timestamp.acs.microsoft.com"
SUBJECT = "CN=Luminary Analytics, O=Luminary Analytics, L=Wilmington, S=Delaware, C=US"
AZURE = {
    "ARTIFACT_SIGNING_ENDPOINT": "https://eus.codesigning.azure.net",
    "ARTIFACT_SIGNING_ACCOUNT": "luminary",
    "ARTIFACT_SIGNING_PROFILE": "lumi-public-trust",
}
# The stand-in signtool appends this; the stand-in Get-AuthenticodeSignature looks for it.
MARK = "<signed by the stand-in signtool>"
# The release runs them in PowerShell 7 (pwsh); people may run them in Windows PowerShell.
SHELLS = ["powershell.exe", *(["pwsh"] if shutil.which("pwsh") else [])]
SETTINGS = {*AZURE, "ARTIFACT_SIGNING_SIGNED_IN", "ARTIFACT_SIGNING_DLIB", "WINDOWS_SIGN_PFX_BASE64",
            "WINDOWS_SIGN_PFX_PASSWORD", "WINDOWS_SIGN_COMMAND", "WINDOWS_SIGNING_REQUIRED",
            "WINDOWS_SIGNTOOL", "WINDOWS_SIGN_TIMESTAMP_URL", "WINDOWS_SIGN_EXPECTED_SUBJECT",
            "WINDOWS_SIGN_RECORD", "GITHUB_ACTIONS", "GITHUB_OUTPUT", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT",
            "GITHUB_REPOSITORY",
            # Each PowerShell finds its own modules: under a PowerShell 7 parent (as in CI),
            # Windows PowerShell given pwsh's module path can't load Get-FileHash or
            # Get-AuthenticodeSignature.
            "PSMODULEPATH"}


def clean_environment() -> dict[str, str]:
    """This process's environment without the signer's and CI's settings (os.environ's names are upper case on Windows)."""
    return {key: value for key, value in os.environ.items() if key.upper() not in SETTINGS}


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
code = int(os.environ.get("FAKE_SIGNTOOL_EXIT", "0"))
if code == 0 and not os.environ.get("FAKE_SIGNTOOL_SIGNS_NOTHING"):
    with open(args[-1], "ab") as target:
        target.write(MARK.encode("ascii"))
sys.exit(code)
""".replace("MARK", repr(MARK))

STAND_IN = """\
function global:Get-AuthenticodeSignature {
    param([string]$LiteralPath)
    # Latin-1 reads byte for byte.
    $text = [IO.File]::ReadAllText($LiteralPath, [Text.Encoding]::GetEncoding(28591))
    if ($text.EndsWith("MARK")) {
        [pscustomobject]@{ Status = "STATUS"; StatusMessage = "(stand-in)";
            SignerCertificate = [pscustomobject]@{ Subject = "SIGNER" }; TimeStamperCertificate = STAMPER }
    } else {
        [pscustomobject]@{ Status = "NotSigned"; StatusMessage = "(stand-in) not signed";
            SignerCertificate = $null; TimeStamperCertificate = $null }
    }
}
"""
# The script's parameters come as JSON (STAND_IN_CALL) and are splatted, as the
# action passes -Files: an array, however many files. A failure is written as the
# script put it: each PowerShell formats, colors and wraps error records its own way.
CALL = """\
$ErrorActionPreference = "Stop"
@@STUB@@
$call = @{}
($env:STAND_IN_CALL | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $call[$_.Name] = $_.Value }
try {
    & "@@TARGET@@" @call
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
exit 0
"""


def wrapper(target: Path, signature: tuple[str, bool, str] | None) -> str:
    """PowerShell that calls ``target``; with ``signature``, (status, timestamped, signer), through the stand-in."""
    stub = ""
    if signature is not None:
        status, stamped, signer = signature
        stamper = '[pscustomobject]@{ Subject = "CN=Microsoft Public RSA Timestamping CA" }' if stamped else "$null"
        stub = (STAND_IN.replace("MARK", MARK).replace("STATUS", status).replace("SIGNER", signer)
                .replace("STAMPER", stamper))
    return CALL.replace("@@STUB@@", stub).replace("@@TARGET@@", str(target))


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
    record = tmp_path / "signed.jsonl"
    output = tmp_path / "github-output.txt"

    def run(settings: dict[str, str | None], *, files: list[Path] | None = None, call: dict | None = None,
            script: Path = SCRIPT, signature: tuple[str, bool, str] = ("Valid", True, SUBJECT),
            real: bool = False, fake_signtool: bool = True) -> subprocess.CompletedProcess:
        """Run ``script`` with ``call``'s parameters (sign_windows.ps1 -Files, the target by default).

        ``signature`` is the (status, timestamped, signer) Get-AuthenticodeSignature's stand-in
        gives a file the fake signtool marked; ``real`` uses the cmdlet itself.
        """
        env = clean_environment()
        if fake_signtool:
            env["WINDOWS_SIGNTOOL"] = str(signtool)
        env.update(FAKE_SIGNTOOL_LOG=str(log), RUNNER_TEMP=str(runner), WINDOWS_SIGN_RECORD=str(record),
                   GITHUB_OUTPUT=str(output))
        for name, value in settings.items():  # None: not set at all
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        if call is None:
            call = {"Files": [str(file) for file in (files or [target])]}
        env["STAND_IN_CALL"] = json.dumps(call)
        path = tmp_path / "call.ps1"
        path.write_text(wrapper(script, None if real else signature), encoding="utf-8")
        return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path)],
                              capture_output=True, text=True, timeout=120, env=env)

    def calls() -> list[dict]:
        return json.loads(log.read_text(encoding="utf-8")) if log.exists() else []

    def recorded() -> list[dict]:
        return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()] if record.exists() else []

    def outputs() -> dict[str, str]:
        """GITHUB_OUTPUT's values, multi-line ones (name<<DELIMITER) included."""
        values, lines = {}, output.read_text(encoding="utf-8").splitlines() if output.exists() else []
        while lines:
            line = lines.pop(0)
            if "<<" in line:
                name, delimiter = line.split("<<", 1)
                end = lines.index(delimiter)
                values[name], lines = "\n".join(lines[:end]), lines[end + 1:]
            elif "=" in line:
                name, value = line.split("=", 1)
                values[name] = value
        return values

    def unsigned_program(name: str = "lumi-real.exe") -> Path:
        path = tmp_path / name
        shutil.copyfile(UNSIGNED_PROGRAM, path)
        return path

    return SimpleNamespace(run=run, calls=calls, recorded=recorded, outputs=outputs, target=target, dlib=dlib,
                           runner=runner, tmp_path=tmp_path, unsigned_program=unsigned_program)


AZURE_SIGNED_IN = {**AZURE, "ARTIFACT_SIGNING_SIGNED_IN": "true", "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT}


def output(result: subprocess.CompletedProcess) -> str:
    return result.stdout + result.stderr


def said(result: subprocess.CompletedProcess, text: str) -> bool:
    """Whether the output says ``text``. Errors come plain from the wrapper, but a
    script run directly gets PowerShell's own view: Windows PowerShell wraps long
    lines at the console's width, even inside words, and PowerShell 7 colors them.
    So colors and whitespace don't count."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output(result))
    return "".join(text.split()) in "".join(plain.split())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class TestNothingConfigured:
    def test_the_file_is_left_as_it_was_and_recorded_unsigned(self, signing):
        result = signing.run({})
        assert result.returncode == 0, output(result)
        assert "Not Authenticode-signed" in result.stdout
        assert signing.target.read_bytes() == b"MZ" and signing.calls() == []
        [entry] = signing.recorded()
        assert entry == {"path": str(signing.target), "sha256": sha256(signing.target), "signed": False}

    def test_signing_required_fails_the_release(self, signing):
        # Once signing works, WINDOWS_SIGNING_REQUIRED turns losing it into a failed release.
        result = signing.run({"WINDOWS_SIGNING_REQUIRED": "true"})
        assert result.returncode != 0
        assert said(result, "WINDOWS_SIGNING_REQUIRED")
        assert signing.target.read_bytes() == b"MZ" and signing.calls() == [] and signing.recorded() == []

    def test_an_expected_subject_without_a_signer_is_refused(self, signing):
        result = signing.run({"WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT})
        assert result.returncode != 0
        assert said(result, "no signer is configured") and signing.recorded() == []


class TestAzureArtifactSigning:
    def test_signtool_signs_through_the_dlib_and_timestamps_with_microsoft(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "2",
                              "GITHUB_REPOSITORY": "Luminary-Analytics/resonant-client"})
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
        assert f"Signed {signing.target} by {SUBJECT}, timestamped by" in result.stdout
        assert list(signing.runner.iterdir()) == []  # the metadata file is gone
        # Recorded as signed, with the signed file's SHA-256.
        assert signing.recorded() == [{"path": str(signing.target), "sha256": sha256(signing.target), "signed": True}]

    def test_a_signature_without_a_timestamp_fails(self, signing):
        # An Artifact Signing certificate lasts days; unstamped, the signature would die with it.
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)},
                             signature=("Valid", False, SUBJECT))
        assert result.returncode != 0
        assert said(result, "no timestamp") and signing.recorded() == []

    def test_a_signature_by_another_certificate_fails(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)},
                             signature=("Valid", True, "CN=Someone Else"))
        assert result.returncode != 0
        assert said(result, 'signed by "CN=Someone Else", not by WINDOWS_SIGN_EXPECTED_SUBJECT')
        assert signing.recorded() == []

    def test_a_signer_needs_the_expected_subject(self, signing):
        settings = {**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)}
        del settings["WINDOWS_SIGN_EXPECTED_SUBJECT"]
        result = signing.run(settings)
        assert result.returncode != 0
        assert said(result, "WINDOWS_SIGN_EXPECTED_SUBJECT isn't") and signing.calls() == []

    def test_a_signer_that_signs_nothing_fails(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "FAKE_SIGNTOOL_SIGNS_NOTHING": "1"})
        assert result.returncode != 0
        assert said(result, f"The signature on {signing.target} is NotSigned")
        assert len(signing.calls()) == 1 and signing.recorded() == []

    def test_a_signer_that_signs_nothing_fails_with_the_real_cmdlet(self, signing):
        program = signing.unsigned_program()
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)}, files=[program],
                             real=True)
        assert result.returncode != 0
        assert said(result, f"The signature on {program} is NotSigned")
        assert len(signing.calls()) == 1

    def test_an_already_signed_file_is_refused_before_anything_signs(self, signing):
        signing.target.write_bytes(b"MZ" + MARK.encode("ascii"))
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)})
        assert result.returncode != 0
        assert said(result, "already carries a signature")
        assert signing.calls() == [] and signing.recorded() == []

    def test_a_failing_signtool_fails(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "FAKE_SIGNTOOL_EXIT": "1"})
        assert result.returncode != 0
        assert said(result, "signtool (Azure Artifact Signing) failed")

    def test_signing_required_still_fails_a_failed_signing(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib),
                              "FAKE_SIGNTOOL_EXIT": "1", "WINDOWS_SIGNING_REQUIRED": "true"})
        assert result.returncode != 0

    def test_without_the_azure_sign_in_nothing_runs(self, signing):
        result = signing.run({**AZURE, "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT,
                              "ARTIFACT_SIGNING_DLIB": str(signing.dlib)})
        assert result.returncode != 0
        assert said(result, "no Azure sign-in")
        assert signing.calls() == []

    def test_an_account_configured_in_part_is_refused(self, signing):
        result = signing.run({"ARTIFACT_SIGNING_ENDPOINT": AZURE["ARTIFACT_SIGNING_ENDPOINT"],
                              "ARTIFACT_SIGNING_SIGNED_IN": "true", "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT})
        assert result.returncode != 0
        assert said(result, "ARTIFACT_SIGNING_ACCOUNT, ARTIFACT_SIGNING_PROFILE")
        assert signing.calls() == []

    def test_a_sign_in_without_an_account_is_refused(self, signing):
        result = signing.run({"ARTIFACT_SIGNING_SIGNED_IN": "true"})
        assert result.returncode != 0
        assert said(result, "sign-in") and signing.calls() == []

    def test_two_signers_are_refused(self, signing):
        result = signing.run({**AZURE_SIGNED_IN, "WINDOWS_SIGN_COMMAND": "echo {file}"})
        assert result.returncode != 0
        assert said(result, "More than one signer")
        assert signing.calls() == []


class TestTheOtherSigners:
    def test_a_pfx_signs_with_its_timestamp_server_and_the_file_is_removed(self, signing):
        certificate = b"not really a certificate"
        result = signing.run({"WINDOWS_SIGN_PFX_BASE64": base64.b64encode(certificate).decode(),
                              "WINDOWS_SIGN_PFX_PASSWORD": "pw", "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT})
        assert result.returncode == 0, output(result)
        [call] = signing.calls()
        args = call["args"]
        assert args[args.index("/tr") + 1] == "http://timestamp.digicert.com" and "/dlib" not in args
        assert bytes.fromhex(call["pfx"]) == certificate
        assert list(signing.runner.iterdir()) == []

    def test_a_command_runs_as_written_for_each_file(self, signing, tmp_path):
        command = f"{tmp_path / 'signtool.cmd'} sign {{file}}"
        result = signing.run({"WINDOWS_SIGN_COMMAND": command, "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT})
        assert result.returncode == 0, output(result)
        assert [call["args"] for call in signing.calls()] == [["sign", str(signing.target)]]

    @pytest.mark.parametrize("signed_before", [False, True])
    def test_a_command_that_exits_0_without_signing_fails(self, signing, signed_before):
        # On an unsigned file the signature check after it fails; on one signed
        # already, the check before it, so the old signature can't pass for a new one.
        if signed_before:
            signing.target.write_bytes(b"MZ" + MARK.encode("ascii"))
        result = signing.run({"WINDOWS_SIGN_COMMAND": "rem {file}", "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT})
        assert result.returncode != 0
        expected = "already carries a signature" if signed_before else "is NotSigned"
        assert said(result, expected) and signing.recorded() == []


class TestInCI:
    """GitHub Actions sets GITHUB_ACTIONS=true: only the pinned tools sign there."""

    def test_a_signtool_of_ones_own_is_refused(self, signing):
        result = signing.run({"GITHUB_ACTIONS": "true", **AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)})
        assert result.returncode != 0
        assert said(result, "WINDOWS_SIGNTOOL is set, but in GitHub Actions only the pinned tools sign")
        assert signing.calls() == [] and signing.target.read_bytes() == b"MZ"

    def test_a_dlib_of_ones_own_is_refused(self, signing):
        result = signing.run({"GITHUB_ACTIONS": "true", **AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)},
                             fake_signtool=False)
        assert result.returncode != 0
        assert said(result, "ARTIFACT_SIGNING_DLIB is set, but in GitHub Actions")

    def test_a_signtool_microsoft_didnt_sign_is_refused(self, signing, tmp_path):
        # Whatever answers to signtool.exe must be Microsoft's (the real cmdlet checks it).
        impostor = tmp_path / "impostor"
        impostor.mkdir()
        shutil.copyfile(UNSIGNED_PROGRAM, impostor / "signtool.exe")
        program = signing.unsigned_program()
        result = signing.run({"GITHUB_ACTIONS": "true", "WINDOWS_SIGN_PFX_BASE64": base64.b64encode(b"x").decode(),
                              "WINDOWS_SIGN_PFX_PASSWORD": "pw", "WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT,
                              "PATH": f"{impostor};{os.environ['PATH']}"},
                             files=[program], real=True, fake_signtool=False)
        assert result.returncode != 0
        assert said(result, f"signtool.exe at {impostor / 'signtool.exe'} isn't validly signed by Microsoft")
        assert sha256(program) == sha256(UNSIGNED_PROGRAM) and list(signing.runner.iterdir()) == []


def _kits_signtool() -> bool:
    kits = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits" / "10" / "bin"
    return any(kits.glob("*/x64/signtool.exe"))


@pytest.mark.skipif(not _kits_signtool(), reason="needs the Windows SDK's signtool")
def test_check_tools_finds_microsofts_signtool(signing):
    result = signing.run({"ARTIFACT_SIGNING_DLIB": str(signing.dlib)}, call={"CheckTools": True}, real=True,
                         fake_signtool=False)
    assert result.returncode == 0, output(result)
    assert "\\x64\\signtool.exe" in result.stdout and f"Artifact Signing dlib: {signing.dlib}" in result.stdout


class TestReleaseFiles:
    """check_release_files.ps1: only what the release job signed is published, unchanged."""

    @staticmethod
    def dist(signing, version: str, *, msi: bool) -> Path:
        dist = signing.tmp_path / "dist"
        (dist / "lumi").mkdir(parents=True)
        (dist / "lumi" / "lumi.exe").write_bytes(b"MZ")
        (dist / f"lumi-{version}-sbom.cdx.json").write_text("{}", encoding="utf-8")
        (dist / f"lumi-{version}-THIRD_PARTY_NOTICES.txt").write_text("Notices", encoding="utf-8")
        (dist / "installer").mkdir()
        (dist / "installer" / f"lumi-setup-{version}.exe").write_bytes(b"MZ setup")
        if msi:
            (dist / "installer" / f"lumi-{version}.msi").write_bytes(b"MSI")
        return dist

    @staticmethod
    def sign(signing, *files: Path, settings: dict[str, str] | None = None) -> None:
        signed = signing.run(settings if settings is not None else {
            **AZURE_SIGNED_IN, "ARTIFACT_SIGNING_DLIB": str(signing.dlib)}, files=list(files))
        assert signed.returncode == 0, output(signed)

    @staticmethod
    def check(signing, version: str, dist: Path, settings: dict[str, str | None] | None = None, *,
              handover: bool = False):
        call = {"Version": version, "Dist": str(dist), **({"Handover": True} if handover else {})}
        return signing.run({"WINDOWS_SIGN_EXPECTED_SUBJECT": SUBJECT, **(settings or {})}, script=CHECK, call=call)

    def test_a_stable_release_publishes_the_signed_installer_and_msi(self, signing):
        dist = self.dist(signing, "1.2.3", msi=True)
        installer, msi = dist / "installer" / "lumi-setup-1.2.3.exe", dist / "installer" / "lumi-1.2.3.msi"
        self.sign(signing, installer, msi)
        result = self.check(signing, "1.2.3", dist)
        assert result.returncode == 0, output(result)
        base = str(dist).replace("\\", "/")
        values = signing.outputs()
        assert values["files"].splitlines() == [f"{base}/installer/lumi-setup-1.2.3.exe", f"{base}/installer/lumi-1.2.3.msi",
                                                f"{base}/lumi-1.2.3-sbom.cdx.json",
                                                f"{base}/lumi-1.2.3-THIRD_PARTY_NOTICES.txt"]
        assert values["msi"] == f"{base}/installer/lumi-1.2.3.msi"

    def test_a_beta_publishes_its_installer_and_no_msi(self, signing):
        dist = self.dist(signing, "1.2.3-beta.1", msi=False)
        self.sign(signing, dist / "installer" / "lumi-setup-1.2.3-beta.1.exe")
        result = self.check(signing, "1.2.3-beta.1", dist)
        assert result.returncode == 0, output(result)
        values = signing.outputs()
        assert [line.rsplit("/", 1)[1] for line in values["files"].splitlines()] == [
            "lumi-setup-1.2.3-beta.1.exe", "lumi-1.2.3-beta.1-sbom.cdx.json", "lumi-1.2.3-beta.1-THIRD_PARTY_NOTICES.txt"]
        assert values["msi"] == ""

    def test_an_msi_in_a_beta_stops_the_release(self, signing):
        # Say the build's artifact brought one: it was never signed, and a beta has none.
        dist = self.dist(signing, "1.2.3-beta.1", msi=True)
        self.sign(signing, dist / "installer" / "lumi-setup-1.2.3-beta.1.exe")
        result = self.check(signing, "1.2.3-beta.1", dist)
        assert result.returncode != 0
        assert said(result, "A beta gets no MSI") and "files" not in signing.outputs()

    def test_a_file_changed_after_signing_stops_the_release(self, signing):
        dist = self.dist(signing, "1.2.3-beta.1", msi=False)
        installer = dist / "installer" / "lumi-setup-1.2.3-beta.1.exe"
        self.sign(signing, installer)
        installer.write_bytes(installer.read_bytes() + b"!")
        result = self.check(signing, "1.2.3-beta.1", dist)
        assert result.returncode != 0
        assert said(result, "changed after it was signed") and "files" not in signing.outputs()

    def test_a_file_this_job_didnt_sign_stops_the_release(self, signing):
        dist = self.dist(signing, "1.2.3", msi=True)
        self.sign(signing, dist / "installer" / "lumi-setup-1.2.3.exe")  # not the MSI
        result = self.check(signing, "1.2.3", dist)
        assert result.returncode != 0
        assert said(result, "lumi-1.2.3.msi wasn't signed by sign_windows.ps1 in this job")

    def test_a_signature_by_another_certificate_stops_the_release(self, signing):
        dist = self.dist(signing, "1.2.3-beta.1", msi=False)
        self.sign(signing, dist / "installer" / "lumi-setup-1.2.3-beta.1.exe")
        result = self.check(signing, "1.2.3-beta.1", dist, {"WINDOWS_SIGN_EXPECTED_SUBJECT": "CN=Someone Else"})
        assert result.returncode != 0
        assert said(result, "not by WINDOWS_SIGN_EXPECTED_SUBJECT")

    def test_unsigned_files_are_published_only_while_signing_isnt_required(self, signing):
        dist = self.dist(signing, "1.2.3-beta.1", msi=False)
        self.sign(signing, dist / "installer" / "lumi-setup-1.2.3-beta.1.exe", settings={})  # no signer
        passed = self.check(signing, "1.2.3-beta.1", dist, {"WINDOWS_SIGN_EXPECTED_SUBJECT": None})
        assert passed.returncode == 0, output(passed)
        assert "is unsigned, as recorded" in passed.stdout
        result = self.check(signing, "1.2.3-beta.1", dist, {"WINDOWS_SIGN_EXPECTED_SUBJECT": None,
                                                            "WINDOWS_SIGNING_REQUIRED": "true"})
        assert result.returncode != 0
        assert said(result, "WINDOWS_SIGNING_REQUIRED is true")

    def test_anything_else_in_dist_stops_the_release(self, signing):
        dist = self.dist(signing, "1.2.3-beta.1", msi=False)
        self.sign(signing, dist / "installer" / "lumi-setup-1.2.3-beta.1.exe")
        (dist / "lumi-1.2.3.msi").write_bytes(b"MSI")
        result = self.check(signing, "1.2.3-beta.1", dist)
        assert result.returncode != 0
        assert said(result, "lumi-1.2.3.msi") and said(result, "expected exactly")

    def test_the_build_hands_over_only_the_bundle_sbom_and_notices(self, signing):
        dist = self.dist(signing, "1.2.3", msi=True)
        result = self.check(signing, "1.2.3", dist, handover=True)
        assert result.returncode != 0
        assert said(result, "The build job hands over the bundle, the SBOM and the notices, and nothing else")
        shutil.rmtree(dist / "installer")
        result = self.check(signing, "1.2.3", dist, handover=True)
        assert result.returncode == 0, output(result)


def test_the_signing_client_is_used_only_as_pinned(tmp_path):
    # packaging/fetch_artifact_signing.ps1 checks the package before extracting anything.
    package = tmp_path / "microsoft.artifactsigning.client.nupkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("bin/x64/Azure.CodeSigning.Dlib.dll", b"not Microsoft's")
    destination = tmp_path / "client"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(ROOT / "packaging" / "fetch_artifact_signing.ps1"), "-Destination", str(destination),
                             "-PackagePath", str(package)], capture_output=True, text=True, timeout=120,
                            env=clean_environment())
    assert result.returncode != 0
    assert said(result, "SHA-256 mismatch")
    assert not (destination / "bin").exists() and list(destination.iterdir()) == []


def test_wix_is_installed_only_as_pinned(tmp_path):
    # packaging/fetch_wix.ps1 checks the package before `dotnet tool install` sees it.
    package = tmp_path / "wix.5.0.2.nupkg"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("tools/net6.0/any/wix.dll", b"not WiX")
    destination = tmp_path / "wix"
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(ROOT / "packaging" / "fetch_wix.ps1"), "-Destination", str(destination),
                             "-PackagePath", str(package)], capture_output=True, text=True, timeout=120,
                            env=clean_environment())
    assert result.returncode != 0
    assert said(result, "WiX 5.0.2 SHA-256 mismatch") and said(result, "nothing was installed")
    assert [path.name for path in destination.iterdir()] == ["source"]
    assert list((destination / "source").iterdir()) == []
