# Lumi on macOS

Lumi builds as a macOS app (`Lumi.app`) in a disk image (`lumi-X.Y.Z.dmg`) for
Apple silicon. It's the same code as the Windows app, with WebKit instead of
Edge for the window. An installer package (`lumi-X.Y.Z.pkg`) holds the same
app for device management; see [Deploying on macOS](deploy-macos.md).

Status: source only, not released. CI builds and starts the app on every
change (`.github/workflows/build-macos.yml`) and keeps the DMG as a build
artifact. No macOS release has been published, and the app isn't signed or
notarized until a Developer ID is configured (below).

## Building

On a Mac with Python 3.13 and PowerShell 7 (`brew install powershell`):

```bash
bash packaging/build_macos.sh
```

It does what `scripts/build_clean.ps1` does on Windows:

1. Fetches the pinned, SHA-256-verified web assets.
2. Installs the hash-pinned `packaging/requirements-release.txt` into a fresh
   virtual environment. The lock includes the macOS-only PyObjC wheels.
3. Writes the third-party notices.
4. Runs PyInstaller with `packaging/lumi.spec`.
5. Checks the bundle against `packaging/bundle-policy-macos.json`.
6. Makes `dist/installer/lumi-X.Y.Z.dmg`, with an Applications shortcut for
   drag-to-install.
7. Makes `dist/installer/lumi-X.Y.Z.pkg` for device management: the same app,
   marked so it leaves updates to the MDM (`packaging/macos_pkg.py`).

## Signing and notarization

Without a Developer ID, the build is unsigned and says so. To open an unsigned
copy, Control-click Lumi in Applications and choose **Open**.

With these set (as repository secrets in CI, or in the environment locally),
the script signs `Lumi.app` with the hardened runtime and
`packaging/macos/entitlements.plist`. It then signs the DMG, notarizes it with
`notarytool` and staples the ticket:

| Variable | Value |
|---|---|
| `MACOS_SIGN_IDENTITY` | `Developer ID Application: <Company> (<TEAMID>)` |
| `MACOS_SIGN_P12_BASE64` | The certificate and private key, exported as .p12 and base64-encoded |
| `MACOS_SIGN_P12_PASSWORD` | The .p12 password |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | The Apple ID, team and an app-specific password for notarization |
| `MACOS_INSTALLER_IDENTITY` | `Developer ID Installer: <Company> (<TEAMID>)`, in the same .p12, to sign the PKG, which is then notarized and stapled too |

A Developer ID needs a paid Apple Developer Program membership.

## Differences from Windows

- **Updates:** the macOS app doesn't update itself yet; WinSparkle is
  Windows-only. Settings > Updates says this copy doesn't update itself.
  Install new versions from the DMG. Sparkle, the macOS counterpart, isn't
  integrated yet.
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

## What has been verified

On `macos-latest`, the CI workflow:

- builds the app and DMG;
- checks that the permission checks run on a real Mac (they answer, rather
  than being missing);
- runs `lumi --version` and `lumi updates`;
- starts the GUI server in browser mode, loads the page, redeems the one-time
  launch code, and checks that the WebSocket refuses a connection without the
  access token and accepts it with the token;
- requires a startup log without errors.

The native window, dictation, computer use and the Keychain haven't been
exercised on a Mac yet.
