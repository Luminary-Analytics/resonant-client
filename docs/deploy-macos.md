# Deploying Lumi on macOS: Jamf Pro, Intune and other device management

Status: source only, not released. CI builds, installs and checks the
installer package on every change (below). No macOS release has been
published, and the package is unsigned until a Developer ID Installer
certificate is configured.

Lumi builds two macOS packages. Both install `Lumi.app` for Apple silicon on
macOS 12 or later:

| Package | For | Updates |
|---|---|---|
| `lumi-X.Y.Z.dmg` | People installing Lumi themselves: drag it to Applications | The macOS app doesn't update itself yet (see [Lumi on macOS](macos.md)) |
| `lumi-X.Y.Z.pkg` | Jamf Pro, Intune, other device management, and `installer` | Never updates itself. Deploy the next PKG instead; it upgrades in place. |

## The PKG

```bash
sudo installer -pkg lumi-X.Y.Z.pkg -target /
```

- **For every user.** It installs `/Applications/Lumi.app`, with no choices
  to make and no install scripts.
- **Upgrades in place.** Every version has the package id
  `com.luminaryanalytics.lumi`. The app isn't relocatable, so the next
  package replaces `/Applications/Lumi.app` even if someone copied Lumi
  somewhere else.
- **Leaves updates to you.** `Lumi.app/Contents/Resources/lumi-install.json`
  marks the copy. Lumi then never checks for updates, whatever the settings or
  policy say. Settings > Updates and Help > Check for Updates say it came from
  the installer package.
- **Refuses Macs it can't run on:** Intel Macs, and macOS before 12.
- **Signing.** With a Developer ID Installer certificate, the build signs the
  package and notarizes and staples it (`MACOS_INSTALLER_IDENTITY`, in the
  same .p12 as the application certificate; see [Lumi on macOS](macos.md)).
  An unsigned package gets a Gatekeeper warning when someone opens it, and
  device management services may refuse it. Check your service's
  requirements.
- **While Lumi runs.** The installer replaces the app's files even if Lumi is
  open. A running copy may not work properly until it restarts, so deploy
  when people aren't mid-task, or ask them to restart Lumi.
- **Removing it:**
  `sudo rm -rf /Applications/Lumi.app && sudo pkgutil --forget com.luminaryanalytics.lumi`.
  Each person's settings and sessions in `~/.lumi` stay.

## Policy through a configuration profile

Lumi reads the organization policy from the `com.luminaryanalytics.lumi`
preferences that a configuration profile sets at **device** scope, in
`/Library/Managed Preferences`. It doesn't read user-scope profiles. The
preferences have two keys:

- `Policy`: the [policy document](enterprise-policy.md), as JSON text or a
  dictionary;
- `PolicyKeys` (optional): the signing keys this Mac trusts, as the
  registry's `PolicyKeys` does on Windows. It's needed for a signed policy,
  and for the Lumi Cloud policy's keys if the machine policy doesn't name
  them.

Make the profile from your policy file:

```bash
python3 packaging/policy/make_mobileconfig.py acme-policy.json --out lumi-policy.mobileconfig
python3 packaging/policy/make_mobileconfig.py acme-signed.json --keys policy-keys.json --out lumi-policy.mobileconfig
```

- It checks the policy with the app's own parser first, so a mistake shows
  up here and not on every Mac. With `--keys`, it also verifies a signed
  policy's signature.
- The profile's identifier defaults to `com.luminaryanalytics.lumi.policy`.
  Keep it the same for every version, with `--identifier` if you use your
  own, so a new version replaces the old one.
- UUIDs come from the contents: the same policy always gives the same
  profile.
- `--plist` writes only the preferences. Use that for Jamf Pro's
  Application & Custom Settings and Intune's preference file.
- The profile sets `PayloadRemovalDisallowed`.

If the preferences exist but can't be used, Lumi refuses model requests
until they're fixed; it never treats that as "no policy". That covers a
plist that can't be read, or a `Policy` that is empty, isn't text or a
dictionary, isn't valid JSON or isn't a valid policy. Preferences without a
`Policy` key set no policy. Lumi reads the policy when it starts.

