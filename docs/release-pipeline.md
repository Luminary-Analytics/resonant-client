# Release pipeline architecture

The operational procedure is [RELEASING.md](../RELEASING.md). This document
reflects the workflow checked through v0.17.2; measured sizes and durations are observations,
not permanent thresholds or guarantees.

## Source to installer

`.github/workflows/tests.yml` checks pushed/PR source; `build-check.yml` checks
Windows packaging. A `v*.*.*` tag starts `release.yml` on Windows runners. Its
`build` job checks the tag against `__version__`, installs test dependencies,
runs Ruff and pytest, then invokes `scripts/build_clean.ps1`, and hands the
bundle to the `release` job, which signs, packages and publishes it. `build`
has no secret, no OIDC token and no environment: the test run installs
packages from PyPI, and nothing it runs may reach a signing identity.

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

The `release` job signs three files with Authenticode, Windows' publisher
signature (SmartScreen, company policies): `lumi.exe` before Inno Setup
packages it, the MSI, and the installer before its EdDSA signature is
computed, so the update feed signs the final bytes. Each goes through
`.github/actions/authenticode-sign`, and `packaging/sign_windows.ps1` decides
how to sign and checks the result: every signature must be `Valid`
(`Get-AuthenticodeSignature`) and timestamped. One signer at a time:

