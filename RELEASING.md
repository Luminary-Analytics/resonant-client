# Releasing Lumi

The release is complete when the tagged source, the published Windows
installer and macOS disk image, and the public signed update feeds agree. A
successful push alone is not deployment. See
[pipeline architecture](docs/release-pipeline.md) for component ownership.

## Prepare the release

1. Inspect the working tree and intended changes. Keep unrelated local changes
   out of the release. Verify the GitHub account has write access to
   `Luminary-Analytics/resonant-client` before pushing.
2. Update both `lumi/__init__.py` and `pyproject.toml` to the chosen
   version. Add `docs/vX.Y.Z-release-notes.md`, update the docs index, and move
   shipped entries out of `docs/unreleased.md`. Do not relabel unshipped work as
   part of an existing release.
3. Run the checks below. Investigate failures rather than weakening gates.
4. Build and smoke-test the final source. Record what was actually exercised,
   especially whether provider generation was live or mocked.

```sh
python -m pip install -e ".[all,dev]"
python -m ruff check .
python -m pytest -q
node --check lumi/gui/static/app.js
node --check lumi/gui/static/settings_view.js
node --test tests/ui_recovery.test.cjs tests/appearance.test.cjs
git diff --check
```

On Windows, run `./scripts/build_clean.ps1`. It builds in a fresh environment
from the hash-pinned `packaging/requirements-release.txt`, fetches verified
ripgrep/web assets, writes the third-party notices (failing on unreviewed
copyleft licenses), runs PyInstaller, and enforces the bundle policy. Add
`-SbomPath dist/lumi-sbom.cdx.json` for the CycloneDX SBOM; that needs the
pinned tools (`python -m pip install --require-hashes -r
packaging/tools-requirements.txt`) in the Python running the script. Do not
build from an arbitrary environment with accumulated packages.

