Here's the complete spec from where it was cut off, plus all remaining sections.

---

## Final spec

**Refined intent:** Ship a Tauri+Svelte desktop launcher app for Ubuntu (`.deb` package) that provides a Windows-like catalog of ~10 productivity/utility apps. Users browse the catalog with green/yellow/red compatibility indicators, install any app with one click (the engine downloads the vendor installer, verifies its SHA-256 checksum, creates an isolated Wine prefix, runs winetricks dependencies, and executes the installer), then launch the app with one click. The launcher manages its own Wine runtime (downloaded via a first-run setup wizard), bundles vendored winetricks, supports a local recipe override directory for contributors, and surfaces failures with structured diagnostic modals + GitHub-issue-template clipboard exports. No games, no cross-distro support, no remote recipe fetching in v0.1.

**Key assumptions:**
- Greenfield — repo is empty (fresh `git init`), no existing code to integrate with.
- Host machine for development is Windows; Wine end-to-end testing is not possible on this host. Acceptance criteria focus on compilation, unit tests, and recipe-schema correctness.
- Target users are on Ubuntu 24.04 with an internet connection and at least 2 GB free disk space.
- All v0.1 catalog apps have stable direct-download URLs from vendor sites (no Steam/GOG/itch.io authentication required).

**In scope:**
- Tauri (Rust backend) + Svelte (webview frontend) project scaffold.
- Recipe YAML schema (versioned: `schema_version` + `recipe_version`) with fields: `id`, `display_name`, `description`, `icon_url`, `categories`, `tested_on`, `compatibility` (green/yellow/red), `maintainers`, `homepage`, `install_steps`, `launch`, `notes`.
- Install step types: `download` (url, sha256, optional filename), `winetricks` (packages list), `run-exe` (file template var, args array), `copy-file` (src/dst template vars), `verify-checksum` (file, expected — for non-download edge cases).
- Template variable resolution: `{{prefix}}` (resolves to `~/.resonant/prefixes/<app-id>/`), `{{download.<filename>}}` (resolves to downloaded file path).
- `download` steps auto-verify SHA-256 after download; mismatch → stop install, delete unverified file, show security-failure modal with actual vs. expected hash, no Retry button.
- Per-app isolated Wine prefixes under `~/.resonant/prefixes/<app-id>/`.
- One bundled Wine runtime (`wine-9.0-staging`), downloaded as a GitHub release asset tarball during first-run setup wizard.
- First-run wizard: downloads runtime (~500 MB) with percentage progress bar; "Skip for now, browse catalog read-only" link available; blocks install actions until runtime is present.
- Vendored winetricks script committed to the repo; engine invokes it as `bash <vendored-path> --prefix <prefix> <packages>`.
- Install pipeline phases: Downloading → Verifying → Preparing prefix → Running installer → Cleaning up. Indeterminate spinner with phase labels; collapsible "Show details" terminal-log pane.
- Install queue: one-at-a-time, in-memory only (closing app = cancel queued items with confirm dialog). Queue UX: tile shows "Queued (N ahead)", persistent header chip "Installing X · N queued".
- Cancellation: immediate (SIGKILL child processes), auto-delete partial download + half-built prefix, tile reverts to "Install", brief toast "Installation cancelled."
- Launch: executes `launch.command` with optional `args` array via Wine; detects nonzero exit code → non-blocking toast "X closed unexpectedly (Wine exit code: N)" with "View details" link to per-launch log.
- Per-launch logs: `~/.resonant/logs/<app-id>/<timestamp>.log`, last 10 retained per app.
- "Recent runs" mini-section in per-app detail panel: last 5 launches with timestamp + outcome icon.
- Failure modals with: human-readable summary, diagnostic info (exit code, last 30 lines of stderr), Retry button (where applicable), "Report this" button generating GitHub-issue-template-formatted clipboard text (recipe id, recipe version, OS, Wine version, last 30 lines of stderr).
- ENOSPC detection: stop install, modal with "Ran out of disk space" message, actual vs. needed space, auto-cleanup of half-installed prefix, "Free up space" opens `xdg-open ~/.resonant/`.
- Uninstall: full removal of everything app-id-namespaced (prefix, installer cache, logs, launcher state). Confirmation dialog "This cannot be undone" with "Delete app data" checkbox + "Save logs before uninstall?" → exports to `~/Downloads/<app-id>-logs-<date>.zip`.
- Local recipe override: at startup, scan `~/.resonant/recipes/` for valid recipe YAML files; prefer overrides over bundled recipes. Enables contributor iteration + user private recipes.
- Icon handling: lazy-fetch `icon_url` on first catalog render, cache forever at `~/.resonant/icons/<app-id>.png`, re-fetch on `recipe_version` bump. Generate initial-letter colored avatar (Slack/Teams style) as fallback on unreachable URL.
- State management: single `~/.resonant/state.json` (pretty-printed, `schema_version`-tagged, atomic writes via temp-file-and-rename).
- Startup filesystem reconciliation: if `state.json` says "installed" but prefix dir missing → yellow "prefix missing" badge, primary action becomes "Reinstall."
- Recipe schema validation at load time: malformed recipes fail at app-load time, not at install time.
- Catalog UI: 3–4 column app grid, search bar at top, per-app detail panel with Install/Launch/Uninstall buttons, right-click context menu (Install, Uninstall, Open prefix folder).
- `.deb` packaging via `tauri build --target deb`; install one-liner in README: `curl -L <github-release-url> -o /tmp/rb.deb && sudo dpkg -i /tmp/rb.deb`.
- One canonical, complete recipe bundled (e.g., Notepad++ with download + winetricks vcrun2019 + run-exe silent install).
- At least 9 additional recipe stubs (valid YAML, all required fields, `compatibility` populated but install steps may be placeholder TODOs) to round out the ~10-app catalog.

