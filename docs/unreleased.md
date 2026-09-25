# Unreleased

**September 23, 2026: AI Employee work remains PAUSED by the user.**
The [consolidated product checkpoint](D:/Repos/Lumina_DO/SelfOrganizingNN/product/AI_EMPLOYEES_CHECKPOINT_2026_09_23.md)
records subsequent paid research, negative/control results and remaining work.
No added SONN learning value or qualified employee/router release is established.
The heartbeat remains paused. Documentation maintenance does not resume work,
spending or grants, and changes no native implementation or installed bundle.
The dated September 15/18 records below are historical.

## September 25 zero data retention — source only, not released

- **Connections that keep no data**: a custom connection can be marked
  **This endpoint keeps no prompts or responses** (`zero_retention`), shown
  as a "Zero retention" badge.
- **Policies can require it** (`models.require_zero_retention`,
  [organization policy](enterprise-policy.md)): only local Ollama and EXO
  models (Ollama's `-cloud` models run on ollama.com, so they don't count),
  marked connections, and providers the policy names in
  `models.zero_retention_providers` stay in the model menu; others are
  refused. The check is part of `Policy.model_allowed`, so every place that
  already checks models enforces it.

Validation on September 25, 2026:

- Full `pytest`: 3,963 passed, 4 skipped. `test_zero_retention.py`: the
  flag, each kind of provider under the requirement, Ollama cloud models,
  other rules still applying, invalid fields, an unreadable connection.
- In the browser pane, a custom connection saved with the box ticked showed
  the "Zero retention" badge and was stored with `zero_retention: true`.
  The first try stored `false`: the form's save payload lists its fields and
  didn't include the new one, which this check caught and which is fixed.

## September 25 an organization's shared model credit — source only, not released

