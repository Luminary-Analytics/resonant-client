# Release pipeline architecture

The operational procedure is [RELEASING.md](../RELEASING.md). This document
reflects the workflow checked through v0.17.2; measured sizes and durations are observations,
not permanent thresholds or guarantees.

## Source to installer

`.github/workflows/tests.yml` checks pushed/PR source; `build-check.yml` checks
Windows packaging. A `v*.*.*` tag starts `release.yml` on a Windows runner.
The release workflow checks the tag against `__version__`, installs test
dependencies, runs Ruff and pytest, then invokes `scripts/build_clean.ps1`.

The clean build creates a temporary virtual environment, installs
`packaging/requirements-release.txt` with `--require-hashes` and then the local
package without resolving anything again. Fetch scripts verify pinned ripgrep
and frontend assets. `lumi.spec` selects code/data, and `check_bundle.py`
enforces `bundle-policy.json`, producing a manifest.
The v0.17.0 local bundle was 55.5 MiB across 256 files.

CI smoke-checks the executable's version, then Inno Setup wraps the bundle into
`lumi-setup-X.Y.Z.exe`. The published v0.17.0 installer was 26,113,856 bytes.
Interactive HTTP/WebSocket/UI checks remain part of the local release runbook;
the release workflow's executable smoke test alone does not perform them.

## Supply chain