**Out of scope:**
- Games (Steam/GOG/itch.io integration, DXVK, VKD3D, GPU driver probing).
- Cross-distro support (Fedora, Arch, Debian); Ubuntu 24.04 only.
- Remote recipe fetching / update server; all recipes bundled.
- Per-recipe Wine version pinning; one global runtime.
- Persistent install queue across app restarts.
- Snap/Flatpak/PPA distribution.
- Per-machine compatibility probing (GPU, kernel, Wine variant detection).
- Virtual desktop / Windows-shell-clone metaphor.
- Background daemon for installs (app must stay open during install).
- Any telemetry or analytics.

**Time budget:** 4 hours.

**Technical constraints:**
- Tauri v2 (Rust backend), Svelte frontend (no React/Vue — use Svelte stores for reactive state).
- Rust backend handles: recipe parsing, state.json read/write, process lifecycle (spawn/kill Wine + winetricks), checksum verification, filesystem reconciliation, CLI bridge to frontend via Tauri commands.
- Frontend communicates with backend exclusively via Tauri's `invoke()` command bridge; no direct filesystem access from JS.
- Recipe YAML files validated against the schema at startup using a Rust-side parser (likely `serde_yaml` + custom validation).
- Atomic file writes: write to temp file, `fsync`, rename — used for `state.json` and any persisted config.
- Vendored winetricks committed as a plain file under e.g. `vendor/winetricks`; invoked via `bash` with the `--prefix` flag.
- Wine runtime tarball URL is hardcoded in the Rust backend (configurable via a build-time env var for testing).
- All paths under `~/.resonant/` use `dirs::home_dir()` resolution in Rust, never hardcoded `/home/`.
- Install pipeline steps are executed sequentially by the Rust backend; the frontend receives phase-change events via Tauri events.
- No `unsafe` Rust unless required by a Tauri API; prefer safe abstractions for process management.
- Logging: Rust side uses `tracing` or `log` crate; frontend logs install-phase transitions to the "Show details" pane.

**Acceptance criteria:**
- `[bash]` `cargo build --manifest-path src-tauri/Cargo.toml` exits 0
- `[bash]` `cargo test --manifest-path src-tauri/Cargo.toml` exits 0 (all unit tests pass)
- `[bash]` `npm run build --prefix src/` (or equivalent Svelte build command) exits 0 with no errors
- `[bash]` `cargo run --manifest-path src-tauri/Cargo.toml -- --help 2>&1 ; echo "exit: $?"` — verify the Tauri binary at least starts without panicking on launch (exit code 0); actual GUI rendering cannot be validated on this headless host, but a panic-free startup is the minimum bar
- `[bash]` `python -c "import yaml, os; recipes = [f for f in os.listdir('recipes') if f.endswith('.yml') or f.endswith('.yaml')]; assert len(recipes) == 10, f'Expected 10 recipes, found {len(recipes)}'; [__import__('yaml').safe_load(open(f'recipes/{r}')) for r in recipes]; print('OK')"` — all 10 recipe files exist, parse as valid YAML, and load without error
- `[bash]` `python -c "
import yaml, sys
required = ['schema_version','recipe_version','id','display_name','description','compatibility','install_steps','launch']
for r in __import__('os').listdir('recipes'):
    if r.endswith(('.yml','.yaml')):
        d = yaml.safe_load(open(f'recipes/{r}'))
        missing = [k for k in required if k not in d]
        assert not missing, f'{r}: missing fields {missing}'
        assert d['compatibility'] in ('green','yellow','red'), f'{r}: invalid compatibility {d[\"compatibility\"]}'
        assert isinstance(d['install_steps'], list) and len(d['install_steps']) > 0, f'{r}: install_steps empty'
        for step in d['install_steps']:
            assert 'type' in step, f'{r}: step missing type'
            assert step['type'] in ('download','winetricks','run-exe','copy-file','verify-checksum'), f'{r}: unknown step type {step[\"type\"]}'
        print(f'  {r}: OK')
print('All recipes valid')
"` — every recipe satisfies the mandatory schema (all required fields present, `compatibility` is a valid value, `install_steps` is non-empty, every step has a known `type`)

**Open risks:**
- Wine end-to-end behavior is untestable on the Windows dev host — the install pipeline is structurally complete but the actual Wine subprocess invocation path (prefix creation, `wine setup.exe`, winetricks execution) has not been exercised. First real test will be on an Ubuntu VM.
- The vendored winetricks script assumes `bash` is available; this is true on Ubuntu but would be a portability issue on minimal containers or non-GNU userlands.
- The ~500 MB Wine runtime download on first run may hit rate limits or CDN issues on GitHub releases; need to verify GitHub's bandwidth policies for large public release assets.
- Recipe staleness (checksum mismatches) has no automated fix path in v0.1 — users must manually clone the updated recipes repo or install a newer `.deb`. This could generate support burden if a popular vendor changes their installer URL or binary frequently.
- The in-memory-only install queue means closing the app during a long install loses the queued items. Users accustomed to background installs (Steam, app stores) may find this surprising.
- Tauri's webview on Linux uses WebKitGTK, which may have rendering differences from the Windows/macOS webviews — UI testing on the Windows host won't catch Linux-specific layout or font issues.