When dependencies in `pyproject.toml` change, run `python scripts/lock_release.py`
(needs [uv](https://docs.astral.sh/uv/)), review the lock diff, and commit it.
The tests fail while the lock misses a declared dependency, and the weekly
**Dependency audit** workflow reports newly published vulnerabilities in pinned
versions.

For UI/provider changes, test the source UI's affected flows and the packaged
`dist/lumi/lumi.exe`. Use an isolated profile and fixture project; do
not kill a user's running app or delete their startup logs. Launch any test
process hidden, keep its PID, and stop only that owned process afterward.
Verify:

- `--version` reports the intended version.
- The GUI serves HTTP 200 and accepts an actual WebSocket connection.
- Changed commands/assets are present; drafts and navigation work.
- Bundled skills install and startup logs have no unexplained errors.
- Relevant desktop/compact layouts and keyboard controls remain usable.

## Commit, push, and publish

Stage the intended source, tests, and documentation. Commit them with the
version change. Create an annotated `vX.Y.Z` tag at that commit and push the
branch and tag together where supported:

```sh
git tag -a vX.Y.Z -m "Lumi X.Y.Z"
git push --atomic origin main vX.Y.Z
```

Replace `X.Y.Z` with the chosen version. Use a fresh version for new source;
do not move or reuse a published tag.

The tag triggers `.github/workflows/release.yml`. It checks the package version,
runs lint/tests, builds the bundle and Inno Setup installer, signs the installer
for WinSparkle and creates a GitHub Release. For stable `X.Y.Z` tags it then
copies the installer to the Pages site under `downloads/vX.Y.Z/`, keeps the
newest three installers, points the new appcast entry at that copy, and
publishes `gh-pages` as one fresh commit. GitHub Pages then deploys it.

Installed apps download from Pages, not from the GitHub Release, because the
source repository may be private and its Release assets then need sign-in.
A pre-release tag reaches only the beta feeds, so installs on the stable
channel never update to one.

The same tag builds the macOS release on an Apple silicon runner (jobs `macos`
and `publish-macos`): Lumi.app with Sparkle, `lumi-X.Y.Z.dmg` and
`lumi-X.Y.Z.pkg` from `packaging/build_macos.sh`, signed with the Developer
ID, notarized and stapled when the Apple secrets below exist, otherwise signed
ad hoc with a warning on the run, the release and the download page. After
the Windows job has published, `publish-macos` signs the DMG with the same
EdDSA key (and checks the signature with the key in `lumi/updater.py`), adds
the DMG, PKG, macOS SBOM and notices to the GitHub Release, copies the DMG
(and for stable tags the PKG) into the same `downloads/vX.Y.Z/` folder on
Pages, adds the DMG to the macOS feeds (`appcast-macos*.xml`) and pushes
gh-pages again as one fresh commit. The Windows feeds aren't touched. A failed
macOS job doesn't hold the Windows release back.

Inspect the release, Tests, and Build check runs for the exact commit SHA.
Identify the release run ID before watching it; another workflow may be newer.

```sh
gh run list --repo Luminary-Analytics/resonant-client --workflow release.yml
gh run view RUN_ID --repo Luminary-Analytics/resonant-client
gh release view vX.Y.Z --repo Luminary-Analytics/resonant-client
```

CI generates release notes automatically. Replace them with the reviewed
versioned notes using `gh release edit ... --notes-file <path>` when appropriate;
use a file to preserve literal text and newlines.

## Verify deployment

- The release workflow and relevant checks succeeded for the tagged commit.
- The published, non-draft release includes `lumi-setup-X.Y.Z.exe` in
  uploaded state with a nonzero size.
- The Pages deployment succeeded, and the live feed at
  [appcast.xml](https://luminary-analytics.github.io/resonant-client/appcast.xml)
  has the intended version as its first item.
- Its enclosure points to `downloads/vX.Y.Z/lumi-setup-X.Y.Z.exe` on the
  Pages site. Download it without signing in and confirm the byte length and a
  nonempty EdDSA signature. A local `gh-pages` commit alone is insufficient.
- The [download page](https://luminary-analytics.github.io/resonant-client/)
  links to the new installer.
- macOS: the release also has `lumi-X.Y.Z.dmg` and `lumi-X.Y.Z.pkg`, and
  [appcast-macos.xml](https://luminary-analytics.github.io/resonant-client/appcast-macos.xml)
  (or `appcast-macos-beta.xml` for a beta) has the version first, with
  `sparkle:os="macos"` and an enclosure at `downloads/vX.Y.Z/lumi-X.Y.Z.dmg`
  whose length and EdDSA signature match the file you download from there.
  `appcast.xml` has no macOS item. The download page offers the disk image,
  and says how to open it when the run warned that it isn't notarized.
- The working tree and pushed branch state match the intended result.

Existing installations discover the release on their next update check;
publication does not prove every installed client has updated.

## Signing and infrastructure

The workflow needs repository contents write access, the `EDDSA_PRIVATE_KEY`
secret, and Pages configured for `gh-pages` at the root. If the repository is
private, Pages must still publish publicly; that needs a paid GitHub plan such as
Team. On the free plan, making the repository private unpublishes the site and
stops every installed app from updating. The public verification
key is embedded in `lumi/updater.py`; the private key stays outside
source control. WinSparkle tools are under `packaging/winsparkle/`.

EdDSA validates the installer bytes against the update feed. It is separate
from Windows Authenticode publisher signing; do not describe an update-feed
signature as a SmartScreen-trusted publisher certificate.

Authenticode signing of `lumi.exe` and the installer runs through
`packaging/sign_windows.ps1` when credentials are configured, and otherwise
leaves a warning on the run; see
[pipeline architecture](docs/release-pipeline.md#authenticode). Buying a
certificate or a signing service is an account decision for the owner.

The same `EDDSA_PRIVATE_KEY` signs the macOS disk image; Lumi.app's
`SUPublicEDKey` is the public key from `lumi/updater.py`, so rotating the key
changes both platforms at once.

## macOS signing and notarization

Without the secrets below, releases still publish the macOS build, signed ad
hoc: it runs, updates itself (Sparkle trusts the EdDSA signature), and macOS
asks each person to approve it once in System Settings › Privacy & Security ›
**Open Anyway** ([Lumi on macOS](docs/macos.md#opening-a-build-that-isnt-notarized)).
To sign and notarize it, the owner needs an Apple account. Buying the
membership is an account decision for the owner.

1. **Apple Developer Program membership**, $99 a year. Enroll Luminary
   Analytics as an organization at
   [developer.apple.com/programs](https://developer.apple.com/programs/enroll/),
   so the apps say "Luminary Analytics", not a person's name. An organization
   needs a D-U-N-S number (free from Dun & Bradstreet; Apple's enrollment
   page can look it up or request one, which takes up to a few weeks), a
   website on the company's domain, and someone with the authority to sign
   Apple's agreements. The Account Holder does the next steps.
2. **Developer ID certificates.** In Certificates, Identifiers & Profiles (or
   Xcode › Settings › Accounts › Manage Certificates), create a **Developer ID
   Application** certificate, which signs the app and the DMG, and a
   **Developer ID Installer** certificate, which signs the PKG. Both need a
   certificate signing request from Keychain Access on a Mac. Export both,
   with their private keys, from Keychain Access as one `.p12` file with a
   password. They're valid for five years; keep a backup outside GitHub.
3. **Notarization credentials** for `notarytool`, either:
   - an **App Store Connect API key** (preferred: no personal Apple ID, and
     it can be revoked on its own). In App Store Connect › Users and Access ›
     Integrations › App Store Connect API › Team Keys, create a key with the
     Developer role and download the `AuthKey_XXXXXXXXXX.p8` file (it can be
     downloaded only once). Note its key ID and the issuer ID shown on that
     page; or
   - an **Apple ID** in the team with an **app-specific password** (created
     at [account.apple.com](https://account.apple.com) › Sign-In and
     Security › App-Specific Passwords), and the team ID shown under
     Membership details.
4. **Repository secrets** (as LA-Rich, or anyone with admin rights):

   ```sh
   base64 -i lumi-developer-id.p12 | gh secret set MACOS_SIGN_P12_BASE64 --repo Luminary-Analytics/resonant-client
   gh secret set MACOS_SIGN_P12_PASSWORD --repo Luminary-Analytics/resonant-client
   gh secret set MACOS_SIGN_IDENTITY --repo Luminary-Analytics/resonant-client --body "Developer ID Application: Luminary Analytics (TEAMID)"
   gh secret set MACOS_INSTALLER_IDENTITY --repo Luminary-Analytics/resonant-client --body "Developer ID Installer: Luminary Analytics (TEAMID)"
   # an API key...
   base64 -i AuthKey_XXXXXXXXXX.p8 | gh secret set APPLE_API_KEY_BASE64 --repo Luminary-Analytics/resonant-client
   gh secret set APPLE_API_KEY_ID --repo Luminary-Analytics/resonant-client --body "XXXXXXXXXX"
   gh secret set APPLE_API_ISSUER_ID --repo Luminary-Analytics/resonant-client --body "<issuer UUID>"
   # ...or an Apple ID
   gh secret set APPLE_ID --repo Luminary-Analytics/resonant-client --body "releases@example.com"
   gh secret set APPLE_TEAM_ID --repo Luminary-Analytics/resonant-client --body "TEAMID"
   gh secret set APPLE_APP_PASSWORD --repo Luminary-Analytics/resonant-client
   ```

   `packaging/build_macos.sh` reads the same names locally from the
   environment. The build-check workflow for macOS uses them too, so a pull
   request signs and notarizes its build once they exist.

With them, the release job signs with the hardened runtime, notarizes the DMG
and the PKG and staples their tickets, and the warning goes away. The first
signed release changes the app's signature from ad hoc to the Developer ID;
Sparkle accepts that change because the EdDSA signature still verifies.
Keychain items saved by an ad-hoc build may ask once for permission after
that update.

## Failures and recovery

- **Account mismatch / 403:** check `gh auth status`. Git HTTPS credentials can
  select a different account from `gh`; use the intended account and configured
  credential helper without printing tokens.
- **Missing bundled asset:** check tracked files, the spec, fetch scripts, and
  bundle policy. A source checkout can work while the frozen app cannot.
- **Frozen startup:** inspect imports, `sys.stdout`/`stderr` assumptions, resource
  paths, and WebSocket dependencies. Test the actual executable.
- **Failed CI test:** inspect failed logs. A rerun is appropriate for a diagnosed
  environment-dependent failure, not a substitute for fixing a product defect.
  The v0.17.0 separate test job exposed a low-PID assertion in
  `TestKillGuardrails.test_refuses_self`; both the product guard and release
  tests succeeded, and the separate job passed on rerun.
- **Appcast mismatch after retry:** rebuilt installers may differ byte-for-byte.
  The asset, size, and signature must be updated together. Never publish a stale
  signature or rerun an already successful release casually.
- **Version/source correction:** prefer a new version for changed source. Rerun
  failed jobs at the existing SHA only when the source and version are correct.
- **Betas:** tag `vX.Y.Z-beta.N` (or `-alpha.N`, `-rc.N`) with the same string
  in both version files. The GitHub release is marked prerelease; the installer
  goes to the Pages site and only into `appcast-beta.xml`, so installs on the
  stable channel never see it. The download page is left alone. Other
  hyphenated or Python `a1`-style tags fail the Pages step, which accepts only
  `X.Y.Z` and those three forms, instead of reaching installed apps. See
  [Updates](docs/updates.md#how-the-feeds-work). A beta's DMG goes only into
  `appcast-macos-beta.xml`, and a beta gets its PKG only on the GitHub Release.
- **macOS job failed:** the Windows release is already out. Fix the cause
  (for a signing or notarization failure, the log shows `notarytool`'s
  verdict and its log) and rerun the failed jobs. `publish-macos` signs
  whatever DMG the `macos` job built in the same run, so the release asset,
  the Pages copy and the feed's length and signature stay together; rerun
  both jobs rather than editing any of them by hand. Adding the macOS files
  to the published release needs the repository's immutable releases off,
  as they are now.
- **Sparkle's pin:** `packaging/fetch_sparkle.sh` holds Sparkle's version and
  SHA-256 (take it from the release asset's digest on GitHub, not from a
  download alone), and `packaging/third-party-components.json` its version
  for the notices; `tests/test_release_supply_chain.py` checks they agree.

Current release evidence is recorded in [0.19.1 notes](docs/v0.19.1-release-notes.md).

## Lumi rebrand and upgrades

The product is Lumi: `lumi.exe`, `lumi-setup-X.Y.Z.exe`, `Program Files\Lumi`
and the `lumi` distribution. The installer has its own AppId and silently runs
the pre-rebrand SONN Client/Resonant uninstaller first (per-machine and
per-user), so Apps & Features keeps one entry. WinSparkle preferences move to
`Software\Luminary Analytics\Lumi\WinSparkle` and start fresh.

The appcast URL deliberately stays at the current Pages address: installed
SONN Client builds poll it, so the first Lumi releases are published there.
Moving the feed to a Lumi domain needs a bridge release whose binary points
at the new URL. Rename the repository only after that; GitHub does not
redirect Pages project sites after a rename.

Before publishing the first Lumi release, upgrade an installed SONN Client
from the new installer and check that the old Apps & Features entry is gone,
Lumi launches from the Start menu, and `~/.resonant` moved to `~/.lumi` with
sessions and settings intact. Regenerate icons with
`python scripts/build_brand_assets.py` (see [brand/README.md](brand/README.md)).
