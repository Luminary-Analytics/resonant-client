# Releasing Resonant Client

Operational runbook for shipping a new release. Designed so any future contributor (or LLM session) can ship `v0.X.Y+1` in under 10 minutes once the prerequisites are met.

> **TL;DR** — Bump `__version__`, commit it, push the tag, watch CI. The auto-update channel handles the rest.

---

## What you get when you cut a release

A single `git push origin vX.Y.Z` triggers a fully automated pipeline that produces:

1. **Signed Windows installer** at `https://github.com/Luminary-Analytics/resonant-client/releases/download/vX.Y.Z/resonant-setup-X.Y.Z.exe` (~26 MB)
2. **GitHub Release page** with auto-generated release notes
3. **Updated `appcast.xml`** on the `gh-pages` branch
4. **Silent auto-update** — every machine running v0.2.0+ silently picks up the new version on next launch (or within 24 hours via background poll)

The whole thing is signed end-to-end with EdDSA so the update channel can't be hijacked.

---

## Prerequisites (one-time)

These should already be set up. If a prerequisite is missing, see [Setup from scratch](#setup-from-scratch) below.

| What | Where it lives | Verify |
|------|---------------|--------|
| EdDSA private key | `~/.resonant/keys/eddsa_priv.key` (your dev machine, **outside the repo**) | `ls ~/.resonant/keys/eddsa_priv.key` should show 44 bytes |
| `EDDSA_PRIVATE_KEY` repo secret | GitHub: Luminary-Analytics/resonant-client → Settings → Secrets → Actions | `gh secret list --repo Luminary-Analytics/resonant-client` should show it |
| Public key in code | `resonant_client/updater.py:EDDSA_PUBLIC_KEY` | `HgNb0s7xavpa1bFyX/8B24AnuUdgekpvgO6HQU+zv8k=` |
| `gh-pages` branch | `https://luminary-analytics.github.io/resonant-client/appcast.xml` | `curl -I` should return 200 |
| Workflow file | `.github/workflows/release.yml` on `main` | Visible in repo |
| GitHub Pages enabled | Repo → Settings → Pages → "gh-pages" / `(root)` | "Your site is published at..." banner |

---

## How to cut a release

### 1. Bump version

Update `resonant_client/__init__.py`:

```python
__version__ = "0.2.1"   # was 0.2.0
```

The CI workflow has a sanity check that compares the tag against this value and fails the run if they don't match. Both `pyproject.toml` and `__init__.py` track version, but the CI sanity check reads only `__init__.py` — keep them in sync to avoid drift.

### 2. Commit + push to main

```bash
git add resonant_client/__init__.py
git commit -m "chore: bump version to 0.2.1"
git push origin main
```

### 3. Tag + push the tag

```bash
git tag v0.2.1
git push origin v0.2.1
```

The tag MUST match `v[0-9]+.[0-9]+.[0-9]+` for the workflow's `on.push.tags` filter (`v*.*.*`) to fire.

### 4. Watch the run

```bash
gh run watch --repo Luminary-Analytics/resonant-client
```

Or open https://github.com/Luminary-Analytics/resonant-client/actions to watch in the browser. Expected runtime: **3-5 minutes** end-to-end.

### 5. Verify the release

After the run completes successfully:

```bash
# Release exists with installer attached
gh release view v0.2.1 --repo Luminary-Analytics/resonant-client

# Appcast updated with new entry
curl -s https://luminary-analytics.github.io/resonant-client/appcast.xml | grep -A2 "0.2.1"
```

### 6. (Optional) Smoke-test the installer

Download the installer from the Releases page and run it on a fresh VM or your machine. The installer:
- Installs to `%LOCALAPPDATA%\Programs\Resonant Client\`
- Adds Start Menu entry "Resonant Client"
- Auto-launches the app on completion (which opens a browser to `localhost:8909`)

---

## Pre-release checklist

Before cutting any non-trivial release, run this checklist:

- [ ] `python -m pytest` is green locally
- [ ] `pyinstaller packaging/resonant.spec --clean --noconfirm` builds successfully on your dev machine
- [ ] **Bundled-exe smoke test** (the v0.2.4–v0.2.7 lesson — see [Frozen-only bug class](#frozen-only-bug-class) below):
  - Kill any running `resonant.exe`: `Stop-Process -Name resonant -Force -ErrorAction SilentlyContinue`
  - Delete log: `Remove-Item ~/.resonant/logs/resonant-startup.log -Force`
  - Launch bundled exe: `dist/resonant/resonant.exe gui --port 8950`
  - Wait ~10s for the server to bind
  - HTTP probe: `curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8950/` → expect **200**
  - WebSocket probe: `curl --http1.1 -H "Connection: Upgrade" -H "Upgrade: websocket" -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Origin: http://127.0.0.1:8950" http://127.0.0.1:8950/ws -m 5 -o /dev/null -w "%{http_code}\n"` → expect **101**
  - Log inspection: `cat ~/.resonant/logs/resonant-startup.log` → expect **empty** (no warnings, no tracebacks)
  - All four checks must pass. If ANY fails, the tag push will produce a broken release.
- [ ] All files referenced by `packaging/resonant.spec` are tracked in git (the missing-`plan_graph_view.js` bug was a real CI failure — see [Bug #12](docs/known-issues.md#12))
- [ ] No new unicode characters introduced into `.github/workflows/release.yml` via web UI paste (CodeMirror's atob path drops UTF-8; see [Lessons learned](#lessons-learned-from-v020) below)
- [ ] `__version__` in `resonant_client/__init__.py` matches the intended tag

---

## Common failure modes & fixes

### "version X.Y.Z already in appcast — bump and retry"

Cause: `packaging/update_appcast.py` refuses duplicate version entries to prevent accidental double-publishes. This fires when CI tries to update an appcast that already has the version (e.g., from a prior failed run, or from a manual placeholder).

Fix: edit `packaging/update_appcast.py` to UPDATE the existing entry instead of refusing. Currently the duplicate guard is at the `for existing in channel.findall("item"):` block — change `sys.exit(1)` to `channel.remove(existing)` so the new entry replaces the old one.

This is tracked as a follow-up fix; the script as-shipped is intentionally conservative.

### "Tag version (X.Y.Z) does not match resonant_client/__init__.py __version__ (A.B.C)"

Cause: you tagged before bumping, or vice versa.

Fix:

```bash
git tag -d vX.Y.Z                   # delete locally
git push --delete origin vX.Y.Z     # delete remote
# bump __version__ to X.Y.Z properly, commit, push
git tag vX.Y.Z <new-commit-sha>
git push origin vX.Y.Z
```

### CI fails at "Build PyInstaller bundle" — `Unable to find <some_file>`

Cause: a file referenced by `packaging/resonant.spec` is in your local filesystem but not tracked in git. Local builds work because the file exists on disk; CI's clean checkout fails because it doesn't.

Fix:

```bash
git status                          # see if the file is "Untracked"
git add <missing-file>
git commit -m "fix: track <missing-file>"
git push origin main
# Then retag at the new commit:
git push --delete origin vX.Y.Z
git tag -d vX.Y.Z
git tag vX.Y.Z $(git rev-parse origin/main)
git push origin vX.Y.Z
```

### CI fails at "Sign installer with EdDSA" — `EDDSA_PRIVATE_KEY repo secret is not set`

Cause: secret is missing from the repo.

Fix (regenerate keypair if needed; see [Setup from scratch](#setup-from-scratch)):

```bash
cat ~/.resonant/keys/eddsa_priv.key | gh secret set EDDSA_PRIVATE_KEY --repo Luminary-Analytics/resonant-client
gh secret list --repo Luminary-Analytics/resonant-client   # verify
```

Then re-run the failed job from the Actions UI (no need to retag — the secret read happens at job time, not at tag-push time).

### CI fails at "Update appcast.xml on gh-pages"

Most common cause: duplicate-version guard (see above). Less common: `gh-pages-checkout` directory missing or appcast.xml malformed. Check the run log for the specific Python error.

### Pushing the workflow file (`.github/workflows/release.yml`) fails with 403

Cause: your `gh` token (or git credential) doesn't have the `workflow` OAuth scope. GitHub requires that scope for any push that touches `.github/workflows/*.yml`, even creates.

Three workarounds:

1. **Edit via GitHub web UI** instead of `git push` — the web UI uses session cookies, not the OAuth scope. Navigate to `https://github.com/Luminary-Analytics/resonant-client/edit/main/.github/workflows/release.yml`, paste content, commit.
2. **Refresh the gh token with workflow scope:** `gh auth refresh -h github.com -s workflow` and complete the device-code flow.
3. **Switch to a token that has it:** `gh auth switch -u <username-with-workflow-scope>` then push.

---

## Setup from scratch

If you're standing up a *new* repo or rotating compromised keys, do all of this. None of these steps are needed for normal `vX.Y.Z` releases.

### Generate EdDSA keypair

```powershell
# Generate the private key (keep outside the repo)
& packaging/winsparkle/WinSparkle-0.9.2/bin/winsparkle-tool.exe generate-key --file ~/.resonant/keys/eddsa_priv.key
# This prints the public key. Update resonant_client/updater.py:EDDSA_PUBLIC_KEY with it.
```

### Set the repo secret

```bash
cat ~/.resonant/keys/eddsa_priv.key | gh secret set EDDSA_PRIVATE_KEY --repo <owner>/<repo>
```

### Create gh-pages branch

```bash
git worktree add --orphan -b gh-pages ../resonant-client-pages
cd ../resonant-client-pages
# Copy templates from existing gh-pages branch (appcast.xml + index.html + .nojekyll)
git add .
git commit -m "Initial gh-pages: appcast.xml + landing page"
git push -u origin gh-pages
```

### Enable GitHub Pages

GitHub auto-enables Pages for any branch named `gh-pages`. Verify at Repo → Settings → Pages.

### Add the workflow file

If pushing fails with 403 on the workflow scope, use the web UI path described above.

### Verify

```bash
# 1. Pages site live
curl -I https://<owner>.github.io/<repo>/appcast.xml      # should be 200

# 2. Cut a test release with prerelease semver
python -c "import resonant_client; print(resonant_client.__version__)"   # check version
git tag v0.2.0-rc1 && git push origin v0.2.0-rc1
gh run watch --repo <owner>/<repo>
```

---

## Frozen-only bug class

Bugs that ONLY surface in the PyInstaller-bundled exe — never in `python -m resonant_client` dev mode. The v0.2.4–v0.2.7 release arc hit four of these in a row, each blocking until shipped.

**The pattern:** anything that depends on `__file__` resolving to a real on-disk path, on `sys.std*` being a real terminal, on a package being importable that the spec didn't bundle, or on a library's auto-discovery via `importlib` — all behave differently when frozen. The only reliable test is to **launch the bundled exe and probe its endpoints**, never just trust that `python -m resonant_client` working means the bundle works.

**Real bugs that hit in this class:**

| Bug | Symptom | Root cause | Fix |
|---|---|---|---|
| #19 | `AttributeError: 'NoneType' has no attribute 'isatty'` at startup | `console=False` makes `sys.stderr = None`; uvicorn's `ColourizedFormatter.__init__` crashes calling `.isatty()` | Redirect `None` std streams to a real file at top of `__main__.py` |
| #20 | Every page returns "Internal Server Error" | `Path(__file__).parent` resolves to a *virtual* path inside PyInstaller's PYZ archive; Jinja2 can't find `templates/index.html` | Detect frozen mode and use `sys._MEIPASS` to resolve template + static dirs |
| #21 | Errors invisible (false-success during diagnosis) | v0.2.4's stderr→/dev/null shim made all runtime errors silent | Redirect stderr to `~/.resonant/logs/resonant-startup.log` instead of /dev/null |
| #23 | Homepage returns 500 with `TypeError: unhashable type: 'dict'` | Starlette 0.29+ changed `TemplateResponse` signature from `(name, context)` to `(request, name, context=None)`; old call form sends dict to wrong slot | Use new request-first API; pin starlette range in pyproject |
| #24 | UI hangs at "Reconnecting..."; sidebar never hydrates | `websockets` package is in `[server]` extras but not `[gui]`; CI builds without it; uvicorn can't load WS protocol | Add `websockets>=12.0` to `[gui]` extras + hidden imports |

**Mandatory mitigations** (now in the pre-release checklist above):

1. Run the **bundled** exe (not dev Python) for smoke tests
2. **Empty the log** before each smoke run, then **inspect it after** — a clean HTTP 200 isn't enough, the request handler can return 200 from a fallback while the log captures the real warning
3. **Probe WebSocket separately** from HTTP — the WS upgrade path is a different code path that HTTP probes won't exercise
4. **Test on a fresh checkout** — the dev machine has system packages that PyInstaller may pull in transitively, masking missing-dep bugs that bite CI's clean env

**Future preventative work:**

- `.github/workflows/build-check.yml` — PR-time job that does the bundled-exe smoke automatically on every PR. Catches frozen-only bugs before tag push instead of after. Tracked as bug #22 in [docs/known-issues.md](docs/known-issues.md).

---

## Lessons learned from v0.2.0

The first three CI runs all failed. Each fix is documented here as a guard against future repeats.

### Run #1 — Untracked `plan_graph_view.js` (1m41s, fixed at PyInstaller step)

`packaging/resonant.spec` listed `plan_graph_view.js` in `datas`. It existed on disk locally (so local builds worked) but was never committed. CI's clean checkout failed.

**Lesson:** add a `git ls-files | grep <file>` check to the pre-release checklist for any file referenced by the spec.

**Future bug fix:** add a PR-time job that runs `pyinstaller packaging/resonant.spec --clean --noconfirm` on every PR — would catch this in seconds.

### Run #2 — Choco refused to downgrade Inno Setup (2m29s, fixed at Set up Inno Setup step)

The workflow had a `choco install innosetup --version=6.4.3` step. `windows-latest` ships with Inno Setup 6.7.1 already installed, and choco refuses downgrades by default.

**Lesson:** GitHub-hosted runners ship with a lot of pre-installed tooling. Before adding a `choco install` / `apt-get install`, check the runner image manifest at `https://github.com/actions/runner-images`.

**Fix:** removed the install step entirely. The "Build installer with Inno Setup" step uses a hardcoded path (`C:\Program Files (x86)\Inno Setup 6\ISCC.exe`) with a recursive-search fallback for path drift across runner images.

### Run #3 — Appcast duplicate-version guard (2m40s, fixed at last step)

`packaging/update_appcast.py` refuses to add a version that's already in the feed. A placeholder v0.2.0 entry from earlier dev work was sitting on `gh-pages` already; the script refused.

**Lesson:** keep the `gh-pages` appcast in sync with reality. Don't manually add placeholder entries; let the script populate them.

**Fix:** for v0.2.0 specifically, manually replaced the placeholder entry with the real signed one. For v0.2.1+, the duplicate-check needs to become an UPDATE (track via [Bug ledger](docs/known-issues.md)).

### CodeMirror 6 paste UTF-8 corruption

Editing the workflow file via GitHub's web UI requires injecting content into a CodeMirror 6 editor. The naïve `atob(b64)` approach interprets bytes as Latin-1, then CodeMirror's paste handler re-encodes them as UTF-8 — producing the classic "double-encoded UTF-8" corruption visible as `â` in place of `—`.

**Fix:** always use `new TextDecoder('utf-8').decode(Uint8Array.from(atob(b64), c => c.charCodeAt(0)))` for proper UTF-8 decoding.

**Better fix:** access CodeMirror's `EditorView` directly via `document.querySelector('.cm-content').cmTile.view` and dispatch a transaction:

```js
view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: properUTF8String}});
```

This bypasses the paste handler entirely and writes directly to the document model. Used during v0.2.0 to fix the corrupted workflow file.

### `gh` CLI active-account vs git push auth

This repo lives in the `Luminary-Analytics` org owned by `LA-Rich` (personal GitHub account). The dev machine had three accounts in `gh`'s keyring:
- `LA-Rich` — has `repo` + `read:org` scopes (write access to org repos, NO `workflow` scope)
- `rbellantoni85` — has `repo` + `workflow` (but not added as a collaborator on this repo)
- `rbellantoni` — same as 85

Initial pushes failed because `rbellantoni85` was the active `gh` account but didn't have repo write. Switching to `LA-Rich` fixed regular pushes but workflow file pushes still failed (no `workflow` scope).

**Resolution path:** `gh auth switch -u LA-Rich` for normal pushes; for workflow files use the web UI fallback. Long-term fix: add `workflow` scope to LA-Rich's token via `gh auth refresh -s workflow` and complete the device-code flow.

---

## Architecture references

- **`docs/release-pipeline.md`** — full architectural deep-dive of the release pipeline (PyInstaller spec, WinSparkle wrapper, Inno Setup, CI workflow, EdDSA flow, gh-pages topology)
- **`docs/known-issues.md`** — bug ledger #1-#12 with reproductions
- **`packaging/resonant.spec`** — PyInstaller config, heavily commented
- **`packaging/installer.iss`** — Inno Setup script
- **`packaging/update_appcast.py`** — appcast XML mutator (idempotent module, but rejects duplicates)
- **`resonant_client/updater.py`** — ctypes wrapper around WinSparkle.dll, the runtime side of the auto-update channel
- **`.github/workflows/release.yml`** — the CI pipeline

---

## Quick reference

```bash
# Cut a release
git tag v0.2.1 && git push origin v0.2.1

# Watch CI
gh run watch --repo Luminary-Analytics/resonant-client

# View the release
gh release view v0.2.1 --repo Luminary-Analytics/resonant-client

# Verify appcast is updated
curl -s https://luminary-analytics.github.io/resonant-client/appcast.xml | head -20

# Tag was wrong; fix it
git tag -d vX.Y.Z
git push --delete origin vX.Y.Z
git tag vX.Y.Z <correct-sha>
git push origin vX.Y.Z

# Re-run a failed CI job (no retag needed)
gh run rerun <run-id> --failed --repo Luminary-Analytics/resonant-client

# Sign a local installer to test the signing path
./packaging/winsparkle/WinSparkle-0.9.2/bin/winsparkle-tool.exe \
    sign --private-key-file ~/.resonant/keys/eddsa_priv.key \
    dist/installer/resonant-setup-X.Y.Z.exe
```
