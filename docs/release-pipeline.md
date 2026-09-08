# Release pipeline architecture

The operational procedure is [RELEASING.md](../RELEASING.md). This document
reflects the v0.17.0 workflow; measured sizes and durations are observations,
not permanent thresholds or guarantees.

## Source to installer

`.github/workflows/tests.yml` checks pushed/PR source; `build-check.yml` checks
Windows packaging. A `v*.*.*` tag starts `release.yml` on a Windows runner.
The release workflow checks the tag against `__version__`, installs test
dependencies, runs Ruff and pytest, then invokes `scripts/build_clean.ps1`.

The clean build creates a temporary virtual environment and installs the local
package with GUI/desktop dependencies and PyInstaller. Fetch scripts verify
pinned ripgrep and frontend assets. `resonant.spec` selects code/data, and
`check_bundle.py` enforces `bundle-policy.json`, producing a manifest.
The v0.17.0 local bundle was 55.5 MiB across 256 files.

CI smoke-checks the executable's version, then Inno Setup wraps the bundle into
`resonant-setup-X.Y.Z.exe`. The published v0.17.0 installer was 26,113,856 bytes.
Interactive HTTP/WebSocket/UI checks remain part of the local release runbook;
the release workflow's executable smoke test alone does not perform them.

## Signing and publication

WinSparkle's signing tool uses the repository `EDDSA_PRIVATE_KEY` secret to
produce an EdDSA signature of the installer. The matching public key lives in
`resonant_client/updater.py`. The private key must never enter source control,
logs, documentation, or fixtures.

The workflow publishes the installer as a GitHub Release asset, then runs
`packaging/update_appcast.py` against `gh-pages/appcast.xml`. Each entry includes
version, URL, byte length, release notes, and signature. GitHub Pages publishes
the branch separately. The live feed URL is:

[Resonant update feed](https://luminary-analytics.github.io/resonant-client/appcast.xml).

The WinSparkle client checks that feed and verifies downloaded installer bytes.
EdDSA update signing is separate from Authenticode publisher signing. Rebuilding
an installer can change its bytes; a rerun must keep the asset and appcast
signature/length synchronized.

## Deployment evidence

A release is ready when the exact tagged commit passed its checks, the installer
is uploaded to a published release, the Pages deployment succeeded, and the
public feed serves the matching version, asset length, and signature. Neither
pushing a tag nor committing an appcast proves the public feed is current.

The pipeline currently publishes Windows installers. Release-note prose needs
review; CI's generated notes are not a replacement for describing behavior and
limitations. Provider authentication and real inference are not exercised by
ordinary CI. Keep mocked wire-contract tests distinct from live model evidence.

## Component map

| File | Responsibility |
| --- | --- |
| `.github/workflows/release.yml` | Test, build, sign, release, update appcast |
| `.github/workflows/tests.yml` | Source correctness checks |
| `.github/workflows/build-check.yml` | Packaging checks without publication |
| `scripts/build_clean.ps1` | Isolated Windows build and cleanup |
| `packaging/fetch_ripgrep.ps1`, `packaging/fetch_web_assets.ps1` | Verified build assets |
| `packaging/resonant.spec` | PyInstaller code/data selection |
| `packaging/check_bundle.py`, `packaging/bundle-policy.json` | Bundle contents and size gate |
| `packaging/installer.iss` | Windows installer |
| `packaging/update_appcast.py` | Versioned update-feed entries |
| `resonant_client/updater.py` | WinSparkle client and verification key |

Paths are relative to the repository root. See the
[documentation index](README.md) for release records and current guides.
