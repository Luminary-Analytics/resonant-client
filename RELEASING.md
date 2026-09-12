# Releasing Resonant

The release is complete when the tagged source, published Windows installer,
and public signed update feed agree. A successful push alone is not deployment.
See [pipeline architecture](docs/release-pipeline.md) for component ownership.

## Prepare the release

1. Inspect the working tree and intended changes. Keep unrelated local changes
   out of the release. Verify the GitHub account has write access to
   `Luminary-Analytics/resonant-client` before pushing.
2. Update both `resonant_client/__init__.py` and `pyproject.toml` to the chosen
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
node --check resonant_client/gui/static/app.js
node --check resonant_client/gui/static/settings_view.js
node --test tests/ui_recovery.test.cjs
git diff --check
```

On Windows, run `./scripts/build_clean.ps1`. It builds in a fresh environment,
fetches verified ripgrep/web assets, runs PyInstaller, and enforces the bundle
policy. Do not build from an arbitrary environment with accumulated packages.

For UI/provider changes, test the source UI's affected flows and the packaged
`dist/resonant/resonant.exe`. Use an isolated profile and fixture project; do
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
git tag -a vX.Y.Z -m "Resonant X.Y.Z"
git push --atomic origin main vX.Y.Z
```

Replace `X.Y.Z` with the chosen version. Use a fresh version for new source;
do not move or reuse a published tag.

The tag triggers `.github/workflows/release.yml`. It checks the package version,
runs lint/tests, builds the bundle and Inno Setup installer, signs the installer
for WinSparkle, creates a GitHub Release, and commits its appcast entry to
`gh-pages`. GitHub Pages then publishes that branch in a separate deployment.

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
- The published, non-draft release includes `resonant-setup-X.Y.Z.exe` in
  uploaded state with a nonzero size.
- The Pages deployment succeeded, and the live feed at
  [appcast.xml](https://luminary-analytics.github.io/resonant-client/appcast.xml)
  has the intended version as its first item.
- Its enclosure points to that release asset, with matching byte length and a
  nonempty EdDSA signature. A local `gh-pages` commit alone is insufficient.
- The working tree and pushed branch state match the intended result.

Existing installations discover the release on their next update check;
publication does not prove every installed client has updated.

## Signing and infrastructure

The workflow needs repository contents write access, the `EDDSA_PRIVATE_KEY`
secret, and Pages configured for `gh-pages` at the root. The public verification
key is embedded in `resonant_client/updater.py`; the private key stays outside
source control. WinSparkle tools are under `packaging/winsparkle/`.

EdDSA validates the installer bytes against the update feed. It is separate
from Windows Authenticode publisher signing; do not describe an update-feed
signature as a SmartScreen-trusted publisher certificate.

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
- **Pre-releases:** the tag glob accepts more than stable semver, but the current
  workflow marks prerelease status using a hyphen check. Python `a1` tags are
  not automatically marked prerelease; verify GitHub/appcast behavior before
  using a prerelease channel.

Current release evidence is recorded in [0.18.2 notes](docs/v0.18.2-release-notes.md).