- **Shared credit from Lumi Cloud** (`lumi/budgets.py`,
  [guide](usage-and-costs.md#an-organizations-shared-credit)): a check-in's
  answer can carry the organization's monthly credit and its spend so far.
  Lumi turns it into an `organization` budget that stops model requests
  when the month's spend across every computer, plus this computer's spend
  since the check-in, reaches the credit.
- It's kept across restarts, removed when a check-in no longer carries it,
  and ignored once its month has passed. Usage & cost lists it with the
  other budgets.

Validation on September 25, 2026: full `pytest` 3,960 passed, 4 skipped;
`test_cloud.py` (a check-in setting the
credit, a stop at $51 of $50 with this computer's spend since the check-in,
surviving a restart, removal, and last month's credit) passed with the
budget tests.

## September 25 cost per verified task and activity counts — source only, not released

- **Turn outcomes on this computer** (`lumi/activity.py`). Each finished
  turn, in the app or `lumi run`, adds a line to `~/.lumi/activity.jsonl`:
  its outcome (finished, error or stopped), whether a check the agent ran
  (`check_run`) passed, and how many files it changed. App crashes are
  counted too. No prompts, answers, file names or paths; lines are kept 90
  days.
- **Settings > Usage & cost** shows this month's tasks, verified tasks and
  the cost per verified task ([guide](usage-and-costs.md#cost-per-verified-task)).
- **Lumi Cloud check-ins** carry the same counts since the last check-in,
  for an organization's fleet health and adoption pages
  ([what a check-in sends](lumi-cloud.md)).

Validation on September 25, 2026:

- Full `pytest`: 3,959 passed, 4 skipped. `ruff check .` clean.
- `test_activity.py`: classifying turns (a failed or denied check verifies
  nothing), counts for a period, half-open windows, pruning old and broken
  lines, and counting crashes before the usual handlers.
- `test_cloud.py`: a check-in carries the counts, no content, and nothing
  twice.
- In the browser pane, from an isolated home with the scripted Ollama stub:
  a turn whose `check_run` passed and a turn with a plain command were
  recorded as two lines with no content, and Usage & cost showed 2 tasks,
  2 finished, 1 verified, and $0.00 per verified task (the stub is free).

## September 25 shell sandbox and command guardrails — source only, not released

See [shell sandbox and command guardrails](shell-sandbox.md).

- **Guardrails, always on** (`lumi/engine/guardrails.py`). A short list of
  commands is never run, in any permission mode:
  - deleting the whole file system, a drive or the home folder (`rm -rf /`,
    `rm -rf ~`, `Remove-Item -Recurse C:\`, `rd /s /q C:\`);
  - `chmod -R`/`chown -R` on `/`, `mkfs`, `format C:`, `diskpart`,
    `dd` onto a disk, a fork bomb;
  - shutting down or restarting.

  They're deny rules ahead of every tier's rules and ahead of organization
  and repository `allow` rules. The irreversibility floor checks them again
  before a command, check, job or preview starts, so a hook's rewrite or a
  session without an execution policy can't get past them.
- **The shell sandbox, off by default** (`lumi/engine/os_sandbox.py`,
  `security.shell_sandbox`: `"off"` or `"project"`). When it's on, the agent's
  commands, checks, jobs and previews can write only to the project, the
  temporary folders and terminal devices. Git's own folder stays read-only,
  because hooks written there would run outside the sandbox.
  - macOS uses `sandbox-exec` with a Seatbelt profile.
  - Linux uses bubblewrap.
  - Windows has no sandbox yet. With the setting on, commands are refused
    rather than run unprotected.
  - Settings > Privacy & security says whether a sandbox can run here. The
    check runs in the background at startup, so Settings never waits for it.
  - An organization can require the sandbox. A policy with any other value
    is invalid.
- **`lumi run` keeps file tools inside the project.** Headless sessions had
  no path sandbox; they now get the one the app uses.

Validation on September 25, 2026:

- Full `pytest` on Windows: 3,955 passed, 4 skipped (the live sandbox test
  skips on Windows). `ruff check .` clean.
- `test_guardrails.py` (29 commands that are refused, 26 everyday ones that
  aren't; every tier; repository and organization `allow` rules; the check
  before commands, jobs and previews start; nothing reaches a shell).
- `test_os_sandbox.py`: the setting and its values, Settings never waiting
  for the check, refusing commands, checks, jobs and previews when the
  sandbox can't run, the bubblewrap arguments and the Seatbelt profile, and
  a live test.
- The live test passed in CI (`sandbox.yml`) on macOS with Seatbelt and on
  Ubuntu with bubblewrap. Writing in the project and the temporary folder
  worked; writing in the home folder and in `.git` failed; listing the home
  folder worked. The first CI run failed because the test wrote to the
  suite's isolated home, which is under the temporary folder.
- `test_headless.py`: a headless session refuses a file write outside the
  project. `test_policy.py`: a policy with another sandbox value is invalid.
- In the browser pane, from an isolated home with the scripted Ollama stub:
  - with the sandbox off, `echo hello from lumi` ran (exit 0);
  - Settings > Privacy & security > Shell sandbox said "No sandbox can run
    here: Lumi has no shell sandbox on Windows yet." Choosing **Only the
    project and temporary folders** with the keyboard saved
    `security.shell_sandbox: "project"`, and the note added that the agent
    can't run commands;
  - the next command was refused: "The shell sandbox is on … No command was
    executed.";
  - in Full-auto, `mkfs.ext4 /dev/sda1` was refused before running: "Blocked
    by policy: Formatting a disk is never allowed, in any permission mode."

## September 25 large repositories — source only, not released

- **The codebase index follows `.gitignore`** ([guide](desktop-workflow.md#large-repositories)).
  In a Git repository, or a folder inside one, the file list comes from
  `git ls-files`; elsewhere the folder is walked as before. A repository in
  the home folder itself (dotfiles) isn't used for projects under it.
- **A cap of 100,000 files.** The index logs when it stops there.
- **Indexing does less work per file:**
  - each changed file is read once, on eight threads, and parsed once;
  - a missing tree-sitter is looked up once instead of once per file;
  - search reuses each file's lowercased fields;
  - the cache is compact JSON, replaced atomically, and written only when
    something changed.
- **The repo map scales.** An import counts for the files matching its most
  specific path suffix, and for none when more than three match. Before,
  one import credited every file sharing any suffix, such as all of a
  monorepo's `index.ts`. That took about a minute for 100,000 files.
- `scripts/benchmark_index.py` builds a synthetic repository and measures
  all of this.

Validation on September 25, 2026:

- Full `pytest`: 3,883 passed, 3 skipped.
- 10 tests in `test_rag_large_repos.py`:
  - `.gitignore` through Git, in the repository and in a folder inside it;
  - no repository in the home folder, the walk outside Git and the cap;
  - one read and one parse per changed file, and none when nothing changed;
  - the cache left alone when nothing changed;
  - the tree-sitter lookup, the cached search fields and import matching.
- Benchmarks on Windows (this machine's antivirus scans each newly written
  file on its first read):

| Repository | First index | Nothing changed | 1% changed | Search, median | Repo map | Cache |
| --- | --- | --- | --- | --- | --- | --- |
| 20,000 files plus 20,000 ignored, before | 213 s (ignored files included) | | | 93 ms | 1.9 s | 20 MB |
| The same, after | 15 s | 0.5 s | 0.8 s | 30 ms | 0.1 s | 9.8 MB |
| 100,002 files, after (capped at 100,000) | 76 s | 2.5 s | 4.0 s | 187 ms | 0.9 s | 49 MB |

  At 100,000 files the repo map took 59 s before the import change. The
  slowest search there took 691 ms, and the cache loaded in 0.5 s.

## September 25 Lumi account and Lumi Cloud enrollment — source only, not released

- **Settings > Lumi account** signs the app in to an organization's Lumi Cloud
  ([guide](lumi-cloud.md), `lumi/cloud.py`).
  - It follows OAuth 2.0 for native apps: the browser opens, the person
    approves, and the answer returns to a one-time listener on `127.0.0.1`,
    checked with PKCE and a state value.
  - The refresh token is kept in the credential store (`api_keys`), and the
    access token only in memory.
  - The page lists the person's organizations, roles and seats. **Sign out**
    also ends the sign-in on Lumi Cloud.
- **Use on this computer** enrolls the computer in an organization where
  the person has a seat.
  - Lumi generates an Ed25519 key. The private half stays in the credential
    store, and the device signs short assertions to get device tokens.
  - A machine policy with a `cloud` section enrolls managed computers with an
    administrator's enrollment token instead
    ([policy](enterprise-policy.md#lumi-cloud)).
- **Check-ins** run hourly in the background.
  - They send the version, the policy in force, and usage totals per model
    since the last check-in; never prompts, code or paths.
  - A new or nearly expired organization policy is downloaded, verified and
    applied. Right away, the app confirms which version is in force.
  - A revoked computer forgets its enrollment and the downloaded policy.
- **Applying the organization's policy** (`lumi/policy.py`).
  - A downloaded policy counts only when its signature verifies: against the
    keys an administrator set (with a machine policy), or the keys pinned when
    the person joined (without one).
  - With a machine policy, the cloud policy replaces its rules once it
    verifies; the machine rules apply before that and when a download fails.
  - An organization joined in the app never overrides a machine policy.
  - The running app applies a new policy at once: services, permission mode
    and file exclusions. Settings and the chat follow without a reload.
- Help text on the About page now points organizations to Lumi account.

Validation on September 25, 2026:

- `test_cloud.py`, 17 tests against a fake Lumi Cloud. They cover:
  - sign-in through the real loopback listener;
  - a callback with the wrong state, cancelling, and https addresses;
  - refresh rotation, and an ended sign-in signing out;
  - joining an organization and applying its signed policy, with the
    version confirmed at once;
  - usage totals without paths, and no seat meaning no enrollment;
  - a tampered download not applied, and a revoked computer forgetting its
    enrollment;
  - leaving on this computer;
  - managed enrollment from a machine policy, and a download signed by an
    untrusted key leaving the machine policy in force;
  - the page's status never carrying secrets.
- Full `pytest`: 3,873 passed, 3 skipped.
- End to end in the browser pane, with the real Lumi Cloud (development
  server) and this app in an isolated home:
  - Published a policy allowing only Ask and Plan, with secret scanning
    locked.
  - In Settings > Lumi account, entered the address and pressed Enter; the
    browser pane (standing in for the system browser) approved; the app
    showed the account and Acme Robotics.
  - **Use on this computer** enrolled it. Privacy & security showed the
    policy from Lumi Cloud (signed, valid for 14 days), and the mode
    picker changed from Full-auto to Ask, offering only Ask and Plan, all
    without a reload.
  - After a restart, publishing version 2 and pressing **Check in now**
    applied it, and Lumi Cloud's Devices page showed v2 at once.
  - The run found and fixed three problems:
    - the page kept an old policy snapshot until the app pushed settings
      after a policy change;
    - the page didn't redraw while the address field kept focus after Enter;
    - the fleet page showed no policy until the next hourly check-in.

## September 25 documentation site — source only, not published

- **The user and administrator guides build as a site** (`mkdocs.yml`, MkDocs
  with the Material theme, in Lumi's gold-on-night colors with light and dark
  modes). [The home page](index.md) groups them:
  - **Using Lumi**: getting started, models, enterprise sign-in, usage and
    costs, updates, macOS and plans;
  - **For organizations**: policy, Windows deployment, the audit log, running
    without a UI, and GitHub;
  - **Extending Lumi**: capability packs, skills and creative editors.
- **[Writing a capability pack](packs.md)** is new: the manifest's fields,
  agent and skill files, every hook type, how approval and digests work, and
  sharing a pack from Git.
- The **Docs** workflow builds the site with `mkdocs build --strict` on every
  pull request and keeps it as the `lumi-docs-site` artifact. It isn't
  published anywhere yet; where it goes (GitHub Pages or the Lumi website)
  needs the owner's decision. Build it locally with
  `pip install -r packaging/docs-requirements.txt` and `mkdocs serve`.
- Historical plans and release notes are built but left out of the site's
  navigation. `docs/README.md`, the repository's documentation index, isn't
  part of the site, because it and the site's home page would share an
  address.

Validation on September 25, 2026:

- `test_docs_links.py`: every page in the navigation exists, and every
  relative link in those pages points to a file in the repository.
- Full `pytest`: 3,856 passed, 3 skipped.
- MkDocs isn't installed on the development machine, so the site itself is
  built by the Docs workflow on the pull request.

## September 25 free for individuals: About Lumi and plans — source only, not released

- **Settings > About Lumi**, also opened from **Help > About Lumi**, which
  used to flash a one-line status. It shows:
  - the version, and who manages this copy (the policy's organization, or
    device management for an MSI install);
  - that the whole app is free for individuals, without an account;
  - what leaves the computer: prompts, code and keys go only to the chosen
    model providers, and Luminary Analytics receives only the update check;
  - the MIT license and, in an installed copy, where the third-party notices
    are.
- **[Plans](plans.md)** (a draft for product review) writes the free tier down
  and lists what Lumi Cloud will add for organizations. Paid plans and prices
  aren't decided.

Validation on September 25, 2026:

- `test_about.py`: the About command's version, license, notices and
  organization under a policy.
- In the browser pane, with a pilot policy from "Example Corp", Help > About
  Lumi opened Settings > About Lumi with "Managed by Example Corp" and the
  sections above.

## September 25 getting-started checklist — source only, not released

- **A first-run checklist in the empty chat** ([guide](desktop-workflow.md#getting-started),
  `lumi/gui/onboarding.py`) replaces the static welcome card. Its three steps
  follow real state:
  - **Connect a model**, with a shortcut to Connections;
  - **Open a project**, with the folder picker, or **Try the sample
    project**, which creates `Documents/Lumi Projects/lumi-sample`, a tiny
    Python program with a planted bug, and opens it;
  - **Finish a first task**: **Use a suggested task** fills the composer
    without sending. The first turn that ends without an error sets
    `onboarding.first_task_done`.
- The **×** hides the checklist for good (`onboarding.dismissed`). The page
  can't mark the task done itself. The checklist moves the empty chat up so
  short windows show it.

Validation on September 25, 2026:

- 4 tests in `test_onboarding.py`:
  - the sample project and its planted bug;
  - never overwriting an existing sample;
  - what counts as a finished turn;
  - the settings and socket commands, including refusing
    `first_task_done` from the page.
- Full `pytest`: 3,853 passed, 3 skipped. A contract test that found the old
  card's comment now looks for the checklist's.
- In the browser pane, from a fresh isolated home with the scripted Ollama
  stub as the only model:
  - the checklist showed the model step done;
  - **Try the sample project** created and opened `lumi-sample`;
  - **Use a suggested task** filled and focused the composer;
  - Enter ran the turn, the task step was ticked, and `first_task_done` was
    saved;
  - a new session showed no checklist;
  - in a second fresh run, **×** hid it and saved `dismissed`;
  - at 1280×720 the whole checklist was above the composer. In the 417-pixel
    browser pane it needs a scroll.

## September 25 image descriptions for text-only models — source only, not released

- **A vision model describes images for a chat model that can't see them**
  (`lumi/engine/image_descriptions.py`, [guide](models.md#images-for-models-that-cant-see-them)).
  - When the chat model's capabilities have no vision and **Models for
    roles** has a `vision` model, attached pictures and tool screenshots are
    described before the request.
  - Each image is described once (at most six per step), and the description
    is saved with the image and the model that wrote it.
  - Text-only adapters send `[Image: …, described by provider:model]` plus the
    description. These are the Ollama tool screenshots, custom connections
    with **Models accept images** off, and SONN's text transport, which still
    says a notice isn't evidence when there is no description.
  - The conversation shows a notice. Usage records the requests as
    `image_description`.
  - Two failures leave the image with its notice.
- Custom connections with **Models accept images** off now send text in place
  of images, in messages and tool screenshots alike. Connections without the
  setting keep sending images, as before.

Validation on September 25, 2026:

- 9 tests in `test_image_descriptions.py`:
  - finding undescribed images;
  - the labelled fallback;
  - a turn where a stand-in vision model describes an attached image once,
    records `image_description` usage, and isn't asked again;
  - tool screenshots;
  - nothing is asked without a vision model, or when the chat model sees;
  - failures and the retry cap;
  - a gateway without vision getting text for both images;
  - a gateway with vision still getting the image;
  - SONN passing a labelled description.
- The new conversation notice mirrors the fallback notice. It wasn't checked
  in the browser, because attaching an image needs a native file dialog the
  browser pane can't drive.

## September 25 capability packs from Git — source only, not released

- **Settings > Capability packs > Install from Git** (`lumi/engine/pack_install.py`, [guide](desktop-workflow.md)):
  - installs a pack from a public https repository, pinned to one commit;
  - a tag or branch is resolved to the commit it names now;
  - only that commit is fetched (depth 1, no submodules, no credential
    helper or prompt, links checked out as plain files);
  - the pack arrives turned off in `~/.lumi/packs/<id>` and runs only after
    the usual review and approval;
  - reinstalling at another commit drops the earlier approval, and **Remove**
    deletes a pack installed this way;
  - `plugins[<id>].source` records the URL, commit and folder.
- **Policy:** `extensions.allowed_sources` limits the repositories packs may
  come from.
- **Audit:** `extension.install` and `extension.remove`, with the commit.
- Refused: non-https addresses, credentials in the URL, folders with `..`,
  manifests without an `id`, and symbolic links.

Validation on September 25, 2026:

- 18 tests in `test_pack_install.py` (one is skipped on Windows), against
  local git repositories:
  - addresses and allowed sources;
  - lightweight and annotated tags, branches and commits resolving;
  - a pinned install that ignores newer commits, leaves no `.git` or scratch
    folders, and is discovered untrusted;
  - refusals;
  - a pack in a subfolder;
  - the Settings flow through the socket: install, approve, reinstall (back
    to needing approval), remove, and the audit records.
- In the browser pane (isolated home; a local repository stood in for the
  https remote, allowed only by the fixture):
  - an `http://` address was refused on the page, with the typed values
    kept;
  - the local repository at tag `v1` installed as "Not approved · off", with
    "Installed from … at commit 15d389f6ff93" and its hook listed;
  - **Approve and enable** made it "Approved · active";
  - **Remove** deleted it from `~/.lumi/packs` and from the settings.
  - This check found that a refusal wasn't shown while the form kept focus;
    install results now always redraw the page.

## September 25 macOS app and DMG — source only, not released

- **`Lumi.app` in `lumi-X.Y.Z.dmg`** for Apple silicon ([guide](macos.md)):
  - `packaging/build_macos.sh` builds it from the same hash-pinned lock (with
    its macOS PyObjC wheels) and `packaging/lumi.spec`, then checks it against
    `packaging/bundle-policy-macos.json`;
  - the spec now picks pywebview's WebKit backend on macOS and leaves out the
    Windows-only WinSparkle;
  - the app carries its version, a minimum of macOS 12, and usage strings for
    the microphone and automation.
- **Signing and notarization** (hardened runtime,
  `packaging/macos/entitlements.plist`, `notarytool`, stapling) run when a
  Developer ID and Apple credentials are configured as secrets; otherwise the
  build is unsigned and says so.
- **CI:** `.github/workflows/build-macos.yml` builds on `macos-latest` and
  starts the app: `--version`, `lumi updates`, and the GUI server with the
  page, the launch code and the WebSocket token check. It keeps the DMG as an
  artifact. Nothing is published.
- **Search without ripgrep** (macOS and Linux) now uses `grep -E`, so
  alternation, `+` and groups mean what they do in ripgrep.
- Third-party notices list ripgrep and WinSparkle only in Windows builds.

Validation on September 25, 2026, from PR #23's macOS build (`macos-latest`,
Apple silicon):

- The bundle was 85.4 MiB (232 files) and passed its policy. The DMG was
  32 MB, unsigned, with the expected warning.
- `lumi --version` and `lumi updates` ran.
- The GUI server served the page (52 KB), redeemed the launch code, refused
  the WebSocket without the token (403) and accepted it with the token (101),
  and its log was clean.
- The first run found that the launch link stayed in a frozen app's stdout
  buffer; those lines are now flushed.
- No Mac was available locally, so the native window, dictation, computer use
  and the Keychain are untested.

## September 25 updates that wait for running turns — source only, not released

- **An update never cuts off an agent turn** (`lumi/updater.py`, [guide](updates.md#installing-an-update)):
  - WinSparkle asks whether Lumi can close before running a verified
    installer. While a turn runs, Lumi says no, and WinSparkle holds the
    installer back.
  - Otherwise Lumi closes itself once the installer starts: the window closes
    and the local server stops, instead of the installer forcing it closed.
  - When Lumi can't tell whether a turn is running, it keeps the turn.
- **Update events in the audit log:** `update.check` (found, none, error),
  `update.deferred`, `update.install`, `update.skipped`, `update.postponed`
  and `update.cancelled`, with the version and feed.

Validation on September 25, 2026:

- 5 tests in `test_updates.py`:
  - the callbacks are registered before WinSparkle starts;
  - an update waits for a running turn, then closes the app;
  - an unknown state keeps the turn;
  - each event is recorded;
  - the vendored `WinSparkle.dll` exports every function Lumi declares (it is
    loaded, and nothing in it is called).
- The callbacks were called directly in these tests. A real WinSparkle update
  hasn't been installed with them yet, because that needs a published release.

## September 25 MSI for Intune, Configuration Manager and Group Policy — source only, not released

- **`lumi-X.Y.Z.msi`** ([guide](deploy-windows.md), `packaging/lumi.wxs`,
  `packaging/build_msi.ps1`, WiX 5):
  - per machine into `Program Files\Lumi`, silent with
    `msiexec /i … /qn`, with a Start menu shortcut;
  - upgrades in place (fixed upgrade code; a rebuild of the same version
    replaces it too);
  - removed with `msiexec /x`.
- **`POLICYFILE=…`** sets the machine policy's `PolicyFile` value on install and
  removes it on uninstall.
- **MSI copies never update themselves.** The package puts `lumi-install.json`
  beside `lumi.exe`, which turns updates off whatever settings or policy say.
  Settings > Updates and Check for Updates say why.
- **The EXE and the MSI refuse each other,** so one folder never has two
  installers.
- **`lumi updates`** prints the update settings in effect as JSON, for
  administrators and detection scripts.
- **Release workflow:** stable tags build, Authenticode-sign (when configured)
  and attach the MSI, and the download page offers it to administrators.
  **Build check:** every change builds the MSI, installs it silently on the
  Windows runner, checks it, and uninstalls it.

Validation on September 25, 2026:

- 7 tests in `test_msi_package.py`. They check that the package, the EXE
  installer, the policy reader and the update settings agree:
  - per-machine scope, the upgrade code and the shortcut;
  - the EXE's AppId in the launch condition;
  - the policy key for `POLICYFILE`;
  - the marker `build_msi.ps1` writes, and that it turns updates off;
  - the Check for Updates message;
  - `python -m lumi updates`.
- `test_publish_pages.py` covers hosting the MSI and the download page link.
- WiX isn't installed on the development computer, so the MSI is built only
  in CI. On PR #21's build check (WiX 5.0.2 on `windows-latest`), the MSI
  (29.4 MB) was built and installed silently with `POLICYFILE`. The files,
  marker, shortcut and policy value were present. The installed
  `lumi updates` reported `mode=off installed_by=msi channel=beta
  managed_by=CI`, and the MSI uninstalled cleanly.
- Not yet deployed through a real Intune tenant, Configuration Manager site or
  Group Policy.

## September 25 update channels, pins and turning updates off — source only, not released

- **Settings > Updates** ([guide](updates.md), `lumi/update_channels.py`), under
  Advanced:
  - **Check for updates:** automatically (once a day and on demand), only
    when asked, or never;
  - **Channel:** stable or beta;
  - **Stay on release line:** a pin such as `0.20`, which takes only that
    line's stable releases.

  These are read at startup like the policy. The page shows the installed
  version, the feed in use, the last check, and what applies after a restart.
- **Policy:** `updates.mode`, `updates.channel` and `updates.pin` can be locked.
  A value Lumi can't apply makes the policy invalid. An invalid machine policy
  pauses automatic checks.
- **Check for updates** says why nothing happens: updates are turned off by
  the organization or in Settings, will start after a restart, or this copy
  runs from source.
- **Running from source** no longer loads WinSparkle. Before, development runs
  and fixtures wrote the real HKCU WinSparkle key and could show its dialogs.
  `LUMI_UPDATER_FROM_SOURCE=1` restores the old behavior for testing the
  updater.
- **Release pipeline:**
  - `update_appcast.py` now writes `appcast.xml` (stable, unchanged address),
    `appcast-beta.xml`, and `appcast-X.Y.xml` for the four newest release lines.
  - Beta tags (`vX.Y.Z-beta.N`, `-alpha.N`, `-rc.N`) reach only the beta feed.
    Before this change they weren't published to Pages at all.
  - `publish_pages.py` keeps the newest installer of each recent line, so
    pinned installs can download their update, and always links the newest
    stable release.
- Settings text fields, like multi-line ones, keep typed text after a refused
  save so it can be corrected.

Validation on September 25, 2026:

- 28 tests in `test_updates.py`:
  - pins, feeds, settings.json values and their mistakes;
  - policy locks and invalid policy values;
  - the WinSparkle calls for each mode, run against a recording stand-in for
    the DLL;
  - status and restart-pending reporting;
  - the from-source guard;
  - Settings commands and messages.
- 6 tests in `test_update_feeds.py`:
  - version ordering (alpha < beta < rc < release);
  - a stable release in every feed, with idempotent re-runs;
  - betas only in the beta feed and retired by their release;
  - line feeds, including a fix for an older line;
  - refusal of unsigned releases and bad versions;
  - agreement between the feeds the client reads and those the script writes.
- `test_publish_pages.py` gained tests for line retention and betas.
- Full `pytest`: 3,810 passed, 2 skipped.
- In the browser pane, with an isolated home and a pilot policy from "Example
  Corp" that locks the channel:
  - Ctrl+, opened Settings, and searching "beta" found only Updates;
  - the channel showed "Managed by Example Corp" and was disabled;
  - the status read "Lumi 0.19.2.dev11 · Automatic, from the stable channel ·
    managed by Example Corp", noted that a copy running from source doesn't
    update itself, and disabled Check for updates;
  - the pin "latest" was refused with the reason and stayed in the field;
    "0.20" saved;
  - "After Lumi restarts" changed with each save, including while the mode
    select kept focus;
  - Help > Check for Updates said the copy doesn't update itself.
- No release has been published with these feeds. The WinSparkle calls were
  exercised against a stand-in, not the real DLL.

## September 25 enterprise sign-in for connections — source only, not released

- **Sign-in instead of a key** ([guide](connection-sign-in.md),
  `lumi/auth_tokens.py`), chosen under a custom connection's
  **Authentication**:
  - **OAuth client credentials** for OpenAI-compatible gateways, OpenAI and
    Anthropic proxies and Azure OpenAI: a token URL, client id, optional scope
    and audience, and the client secret, kept in the credential store. The
    secret goes only to the token endpoint; the model endpoint gets the access
    token, fetched again a minute before it expires.
  - **Microsoft Entra ID** for Azure OpenAI: an app registration (tenant,
    client id and secret), or, without a client id, this computer's sign-in
    through `azure-identity` when installed, else the Azure CLI. A client id
    without its secret is refused rather than falling back to the computer's
    identity. The Azure CLI is found on Windows (`az.cmd`), runs without a
    console window, and gets only plain tenant and resource arguments.
  - **Client certificates** for gateways that require mutual TLS, alongside
    any sign-in, verified against the same trust store. A key protected by a
    passphrase is refused with a message instead of a hidden prompt.
- **Test connection** signs in first, so it reports the identity provider's
  reason (for example *Sign-in was refused: invalid client*) instead of
  "listed no models".
- Model listing for Anthropic and OpenAI proxies uses the sign-in token and
  certificate too.

Validation on September 25, 2026:

- 19 tests in `test_signin.py`, against simulated endpoints:
  - token exchange, caching and refresh; a refusal that doesn't echo the
    secret;
  - Entra ID through an app registration and through a simulated Azure CLI,
    including a missing CLI, a failed `az login`, a client id without a
    secret, and tenant or scope values that could be interpreted by cmd.exe;
  - certificates generated in the test: loading, a bad file, a key with a
    passphrase;
  - a real TLS handshake with a local server that requires a client
    certificate from its CA: models are listed with the certificate, and
    the same trust without it is turned away;
  - connection validation for the new fields;
  - a gateway, an Anthropic proxy and an OpenAI proxy that receive the token
    and never the secret;
  - Azure OpenAI with Entra ID and a certificate, including its health check;
  - Test connection reporting a refused sign-in.
- Full `pytest`: 3,773 passed, 2 skipped.
- In the browser pane, with an isolated home, a simulated identity provider
  and a local HTTPS gateway that requires a client certificate:
  - a connection with OAuth client credentials and a certificate was added
    through the form. **Test connection** with a wrong secret showed "Sign-in
    was refused: Invalid client secret"; with the right one, "Connected · 1
    model available: gateway-coder";
  - after saving, the row read "secret stored", the secret was absent from
    the saved connection, and editing showed every sign-in field with
    "Stored client secret — leave blank to keep it";
  - a turn with `gateway-coder` read `app.py` and finished. All five gateway
    requests carried the token and the client certificate and none carried
    the secret; one token request served them all;
  - on Azure OpenAI with Entra ID, the client secret field appeared while a
    client id was typed and hid when it was cleared; Tab moved on to Scope.
  - This check found two form bugs, now fixed: the sign-in fields broke the
    form's rendering, and re-rendering on leaving the client id field lost
    keyboard focus.
- Not yet tried against a real identity provider, Entra tenant or mTLS
  gateway.

## September 25 fallback models, roles and capability overrides — source only, not released

- **Fallback models** ([guide](models.md)): **Settings > General > If the
  model fails, continue with** (`general.fallback_models`, up to five
  `provider:model` lines).
  - A request that fails before any answer (provider error, overload, a
    connection failure) retries the same step with the next usable model.
  - The conversation shows a notice, and the audit log records
    `model.fallback`.
  - Fallbacks the policy blocks, that an organization budget can't price, or
    that can't be built are skipped. The next turn tries the chosen model
    again.
  - `lumi run --fallback provider:model` adds to the list.
- **Models for roles:** **Settings > General > Models for roles**
  (`role provider:model` lines) feed the existing role router. A `summarize`
  model now names sessions and compacts long conversations. Titles no longer
  need the chat model to be a native one, so a session using Codex or Claude
  Code can be named by a local model. Title requests are recorded with their
  conversation.
- **Capability overrides:** policy `models.capabilities` states a model
  pattern's context window, vision, tools, reasoning levels, computer use or
  concurrency. The override wins over name inference and runtime reports.
- **Settings fields for lists:** multi-line fields, with typed text kept after
  a refused save. A successful save clears the error alert at once.

Validation on September 25, 2026:

- 13 new tests in `test_model_fallback.py`:
  - a fallback after a provider error and after a crashed stream;
  - skipped fallbacks (policy, a budget, a factory that fails);
  - no fallback after partial output;
  - `lumi run --fallback`;
  - list parsing, and the socket's normalization;
  - the summarize model naming a CLI-backed session;
  - capability overrides, and invalid ones refusing the policy.

  The title tests' stand-ins take the new usage context.
- Full `pytest`: 3,754 passed, 2 skipped.
- In the browser pane, with an isolated home, the chosen model on a
  connection to a closed port, and `ollama:stub:latest` as the fallback:
  - the turn showed "conn-broken:claude-sonnet-5 failed (Broken gateway
    connection failed: ConnectError); continuing with ollama:stub:latest."
    and finished with changed files and no error;
  - in Settings > General, invalid lines were refused with an alert and kept
    for fixing; corrected lines saved and cleared the alert.

## September 25 GitHub pull requests — source only, not released

- **GitHub tools** (`lumi/engine/github_tools.py`, [guide](github.md)), loaded
  on demand through `search_tools`:
  - `github_pr_view`: the branch's pull request, with reviews, review comments
    (ids, file:line, outdated), the conversation and every check with its job
    id;
  - `github_check_log`: the end of a GitHub Actions job's log, plus earlier
    error lines;
  - `github_pr_create`: pushes the branch, never forced, and opens the PR;
  - `github_pr_comment`: a comment, or a reply in a review thread;
  - `github_pr_update`: the title, the description, or ready for review.
- The two reading tools are approved like `git_log`. The three that change
  things ask first under Auto accept edits and Ask permissions.
- Works with github.com and GitHub Enterprise Server (`/api/v3`, or
  `GITHUB_API_URL`), with the repository taken from `origin`.
- The token is **Settings > API keys > GitHub token** (credential store), or
  `GITHUB_TOKEN`/`GH_TOKEN`. It goes only into request headers. Job-log
  downloads that redirect to other hosts don't carry it, and error messages
  never echo credentials from a remote URL.

Validation on September 25, 2026:

- 12 new tests in `test_github_tools.py`, against a mocked GitHub API and a
  temporary repository:
  - remote parsing, Enterprise hosts, and a foreign remote refused without
    echoing its password;
  - the PR view and a job log, where the token isn't sent to the log
    redirect's host;
  - a missing token;
  - opening, commenting, replying, updating and marking ready;
  - a real `Session.run` in auto-edit: the view ran unasked, the comment asked
    and was refused.
- Full `pytest`: 3,741 passed, 2 skipped.
- In the browser pane: searching Settings for "GitHub" found the new
  **GitHub token** field, and a typed token was saved (shown as Stored, the
  value never sent back to the page).

Not exercised: the real GitHub API.

## September 25 headless runs — source only, not released

- **`lumi run`** (`lumi/headless.py`, [guide](headless.md)) runs one task
  without a UI, for servers, containers and CI. Options:
  - the prompt from an argument, stdin (`-`) or `--prompt-file`;
  - `--provider`/`--model` (or `LUMI_PROVIDER`/`LUMI_MODEL`, or the desktop
    defaults);
  - `--mode ask|auto-edit|bypass` (default `auto-edit`);
  - `--trust-project`;
  - `--max-requests`, `--timeout`;
  - `--output json|text|jsonl`.

  Nothing asks a person: a disallowed tool call is refused, and a budget that
  needs approval stops the run. The run builds its session like the app does:
  - policy modes and models;
  - execution rules;
  - exclusions;
  - repository instructions only when the project is trusted or
    `--trust-project` is given.

  Budgets, usage records, the audit log and the secret scan apply.
- **The result** is JSON: status, the engine's outcome, text, errors, changed
  files, checks, tool calls, refused calls, model requests, usage and cost.
  Exit codes:
  - 0: completed;
  - 1: failed;
  - 2: the command or setup is wrong;
  - 3: stopped for a person (needs input, incomplete, refused an action, a
    budget, the request limit or the timeout).
- **Container:** `packaging/docker/Dockerfile` builds a `lumi` image with git
  and ripgrep, a non-root user and `LUMI_KEYCHAIN=off`. A new **Container
  build** workflow builds it and checks `lumi run` inside it. Nothing is pushed
  to a registry.
- The execution-rule layering (tier, the project's `lumi-policy.json`,
  organization shell rules first) moved from the GUI to
  `engine/policies.project_execution_policy`, so the app and `lumi run` share it.

Validation on September 25, 2026:

- 10 new tests in `test_headless.py`:
  - key sources, and refused setups;
  - a completed run with a changed file and its cost;
  - a refused edit reported as `needs_attention`;
  - a provider failure;
  - a budget stop, and a timeout;
  - the `jsonl` and `text` outputs, and stdin;
  - policy refusals (exit 2);
  - repository instructions only with trust.
- Full `pytest`: 3,729 passed, 2 skipped.
- `python -m lumi run` as a separate process against the scripted Ollama stub,
  in a throwaway home:
  - `--mode bypass` edited `app.py` and exited 0 with `completed`;
  - `--mode ask` exited 3 with `needs_attention` and one refused call;
  - both runs wrote audit and usage records;
  - missing provider and key setups exited 2 with a message.

Not exercised: a real model, and the container build locally. The Container
build workflow checks the image in CI.

## September 25 budgets — source only, not released

- **Budgets** (`lumi/budgets.py`, [guide](usage-and-costs.md#budgets)) act on
  the priced spend in the usage records. There are three thresholds:
  - an alert, shown once per period as a notice in the conversation;
  - an approval point, where Lumi asks "…continue anyway?" before the next
    model request. It asks once per period, or once per turn for a per-turn
    budget. **Stop**, or a run that can't ask (the gateway, a delegated
    worker), stops the turn;
  - a stop. A spent day or month budget refuses new turns up front, and a
    per-turn cap stops before the next request with "send Continue to go on".
- **Your limits:** **Settings > Usage & cost** adds **Ask before spending more
  than ($ per day)** and **Stop a turn after spending ($)** beside the existing
  daily alert. The alert is now enforced by the engine for the GUI, terminal UI
  and gateway alike; it used to be a GUI-only message. The page lists every
  budget with this period's spend and refreshes after a save.
- **Organization budgets:** policy `budgets` apply per user, per project (a
  folder glob) or per turn, by day or month. `block_unpriced` refuses unpriced
  models while that budget applies. Alerts, answers and stops are audited as
  `budget.warning`, `budget.approval` and `budget.block`.
- **Prompt fix:** the "needs your input" card kept only the sentences with a
  question mark, and split them at decimal points. A question with "$0.27" or
  "Python 3.12" showed only its tail. Sentences now end only at punctuation
  followed by a space.

Validation on September 25, 2026:

- 12 new tests in `test_budgets.py`:
  - rule parsing, and settings turned into rules;
  - levels by scope, user and project;
  - real `Session.run` turns: an alert shown once, the question answered with
    Continue, Stop, or unanswerable, a spent budget refusing a turn, the
    per-turn cap, and `block_unpriced`;
  - limit validation, and the budgets in the costs payload.

  One new node test covers the prompt's question text.
- Full `pytest`: 3,719 passed, 2 skipped.
- In the browser pane, with an isolated home, a policy budget and prices set
  so each stub call cost $0.07–$0.10:
  - the first turn showed "Today's spend is $0.07, past your $0.05 alert.";
  - the next turn asked "Today's spend is $0.27, past your $0.20 limit —
    continue anyway?". Continue resumed it; Stop ended it with "Stopped at your
    request…";
  - Usage & cost listed the organization's and the person's budgets with
    spend, and showed a new per-turn limit after it was saved.

## September 25 usage records and prices — source only, not released

- **Usage records** (`lumi/usage.py`, [guide](usage-and-costs.md)): one JSON
  line per model call in `~/.lumi/usage/YYYY-MM.jsonl`. Each record has:
  - the user, project, conversation and worker;
  - the purpose: `turn`, `subagent`, `title` or `compression`;
  - provider and model;
  - tokens, with cache reads and writes, normalized across providers
    (Ollama's `prompt_eval_count`, Claude Code's separate cache counts,
    Codex's `cached_input_tokens`);
  - the reported and computed cost, and the price source.

  Turns are recorded in `Session.run`, and auxiliary requests in
  `auxiliary_stream`, so titles and compaction are counted now.
- **Prices** (`lumi/pricing.py`) resolve in this order:
  - the provider's reported cost;
  - an organization policy's new `pricing.prices`;
  - the user's prices;
  - local models at $0;
  - subscriptions (Codex, Claude Code, Ollama cloud);
  - a bundled list, checked on September 25, 2026 against Anthropic's and
    OpenAI's published pages.

  The list corrects stale entries: `o3` was $10/$40 and is now $2/$8;
  `gpt-5.4` was $5/$20 and is now $2.50/$15; bare `opus` matched Opus 4's
  $15/$75. Cache writes are priced.
- **Unknown models are unpriced, never $0.** They used to fall back to free.
  Settings counts them separately, and the model table labels them.
- **Settings > Usage & cost** adds this month's calls by model and a **Prices**
  editor. Lines have the form `pattern input output [cached] [write]`, and
  prices an organization sets are listed there too.
- **`lumi usage`** prints a monthly summary by model, provider, project,
  purpose, user or session, or exports CSV or JSONL.
- **Audit fix:** a delegated worker's tool calls and errors, which the parent
  passes on for display, were recorded a second time under the parent. A
  worker's error also marked the parent turn as failed. The worker's own turn
  now records them once. `model.usage` audit records carry the cost and price
  source.
- **One set of totals:** Settings' Today, session and total figures now come
  from the usage records, so titles and compaction count too. Subscription
  calls are counted apart from unpriced ones.
- **Settings errors are visible:** a refused settings change (a bad price
  line, an invalid collector URL, a setting the organization manages) used to
  go to the hidden chat stream. It now shows as an alert on the Settings page,
  and the typed prices stay in place to fix.

Validation on September 25, 2026:

- 22 new tests in `test_pricing_usage.py`:
  - price order and cache pricing;
  - organization, user, local and subscription prices;
  - price lines, and a policy with bad prices;
  - token normalization;
  - the ledger with a second writer;
  - priced session turns, and a recorded title request;
  - worker events recorded once;
  - the costs command, the settings validation and the export;
  - the app counting every recorded call.
- Full `pytest`: 3,707 passed, 2 skipped.
- In the browser pane, with an isolated home and the stub serving an
  Anthropic-format connection (`claude-sonnet-5`), an OpenAI-compatible
  connection (`house-model`) and Ollama (`stub:latest`):
  - the records were priced from the catalog with cache reads at the cache
    rate, as unpriced, and as local $0, and the title request was recorded
    with purpose `title`;
  - Usage & cost showed matching totals ($0.0004 for 154 tokens) and the
    unpriced call;
  - a bad price line showed an alert and kept the text. After the fix, the
    next `house-model` calls were priced from the override.

## September 25 audit log — source only, not released

- **Audit log** (`lumi/audit.py`, [guide](audit-log.md)): hash-chained JSON
  lines in `~/.lumi/audit/`, one file per UTC day. `Session.run` now wraps the
  loop (`_run_turn`) and records every turn from the GUI, gateway, terminal UI
  and workers:
  - turn start and end with outcome;
  - model usage, with each provider's token names normalized;
  - tool calls and results;
  - file changes, including Codex's;
  - user and hook approvals;
  - secret redactions and errors.

  Settings changes (key names only) and project trust decisions are recorded
  too.
- **Tamper evidence:** each record hashes the previous one. **Settings >
  Privacy & security > Audit log status** verifies the whole chain. The GUI,
  terminal UI and gateway share one chain through an OS file lock.
- **Capture levels** (`privacy.audit_capture`):
  - metadata only (the default): names, outcomes, sizes, paths and digests;
  - redacted content;
  - full content.

  Saved keys are removed at every level.
- **Retention:** `privacy.audit_retention_days` (default 365) is separate from
  transcript retention; `privacy.audit_log` turns recording off.
- **OpenTelemetry export:** `audit.otlp_endpoint`, `audit.otlp_auth_header` and
  an `api_keys.otlp` token send spans over OTLP/HTTP JSON. They use GenAI
  semantic-convention attributes and a bounded background queue that drops
  instead of blocking. An organization policy can lock all of these settings.

Validation on September 25, 2026:

- 27 new tests in `test_audit_log.py`:
  - the chain: edits, removals, reordering, a second writer, threads, odd
    text, large records, day rollover;
  - capture levels and retention;
  - span attributes, batching, a failing collector, the background thread,
    and starting and stopping the export;
  - real `Session.run` turns: file change, usage, denial, stop, error, and a
    broken log;
  - the socket's validation of the new settings, and change records that name
    only keys that really changed.
- Full `pytest`: 3,686 passed, 2 skipped.
- In the browser pane, with an isolated home, the scripted Ollama stub and a
  local HTTP server standing in for a collector:
  - a turn produced ten chained records;
  - the collector received the batches with the `Bearer` token header and
    GenAI span attributes;
  - Privacy & security showed "Verified: 9 records". After one record was
    edited on disk, **Verify again**, pressed with the keyboard, reported
    "2026-09-25.jsonl:6 was changed";
  - after switching the capture level to redacted, a prompt's GitHub-format
    token was kept only as `[REDACTED GitHub token]`;
  - leaving a field unchanged recorded nothing.

Not exercised: a real OpenTelemetry collector product, and the packaged app.

## September 25 organization policy — source only, not released

- **`lumi.policy/v1`** (`lumi/policy.py`, [administrator guide](enterprise-policy.md)):
  a machine-wide policy from the registry (Group Policy/Intune), macOS managed
  preferences (a configuration profile), `ProgramData`, `/Library/Application
  Support` or `/etc`. `LUMI_POLICY_FILE` applies only when none of those exists,
  so a user can't replace their organization's policy.
- **Locked settings:** policy values win over the user's. Settings shows them
  disabled with "Managed by <organization>", and the app's settings commands
  refuse changes. This locks the secret scan, retention, the Codex/Claude Code,
  computer use and gateway switches, the network settings and others.
- **Enforcement:**
  - allowed permission modes (others hidden; a saved default outside the list
    becomes the first allowed mode);
  - allowed and blocked `provider:model` patterns (removed from the model menu
    and refused at spec construction and at the start of every turn);
  - extra file exclusions;
  - shell rules checked before built-in and repository rules;
  - MCP server allowlist and a switch for command-based servers;
  - capability pack allowlist.
- **Signed policies:** Ed25519 over the policy's canonical JSON, verified against
  keys only an administrator can set (registry `PolicyKeys`, `policy-keys.json`
  or the machine policy's `trusted_keys`). A signed policy with `expires_at`
  stays enforced for `grace_days` offline, then Lumi refuses model requests. An
  invalid policy also refuses requests instead of silently meaning "no policy".
- **Administrative templates:** `packaging/policy/lumi.admx` with
  `en-US/lumi.adml`, a macOS `lumi-policy.mobileconfig` and `example-policy.json`.
- Settings > Privacy & security starts with an **Organization policy** summary.
- `cryptography` is a core dependency (for Ed25519), and the release lock was
  regenerated.

Validation on September 25, 2026:

- 28 new tests: `test_policy.py` (parsing, signatures and tampering, expiry,
  source precedence, invalid policies, templates) and `test_policy_enforcement.py`
  (locked settings and socket refusal, modes, models in the UI data, org shell
  rules, exclusions, MCP, packs, refused turns).
- Full `pytest`: 3,659 passed, 2 skipped.
- In the browser pane, against an isolated home with a policy from
  `LUMI_POLICY_FILE` and a stub offering an allowed and a blocked model:
  - the permission menu offered only Ask and Auto accept edits (a saved
    Full-auto default became Ask);
  - the model list dropped the blocked model;
  - Privacy & security opened with the policy summary, and locked fields were
    disabled with "Managed by Example Corp" (the scan showed on although the
    saved value was off).

Not exercised: a real Group Policy or MDM deployment, signed delivery from Lumi
Cloud (not built yet), and the packaged app.

## September 25 client security: file exclusions, project trust, retention and tool switches — source only, not released

- **Files Lumi never reads** (`lumi/engine/exclusions.py`): gitignore-style
  patterns in **Settings > Privacy & security**, plus a project's `.lumiignore`.
  Both apply at once when edited.
  - File tools refuse excluded files, including `batch` children, and so does a
    search rooted at an excluded folder.
  - glob, grep, git status and git diff leave them out and say how many were
    hidden. The codebase index skips them.
  - `@file:` attachments explain the refusal instead of attaching.
    `@diff:working` excludes them from the diff text.
  - Opening a `file://` page in the browser tools now goes through the same
    sandbox and exclusion checks; before, it could read any local file.
  - Shell commands can still open excluded files.
- **Project trust** (`lumi/gui/workspace_trust.py`): until the user trusts a
  project, Lumi doesn't use what the repository brings:
  - its instruction files (AGENTS.md, LUMI.md, CLAUDE.md and similar), including
    in a Mission's first message;
  - its committed notes (`.lumi/memory.json`) and codebase summary;
  - the `allow` rules in its `lumi-policy.json`, which previously let a cloned
    repository skip approval prompts (its deny and ask rules still apply);
  - automatic lint and test runs, which execute the repository's own code.

  A banner in the chat offers **Trust this project** or **Keep restricted**.
  A policy file that changes after trust needs review again. Projects already in
  Recent projects are trusted on first run, so upgrading changes nothing for
  existing work.
- **Transcript retention** (`lumi/gui/retention.py`): "Delete transcripts after
  (days)" deletes everything holding conversation content that was last touched
  before then, at startup and daily:
  - sessions and their ledgers;
  - drafts, checkpoints, worker records, artifacts, traces and mission audit logs;
  - recordings and dated logs.

  The open session is never deleted. The default keeps everything.
- **Tools outside Lumi's own loop:** switches for Codex and Claude Code (they
  leave Models; a selected one falls back to another provider), computer use
  (tools hidden and refused) and the chat gateway (`lumi gateway` refuses to
  start). Organization policy will be able to lock these.
- `@diff:` mentions no longer pass a selector starting with `-` to git, where
  `--output=<file>` would have written a file.

Validation on September 25, 2026:

- New tests: `test_exclusions_and_trust.py`, `test_client_security.py`,
  `test_retention.py`. They cover the real tools, git, the context broker,
  project notes across a trust change, the policy builder and `AppState` wiring.
- Full `pytest`: 3,631 passed, 2 skipped.
- In the browser pane against an isolated home with a scripted model:
  - The trust banner appeared for a project with AGENTS.md and a policy allow
    rule.
  - `.env` was excluded through Settings: the model's read of it was blocked
    with the rule named, and the file's value never reached the model.
  - **Trust this project** hid the banner, and the next request carried
    AGENTS.md (it hadn't before). The decision persisted with the policy
    digest.

Not exercised: organization policy locking these settings (next group), and
the packaged app.

## September 24 supply chain: pinned dependencies, audit, notices, SBOM and signing — source only, not released

- **Pinned, hash-checked release builds:** `scripts/build_clean.ps1` installs
  `packaging/requirements-release.txt` with `--require-hashes` instead of resolving
  `.[gui,desktop]` at build time. `python scripts/lock_release.py` (uv) regenerates
  it and the CI tools lock for every supported platform and Python version, and a
  test fails when `pyproject.toml` declares a dependency the lock does not pin.
- **Vulnerability audit:** a **Dependency audit** workflow runs pip-audit on every
  pin when the locks change and weekly. `packaging/audit_locks.py` removes platform
  markers first; otherwise pip-audit silently skips packages for other operating
  systems.
- **Third-party notices:** `THIRD_PARTY_NOTICES.txt` is generated from the build
  environment with each package's license texts, plus ripgrep, WinSparkle, the web
  assets and fonts, the Python runtime and PyInstaller's bootloader. It ships in
  the bundle (required by the bundle policy) and with each release.
- **License gate:** the build fails if a shipped Python package is GPL, AGPL or
  LGPL without a recorded review. The first run found two, both optional
  PyAutoGUI helpers that Lumi never calls: **MouseInfo** and **PyMsgBox** (GPL-3.0).
  They are now excluded from the bundle.
- **CycloneDX SBOM:** `build_clean.ps1 -SbomPath` writes a CycloneDX 1.6 SBOM of
  the build environment plus the bundled non-Python components, validated against
  the schema. The build check uploads it with the notices; releases attach both.
- **Authenticode:** the release workflow signs `lumi.exe` and the installer
  through `packaging/sign_windows.ps1` when a PFX or a cloud/hardware signing
  command is configured. The installer is signed before its EdDSA update
  signature. Without credentials the release continues unsigned with a warning.
  No certificate is configured yet, and macOS notarization waits for a macOS
  build pipeline.

Validation on September 24, 2026:

- 14 new tests in `test_release_supply_chain.py`: lock coverage and hashes,
  component versions against the fetch scripts, notices, the license gate, SBOM
  additions, marker stripping, and signing without credentials.
- A local `build_clean.ps1 -SbomPath` run from the locks built a 61.6 MiB bundle.
  - The notices listed 44 packages and 8 other components.
  - MouseInfo and PyMsgBox were absent from PyInstaller's module list.
  - The SBOM had 59 components and passed schema validation.
  - A scratch copy of `lumi.exe` reported its version.
- pip-audit on every pin of both locks: no known vulnerabilities.

Not exercised: Authenticode signing with a real certificate (none exists), the
release workflow itself (it runs only on a tag), and the new CI jobs until this
PR's checks run.

## September 24 network trust, credential store and secret hygiene — source only, not released

- **Corporate networks** (`lumi/net.py`, **Settings > Connections > Network**):
  - TLS is verified with the operating system's certificate store (`truststore`),
    so a company root certificate used for TLS inspection works without exporting
    PEM bundles. A toggle turns this off.
  - A configured proxy is exported as `HTTPS_PROXY`/`HTTP_PROXY`, and a bypass list
    is added to `NO_PROXY`. Local addresses always connect directly.
  - Clearing the proxy restores the variables the user's own environment had.
  - Proxy URLs with a user name or password are refused; authenticating proxies
    need a machine-level proxy or a local helper.
- **API keys in the OS credential store** (`lumi/secrets_store.py`): keys saved in
  Settings go to Windows Credential Manager, the macOS Keychain or the Secret
  Service (service `Lumi`), and `settings.json` keeps the placeholder
  `__keychain__`.
  - Existing plaintext keys move on first load.
  - Settings says where keys are kept.
  - The store is never used while settings live in the legacy `~/.resonant`
    folder: an older SONN Client sharing that folder would read the placeholder as
    its key.
  - `LUMI_KEYCHAIN=off` and `LUMI_KEYCHAIN_SERVICE` control it.
- **Keys stay out of child processes:** the agent's shell, hooks, stdio MCP servers,
  managed jobs and previews, the Python REPL, automatic tests and lint, and
  acceptance checks start without Lumi's model-provider keys
  (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and the others in
  `secrets_store.PROVIDER_KEY_ENV`). An MCP server's own `env` entries still apply.
  Codex and Claude Code keep their environment.
- **Secrets removed before each model request** (`lumi/secret_scan.py`):
  - The values of saved keys, sensitive settings and provider keys in the
    environment are always replaced in tool output. Values shorter than 16
    characters are ignored.
  - **Settings > Privacy & security > Scan for secrets** (off by default) also
    replaces well-known credential formats in tool output and in your messages:
    cloud and platform keys, tokens, private keys, passwords in connection
    strings and `.env` lines.
  - The model sees `[REDACTED <kind>]`, and the chat shows a note saying what was
    removed. Codex and Claude Code read files through their own tools and are not
    scanned.
- **Diagnostics redact by value:** **Help > Save diagnostics** removes the actual
  values of saved keys (including keys in the credential store) wherever they
  appear, in addition to the existing patterns. The embedded `settings.json` is
  masked field by field, so a triager still sees which providers were configured.
- Settings no longer rebuilds a form under a click. After a toggle or menu saved,
  clicking straight into a text or key field lost the typing, because the
  deferred refresh ran before the clicked field had focus.
- `truststore` and `keyring` are now core dependencies. `packaging/bundle-policy.json`
  requires keyring's entry-point metadata in the bundle, because without it the
  frozen app would silently keep keys in `settings.json`.

Validation on September 24, 2026:

- 53 new tests: `test_secret_hygiene.py`, `test_network_settings.py`,
  `test_secret_scan.py`.
  - The credential store is an in-memory keyring.
  - The shell, a hook and an MCP server are checked for the key they must not
    receive.
  - Two turns with a scripted backend confirm that the model never received a
    planted token or a saved key.
- Full `pytest`: 3,570 passed, 2 skipped.
- In the browser pane against an isolated home, with an in-memory keyring and the
  scripted Ollama stub:
  - The scan toggle, the proxy (a credentialed URL was refused, a valid one saved
    normalized), the bypass list and the certificate toggle (by keyboard) were set
    through Settings.
  - An OpenAI key was saved to the store and cleared.
  - A turn that read a file containing a fake GitHub token showed the redaction
    note, and the stub received `[REDACTED GitHub token]`, never the token.
  - The Privacy page was checked at phone width.

Not exercised: a real proxy or TLS-inspecting network, the real Windows Credential
Manager or macOS Keychain (the fixture replaced them), and the packaged app.

## September 24 model connections: Anthropic, OpenAI and custom endpoints — source only, not released

- **Anthropic (Claude):** a native Messages API adapter (`lumi/anthropic_api.py`)
  with tool use, extended thinking and prompt caching (system prompt, tools and the
  latest user turn). Signed thinking blocks are replayed within a tool loop only
  to the model that produced them. If a loop started without thinking, thinking is
  dropped for that request instead of failing.
  - The same adapter reaches Claude on Amazon Bedrock (SigV4 signing and AWS event
    stream decoding in-house, botocore used for credentials when installed, or a
    Bedrock API key) and Vertex AI (google-auth or the gcloud CLI).
- **OpenAI:** a Responses API adapter (`lumi/openai_api.py`) for OpenAI and Azure
  OpenAI. It is stateless (`store: false`). Encrypted reasoning items are replayed
  only to the same model, quota errors are not retried, and chat model discovery
  drops embedding, audio and image models.
- **Custom connections** (`lumi/connections.py`): OpenAI-compatible gateways, Azure
  OpenAI, Bedrock, Vertex, and Anthropic or Responses proxies are defined as
  validated data in **Settings > Connections**. Each has a test button and appears
  in the model menu under its name.
  - HTTPS is required outside localhost and private networks, and credentials in
    URLs are refused.
  - Keys are stored as `conn_<id>`, are written only by the connection commands,
    and are never returned to the page.
  - A connection in use by a running turn can't be edited or removed.
- Anthropic and OpenAI keys sit alongside the other API keys and have their own
  connection checks. Capability profiles now cover the Claude, GPT/o-series and
  Gemini families, where the generic fallback had assumed a 32K window.
- The runtime status shows a connection's name, not its internal key.

Validation on September 24, 2026:

- 38 new tests: `test_anthropic_api.py`, `test_openai_responses.py`,
  `test_connections.py`. The SigV4 signature matches botocore's for a Bedrock model
  path that needs double encoding.
- Full `pytest`: 3,517 passed, 2 skipped.
- In the browser pane against an isolated home, three connections were added
  through the Settings form (typing, selects and Enter), tested, saved, and used
  from the model menu. Each completed a scripted tool loop through its own wire
  format: Chat Completions, Anthropic Messages and OpenAI Responses.

Not exercised: live Anthropic, OpenAI, Azure, Bedrock or Vertex accounts (no keys
were used), extended thinking against a real model, and the packaged app.

## September 24 dark and light themes — source only, not released

- **The saved theme now survives a restart.** The desktop window uses a new
  port and private browser storage on every launch, and the page restored the
  theme only from browser storage. A Light choice therefore came back Dark, while
  Settings still showed Light; density and font size were lost the same way. The
  server now renders the saved appearance into the page (`gui/appearance.py`).
  The native window also opens in the theme's background instead of white.
- **Match system:** a third theme choice that follows the operating system's
  light or dark setting, including changes while Lumi is open.
- **Light theme coverage.** 268 color literals that only suited the dark
  theme now use theme tokens. They had left light mode with a dark composer,
  code blocks, task cards, menus and status popover, a dark "Review" button with
  dark text, and hints below 2.5:1 contrast. Code blocks get a light
  highlighting palette. New tokens cover gold-tinted text and chips, softer
  status text, and diff colors. Native controls and scrollbars follow the theme
  through `color-scheme`.
- Dark mode is unchanged. Computed colors of every element were compared before
  and after in the chat view, the command palette, the status popover, menus,
  the model and permission pickers, New session, and Settings. The only
  differences were the same colors written in a new format, one border moving
  from 12% to 15% opacity, and native checkboxes now following the dark scheme.

Validation on September 24, 2026, in the browser pane against an isolated home
and a scripted Ollama-compatible model (no live model):

- Light mode had no text below 3:1 and no dark surfaces in the chat, all 17
  Settings pages, the command palette, status popover, menus, the model and
  permission pickers, and New session. Only hint-level text is between 3.7:1
  and 4.5:1; dark mode's hints measure 3.3–3.8:1.
- Choosing Light in Settings saved it, and a reload with empty browser storage
  still showed Light.
- Match system followed an emulated OS switch from dark to light without a
  reload, and resolved on load.
- New tests: `tests/test_appearance.py` and `tests/appearance.test.cjs`.

Not exercised: the packaged desktop window (its native background color and a
real OS theme switch), macOS, and surfaces the fixture did not open (live-run
progress, steer queue, onboarding, mission and autonomous views). Those use the
same tokens, but no one has looked at them in light mode.

## September 24 rebrand to Lumi — source only, not released

SONN Client (originally Resonant) is now **Lumi**. SONN keeps its name as the
model service Lumi can connect to. The new identity is the Lantern mark, an L
holding a gold light on night; see [brand/README.md](../brand/README.md).

- **Name everywhere a user or admin looks:** window and page titles, menus,
  prompts, notifications, the browser extension, the diagnostics bundle, the
  Windows taskbar id, the model's system prompt, `lumi.exe`, the `lumi`,
  `lumi-gui`, `lumi-tui`, `lumi-smoke` and `lumi-skill` commands, and the
  `lumi` package and distribution.
- **Logo and icons:** new favicon, sidebar mark and wordmark, welcome and
  empty-chat icons, `lumi.ico` (16–256 px; 16 and 32 px pixel-aligned),
  notification PNG, a macOS `lumi.icns` on Apple's icon grid, and the Dock
  name and icon when run from source on macOS. `scripts/build_brand_assets.py`
  draws the rasters from the SVG masters in `brand/`.
- **Palette:** gold on night replaces SONN teal. The light theme uses bronze on
  paper. Warnings are orange so they are not confused with the accent. The
  titlebar and sidebar now follow the theme; the light theme previously drew a
  dark sidebar with dark text.
- **Installer:** "Lumi", `lumi-setup-X.Y.Z.exe`, `Program Files\Lumi`, a new
  AppId. It silently removes the pre-rebrand SONN Client/Resonant install
  first, so Apps & Features keeps one entry. The macOS `Lumi.app` bundle step
  is in `packaging/lumi.spec`, but no macOS build has been made or tested.

Compatibility:

- `~/.resonant` moves to `~/.lumi` on first launch. The move happens before the
  startup log opens. If it is blocked, for example by an older build that is
  still running, the old folder stays in use and the move is retried next
  launch.
- `RESONANT_*` environment variables are read as their `LUMI_*` names.
- Hooks receive both the `LUMI_*` and `RESONANT_*` variables.
- The `resonant*` commands remain as aliases.
- Projects keep an existing `.resonant/` folder, and new projects get `.lumi/`.
- `resonant-pack.json`, `resonant-policy.json` and `RESONANT.md` are still read,
  alongside `lumi-pack.json`, `lumi-policy.json` and `LUMI.md`.
- An existing `~/Documents/Resonant Projects` stays the default projects folder.
- Agent handoff and checkpoint commits are now authored `@lumi.local`.

Deliberately unchanged:

- The update feed stays at the current GitHub Pages address, because every
  installed SONN Client polls it.
- Moving the feed to a Lumi domain needs a bridge release. Rename the
  repository only after that, because GitHub does not redirect Pages project
  sites after a rename.
- The SONN conversation-id prefix, the Engram memory namespace, the editor
  MCP entry names (`resonant_blender`…), model-facing tool names, the
  harness output fence, the `refs/resonant/checkpoints` git refs and the
  checkpoint archive marker are unchanged, because saved data uses them.

Validation on September 24, 2026:

- `ruff` and `git diff --check`: clean.
- UI recovery checks: 30.
- Full `pytest` on the final source: 3,472 passed, 2 skipped.
- New `tests/test_rebrand_compat.py` covers the state move, a blocked move,
  overrides, legacy project folders, and legacy environment and hook variables.

The browser pane checked a source run with an isolated home:

- the launch link and socket;
- the sidebar mark and wordmark;
- the gold send button;
- Settings;
- the light theme;
- a 375 px width without horizontal overflow;
- no remaining "SONN Client" or "Resonant" text.

A local PyInstaller build of `lumi.exe` passed the bundle policy (146.6 MiB,
316 files). Run against an isolated home holding a legacy `~/.resonant`:

- `--version` printed `lumi 0.19.2.dev11` and moved the folder to `~/.lumi`;
- the page was titled Lumi and served the new icons;
- the launch code was redeemed;
- the socket returned 403 without the token and 101 with it;
- the startup log was clean;
- the executable carries the Lantern icon, and Windows `LoadImageW` (used for
  the window icon) loads `lumi.ico` at 16–256 px.

WinSparkle was disabled for that local run so it would not write the real
registry; CI checks its bundling. That build predates the final text-only
command-help changes.

Not exercised: compiling the installer (no Inno Setup locally) or upgrading
an installed SONN Client, a macOS build, and live models.

## September 24 local GUI access control — source only, not released

**Security fix.** The GUI server bound to 127.0.0.1 accepted any WebSocket
without a credential. Any local process, another account on the machine, or a
web page could open the socket; WebSockets are exempt from CORS, and DNS
rebinding defeated the only Origin check. Such a client could then run
`shell_exec`, rewrite settings including hooks and MCP servers, switch the
permission mode and answer approval prompts.

- Each launch creates an access token that the server never prints or logs.
  Pages redeem a one-time code from the launch link's URL fragment at
  `/api/access`. They keep the token in origin storage, which is port-isolated
  unlike cookies, and send it as a WebSocket subprotocol or `X-Lumi-Access` header.
- `/ws` and `/api/ui-state` also require an exact `Host` (`127.0.0.1:<port>`,
  `localhost:<port>`, or a literal non-loopback bind address) and this server's
  `Origin`. A refused handshake is closed before `accept()`, and the client
  receives HTTP 403. A Host guard covers every route; the page refuses framing.
- Launch links: the desktop window opens with one; `--browser` prints one;
  **File > Open in Browser** mints one through the desktop bridge only. A page
  without access shows how to get a link instead of retrying. Pasting a new link
  into an open tab reloads it and redeems the code. Diagnostics ZIPs redact
  launch links.
- `update_settings` accepts only the fields Settings edits. Hooks, LSP servers,
  plugins, the gateway, stdio MCP servers and whole-section writes are refused.
  HTTP MCP entries are rebuilt without `command`/`args`/`env`. The Ollama setup
  wizard's `values` payload was previously ignored, so its typed URL was never
  saved; it is now validated and saved.
- `set_permission_mode` requires an explicit known mode; the backends had
  treated a missing or unknown mode as Full-auto. `approve` requires an explicit
  `true`. Wildcard `--host` binds print and probe a loopback URL.

Compatibility: bookmarks and scripts that open the page or socket without a
launch link are refused. After a restart, a tab needs the new link.

Validation on September 24, 2026: 3,416 passed / 3 skipped (baseline before the
change 3,357 / 3), 29 UI recovery checks, ruff and `git diff --check`. Live
checks used an isolated home and a scripted Ollama-compatible model, not a live
model. In the browser pane, with real keyboard events, they covered:

- the locked page at desktop and phone widths, and link redemption;
- sending a message, F5 reload with draft restore, and a dropped socket with
  automatic reconnect;
- a run that continued across a mid-run reload;
- a stale tab after a restart, recovered by pasting the new link, and a reused
  link falling back to the stored token.

Raw HTTP against the live server returned 403 for missing, wrong, cross-origin,
dev-server-origin and rebinding handshakes, and 101 with `lumi.v1` otherwise.

The desktop window (pywebview 6.1, WebView2) redeemed its code and connected.
**Open in Browser** was triggered through its click handler from the fixture's
own `evaluate_js`; operating-system input was not used. The minted link,
recorded by a stub `webbrowser.open`, connected a browser pane that sent a
message.

Not exercised: a packaged build, the real default-browser handoff, macOS/Linux
webviews and live models.

## September 24 security fixes: capability-pack trust and tool approvals

Source-only; not bundled or released. These are separate from the AI Employee pause.

**Repository capability packs could approve themselves.** The client
discovered `<project>/.resonant/packs` automatically, and a pack's own manifest
could set `trust`, `enabled` and `sha256`. Opening a cloned repository could
then connect the pack's MCP servers and register its shell hooks. The digest
covered only the manifest.

- Trust and enablement now come only from user settings. Manifest `trust`,
  `enabled` and `sha256` are ignored.
- Approval is location-bound. A repository pack can only be trusted by a user
  approval of that pack directory, so an approval never follows a copied pack
  into another repository. Pinned trust by pack id still works for packs
  outside the project.
- Every approval pins one digest covering every file in the pack (except
  `.git`) plus the repository files that its hook and MCP commands name. Any
  change turns the pack off until it is approved again. Packs with links, more
  than 4,000 files, or more than 64 MB cannot be verified.
- Pack hooks re-verify the digest before each run. Skills, agents and MCP
  servers stop contributing once the pack changes.
- Pack hooks now live on per-session runners. Opening another project
  disconnects the previous project's pack MCP servers; previously only a
  settings reload removed them.
- **Settings > Capability packs** shows what each pack would run and offers
  Approve or Revoke. If the pack changed after the list was drawn, approval is
  refused with an explanation. A banner above the composer names packs that
  are waiting for review.
- A project's `resonant-policy.json` could weaken built-in denies: an earlier
  `allow` beat, for example, Auto-edit's recursive-delete deny. Built-in denies
  are now checked first. Repository rules can still tighten the policy, and a
  policy `prompt` rule now requires approval.

**A user's Deny could run the tool.** After a Deny, the engine emitted
PERMISSION_REQUEST and read `HookResult`'s default decision, "allow", as
approval. The GUI always attaches a hook runner, so in Ask mode a denied tool
ran anyway.

- The user's answer is final, and only an explicit `true` approves.
- A missing or unknown hook decision is no decision. When no prompt can be
  shown, only an explicit allow or deny from a matching PERMISSION_REQUEST hook
  decides; otherwise the call fails closed. Arguments rewritten by a hook are
  checked against the policy again.
- Auto-edit used to run shell and MCP actions without asking. The GUI attached
  a prompt only in Ask mode, and the engine fell back to a legacy
  `auto_approve` flag. Auto-edit now asks before shell, MCP, browser, desktop,
  REPL, process and git actions, and before any new tool. `auto_approve` now
  follows the autonomy tier. Background work without a prompt, such as sprint
  roles in Auto-edit, now skips calls that need approval.
- Changing the permission mode now updates the live session's tier and policy.
  Before, switching Full-auto to Ask mid-session kept auto-approving.
- Delegated workers ask through the parent's prompt, one at a time, instead of
  auto-approving; restarted workers use their own run's prompt.
- Every prompt has a request id. Late or stale answers are ignored instead of
  approving the next request, and a missing or non-boolean `approved` no longer
  counts as approval.
- The approval dialog now takes focus when it opens: Tab reaches Deny and
  Allow, Escape denies, and focus returns to the composer. Denied shell cards
  read "not run" instead of "running…".

Validation: `ruff`, 23 Node UI tests (one new), `git diff --check`, and the
full suite: 3,405 passed, 2 skipped. The new tests are in `test_permission_decisions.py`,
`test_gui_permission_modes.py` and `test_capability_pack_trust.py`. Of these,
45 were written before the fix: 41 failed against the unfixed code, and 4
contract tests passed. Two more cover the restarted-worker prompt and dropping
servers of a pack edited after approval; they were written with their fixes
and mutation-checked. Real browser events drove the actual app, WebSocket,
engine, hook runner and approve handler. The model was scripted, no provider
was called, and the home and project were temporary. Checked there:

- Opening a repository with a self-trusting pack ran neither its hook nor its
  MCP server.
- In Auto-edit, a shell command prompted. Deny left no file; Allow ran it.
- After switching to Ask mid-session, a keyboard Deny (Tab, then Enter) and an
  Escape each left no file.
- Approving a pack edited after review was refused. Approving the current
  content started its MCP server and ran its hook on the next turn.
- Editing the approved pack stopped the hook, showed "Changed since approval",
  and brought the banner back. Revoking worked.
- The Settings page and dialog fit a 375 px viewport without horizontal
  overflow.

No live model run, packaged build or CLI-provider path was exercised.

## September 15 AI Employee source integration — paused

The user paused implementation; see the [resume handoff](ai-employees-handoff.md).
Integrated durable SONN task/advice controllers, a task panel with assignment
inspection, scoped file-only workers, isolated artifact handoffs and recovery.
Full native candidate regression: **3,351 passed / 3 skipped**. Product-side real
native-process checks use scripted providers and synthetic release artifacts; they
do not establish learned quality/cost benefit. No new bundle or release was built.
Automatic supervision, trusted outcome attribution, host enrollment and full
qualification remain open. No paid training phase is authorized.

## Prior package checkpoint

**Latest local package: 0.19.2.dev11; not a public release.** Its full suite passed
3,322 tests (two skipped), 40 focused job/editor checks, and packaged source/asset
comparison across 146 files. Later browser picker changes remain source-only;
22 UI recovery checks and isolated actual-browser interaction pass. Historical
candidate sections below retain their original results.
See [Blender qualification](blender-qualification-20260914.md).

Historical candidate 0.19.2.dev3 follows [0.19.1](v0.19.1-release-notes.md).

Native SONN desktop turns bind outgoing requests to a saved project/session
identity across model switches and reloads. Generated titles and compression
requests use independent identities with learning disabled. Generated repair
and diagnostic user turns carry explicit learning exclusions; actual tool
observations retain their original role. These controls do not grant account
access or imply that retained observations are useful lessons.

Successful edits that alternate four times between the same two text states
trigger a specific diagnostic recovery message. This does not terminate the
run, erase failures, weaken assertions, or limit ordinary repository reads.

The clean packaged candidate passed the bundle gate. The client suite passed
3,280 tests with 2 skipped, plus 19 UI recovery tests and lint. A native desktop continuation after process restart reached the same hosted
SONN learning session and retained the new request. Generated model-summary
exclusions are contract-tested; a later live continuation compacted the saved
history and resumed coding under the same hosted session. Malformed-summary
fallback was verified in a no-network replay, not exercised in that live attempt.
Delegated-worker identities and automatic learning benefit remain unqualified.
This is a local candidate, not a published release.

The Windows clean-build script checks for a running target bundle before deleting
any build files. `-ValidateOnly` exercises that guard without changing files.
An operator build interrupted the first candidate; the guard prevents that
specific packaging mistake from recurring.

Malformed or empty model summaries can now recover through a complete archived
transcript plus mechanically retained requirements, checklist and tool evidence.
The archive must be readable through the allowed artifact tool; unavailable
storage or provider errors keep history intact and stop compaction. This fallback
does not infer decisions, successful checks or task completion. The actual failed
desktop transcript shrank from 51,144 to 12,307 estimated history tokens in a
no-network replay with a deliberately malformed summary.

The guided drawing repair finished with 7/7 backend tests, nine independent
browser checks, a six-round game and durable account/score recovery. The builder
exceeded its prompted 15-tool boundary (17 observed) and was stopped; its extra
Windows launch command failed quoting. These checks do not establish unattended
completion or an enforced tool-call budget.


## September 14 long-run candidate: 0.19.2.dev5

- Optional enforced main model-request allowance, counted through recovery loops;
  reaching it retains work and permits continuation instead of asking for /clear.
- Reject active project/session replacement; fix Stop to target the active engine
  and final saves to target the captured record. Save completed step history.
- Native dev4 qualification exposed the project-switch failure; it was stopped
  before the new application was submitted. Keep it as a failed product-path test.
- Two-project native qualification is ongoing. No beta or public release claim.

September 14 dev6 candidate: classify SONN HTTP failures without leaking provider
text, explain interrupted generation, disable inherited paid-request retries, and
make SONN timeout wording explicit. Targeted adapter/identity checks:41passed.
Native two-project qualification remains in progress.

Dev7 also rejects multiline Windows shell commands before dispatch. The Windows
command runner can otherwise return zero while ignoring code after a newline.
Write a project script and invoke it with a single-line command. Four boundary
checks pass; the observed silent non-execution is preserved in qualification.

Dev8 fixes browser screenshots in SONN tool history. The gateway currently accepts
text only: retain image artifacts locally, preserve tool text, and direct the
agent to DOM/accessibility/evaluation evidence. Do not append a synthetic human
image message. The actual failed GearDesk history was rejected by the gateway
parser before this fix and accepted afterward, without changing saved history.

- SONN learning queue admission now has a bounded, cancellable cooldown/retry.
  Only the structured pre-dispatch code qualifies; uncertain paid failures remain terminal.

- Fix Blender Check editor argument compatibility with pinned blender-mcp1.9.1; live command-handler check and regression coverage.
## Installers and updates served from GitHub Pages

The release workflow now copies each stable installer to the GitHub Pages site
under `downloads/vX.Y.Z/` and points the signed appcast there, instead of at the
GitHub Release asset. This lets the source repository become private without
breaking updates for installed apps, as long as Pages stays public. The feed URL
in `updater.py` is unchanged, so no client rebuild is required. The Pages site
keeps the three newest installers and is published as one fresh commit per
release. Pre-release tags no longer reach the appcast. The installer's publisher
and support links now point to getsonn.com and the download page.

## Local dev11 long-job candidate

Adds project-owned `job_start`, `job_status`, and `job_cancel` for bounded
foreground workers such as Blender rendering. Preserves ordinary shell cleanup
and separates worker completion from artifact verification. Jobs stop on client
exit; application checkpoint recovery is explicit. This is a local candidate,
not a published release or a completed Blender qualification.

### Browser folder picker recovery
Source candidate: browser pages sharing the desktop server now open an in-page path dialog based on the page native bridge, rather than sending a native picker command. The running dev11 Blender qualification package is unchanged. Twenty-two UI recovery checks and isolated browser keyboard/cancel interaction pass.
