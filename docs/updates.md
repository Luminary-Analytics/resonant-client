# Updates: channels, pins and turning them off

Installed copies of Lumi update themselves: on Windows with WinSparkle, and on
macOS with Sparkle 2 (see [Lumi on macOS](macos.md#updates)). The update check
reads a feed on the update site. The feed lists signed installers (disk images
on macOS), and Lumi installs one only after verifying its EdDSA signature
against the key built into the app. Both platforms use the same key.

**Settings > Updates** chooses how and from where Lumi updates. An organization
can set the same choices by policy.

| Setting | Values | Effect |
|---|---|---|
| **Check for updates** (`updates.mode`) | `automatic` (default), `manual`, `off` | `automatic` checks once a day and whenever you choose **Check for updates**. `manual` checks only when you choose it. `off` never checks or prompts; it's for organizations that deploy Lumi themselves. |
| **Channel** (`updates.channel`) | `stable` (default), `beta` | The beta channel gets beta releases first. It also gets every stable release, so beta users aren't left behind. |
| **Stay on release line** (`updates.pin`) | empty (default), or a line such as `0.20` | Lumi takes only stable releases of that line (0.20.1, 0.20.2 …) and nothing newer. A pin wins over the channel. Lumi never moves to an older version, so a pin below the installed version means no updates. |

Changes apply the next time Lumi starts. Until then, **This installation**
under Settings > Updates shows the current behavior and, separately, what applies
after a restart. **Check for updates** there, or in the Help menu, checks the
feed in use now.

Settings > Updates also shows the installed version and the last check. A copy
that runs from source, or on Linux, doesn't update itself, and Settings says
why; the check button is off there. On macOS only the app updates itself, not
`lumi` in Terminal or the chat gateway. Running from source never loads
WinSparkle or Sparkle, so development runs and tests don't write the WinSparkle
registry key or Sparkle's preferences, or show their dialogs. To try WinSparkle
from source, set `LUMI_UPDATER_FROM_SOURCE=1`; Sparkle updates the app bundle
it runs in, so it runs only in the packaged Lumi.app.

## Installing an update

When a check finds a newer version, WinSparkle offers it. If you accept, it
downloads the installer and verifies its EdDSA signature against the key
built into Lumi. It won't run an installer that fails the check.

Before running the installer, WinSparkle asks Lumi whether it can close:

- **While an agent turn runs, Lumi says no.** An update never cuts a turn
  off. WinSparkle holds the installer back and tells you; install the update
  once the turn finishes.
- **Otherwise Lumi closes itself** after the installer starts, so the
  installer can replace its files. Sessions are saved as they go, and Lumi
  opens again on the new version.

The installer asks for administrator rights, since Lumi is installed for all
users.

### On macOS

Sparkle offers the update in its own window (**Install Update**, **Remind Me
Later**, **Skip This Version**). It downloads the disk image and checks its
EdDSA signature against the same key before mounting it; it installs nothing
that fails. Before it closes Lumi to replace Lumi.app it asks once:

- **While an agent turn runs, Lumi asks it to wait**, and lets it go on as
  soon as the turn finishes. You don't have to install it again.
- **Otherwise it goes ahead:** Lumi quits, Sparkle replaces Lumi.app and
  opens the new version. macOS asks for an administrator's password only if
  you can't write to the folder Lumi.app is in.

A copy running from the disk image, rather than from Applications, can't
update itself; Sparkle says so.

## Installing an update from a file

A computer without the internet, or one in [offline mode](offline.md), takes
updates from a file: the installer and the update feed that lists it,
downloaded elsewhere. **Settings > Updates > Install an update from a file**
checks the installer's EdDSA signature against the same built-in key, its
size, and that the version inside the signed installer (the feed isn't
signed) is newer and on your channel or release line, then hands it to the
same install flow: never during an agent turn, an `update.install` record,
and Lumi closes for the installer. `lumi updates verify <file>` runs the check
from the command line. See [Updates from a file](offline.md#updates-from-a-file).

Installing from a file is Windows-only. On a Mac without the internet, open
the new disk image and drag Lumi to Applications, or install the new PKG.

In offline mode Lumi doesn't check the update site unless it is an allowed
host: WinSparkle and Sparkle don't start, and turning offline mode on stops
them at once. WinSparkle is shut down; Sparkle has no way to be, so Lumi
refuses each check and download it asks to start. A download is checked as
Sparkle starts it, not only when Sparkle found the update, so one started
later from an update window left open is refused too, as is one from a host
offline mode doesn't allow. Checks start again after a restart with offline
mode off.

The [audit log](audit-log.md) records what the updater does, on both platforms:

- `update.check`: `found`, `none` or `error` (with Sparkle's error code on
  macOS; a failed download is an `error` with `stage: download`);
- `update.deferred`: an update waited for a turn;
- `update.refused` (macOS): offline mode stopped a download before it
  started, with the reason;
- `update.install`: the installer started;
- `update.skipped`, `update.postponed` and `update.cancelled`: your choices in
  the update window (on macOS, Skip This Version, Remind Me Later, and a
  cancelled download or password prompt).

## For administrators

Lock any of the three with the organization policy's `settings` (see
[Organization policy](enterprise-policy.md)):

```json
"settings": {
  "updates.mode": "manual",
  "updates.channel": "stable",
  "updates.pin": "0.20"
}
```

- Locked settings show "Managed by <organization>" and can't be changed in the
  app.
- A policy with a value Lumi can't apply is refused as a whole, like other
  invalid policies. Examples are a mode other than the three above, or a pin
  that isn't a release line.
- If the machine's policy is invalid, automatic checks pause (`manual`). Lumi
  then refuses model requests until the policy is fixed.
- To deploy Lumi yourself, set `"updates.mode": "off"` and install new versions
  through your device management.
- A copy installed from the MSI package, the macOS installer package or a
  Linux .deb or .rpm never updates itself, whatever the settings or policy
  say. `lumi-install.json` marks it: beside `lumi.exe` or `/opt/lumi/lumi`,
  or in `Lumi.app/Contents/Resources`. See
  [Deploying on Windows](deploy-windows.md),
  [Deploying on macOS](deploy-macos.md) and [Lumi on Linux](deploy-linux.md).
- `lumi updates` prints the settings in effect as JSON, without checking for
  updates. Its `offline` field says why offline mode keeps Lumi from the
  update site, if it does, and `platform` whose feeds it reads (`windows`,
  `macos` or `linux`).

## How the feeds work

WinSparkle and Sparkle take a feed address, not a filter, so each choice has
its own feed on the update site
(`https://luminary-analytics.github.io/resonant-client/`), and macOS has its
own set:

| Feed (Windows) | Feed (macOS) | Lists | Used when |
|---|---|---|---|
| `appcast.xml` | `appcast-macos.xml` | Stable releases | Stable channel, no pin. Every copy of Lumi before these settings existed polls `appcast.xml`, so it keeps its address. |
| `appcast-beta.xml` | `appcast-macos-beta.xml` | Betas newer than the newest stable release, and every stable release | Beta channel, no pin |
| `appcast-X.Y.xml` | `appcast-macos-X.Y.xml` | Stable releases of one line | Pinned to `X.Y` |

The release workflow writes them with `packaging/update_appcast.py`:

- A stable tag (`vX.Y.Z`) goes into `appcast.xml`. The beta feed and the line
  feeds are rebuilt from it.
- A beta tag (`vX.Y.Z-beta.N`, or `-alpha.N` or `-rc.N`) goes into the beta feed
  only. A stable release retires the betas it has caught up with.
- Line feeds exist for the four newest release lines.
- The macOS build goes into the macOS feeds the same way, with
  `--platform macos`, pointing at the disk image. Nothing is added to the
  Windows feeds, so WinSparkle never sees a disk image and installed copies'
  feeds stay exactly as they were. A macOS item also says
  `sparkle:os="macos"` and the minimum macOS version (12.0), and gives
  Sparkle the release's version without the dash (`0.21.0beta.1`), because
  Sparkle compares versions only up to a dash; `sparkle:shortVersionString`
  keeps `0.21.0-beta.1`. The first macOS release creates
  `appcast-macos.xml`, empty if that release is a beta.
- `packaging/publish_pages.py` keeps each of those lines' newest installer on
  the site, besides the newest three releases, so pinned installs can still
  download their update. The disk image sits beside the Windows installer in
  the same version's folder.

See [the release pipeline](release-pipeline.md) and [RELEASING.md](../RELEASING.md).

## Status

These settings are source only and not released. The first release that
contains them also creates the beta and line feeds; until then a copy set to
the beta channel or a pin finds no feed to check. The feed generation and the
settings are covered by tests. No real release has published these feeds yet.

The macOS updater is source only too. CI starts the built app and sees Sparkle
load, resolve the macOS feed through Lumi and fetch it; tests run the real
Sparkle against a feed `update_appcast.py` wrote. No macOS release has been
published, so no Mac has installed an update yet ([what still needs a real
Mac](macos.md#what-still-needs-a-real-mac)).
