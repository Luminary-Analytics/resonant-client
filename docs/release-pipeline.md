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
  bootloader, ripgrep, WinSparkle, web assets and fonts; Sparkle in the macOS
  build), and code ported into Lumi's own modules with its original license
  (pi-coding-agent's `truncate.ts`, ported as `lumi/engine/truncation.py`; the
  text is in `packaging/licenses/`).
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

- `WINDOWS_SIGN_PFX_BASE64` and `WINDOWS_SIGN_PFX_PASSWORD` secrets (in the
  [release environment](#the-release-environment)): a
  code-signing certificate exported as PFX, signed with signtool and an RFC 3161
  timestamp (`WINDOWS_SIGN_TIMESTAMP_URL` overrides the DigiCert default).
- A `WINDOWS_SIGN_COMMAND` repository variable: a command with `{file}` for a
  cloud or hardware signer set up by an earlier workflow step. Certificates
  issued since June 2023 keep their keys in hardware, so a new certificate
  normally takes this form:
  - Azure Trusted Signing through signtool's `/dlib`;
  - DigiCert KeyLocker;
  - SSL.com eSigner.

Without either, the release continues unsigned and the run shows a warning,
until the repository variable `WINDOWS_SIGNING_REQUIRED` is `true`: set it once
a certificate exists, and a lost secret fails the release instead of shipping
unsigned files.

## macOS

The same tag starts two more jobs. `macos` (Apple silicon) runs
`packaging/build_macos.sh`: the pinned build as on Windows, then Sparkle 2
(`packaging/fetch_sparkle.sh`: a pinned release checked against its SHA-256
before extraction) copied into `Lumi.app/Contents/Frameworks`, the DMG and the
PKG. With the Apple secrets ([RELEASING.md](../RELEASING.md#macos-signing-and-notarization))
the app is signed with the Developer ID and the hardened runtime (Sparkle's
helpers without Python's entitlements), and the DMG and PKG are notarized and
stapled; otherwise the app is signed ad hoc and the run, the release notes and
the download page say it isn't notarized. With the repository variable
`MACOS_SIGNING_REQUIRED` set to `true` (once the Developer ID exists), missing
Apple secrets fail the build instead. `publish-macos` waits for the Windows
job, then runs `packaging/publish_macos.ps1`: it EdDSA-signs the final DMG with
`winsparkle-tool` and the same key, checks that signature against
`lumi/updater.py`'s key, publishes the DMG to Pages and adds it to the macOS
feeds, and signs each macOS feed it wrote the same way (Sparkle's signed feeds,
below). The job then adds the files to the GitHub Release. `build-macos.yml`
rehearses that script on every packaging change with a throwaway key on a
scratch copy of `gh-pages`, pushing nothing. See [Lumi on macOS](macos.md).

The macOS feeds are signed because Lumi.app sets `SURequireSignedFeed`: Sparkle
reads a feed only when the signing block at its end (`packaging/feed_signature.py`,
the format of Sparkle's own `sign_update`) verifies with the app's key, so no
one who can change a feed, but not sign it, can point Macs at another download
or change what a release says. A feed that fails is an update error (Sparkle's
code 1000). After 20 days of failures Sparkle falls back to its safe mode for
key rotation: it reads the feed but ignores its release notes and critical or
informational items, and every download still needs its EdDSA signature (or
the Developer ID, below). The Windows feeds stay as they are: WinSparkle
doesn't read a feed signature, and installed copies poll `appcast.xml`
unchanged.

## The release environment

Every job that can sign an update runs in the `release` environment: the
Windows `release` job (the EdDSA key and the Authenticode secrets), `macos`
(the Apple secrets) and `publish-macos` (the EdDSA key). Workflows that
pull requests start (`build-check.yml`, `build-macos.yml`, `tests.yml`) get no
signing secret: a pull request runs the workflow files from its own branch,
so anything they are given could be read out. Their macOS builds are signed ad
hoc, and the publishing rehearsal uses a key it makes and throws away.

The Apple certificate needs the same care as the EdDSA key. Sparkle accepts an
update signed with the same team's Developer ID when its EdDSA signature
doesn't verify: `SUVerifyUpdateBeforeExtraction` allows that before
extraction, and the check after extraction accepts the code signature alone.
Either key can therefore ship an update to every Mac, and
`SUVerifyUpdateBeforeExtraction` doesn't change that; keeping both where only
tagged releases reach them does.

The environment exists once a job names it, but it protects nothing until
the owner configures it (Settings › Environments › `release`):

1. **Deployment branches and tags:** *Selected branches and tags*, with one
   tag rule, `v*`. Only runs for a version tag can then enter it.
2. **Environment secrets:** move these from the repository secrets (add each
   to the environment, then delete the repository copy): `EDDSA_PRIVATE_KEY`;
   `MACOS_SIGN_IDENTITY`, `MACOS_SIGN_P12_BASE64`, `MACOS_SIGN_P12_PASSWORD`,
   `MACOS_INSTALLER_IDENTITY`; `APPLE_API_KEY_BASE64`, `APPLE_API_KEY_ID`,
   `APPLE_API_ISSUER_ID` or `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD`;
   `WINDOWS_SIGN_PFX_BASE64`, `WINDOWS_SIGN_PFX_PASSWORD`. The variables
   (`WINDOWS_SIGN_COMMAND`, `MACOS_SIGNING_REQUIRED`,
   `WINDOWS_SIGNING_REQUIRED`) can stay repository variables.
3. Optionally, **required reviewers**, so a person approves each release run.

All of this is free for a public repository. A private one needs GitHub Team
or Enterprise for environments, their secrets and the tag rule, and
Enterprise for required reviewers; make the repository private only on such a
plan, or the release jobs lose the protection above. Keep the name `release`:
the AWS release role trusts GitHub's OIDC subject
`repo:Luminary-Analytics/resonant-client:environment:release`, so the same
environment and tag rule gate it too.

The workflow is hardened in the same spirit:

- The workflow's token only reads; the two jobs that publish get
  `contents: write`, and the macOS build doesn't. Checkouts that never push
  keep no credentials (`persist-credentials: false`).
- The EdDSA key is written to a file only while `winsparkle-tool` signs, and
  deleted in a `finally` before any third-party action runs.
- Third-party actions are pinned to a commit, with the version in a comment.
- The two jobs that push `gh-pages` share the concurrency group
  `release-gh-pages` (never cancelling a running one), and push with
  `--force-with-lease` on the commit they checked out, so neither can
  overwrite what the other published. GitHub keeps one waiting job per group:
  a third arrival (two tags pushed together) cancels the waiting one, which
  then needs a rerun; the lease still keeps anything from being overwritten.

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

For stable tags the workflow also builds `lumi-X.Y.Z.msi` with WiX 5
(`packaging/build_msi.ps1`, `packaging/lumi.wxs`), Authenticode-signs it like
the EXE, attaches it to the release and copies it beside the installer on
Pages, where the download page offers it to administrators. Betas get no MSI;
an MSI version is three numbers. The build-check workflow builds the MSI on
every change and installs, checks and removes it on the runner. See
[Deploying on Windows](deploy-windows.md).

`packaging/update_appcast.py` then adds the release to the update feeds, with
byte length, notes and signature, pointing at that Pages copy:

- a stable tag goes into `appcast.xml`, and the beta feed and release-line
  feeds are rebuilt from it;
- a beta tag (`vX.Y.Z-beta.N`) goes into `appcast-beta.xml` only;
- the macOS disk image goes into the macOS feeds in the same way
  (`appcast-macos.xml`, `-beta`, `-X.Y`), never into the Windows ones.

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

The pipeline publishes the Windows installer and, from the first release with
these jobs, the macOS disk image and package. Release-note prose needs
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
| `packaging/installer.iss` | Windows installer (EXE) |
| `packaging/lumi.wxs`, `packaging/build_msi.ps1` | MSI for device management |
| `packaging/update_appcast.py` | Stable, beta and release-line update feeds, for Windows and macOS |
| `lumi/updater.py`, `lumi/update_channels.py` | WinSparkle client and verification key; update mode, channel, pin and platform |
| `packaging/build_macos.sh`, `packaging/fetch_sparkle.sh` | macOS app, DMG and PKG; pinned Sparkle; Apple signing and notarization |
| `packaging/publish_macos.ps1`, `packaging/feed_signature.py` | Signing the DMG and the macOS feeds, checked with the app's key, and laying them out on Pages |
| `lumi/sparkle.py` | Sparkle 2 on macOS through PyObjC: the delegate and the main-thread hand-off |
| `.github/workflows/build-macos.yml`, `packaging/smoke_gui.py` | macOS build and smoke test on every change, the updater included; a publishing rehearsal with a throwaway key |

Paths are relative to the repository root. See the
[documentation index](README.md) for release records and current guides.
