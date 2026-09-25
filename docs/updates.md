# Updates: channels, pins and turning them off

Installed copies of Lumi on Windows update themselves with WinSparkle. The update
check reads a feed on the update site. The feed lists signed installers, and
Lumi installs one only after verifying its EdDSA signature against the key
built into the app.

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
that runs from source, or outside Windows, doesn't update itself. The check
button is off there. Running from source never loads WinSparkle, so development
runs and tests don't write the WinSparkle registry key or show its dialogs. To
try the updater from source, set `LUMI_UPDATER_FROM_SOURCE=1`.

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

## How the feeds work

WinSparkle takes a feed address, not a filter, so each choice has its own feed
on the update site (`https://luminary-analytics.github.io/resonant-client/`):

| Feed | Lists | Used when |
|---|---|---|
| `appcast.xml` | Stable releases | Stable channel, no pin. Every copy of Lumi before these settings existed polls this feed, so it keeps its address. |
| `appcast-beta.xml` | Betas newer than the newest stable release, and every stable release | Beta channel, no pin |
| `appcast-X.Y.xml` | Stable releases of one line | Pinned to `X.Y` |

The release workflow writes them with `packaging/update_appcast.py`:

- A stable tag (`vX.Y.Z`) goes into `appcast.xml`. The beta feed and the line
  feeds are rebuilt from it.
- A beta tag (`vX.Y.Z-beta.N`, or `-alpha.N` or `-rc.N`) goes into the beta feed
  only. A stable release retires the betas it has caught up with.
- Line feeds exist for the four newest release lines.
- `packaging/publish_pages.py` keeps each of those lines' newest installer on
  the site, besides the newest three releases, so pinned installs can still
  download their update.

See [the release pipeline](release-pipeline.md) and [RELEASING.md](../RELEASING.md).

## Status

These settings are source only and not released. The first release that
contains them also creates the beta and line feeds; until then a copy set to
the beta channel or a pin finds no feed to check. The feed generation and the
settings are covered by tests. No real release has published these feeds yet.
