# Lumi on macOS

Lumi builds as a macOS app (`Lumi.app`) in a disk image (`lumi-X.Y.Z.dmg`) for
Apple silicon Macs with macOS 12 or later. It's the same code as the Windows
app, with WebKit instead of Edge for the window, and it updates itself with
Sparkle as the Windows app does with WinSparkle. An installer package
(`lumi-X.Y.Z.pkg`) holds the same app for device management; see
[Deploying on macOS](deploy-macos.md).

Status: source only, not released. CI builds, starts and checks the app on
every change (`.github/workflows/build-macos.yml`). From the next release tag,
the release workflow publishes the DMG and PKG beside the Windows installer
and adds the DMG to the macOS update feeds. Until a Developer ID is configured
(below), the app is signed ad hoc and isn't notarized, so macOS asks each
person to approve it once.

## Installing

1. Download `lumi-X.Y.Z.dmg` from the
   [download page](https://luminary-analytics.github.io/resonant-client/).
2. Open it and drag **Lumi** to **Applications**.
3. Open Lumi from Applications. Don't run it from the disk image: a copy that
   runs there can't update itself, and Sparkle says so.

### Opening a build that isn't notarized

Builds published without a Developer ID aren't notarized by Apple. The first
time you open one, macOS says it can't check Lumi for malicious software and
doesn't open it. On current macOS versions, Control-clicking and choosing
**Open** no longer gets past this. Instead:

1. In the message, choose **Done**, not **Move to Trash**.
2. Open **System Settings › Privacy & Security** and scroll down to
   **Security**. It says Lumi was blocked; choose **Open Anyway** beside it.
3. Enter your password (or use Touch ID), then choose **Open Anyway** once
   more when macOS asks again.

You do this once on each Mac. Updates shouldn't ask again: Sparkle checks
each update's signature itself and takes the downloaded-from-the-internet mark
off the new copy (not yet tried on a real Mac; see the end of this page). The
download page and the release notes say when a build isn't notarized. A
notarized build only asks whether you're sure you want to open an app
downloaded from the internet.

## Updates

Lumi.app carries Sparkle 2 (`Sparkle.framework`), the macOS counterpart of
WinSparkle, and drives it from Python through PyObjC (`lumi/sparkle.py`). It
behaves as on Windows ([Updates](updates.md)):

- **Settings > Updates** chooses the same things: check automatically (once a
  day), only when you choose **Check for updates**, or never; the stable or
  beta channel; a release line to stay on. An organization's policy can lock
  them, and a copy installed from the PKG never updates itself.
- **The same signing key.** Each disk image is signed with the EdDSA key
  WinSparkle checks the Windows installer with. Lumi.app's `SUPublicEDKey` is
  that key (`updater.EDDSA_PUBLIC_KEY`), and Sparkle checks the signature
  before it even mounts the disk image. It installs nothing that fails.
- **Its own feeds.** The Mac reads `appcast-macos.xml`,
  `appcast-macos-beta.xml` and `appcast-macos-X.Y.xml`, which list disk
  images. The Windows feeds keep their addresses and contents.
- **Installing.** When Sparkle finds a newer version it shows its window:
  **Install Update**, **Remind Me Later** or **Skip This Version**. It
  downloads and checks the update, then asks to close Lumi. **While an agent
  turn runs, it waits:** the update installs as soon as the turn finishes. It
  then replaces Lumi.app and opens the new version. If you can't write to the
  folder Lumi.app is in, macOS asks for an administrator's password.
- **The audit log** gets the same records as on Windows: `update.check`
  (`found`, `none` or `error`), `update.deferred`, `update.install`, and your
  choices as `update.skipped`, `update.postponed` (Remind Me Later) and
  `update.cancelled`.
- **Offline mode** stops it as it stops WinSparkle: Sparkle isn't started
  unless the update site is an allowed host, and turning offline mode on
  refuses every check and download from then on. Update checks start again
  after a restart.

Only the app updates itself: `lumi` in Terminal (the terminal UI) and the
chat gateway don't start Sparkle, and a copy running from source never loads
it. In browser mode (`lumi gui --browser`) Sparkle runs too, and Lumi closes
itself when the installer starts. Installing an update from a file (Settings >
Updates) is Windows-only; on a Mac, replace Lumi.app from the new disk image.

Sparkle compares versions up to the first dash, so `0.21.0-beta.1` would
equal `0.21.0` and a beta would never update to its release. Lumi.app's
`CFBundleVersion`, and the version the macOS feeds give Sparkle, leave the
dash out (`0.21.0beta.1`); the version you see keeps it.

## Building

On a Mac with Python 3.13 and PowerShell 7 (`brew install powershell`):

```bash
bash packaging/build_macos.sh
```

It does what `scripts/build_clean.ps1` does on Windows, and adds Sparkle:

1. Fetches the pinned, SHA-256-verified web assets, and Sparkle
   (`packaging/fetch_sparkle.sh`: a pinned release, checked against its
   SHA-256 before anything is extracted; the XPC services, which only
   sandboxed apps use, are left out).
2. Installs the hash-pinned `packaging/requirements-release.txt` into a fresh
   virtual environment. The lock includes the macOS-only PyObjC wheels.
3. Writes the third-party notices, Sparkle's license included.
4. Runs PyInstaller with `packaging/lumi.spec`, which writes Sparkle's settings
   into the Info.plist.
5. Checks the bundle against `packaging/bundle-policy-macos.json`.
6. Copies `Sparkle.framework` into `Lumi.app/Contents/Frameworks` and signs
   the app again: with the Developer ID when there is one, else ad hoc.
7. Makes `dist/installer/lumi-X.Y.Z.dmg`, with an Applications shortcut for
   drag-to-install.
8. Makes `dist/installer/lumi-X.Y.Z.pkg` for device management: the same app,
   marked so it leaves updates to the MDM (`packaging/macos_pkg.py`).

## Signing and notarization

Without a Developer ID the build is signed ad hoc, which Apple silicon
requires to run it at all, and says it isn't signed; macOS then needs the
one-time approval above. With these set (as repository secrets in CI, or in
the environment locally), the script signs `Lumi.app` with the hardened
runtime and `packaging/macos/entitlements.plist`, signs Sparkle's installer
and progress app separately without those entitlements, then signs the DMG,
notarizes it with `notarytool` and staples the ticket:

| Variable | Value |
|---|---|
| `MACOS_SIGN_IDENTITY` | `Developer ID Application: <Company> (<TEAMID>)` |
| `MACOS_SIGN_P12_BASE64` | The certificates and private keys, exported as .p12 and base64-encoded |
| `MACOS_SIGN_P12_PASSWORD` | The .p12 password |
| `MACOS_INSTALLER_IDENTITY` | `Developer ID Installer: <Company> (<TEAMID>)`, in the same .p12, to sign the PKG, which is then notarized and stapled too |
| `APPLE_API_KEY_BASE64`, `APPLE_API_KEY_ID`, `APPLE_API_ISSUER_ID` | An App Store Connect API key for `notarytool`: the `.p8` file base64-encoded, its key ID and the issuer ID |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | Or an Apple ID, its team and an app-specific password |

A Developer ID needs a paid Apple Developer Program membership.
[RELEASING.md](../RELEASING.md#macos-signing-and-notarization) says how to get
each of these. The update signature (EdDSA) is separate from Apple's: it's
what Sparkle trusts, signed or not.

## Differences from Windows

- **Search:** ripgrep isn't bundled for macOS yet. The agent's `grep` tool
  uses the system `grep -E`: alternation, `+` and groups work as in ripgrep,
  but classes such as `\d` may not, and ignore files aren't honored.
- **Permissions:** macOS asks the first time Lumi uses the microphone
  (dictation) or controls the computer (computer use needs Accessibility and
  Screen Recording in System Settings > Privacy & Security).
  - **Before each desktop tool, Lumi checks** the permission it needs, with
    the system's own checks, which don't prompt
    (`engine/macos_permissions.py`).
  - **Without one, the tool fails and says where to allow it.** macOS would
    otherwise hand back a blank screenshot, or drop the click, and the tool
    would look like it worked.
  - After allowing Screen Recording, restart Lumi.
- **Retina screens:** screenshots have twice as many pixels as the screen has
  points, and clicks are in points. Lumi maps the model's clicks through the
  screen's size in points, so they land where the model pointed.
- **No on-screen indicator yet.** The "Lumi is using the computer" border and
  banner are Windows-only.
- **Keys:** API keys go to the macOS Keychain.
- **Opening the app:** Finder, the Dock and Sparkle's relaunch start Lumi with
  no arguments and no terminal, which opens the app; `lumi` run in Terminal
  without arguments is the terminal UI, as on Windows. Launched that way, Lumi
  writes its startup messages to `~/.lumi/logs/lumi-startup.log`.

## What has been verified

On `macos-latest` (Apple silicon), on every change to the app or packaging,
the CI workflow:

- builds the app, the DMG and the PKG, with Sparkle in the app, signed ad hoc;
- checks what Sparkle reads before Lumi's code runs: the Info.plist's
  `SUPublicEDKey` and `SUFeedURL` are the key and macOS feed the binary
  prints, the bundle version has no dash, and Sparkle's framework, signature
  and license notice are there;
- runs `lumi --version` and `lumi updates` (automatic, macOS feeds);
- starts the GUI server in browser mode, loads the page, redeems the one-time
  launch code, and checks that the WebSocket refuses a connection without the
  access token and accepts it with the token;
- asks the running app over that WebSocket what Settings > Updates shows:
  Sparkle is loaded, the version pinned, and the feed it resolved through
  Lumi's delegate is `appcast-macos.xml`;
- waits for Sparkle's first scheduled check to fetch that feed (an
  `update.check` audit record; until the first macOS release the feed doesn't
  exist, so the result is an error);
- opens Lumi.app as Finder does, with no arguments: the app serves its page,
  keeps running with its window, and writes a startup log without errors;
- installs the PKG and checks that copy leaves updates to device management.

`tests.yml` also runs the updater's tests on `macos-latest`
(`tests/test_sparkle.py` and the feed, Pages and PKG tests), with the pinned
framework:

- PyObjC registers the Objective-C types Sparkle calls the delegate with
  (BOOL results, `NSInteger` enums, `NSError**` and blocks), and answers
  through the Objective-C runtime;
- the real Sparkle, updating a test app bundle, reads the feed through the
  delegate, finds the macOS item in a feed `update_appcast.py` wrote (and
  skips a newer Windows-only one), and after offline mode asks the delegate,
  is refused and makes no request;
- Sparkle's own version comparison orders Lumi's versions, betas included,
  as the feeds expect.

The logic behind them (feed choice, the wait for a turn, audit records,
offline mode, the main-thread hand-off) is tested on every platform with a
fake Sparkle.

## What still needs a real Mac

CI runs on a Mac, but not as a person does, and it has no Apple account:

- **Gatekeeper.** The first launch of a downloaded, ad-hoc-signed build (the
  Open Anyway approval above), and of a notarized one. CI's copy is built on
  the runner, so macOS never quarantines it.
- **A whole update.** Two published releases on the feed, a copy of the older
  one in Applications: Sparkle's window, the download, the EdDSA check before
  mounting, closing Lumi (and waiting for a running turn), replacing
  Lumi.app and opening the new one. Whether macOS asks for App Management
  permission before an ad-hoc-signed app replaces itself also needs checking.
- **The native window** beyond starting: menus, keyboard shortcuts, dragging,
  resizing, the folder and file pickers, and Sparkle's windows over it.
- **Dictation:** the microphone prompt, and the webview's speech recognition
  or a transcription service.
- **Computer use:** the Accessibility and Screen Recording prompts, and real
  screenshots and clicks.
- **The Keychain:** the first saved API key, and whether macOS asks to let
  Lumi use it after an update replaces the app (an ad-hoc signature changes
  with every build).
- **Signing and notarization** with a real Developer ID: the release
  workflow's signed path has never run.