- **Azure Artifact Signing** (formerly Trusted Signing), the one Lumi means to
  use. The variables `ARTIFACT_SIGNING_ENDPOINT`, `ARTIFACT_SIGNING_ACCOUNT`
  and `ARTIFACT_SIGNING_PROFILE` name the account and certificate profile, and
  `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` and `AZURE_SUBSCRIPTION_ID` the identity
  that signs. No secret exists anywhere: before each file, the action's
  `azure/login` step exchanges the job's short-lived GitHub OIDC token for an
  Azure sign-in (logged out when the job ends). signtool then signs through
  Microsoft's dlib, which may use only that sign-in
  (`packaging/fetch_artifact_signing.ps1`: the NuGet package
  `Microsoft.ArtifactSigning.Client` 1.0.128, checked against its SHA-256
  before anything is extracted), and timestamps with Microsoft's server
  (`http://timestamp.acs.microsoft.com`). The profile's certificates last
  about three days, so the timestamp is what keeps a signature valid.
  [Setting it up](#azure-artifact-signing) takes the owner's steps below.
- `WINDOWS_SIGN_PFX_BASE64` and `WINDOWS_SIGN_PFX_PASSWORD` secrets (in the
  [release environment](#the-release-environment)): a
  code-signing certificate exported as PFX, signed with signtool and an RFC 3161
  timestamp (`WINDOWS_SIGN_TIMESTAMP_URL` overrides the DigiCert default).
- A `WINDOWS_SIGN_COMMAND` variable: a command with `{file}` for another
  cloud or hardware signer set up by an earlier workflow step, such as
  DigiCert KeyLocker or SSL.com eSigner.

With none configured the release behaves as before: it continues unsigned
and the run shows a warning, until the variable `WINDOWS_SIGNING_REQUIRED` is
`true`; set it once signing works, and losing the signer fails the release
instead of shipping unsigned files. A signer configured in part, two signers
at once, an Artifact Signing account without the Azure sign-in (or a sign-in
without an account), and a signature that fails or has no timestamp always
fail the release.

`build-check.yml`'s `signing-dry-run` job runs the same action as a pull
request can, with no identity, token or secret: the Azure sign-in is skipped
and the log says why, the file is left as it was, and an account without a
sign-in, or required signing with no signer, fails. It also checks the
signing client's pinned hash against the package NuGet serves.
`tests/test_sign_windows.py` runs `sign_windows.ps1` with a stand-in signtool
for every signer, in Windows PowerShell and PowerShell 7.

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
below). A feed already on the site whose signature doesn't verify stops the
publish ([Repairing the macOS feeds](#repairing-the-macos-feeds)). The job
checks the site byte for byte, adds the files to the GitHub Release, and
pushes the site ([Publishing gh-pages byte for
byte](#publishing-gh-pages-byte-for-byte)). See [Lumi on macOS](macos.md).

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

One key signs every macOS feed, and Sparkle checks only a feed's own bytes,
so someone who can change the Pages site could serve the beta feed, or a
newer line's, at the stable or a pinned copy's address without breaking a
signature. Lumi.app therefore checks the version Sparkle found against its
channel and pin too (`SparkleUpdater.may_proceed`, `update_channels.refusal_for`):
the stable channel takes only stable releases, a pin only its line's, and a
refusal is an `update.refused` record. Two limits remain:

- **An old feed, replayed.** A feed signed for an earlier release still
  verifies, and Sparkle's signed feeds have no expiry, so serving it again
  keeps Macs on the version they have (it can't install anything older).
  Nothing in Sparkle tells a replayed feed from a quiet week; Lumi doesn't
  try, so as not to lock out real updates.
- **Redirects.** Offline mode checks each download's address as Sparkle
  starts it; Sparkle follows a redirect from an allowed host without asking,
  and exposes no hook for it. The disk image's signature is checked all the
  same, so a redirect can't change what's installed. GitHub Pages doesn't
  redirect the downloads it serves.

## The release environment

Every job that can sign an update runs in the `release` environment: the
Windows `release` job (the EdDSA key, and Authenticode's Azure identity or
secrets), `macos` (the Apple secrets) and `publish-macos` (the EdDSA key). Workflows that
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
   `WINDOWS_SIGNING_REQUIRED`) can stay repository variables. Azure Artifact
   Signing needs no secret; its six variables belong to the environment too
   ([below](#azure-artifact-signing)).
3. **Required reviewers:** the owner, so a person approves each release run.
   Optional until an OIDC identity trusts the environment; required from then
   on (below).

All of this is free for a public repository. A private one needs GitHub Team
or Enterprise for environments, their secrets and the tag rule, and
Enterprise for required reviewers; make the repository private only on such a
plan, or the release jobs lose the protection above. Keep the name `release`:
Azure Artifact Signing's identity trusts GitHub's OIDC subject
`repo:Luminary-Analytics/resonant-client:environment:release` (and the AWS
release role is to trust the same one), so this environment's rules gate
them too. Put the `v*` tag rule and the required reviewer in place before any
credential trusts that subject: while the environment is unprotected, a job
from any branch a collaborator pushes can name it, get a token with that
subject, and sign any file as Luminary Analytics. Only `release.yml`'s
`release` job may ask for a token (`permissions: id-token: write`), never the
whole workflow and never the `build` job, which runs the tests and the
packages they install from PyPI; `tests/test_release_supply_chain.py` fails if
any other job, workflow or action asks.

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

## Azure Artifact Signing

The certificate profile signs as Luminary Analytics for whoever holds a GitHub
OIDC token with the trusted subject, so the order matters. With the Artifact
Signing account, its identity validation and a Public Trust certificate
profile created:

1. **Protect the `release` environment first** (Settings › Environments ›
   `release`): *Deployment branches and tags* › *Selected branches and tags*
   with the one tag rule `v*`, and **Required reviewers** with the owner.
   Do this before step 3: until then any job that names the environment gets
   a token the credential would trust.
2. **Create the app registration** (Microsoft Entra ID › App registrations ›
   New registration, e.g. `lumi-release-signing`, single tenant, no redirect
   URI), and its service principal. Note the Application (client) ID and the
   Directory (tenant) ID. Add no client secret or certificate: nothing needs
   one. From the CLI:
   `az ad app create --display-name lumi-release-signing`, then
   `az ad sp create --id <client id>`.
3. **Add a federated credential** to it (Certificates & secrets › Federated
   credentials › Add credential › *GitHub Actions deploying Azure resources*:
   organization `Luminary-Analytics`, repository `resonant-client`, entity
   type *Environment*, environment `release`). That's issuer
   `https://token.actions.githubusercontent.com`, subject
   `repo:Luminary-Analytics/resonant-client:environment:release` and
   audience `api://AzureADTokenExchange`. From the CLI:

   ```sh
   az ad app federated-credential create --id <client id> --parameters '{"name": "lumi-release", "issuer": "https://token.actions.githubusercontent.com", "subject": "repo:Luminary-Analytics/resonant-client:environment:release", "audiences": ["api://AzureADTokenExchange"]}'
   ```
4. **Assign the "Artifact Signing Certificate Profile Signer" role** to that
   service principal on the certificate profile alone, the narrowest scope:

   ```sh
   az role assignment create --assignee <client id> --role "Artifact Signing Certificate Profile Signer" --scope "/subscriptions/<subscription id>/resourceGroups/<resource group>/providers/Microsoft.CodeSigning/codeSigningAccounts/<account name>/certificateProfiles/<profile name>"
   ```
5. **Fill in the variables** of the `release` environment (Environment
   variables, not secrets: none of them is one):

   | Variable | Value |
   |---|---|
   | `ARTIFACT_SIGNING_ENDPOINT` | The account's regional endpoint, as its Overview shows it, e.g. `https://eus.codesigning.azure.net` |
   | `ARTIFACT_SIGNING_ACCOUNT` | The Artifact Signing account's name |
   | `ARTIFACT_SIGNING_PROFILE` | The certificate profile's name |
   | `AZURE_CLIENT_ID` | The app registration's Application (client) ID |
   | `AZURE_TENANT_ID` | The Directory (tenant) ID |
   | `AZURE_SUBSCRIPTION_ID` | The subscription the signing account is in |

   For example
   `gh variable set ARTIFACT_SIGNING_ENDPOINT --env release --body "https://eus.codesigning.azure.net" --repo Luminary-Analytics/resonant-client`.
6. **Release a beta**, and check the installer and MSI (Properties › Digital
   Signatures: Luminary Analytics, with a timestamp from Microsoft). Then set
   `WINDOWS_SIGNING_REQUIRED` to `true`.

If the sign-in fails because the identity sees no subscription (its only
role is on the certificate profile), leave `AZURE_SUBSCRIPTION_ID` empty: the
action then signs in to the tenant alone, which is all signing needs. The
`azure/login` step prints the subject it presented, for comparing with the
credential's.

**If the subject changes.** Should resonant-client be opted into GitHub's
immutable OIDC subjects, or renamed, the subject becomes
`repo:Luminary-Analytics@105687258/resonant-client@1182612108:environment:release`
(the organization's and the repository's IDs). Change the federated
credential's subject at the same time, or signing stops: nothing else trusts
the old one.

## Publishing gh-pages byte for byte

Sparkle reads a macOS feed only when its signature verifies over the bytes
Pages serves, and Pages serves what the gh-pages commit holds. Git for
Windows, where both publishing jobs run, is installed with
`core.autocrlf=true`: it turns `"\n"` into `"\r\n"` when it checks files out
and back when it commits them, so a feed signed as written on the runner
wasn't what the branch held (a review of this pipeline found every signed
feed broken that way, before any was published). Now:

- `packaging/update_appcast.py` writes the feeds, and `publish_pages.py` the
  page and `macos.json`, with `"\n"` on every platform.
- Both jobs run `git config --global core.autocrlf false` before they check
  out gh-pages (after the source checkout, which is unaffected), and the
  site carries a `.gitattributes` with `* -text` (`publish_pages.py` writes
  it), so every later checkout gets the committed bytes whatever Git's
  settings.
- `packaging/push_pages.py` stages the site and then reads the staged blobs,
  never the working copy: `.gitattributes` must say `* -text`, every macOS
  feed must verify with the app's key, and every disk image a macOS feed
  lists must have the length and signature the feed gives it. Only then does
  it commit the index as one fresh commit and push it with the lease.
  `publish-macos` runs the same check (`--check`) before it adds anything to
  the GitHub Release; `--check --rev gh-pages` checks a pushed commit.
- `build-macos.yml` rehearses all of it on `windows-latest` for two releases
  in a row (`scripts/rehearse_pages_publish.py`): the release's Git setting,
  both jobs' scripts with a throwaway key, a push to a copy of the branch on
  the runner, and a check of each pushed commit. It also shows that a feed
  changed after signing is refused, and that a checkout with Git's own
  defaults gets every file byte for byte. Nothing is pushed to GitHub.
  Locally: `python scripts/rehearse_pages_publish.py --pages <a gh-pages
  checkout> --isolate-git-config` (Windows, with PowerShell).

## Repairing the macOS feeds

A macOS feed whose signing block doesn't verify, after a publish went wrong,
stops the next publish on purpose: signing it again is a person's decision. Macs meanwhile get update error 1000 (and Sparkle's safe
mode after 20 days). To repair it you need the EdDSA private key, which
GitHub never shows, so this uses its backup, on Windows (winsparkle-tool is
a Windows program):

1. Check out gh-pages byte for byte:
   `git -c core.autocrlf=false clone --branch gh-pages https://github.com/Luminary-Analytics/resonant-client pages`.
2. See which feeds fail: `pwsh packaging/publish_macos.ps1 -Site pages -CheckFeeds -PublicKey <EDDSA_PUBLIC_KEY>`
   (the key in `lumi/updater.py`).
3. Sign those again: `pwsh packaging/publish_macos.ps1 -Site pages -ResignFeeds -PublicKey <key> -PrivateKeyFile <key file>`.
   It checks each feed first, signs again only those that don't verify, and
   checks them all afterwards. Delete the key file when it's done.
4. Publish: `python packaging/push_pages.py pages --message "Sign the macOS feeds again" --tool packaging/winsparkle/WinSparkle-0.9.2/bin/winsparkle-tool.exe`
   (as LA-Rich). It commits nothing unless the staged feeds and disk images
   verify, and its lease refuses the push if a release published meanwhile.
5. Confirm what Pages serves: `python packaging/feed_signature.py verify <downloaded appcast-macos.xml> <key>`
   for each feed.

A disk image that doesn't match its feed can't be repaired this way: publish
a new release instead. `scripts/rehearse_pages_publish.py` runs these steps
on a branch it breaks the way Git for Windows once did.

Rotating the key is not covered. One key signs the Windows installers, the
disk images and the macOS feeds, and every installed copy trusts only that
key (Sparkle and WinSparkle each have their own way to move to a new one).
`-ResignFeeds` signs the feeds with whatever key it's given, but
`push_pages.py` checks feeds and disk images against one key, so a rotation
needs a plan of its own first.

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
| `.github/actions/authenticode-sign/action.yml`, `packaging/sign_windows.ps1` | Authenticode signing: the Azure sign-in (OIDC), the signer's choice and each signature's check |
| `packaging/fetch_artifact_signing.ps1` | Microsoft's Artifact Signing client (the signtool dlib), pinned by SHA-256 |
| `packaging/check_bundle.py`, `packaging/bundle-policy.json` | Bundle contents and size gate |
| `packaging/installer.iss` | Windows installer (EXE) |
| `packaging/lumi.wxs`, `packaging/build_msi.ps1` | MSI for device management |
| `packaging/update_appcast.py` | Stable, beta and release-line update feeds, for Windows and macOS |
| `lumi/updater.py`, `lumi/update_channels.py` | WinSparkle client and verification key; update mode, channel, pin and platform |
| `packaging/build_macos.sh`, `packaging/fetch_sparkle.sh` | macOS app, DMG and PKG; pinned Sparkle; Apple signing and notarization |
| `packaging/publish_macos.ps1`, `packaging/feed_signature.py` | Signing the DMG and the macOS feeds, checked with the app's key, and laying them out on Pages; checking or re-signing the feeds |
| `packaging/push_pages.py`, `scripts/rehearse_pages_publish.py` | Publishing gh-pages from the staged blobs, byte for byte and verified; its rehearsal on Windows |
| `lumi/sparkle.py` | Sparkle 2 on macOS through PyObjC: the delegate and the main-thread hand-off |
| `.github/workflows/build-macos.yml`, `packaging/smoke_gui.py` | macOS build and smoke test on every change, the updater included; a publishing rehearsal with a throwaway key |

Paths are relative to the repository root. See the
[documentation index](README.md) for release records and current guides.
