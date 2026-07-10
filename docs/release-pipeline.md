# Release Pipeline — Architectural Deep-Dive

This document explains every component of the Resonant Client release pipeline, why each piece exists, and how they fit together. Read this when you need to **understand** or **modify** how releases work.

For day-to-day use ("I want to ship v0.2.1"), see [RELEASING.md](../RELEASING.md) instead.

---

## What the pipeline does

A single `git push origin vX.Y.Z` produces:

1. A **PyInstaller bundle** (`dist/resonant/`, ~169 MB unpacked locally / ~80 MB in CI's clean environment)
2. An **Inno Setup installer** that wraps it (`resonant-setup-X.Y.Z.exe`, ~26 MB compressed via LZMA2-ultra64)
3. An **EdDSA-signed** record of that installer in the appcast feed
4. A **published GitHub Release** with the installer attached
5. An **updated `appcast.xml`** on the `gh-pages` branch that triggers WinSparkle's auto-update flow on every running v0.2.0+ client

End-to-end runtime: ~3-5 minutes on `windows-latest` GitHub-hosted runner.

---

## High-level data flow

```
                          DEV MACHINE
  ┌─────────────────────────────────────────────────────────────┐
  │  ~/.resonant/keys/eddsa_priv.key  (NEVER committed)         │
  │      │                                                       │
  │      │ winsparkle-tool generate-key                          │
  │      │ → public key                                          │
  │      ▼                                                       │
  │  resonant_client/updater.py:EDDSA_PUBLIC_KEY                 │
  │  (committed, baked into every binary)                        │
  └──────────────────────────┬──────────────────────────────────┘
                             │ git push to main
                             │ + repo secret EDDSA_PRIVATE_KEY
                             ▼
                       GITHUB ACTIONS (windows-latest)
  ┌─────────────────────────────────────────────────────────────┐
  │  on: push.tags: ['v*.*.*']                                   │
  │                                                               │
  │  1. Checkout main + verify tag matches __version__            │
  │  2. pip install -e ".[gui,desktop]"                           │
  │  3. PyInstaller build (packaging/resonant.spec)               │
  │  4. Smoke test: resonant.exe --version                        │
  │  5. Inno Setup compile (packaging/installer.iss)              │
  │  6. winsparkle-tool sign (using EDDSA_PRIVATE_KEY secret)     │
  │  7. Create GitHub Release (softprops/action-gh-release@v2)    │
  │  8. Checkout gh-pages branch into ./gh-pages-checkout         │
  │  9. python packaging/update_appcast.py                        │
  │ 10. git push gh-pages                                         │
  └─────────┬─────────────────────────────────────┬──────────────┘
            │ Release artifact                    │ appcast.xml
            ▼                                     ▼
  GITHUB RELEASES PAGE                    GITHUB PAGES (gh-pages)
  resonant-setup-X.Y.Z.exe                appcast.xml
  + EdDSA signature in appcast           + signed download URL
  + auto-generated release notes         + EdDSA signature
            ▲                                     ▲
            │                                     │
            │ (HTTPS download)                    │ (silent poll every 24h)
            │                                     │
  END-USER MACHINE (any Windows 10+)              │
  ┌─────────────────────────────────────────────────────────────┐
  │  resonant.exe (running from prior install)                   │
  │      │                                                        │
  │      │ on launch:                                             │
  │      │   updater.init_updater()                              │
  │      │     ├─ ctypes.CDLL(WinSparkle.dll)                    │
  │      │     ├─ win_sparkle_set_appcast_url(...)               │
  │      │     ├─ win_sparkle_set_eddsa_public_key(...)          │
  │      │     └─ win_sparkle_init()                             │
  │      │           ↓ (background thread)                        │
  │      │   GET appcast.xml on 24h cadence                       │
  │      │           ↓                                            │
  │      │   if appcast.latest > running version:                │
  │      │     show native "Software Update" dialog              │
  │      │           ↓                                            │
  │      │   on user "Install":                                  │
  │      │     download .exe                                     │
  │      │     verify EdDSA signature against embedded pubkey    │
  │      │     run new installer (which kills + replaces)        │
  └─────────────────────────────────────────────────────────────┘
```

---

## Component reference

### 1. PyInstaller spec — `packaging/resonant.spec`

**Purpose:** transform `resonant_client/` Python source into a self-contained Windows folder that runs without any system Python install.

**Mode:** one-folder (not one-file). The Inno Setup installer wraps the whole folder so users never see the multi-file mess. One-folder gives faster startup and easier debugging vs one-file's tar-extraction overhead.

**Bundled deps (auto-detected):**
- Core: `rich`, `prompt-toolkit`, `httpx`
- GUI server: `starlette`, `uvicorn`, `jinja2`, `websockets`
- Desktop tools: `pyautogui`, `mss`, `Pillow` (for screenshot/click/type)

**Bundled deps (explicit hidden imports):**
- `resonant_client.tui`, `resonant_client.gui.server`, `resonant_client.gui.app` — dispatched via string lookups in `__main__.py`
- `uvicorn.loops.auto`, `uvicorn.protocols.*` — runtime-resolved by uvicorn
- `websockets.legacy.*` — occasionally missed by static analysis
- `PIL.ImageGrab` — `mss` falls through to this on some paths
- All submodules of `resonant_client` itself via `collect_submodules()` — defensive against engine's `importlib.import_module(...)` calls

**Bundled native binaries (via `binaries=` list):**
- `WinSparkle.dll` (vendored at `packaging/winsparkle/WinSparkle-0.9.2/x64/Release/WinSparkle.dll`) — auto-update DLL, must travel next to the exe so `resonant_client/updater.py` can `ctypes.CDLL` it

**Bundled data files (via `datas=` list):**
- `resonant_client/gui/templates/index.html` — Jinja2 template
- `resonant_client/gui/static/{app.js,plan_graph_view.js,styles.css,favicon.svg,resonant.ico,resonant.png}` — frontend assets
- Starlette's bundled HTML error pages via `collect_data_files("starlette")`

**Excludes (explicitly NOT bundled):**
- `pywebview` — requires WebView2 runtime on the target machine. v0.x ships browser-only mode (installer pops `localhost:8909` in default browser). Re-add for v1.0+ if pywebview UX is desired.
- `playwright` — adds 150+ MB and a Chromium download. Will be a one-click "Install browser tools" button in Settings → Tools that runs `playwright install` post-install.
- `cv2` (`opencv-python`) — runtime-optional in `engine/recording.py` (wrapped in try/except). Users who want screen recording can `pip install opencv-python` after install.
- `uiautomation` — runtime-optional in `engine/accessibility.py`.
- `tkinter`, `matplotlib`, `numpy`, `scipy`, `pandas`, `PyQt5` — pulled transitively but not used.

**Console mode:** `console=True` for v0.x. Keeps the terminal visible so first-install Ollama-connection / port-bind issues are debuggable. Will flip to `False` once we have proper logging-to-file in v0.5+.

**Icon:** `resonant_client/gui/static/resonant.ico` — bundled as the exe icon and the installer/uninstaller icon.

**UPX:** disabled. UPX-compressed binaries trigger Windows Defender heuristics on a non-trivial percentage of machines. Not worth the size savings.

---

### 2. Inno Setup script — `packaging/installer.iss`

**Purpose:** wrap the `dist/resonant/` PyInstaller output into a single double-clickable `.exe` installer.

**Compression:** LZMA2/ultra64 — produces ~62% size reduction (169 MB → 64 MB locally; 80 MB → 26 MB in CI). Slow to compress (~36 sec on dev machine, ~40 sec on CI) but only happens at release time.

**Privileges:** `PrivilegesRequired=lowest` — installs to `%LOCALAPPDATA%\Programs\Resonant Client\` per-user. No UAC prompt, no admin rights needed.

**Architecture:** `ArchitecturesAllowed=x64compatible` — won't install on 32-bit Windows. Min OS: Windows 10 (`MinVersion=10.0`).

**Shortcuts:** Start Menu entry "Resonant Client" with launch arg `gui --browser` (so it pops a browser tab). Optional desktop shortcut (user opt-in during install).

**Uninstaller:** removes the install dir but **preserves `~/.resonant/`** — user data, settings, and the EdDSA-trusted skills directory survive uninstall + reinstall.

**Code signing:** `SignTool=` is empty. v0.x is unsigned, so users see a SmartScreen "Unrecognized publisher" warning on first install. Click-past dialog, not blocking. Add an Azure Trusted Signing cert (~$10/month) for v1.0+ if you want to remove that warning.

**Version override:** the script uses `#ifndef AppVersion ... #define AppVersion "0.2.0" ... #endif`. CI passes `/DAppVersion=0.2.1` to override per build, so the same script handles every release.

---

### 3. WinSparkle runtime — `resonant_client/updater.py`

**Purpose:** make every running client instance silently poll for new versions and prompt the user when one is available.

**Loaded mechanism:** `ctypes.CDLL(WinSparkle.dll)`. We bundle WinSparkle 0.9.2's x64 DLL (vendored at `packaging/winsparkle/WinSparkle-0.9.2/x64/Release/WinSparkle.dll`, 2.8 MB). PyInstaller copies it into the bundle's `_internal/` folder; the loader looks there first, then falls back to the vendored source path for dev runs.

**Why ctypes (not pip)?** No `winsparkle` or `pywinsparkle` package exists on PyPI as of 2026-04. Rolling our own ctypes wrapper is ~220 lines, has zero external Python deps, and gives us tight control over the API surface we expose to the rest of the app.

**Public API exposed:**
- `init_updater()` — called once at startup from `__main__.py`. Idempotent. No-op on non-Windows. Returns True if WinSparkle is now active.
- `check_for_updates_now(silent=False)` — manual menu trigger. `silent=False` shows the dialog regardless of result; `silent=True` only shows UI if update found.
- `cleanup_updater()` — stops WinSparkle's background thread on shutdown. Optional (OS reaps anyway).

**Configuration baked in at init:**
- Appcast URL: `https://luminary-analytics.github.io/resonant-client/appcast.xml`
- EdDSA public key: `HgNb0s7xavpa1bFyX/8B24AnuUdgekpvgO6HQU+zv8k=` (32 bytes, base64-encoded)
- Company name: `Luminary Analytics`
- App name: `Resonant Client`
- Registry path: `HKCU\Software\Luminary Analytics\Resonant Client\WinSparkle` — stores `LastCheckTime`, `CheckForUpdates`, `UpdateInterval`, `SkipThisVersion` flags
- Auto-check enabled: yes
- Check interval: 24 hours (86400 sec)

**Failure mode:** if `WinSparkle.dll` isn't present (running from source on a dev machine), every function becomes a no-op. The app runs fine, just without auto-update.

**EdDSA signature verification flow:**
1. WinSparkle background thread fetches `appcast.xml`
2. Parses `<sparkle:edSignature>BASE64_SIG</sparkle:edSignature>` from the latest `<item>`
3. After downloading the new installer, WinSparkle computes the EdDSA signature of the downloaded bytes using the embedded public key
4. If signature matches → run installer
5. If signature mismatch → reject, error dialog ("Update appears corrupted; please try again or reinstall")

This is what protects users from update-channel hijacking. An attacker who compromises GitHub Pages could swap the appcast XML, but they can't forge a valid EdDSA signature without the private key (which lives only on the dev machine).

---

### 4. Appcast XML — `gh-pages/appcast.xml`

**Purpose:** the file WinSparkle polls to know if a new version exists.

**Format:** RSS 2.0 with the Sparkle namespace (`http://www.andymatuschak.org/xml-namespaces/sparkle`).

**Hosted at:** `https://luminary-analytics.github.io/resonant-client/appcast.xml` via GitHub Pages on the `gh-pages` branch.

**Why a separate branch (not `/docs/`)?** The repo's `docs/` directory already holds project planning docs we don't want rendered as a public website. An orphan `gh-pages` branch keeps the Pages content (`appcast.xml`, `index.html`, `.nojekyll`) cleanly separated from source code.

**One `<item>` per release:**

```xml
<item>
    <title>Version 0.2.0</title>
    <pubDate>Tue, 28 Apr 2026 00:00:00 +0000</pubDate>
    <sparkle:version>0.2.0</sparkle:version>
    <sparkle:shortVersionString>0.2.0</sparkle:shortVersionString>
    <description><![CDATA[<p>Release notes...</p>]]></description>
    <enclosure
        url="https://github.com/Luminary-Analytics/resonant-client/releases/download/v0.2.0/resonant-setup-0.2.0.exe"
        sparkle:version="0.2.0"
        length="27281447"
        type="application/octet-stream"
        sparkle:edSignature="iWM2C3x4BUu/TUvfDSO..." />
</item>
```

WinSparkle compares `<sparkle:version>` to the running app's version. If newer, prompt; if equal or older, do nothing.

**Append-only by design:** old `<item>` entries are never removed. A user running v0.1.0 (if such a thing existed) could still see and apply v0.2.0. Keeping history in the feed is cheap and helps with multi-step upgrade paths.

---

### 5. Appcast updater script — `packaging/update_appcast.py`

**Purpose:** programmatically insert a new `<item>` into the appcast after CI has built and signed a release.

**Inputs:**
- `--version` — semver string (e.g., `0.2.1`)
- `--installer` — path to the signed `.exe`
- `--signature` — base64 EdDSA signature from `winsparkle-tool sign`
- `--notes` — HTML release notes
- `--appcast` — path to `appcast.xml` to update
- `--download-base` — `https://github.com/<owner>/<repo>/releases/download`

**Behavior:**
- Parses the existing XML
- Computes file size from `installer.stat().st_size`
- Constructs a new `<item>` element
- Inserts it as the FIRST `<item>` in `<channel>` (after channel-level metadata)
- Pretty-prints with 4-space indent for diff readability
- Writes back via `tree.write(..., encoding="utf-8", xml_declaration=True)`

**Duplicate guard:** if the version is already in the feed, the script `sys.exit(1)` with `version X.Y.Z already in appcast — bump and retry`. This is intentionally conservative — prevents accidental double-publishes — but trips on placeholder entries. A planned fix is to make duplicate-version an UPDATE rather than a refusal.

**Standalone usage** (for local testing):

```bash
python packaging/update_appcast.py \
    --version 0.2.1 \
    --installer dist/installer/resonant-setup-0.2.1.exe \
    --signature "$(./packaging/winsparkle/.../winsparkle-tool.exe sign --private-key-file ~/.resonant/keys/eddsa_priv.key dist/installer/resonant-setup-0.2.1.exe)" \
    --notes "<p>Initial release.</p>" \
    --appcast gh-pages-checkout/appcast.xml \
    --download-base "https://github.com/Luminary-Analytics/resonant-client/releases/download"
```

---

### 6. CI workflow — `.github/workflows/release.yml`

**Trigger:** `on.push.tags: ['v*.*.*']`. Tag-only — never on plain commits to main, never on branches.

**Permissions:** `contents: write` (needed to create Releases and push to gh-pages).

**Runs on:** `windows-latest` (currently `windows-2025` Server) — 30 minute timeout.

**13 steps in order:**

| # | Step | Purpose | Runtime |
|---|------|---------|---------|
| 1 | Checkout main | `actions/checkout@v4` with `fetch-depth: 0` | ~5 sec |
| 2 | Extract version from tag | Strip `refs/tags/v` prefix → `version` output | <1 sec |
| 3 | Sanity check — tag matches `__version__` | Read `resonant_client/__init__.py`, abort if mismatch | <1 sec |
| 4 | Set up Python | `actions/setup-python@v5` Python 3.13 + pip cache | ~10 sec |
| 5 | Install build dependencies | `pip install -e ".[gui,desktop]"` + pyinstaller | ~60-90 sec (first run); ~30 sec cached |
| 6 | Build PyInstaller bundle | `pyinstaller packaging/resonant.spec --clean --noconfirm` | ~60-90 sec |
| 7 | Smoke-test bundled exe | `resonant.exe --version` must match | <1 sec |
| 8 | Build installer with Inno Setup | `ISCC.exe /DAppVersion=X.Y.Z packaging/installer.iss` | ~40 sec |
| 9 | Sign installer with EdDSA | `winsparkle-tool sign --private-key-file <secret-temp-file> ...` | <1 sec |
| 10 | Create GitHub Release with installer | `softprops/action-gh-release@v2` | ~10 sec |
| 11 | Checkout gh-pages branch | `actions/checkout@v4` ref=gh-pages, path=gh-pages-checkout | ~3 sec |
| 12 | Update appcast.xml on gh-pages | `python packaging/update_appcast.py` | <1 sec |
| 13 | Push appcast update to gh-pages | `git commit + git push origin gh-pages` | ~2 sec |

**Secret handling:**

```pwsh
$keyPath = "$env:RUNNER_TEMP\eddsa_priv.key"
$env:EDDSA_PRIVATE_KEY | Out-File -Encoding ascii -NoNewline $keyPath
& $tool sign --private-key-file $keyPath $installer
```

The secret is written to `$RUNNER_TEMP` which is auto-cleaned when the runner is destroyed at end of job. No secret ever lives in the workspace or gets persisted in build logs (PowerShell's `& $tool` doesn't echo args by default).

**Inno Setup discovery:** the workflow uses a hardcoded `C:\Program Files (x86)\Inno Setup 6\ISCC.exe` path with a recursive-search fallback. As of late 2025, `windows-latest` ships with Inno Setup 6.7+ pre-installed at that path; the fallback exists for path drift across runner image versions.

**Pre-release detection:** `prerelease: ${{ contains(steps.version.outputs.version, '-') }}` — a tag like `v0.2.0-rc1` produces a prerelease; `v0.2.0` produces a final release.

---

### 7. EdDSA key management

**Algorithm:** Ed25519 (32-byte private + 32-byte public key, fast verify).

**Generation:** `winsparkle-tool generate-key --file ~/.resonant/keys/eddsa_priv.key` produces:
- A 44-byte file (32 bytes raw key + framing) at the requested path
- Public key printed to stdout: `Public key: HgNb0s7xavpa1bFyX/8B24AnuUdgekpvgO6HQU+zv8k=`

**Storage:**
- Private: `~/.resonant/keys/eddsa_priv.key` on the dev machine. **NEVER** committed to git, **never** echoed in logs. Backed up only via the user's normal home-directory backup pipeline.
- Public: hardcoded in `resonant_client/updater.py:EDDSA_PUBLIC_KEY` (committed). Bundled into every binary so WinSparkle can verify update signatures offline.
- CI access: stored as the `EDDSA_PRIVATE_KEY` GitHub repo secret (set via `gh secret set`). Read at job time, written to `$RUNNER_TEMP\eddsa_priv.key`, used to sign, then auto-cleaned with the runner.

**Signing:**

```bash
./packaging/winsparkle/WinSparkle-0.9.2/bin/winsparkle-tool.exe \
    sign --private-key-file ~/.resonant/keys/eddsa_priv.key \
    resonant-setup-X.Y.Z.exe
# Prints: <88-char base64 EdDSA signature>
```

The signature is 64 bytes (Ed25519 sig = 2 × 32-byte scalars), base64-encoded → 88 chars including padding.

**Verification (what WinSparkle does on the user's machine):** decode the public key from the binary's hardcoded constant, decode the signature from the appcast XML, compute `Ed25519Verify(pubkey, downloaded_installer_bytes, signature)`. Match → proceed; mismatch → refuse update.

**Key rotation:** if the private key is ever compromised, generate a new keypair, update `EDDSA_PUBLIC_KEY` in `updater.py`, ship a new release at the **old** key (so existing clients can verify it), then ship a follow-up release where the **new** binary trusts only the new key. There's no automatic key migration; users on the compromised key are stuck on whatever version they have until they manually reinstall from a fresh download.

---

### 8. GitHub Pages topology

**Branch:** `gh-pages` (orphan — no shared history with `main`).

**Files:**
- `appcast.xml` — the WinSparkle feed (mutated by CI on every release)
- `index.html` — minimal landing page at `https://luminary-analytics.github.io/resonant-client/`
- `.nojekyll` — disables Jekyll processing (we serve raw files only)

**Why orphan branch (not a `/docs/` subfolder)?** GitHub Pages can serve from `main:/docs/` but the repo's existing `docs/` folder holds internal planning docs that shouldn't be on a public website. The orphan branch keeps Pages content cleanly separated.

**Deploy time:** ~10-30 seconds after a push to `gh-pages`. GitHub Pages has its own build pipeline that runs after every push to the configured branch.

**Custom domain:** none, currently. The default `<owner>.github.io/<repo>/` URL is fine. If a custom domain is added later, update `APPCAST_URL` in `resonant_client/updater.py` AND ship a release at the OLD URL first (so existing clients can pick up the new URL), THEN flip the URL.

---

## Bundle size analysis

| Stage | Local dev machine | CI runner | Why the delta |
|-------|-------------------|-----------|---------------|
| Source tree | — | — | — |
| PyInstaller `dist/resonant/` (one-folder) | ~169 MB | ~80 MB | Local has system numpy/PyQt5/cv2 that PyInstaller pulls transitively despite excludes; CI's clean env doesn't |
| Inno Setup compressed installer | ~64 MB | ~26 MB | Same delta, plus LZMA2 squeezes the larger blob even more aggressively |

The CI bundle is the canonical artifact. Local builds are useful for testing the spec but the published installer is what users get.

**Eventual trim target:** sub-50 MB installer. PyInstaller's `--debug=imports` flag can identify which excluded modules are still being pulled transitively. Worth a 30-min investigation before v1.0.

---

## What's NOT automated yet

These remain manual / TODO:

1. **Code signing.** `SignTool=` is empty in `installer.iss`. SmartScreen warns on first install. Add Azure Trusted Signing cert (~$10/month) for v1.0+.
2. **Updating release notes.** CI uses `generate_release_notes: true` which auto-generates from PR titles. Better notes require a manual edit on the GitHub release page or a CHANGELOG.md feed-in.
3. **Cross-platform builds.** Windows-only currently. macOS DMG would need a separate workflow with a macOS runner + Sparkle (the original) instead of WinSparkle.
4. **Bundle-size CI gate.** No alert if the bundle suddenly doubles. Consider a CI step that fails if `installer.size > 100 MB`.
5. **PR-time PyInstaller smoke build.** The v0.2.0 release needed three CI runs to converge because untracked files / missing deps only surface on tag push. A PR job that builds (but doesn't publish) would catch these in seconds. Tracked as bug #12 in the [known issues ledger](known-issues.md).

---

## Files involved (cheat sheet)

| File | Lines | Purpose |
|------|-------|---------|
| `RELEASING.md` | 380 | Operational runbook for cutting a release |
| `docs/release-pipeline.md` | this file | Architectural deep-dive |
| `docs/known-issues.md` | (see file) | Bug ledger #1-#12 |
| `packaging/resonant.spec` | 205 | PyInstaller config |
| `packaging/installer.iss` | 87 | Inno Setup script |
| `packaging/update_appcast.py` | 186 | Appcast XML mutator |
| `packaging/winsparkle/` | (3.3 MB) | Vendored WinSparkle 0.9.2 binaries |
| `resonant_client/updater.py` | 220 | ctypes wrapper around WinSparkle.dll |
| `resonant_client/__main__.py` | 41 | Wires updater into startup; `--version` flag |
| `.github/workflows/release.yml` | 174 | CI pipeline |

---

## Related reading

- [RELEASING.md](../RELEASING.md) — operational runbook (start here if you just want to ship)
- [docs/known-issues.md](known-issues.md) — bug ledger including release-pipeline issues
- [USER-DOGFOOD-2026-04-27.md](../USER-DOGFOOD-2026-04-27.md) — the dogfood marathon that surfaced the bugs being fixed in v0.2.x
- [WinSparkle docs](https://github.com/vslavik/winsparkle) — upstream WinSparkle reference
- [Inno Setup docs](https://jrsoftware.org/ishelp/) — Inno Setup language reference
- [PyInstaller docs](https://pyinstaller.org/) — spec file format and hooks