- **Pinned dependencies.** `scripts/lock_release.py` runs `uv pip compile` for
  every platform and Python version Lumi supports, with hashes. It writes
  `packaging/requirements-release.txt` (the app's dependencies plus PyInstaller)
  and `packaging/tools-requirements.txt` (pip-audit and cyclonedx-bom for CI).
  `tests/test_release_supply_chain.py` fails when `pyproject.toml` declares a
  dependency the release lock does not pin within its range.
- **Vulnerability audit.** `.github/workflows/dependency-audit.yml` runs
  `packaging/audit_locks.py` on lock changes and weekly. The script removes the
  platform markers first, because pip-audit otherwise skips every package that
  doesn't apply to the runner's own OS.
- **Third-party notices.** `packaging/third_party_notices.py` writes
  `THIRD_PARTY_NOTICES.txt` from the build environment's metadata and the
  license texts each package ships. It adds the non-Python parts listed in
  `packaging/third-party-components.json` (Python runtime, PyInstaller
  bootloader, ripgrep, WinSparkle, web assets and fonts).
  - The build fails if a shipped Python package is GPL, AGPL or LGPL without a
    recorded `license_reviews` entry.
  - Packages under `not_shipped` are excluded from the bundle by the spec and
    left out of the notices. Today these are PyAutoGUI's optional GPL-3.0
    helpers MouseInfo and PyMsgBox, which Lumi never calls.
  - The notices ship at `_internal/licenses/THIRD_PARTY_NOTICES.txt`, which the
    bundle policy requires.
- **SBOM.** With `-SbomPath`, `build_clean.ps1` runs `cyclonedx-py environment`
  against the build environment, adds the bundled non-Python components, and
  validates the result against the CycloneDX 1.6 schema. The build check uploads
  the SBOM and notices on every packaging change. Releases attach them as
  `lumi-X.Y.Z-sbom.cdx.json` and `lumi-X.Y.Z-THIRD_PARTY_NOTICES.txt`.

## Authenticode

`packaging/sign_windows.ps1` signs `lumi.exe` before Inno Setup packages it, and
the installer before its EdDSA signature is computed, so the update feed signs
the final bytes. It verifies each signature afterwards. It uses one of:

- `WINDOWS_SIGN_PFX_BASE64` and `WINDOWS_SIGN_PFX_PASSWORD` repository secrets: a
  code-signing certificate exported as PFX, signed with signtool and an RFC 3161
  timestamp (`WINDOWS_SIGN_TIMESTAMP_URL` overrides the DigiCert default).
- A `WINDOWS_SIGN_COMMAND` repository variable: a command with `{file}` for a
  cloud or hardware signer set up by an earlier workflow step. Certificates
  issued since June 2023 keep their keys in hardware, so a new certificate
  normally takes this form:
  - Azure Trusted Signing through signtool's `/dlib`;
  - DigiCert KeyLocker;
  - SSL.com eSigner.

Without either, the release continues unsigned and the run shows a warning.
macOS notarization is not wired: there is no macOS build pipeline yet.

## Signing and publication

WinSparkle's signing tool uses the repository `EDDSA_PRIVATE_KEY` secret to
produce an EdDSA signature of the installer. The matching public key lives in
`lumi/updater.py`. The private key must never enter source control,
logs, documentation, or fixtures.

The workflow publishes the installer as a GitHub Release asset. Then
`packaging/publish_pages.py` copies it to `gh-pages/downloads/vX.Y.Z/`. It keeps
the newest three installers, the newest installer of each of the four newest
release lines (for installs pinned to one), and betas newer than the newest
stable release. A stable tag also regenerates the download page.

`packaging/update_appcast.py` then adds the release to the update feeds, with
byte length, notes and signature, pointing at that Pages copy:

- a stable tag goes into `appcast.xml`, and the beta feed and release-line
  feeds are rebuilt from it;
- a beta tag (`vX.Y.Z-beta.N`) goes into `appcast-beta.xml` only.

[Updates](updates.md#how-the-feeds-work) describes the feeds and how installs
choose one. The branch is pushed as one fresh commit so old installers do not
accumulate in its history; `appcast.xml` itself keeps the version history. The Pages copy exists because
the source repository may be private, which puts Release assets behind sign-in.
The live feed URL is:

[Lumi update feed](https://luminary-analytics.github.io/resonant-client/appcast.xml).

The WinSparkle client checks that feed and verifies downloaded installer bytes.
EdDSA update signing is separate from Authenticode publisher signing. Rebuilding
an installer can change its bytes; a rerun must keep the asset and appcast
signature/length synchronized.

## Deployment evidence

A release is ready when the exact tagged commit passed its checks, the installer
is uploaded to a published release, the Pages deployment succeeded, and the
public feed serves the matching version and signature with a Pages-hosted
installer of the matching length that downloads without signing in. Neither
pushing a tag nor committing an appcast proves the public feed is current.

The pipeline currently publishes Windows installers. Release-note prose needs
review; CI's generated notes are not a replacement for describing behavior and
limitations. Provider authentication and real inference are not exercised by
ordinary CI. Keep mocked wire-contract tests distinct from live model evidence.

## Component map

| File | Responsibility |
| --- | --- |
| `.github/workflows/release.yml` | Test, build, sign, release, publish Pages site |
| `packaging/publish_pages.py` | Pages-hosted installers and download page |
| `.github/workflows/tests.yml` | Source correctness checks |
| `.github/workflows/build-check.yml` | Packaging checks without publication |
| `scripts/build_clean.ps1` | Isolated Windows build and cleanup |
| `packaging/fetch_ripgrep.ps1`, `packaging/fetch_web_assets.ps1` | Verified build assets |
| `packaging/lumi.spec` | PyInstaller code/data selection |
| `scripts/lock_release.py`, `packaging/*requirements*` | Hash-pinned release and CI-tool dependencies |
| `packaging/audit_locks.py`, `.github/workflows/dependency-audit.yml` | Vulnerability audit of every pin |
| `packaging/third_party_notices.py`, `packaging/third-party-components.json` | Notices, license gate, SBOM additions |
| `packaging/sign_windows.ps1` | Authenticode signing when configured |
| `packaging/check_bundle.py`, `packaging/bundle-policy.json` | Bundle contents and size gate |
| `packaging/installer.iss` | Windows installer |
| `packaging/update_appcast.py` | Stable, beta and release-line update feeds |
| `lumi/updater.py`, `lumi/update_channels.py` | WinSparkle client and verification key; update mode, channel and pin |

Paths are relative to the repository root. See the
[documentation index](README.md) for release records and current guides.