`packaging/policy/lumi-policy.mobileconfig` is a hand-written example of the
same profile.

## Jamf Pro

1. **The app.** Upload `lumi-X.Y.Z.pkg` as a package, and install it with a
   policy scoped to the computers, for example at check-in or from Self
   Service.
2. **The policy.** Create a computer configuration profile with an
   Application & Custom Settings payload:
   - the preference domain `com.luminaryanalytics.lumi`;
   - the plist that `make_mobileconfig.py --plist` writes, uploaded.

   Or upload the `.mobileconfig` it writes as a profile.
3. **Reporting (optional).** An extension attribute can report the version
   without starting Lumi:

   ```bash
   #!/bin/bash
   plist=/Applications/Lumi.app/Contents/Info.plist
   if [ -f "$plist" ]; then
     echo "<result>$(defaults read "$plist" CFBundleShortVersionString)</result>"
   else
     echo "<result>Not installed</result>"
   fi
   ```

## Microsoft Intune

1. **The app.** Add `lumi-X.Y.Z.pkg` as a macOS app, the PKG or
   line-of-business type, and assign it to devices. Intune detects it by the
   package id `com.luminaryanalytics.lumi` and its version.
2. **The policy.** Create a macOS configuration profile for devices:
   - **Templates > Custom:** upload the `.mobileconfig`, on the device
     channel.
   - **Or a Preference file:** the domain `com.luminaryanalytics.lumi` and
     the `--plist` file.

## Checking a Mac

- `pkgutil --pkg-info com.luminaryanalytics.lumi` shows the installed
  version.
- `/Applications/Lumi.app/Contents/MacOS/lumi updates` prints the update
  settings in effect as JSON, without checking for updates. Run it as the
  person who uses Lumi. It reports:
  - `"installed_by": "pkg"` and `"mode": "off"`;
  - with a policy that sets update settings, the organization in
    `managed_by` and the settings it locks;
  - under `problems`, a policy that can't be used, when updates would
    otherwise have been automatic. Settings > Organization policy always
    shows the error.
- `sudo profiles show -type configuration` lists installed profiles, and
  `/Library/Managed Preferences/com.luminaryanalytics.lumi.plist` holds what
  Lumi reads.
- In Lumi, Settings > Organization policy shows the policy and where it came
  from: "configuration profile (com.luminaryanalytics.lumi)".

## Privacy permissions

Computer use needs Accessibility and Screen Recording, and dictation the
microphone.

- A Privacy Preferences Policy Control profile can pre-approve Accessibility
  for Lumi's bundle id and code requirement.
- macOS doesn't let device management pre-approve Screen Recording or the
  microphone; each person allows those.
- To keep computer use off, set `security.computer_use` to `false` in the
  policy.

## What has been verified

On every change to the app or packaging, CI (`.github/workflows/build-macos.yml`)
does the following on an Apple silicon runner:

- **Build and install.** It builds the PKG, installs it with
  `sudo installer`, and checks:
  - the package receipt's version;
  - the installed app's signature (ad hoc without a Developer ID);
  - the install marker.
- **The installed copy.** Its `lumi updates` reports the PKG install and
  updates off.
- **A profile.** It generates managed preferences with
  `make_mobileconfig.py` and puts them in `/Library/Managed Preferences`:
  - `lumi updates` then shows the policy's organization and the update
    channel it locks;
  - with an empty `Policy`, `lumi updates` reports the policy as invalid.

Tests (`tests/test_macos_pkg.py`) cover:

- the package pieces: the marker, the non-relocatable component and the
  distribution file;
- the profile generator, including signed policies with their keys;
- the preferences reader failing closed.

Not verified yet:

- a real Jamf Pro or Intune tenant;
- a signed and notarized PKG (no Developer ID Installer certificate yet);
- `profiles install` of a generated profile, which needs a person's approval
  outside device management;
- installing over a Lumi that's running.
