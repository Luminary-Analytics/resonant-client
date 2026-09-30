# Deploying Lumi on Windows: Intune, Configuration Manager and Group Policy

Lumi ships two Windows packages. Both install for all users into
`C:\Program Files\Lumi` and add a Start menu shortcut:

| Package | For | Updates |
|---|---|---|
| `lumi-setup-X.Y.Z.exe` (Inno Setup) | People installing Lumi themselves, and Intune Win32 apps | Lumi updates itself (see [Updates](updates.md)), unless a policy sets `updates.mode` to `off` |
| `lumi-X.Y.Z.msi` (WiX, stable releases only) | Intune line-of-business apps, Configuration Manager, Group Policy software installation | Never updates itself. Deploy the next MSI instead; it upgrades in place. |

Both are on the [download page](https://luminary-analytics.github.io/resonant-client/)
and the GitHub release. They are Authenticode-signed once a signing
certificate is configured (see [the release pipeline](release-pipeline.md)).

Use one package per computer. The MSI refuses to install over a copy from the
EXE, and the EXE refuses to install over the MSI's copy. To switch, uninstall
the first: the EXE's `unins000.exe /VERYSILENT` in its folder, or
`msiexec /x`. Settings and sessions in each person's `%USERPROFILE%\.lumi`
stay.

## The MSI

```
msiexec /i lumi-X.Y.Z.msi /qn /norestart
msiexec /i lumi-X.Y.Z.msi /qn POLICYFILE="\\fileserver\it\lumi-policy.json"
msiexec /x lumi-X.Y.Z.msi /qn
```

- **Per machine and silent.** It needs administrator rights, which device
  management agents have.
- **Upgrades in place.** Every Lumi MSI has the same upgrade code
  (`{144A302C-54FB-441F-B742-7B597A6A3F4A}`). Installing a newer one removes the
  older one; a rebuild of the same version replaces it too.
- **`POLICYFILE`** (optional) sets the organization policy's `PolicyFile`
  registry value (`HKLM\SOFTWARE\Policies\Luminary Analytics\Lumi`) to a policy
  file path. The package remembers it (under
  `HKLM\SOFTWARE\Luminary Analytics\Lumi\Msi`), so an upgrade that doesn't
  name `POLICYFILE` again keeps the value; one that names it changes it.
  The copy only keeps the value while it's still in place: to stop using a
  policy file, delete the value
  (`reg delete "HKLM\SOFTWARE\Policies\Luminary Analytics\Lumi" /v PolicyFile /f`,
  or through Group Policy), and the next upgrade neither brings it back nor
  keeps the copy. Uninstalling removes both. Group Policy or Intune can set
  the policy instead; see [Organization policy](enterprise-policy.md).
- **A `PolicyFile` fails closed.** Lumi uses the file only when it and the
  folders above it can't be changed by anyone but administrators. If it can't
  read the file (a share out of reach, a missing file, a path that isn't a
  full one) or others can change it, Lumi refuses model requests until that's
  fixed, and never falls back to a policy further down, such as one a person
  names with `LUMI_POLICY_FILE`. Settings and `lumi policy` say why
  ([the file rules](enterprise-policy.md#only-files-only-administrators-can-change-count)).
- **Creates `%ProgramData%\Lumi` locked down**, the folder for a machine
  `policy.json` and `license.json`: owned by Administrators, full control for
  SYSTEM and Administrators, read and execute for Users, nothing inherited
  from ProgramData (whose folders let every user add files). A folder someone
  made there before the install is taken over and locked the same way.
  Uninstalling removes it only when it's empty. Without the MSI, create it
  with the [icacls recipe](enterprise-policy.md#locking-down-a-policy-folder-on-windows).
  - Each install **replaces** the folder's owner and permissions: entries
    your organization added to `%ProgramData%\Lumi` are removed, so add
    them back after installing or upgrading if you need them.
  - Files already inside keep their own owners. Files whose owner isn't
    Administrators, SYSTEM or TrustedInstaller, such as ones copied by an
    administrator account that owns what it creates, don't count until you
    run `icacls "%ProgramData%\Lumi" /setowner *S-1-5-32-544 /T`.
- **Leaves updates to you.** The MSI puts `lumi-install.json` beside
  `lumi.exe`. Lumi then never checks for updates, whatever the settings or
  policy say. Settings > Updates and Help > Check for Updates say the copy came
  from the MSI.
- **A license page when opened by hand.** It shows Lumi's terms (the End User
  License Agreement), and Install is available once the person accepts them.
  Silent installs show no pages; see [Lumi's terms](#lumis-terms). Square
  brackets in the text are RTF escapes, so Windows Installer never reads them
  as properties.

## Lumi's terms

Lumi asks each person to accept its
[End User License Agreement](../lumi/legal/EULA.md) at first launch (and, on
pre-release builds, the [Alpha and Beta Test Terms](../lumi/legal/ALPHA-TERMS.md)).
Until they do, nothing is sent to a model.

- **Installing by hand:** the EXE's and the MSI's license pages show the terms
  (rendered for the version by `packaging/legal_texts.py`), and setup goes on
  only once the person accepts them. The EXE shows its page once for each
  version of the terms: it records the versions it showed
  (`HKLM\SOFTWARE\Luminary Analytics\Lumi\Setup`), and an update with the same
  versions skips the page, while a new EULA, or a beta's test terms after a
  stable install, shows it again. That record is the installer's own, never
  a person's acceptance: the app still asks each person.
- **Silent and managed installs** (`msiexec /qn`, `lumi-setup-X.Y.Z.exe
  /VERYSILENT`, Intune, Configuration Manager, Group Policy) show no license
  page and need no property to accept it: deploying Lumi to your
  organization's computers accepts the agreement for the organization, under
  its agreement with Luminary Analytics, and the organization is responsible
  for its people's use of Lumi under it.
- **Sparing each person the prompt:** put
  `"legal": {"accepted_by_organization": "Example Corp"}` in the machine policy
  ([Lumi's terms for your organization](enterprise-policy.md#lumis-terms-for-your-organization)).
  Only a machine policy from a place only administrators can write counts:
  the Group Policy key (its `Policy` value, or the `PolicyFile` it names,
  under the file rules above) or `%ProgramData%\Lumi\policy.json` in a
  locked folder; never `LUMI_POLICY_FILE` or a Lumi Cloud policy. The MSI has
  no property of its own for this: point `POLICYFILE` at a policy that says
  so, or set the policy with Group Policy or Intune. Without it, each person
  accepts once in the app.
- **Checking a computer:** `lumi terms` prints `"pending": false` and the
  organization once the policy accepts (redirect its output as below).

## Intune

- **MSI as a line-of-business app:** upload `lumi-X.Y.Z.msi`. Intune detects it
  by product code. Put `POLICYFILE=...` in the command-line arguments if you
  deliver the policy as a file.
- **EXE as a Win32 app:** wrap `lumi-setup-X.Y.Z.exe` with the Win32 Content
  Prep Tool.
  - Install: `lumi-setup-X.Y.Z.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART`.
  - Uninstall: `"C:\Program Files\Lumi\unins000.exe" /VERYSILENT`.
  - Detection: the registry value `DisplayVersion` under
    `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\{F324242E-23A7-45B0-BEB9-0961AAD3745A}_is1`
    is at least X.Y.Z.
  - These copies update themselves unless your policy sets `updates.mode` to
    `off` or `manual`.
- **Policy:** import `packaging/policy/lumi.admx` as an imported ADMX, or push
  the registry values with a script. See
  [Organization policy](enterprise-policy.md#group-policy-and-intune). To
  deliver `%ProgramData%\Lumi\policy.json` with a platform script instead,
  create the folder with the
  [icacls recipe](enterprise-policy.md#locking-down-a-policy-folder-on-windows)
  first; a folder made the usual way lets every user add files, so Lumi
  won't use a policy there and stops model requests until it's locked down.

## Configuration Manager

Create an application with a Windows Installer (MSI) deployment type.

- Install: `msiexec /i "lumi-X.Y.Z.msi" /qn /norestart` (add `POLICYFILE=...`
  if you use it).
- Uninstall: `msiexec /x "lumi-X.Y.Z.msi" /qn`.
- Detection: the MSI product code. Configuration Manager fills it in from the
  package.

A newer MSI supersedes the older application and upgrades it in place.

## Group Policy software installation

Assign `lumi-X.Y.Z.msi` to computers from a network share. Set the
organization policy with the ADMX template in the same or another GPO. To
upgrade, add the newer MSI as an upgrade of the package already assigned.

A `PolicyFile` on a share must be named by its UNC path and be one only
administrators can change, with the folders above it in the share; a startup
script that copies a policy to `%ProgramData%\Lumi` must create that folder
with the
[icacls recipe](enterprise-policy.md#locking-down-a-policy-folder-on-windows).
Group Policy Preferences' Files item alone makes the folder the usual way, and
Lumi then won't use the file and stops model requests until the folder is
locked down (Settings says why). For laptops that leave the network, prefer a
local copy or the `Policy` value: a `PolicyFile` Lumi can't read stops model
requests. Lumi can't keep a last good copy itself: it runs as the person, who
can't write the folders only administrators can.

## Checking a computer

`lumi updates` prints the update settings in effect as JSON: the mode, channel,
pin, feed, who manages them, and `"installed_by": "msi"` for an MSI copy. It
reads settings and policy only and never checks for updates. `lumi terms`
prints whether Lumi's terms are accepted, and by which organization.

`lumi policy` prints the organization policy in force as JSON: `organization`,
`source`, `error` and `blocked` (why Lumi refuses model requests, if it does),
`ignored` (every policy, key or license file Lumi didn't use, with the
reason, such as a folder that lets `BUILTIN\Users` add files) and `summary`.
It exits 1 while Lumi refuses model requests under the policy. It reads the
policy as the app does; it only adds a `policy.file_ignored` record to the
audit log for a file it ignored.

`lumi.exe` is a windowed program. Opened from Explorer or a shortcut without
arguments, it has no console, and opens the app as `lumi gui` does. With any
argument, or with its input or output redirected, it runs the command. To see
its output from PowerShell, redirect it:

```
Start-Process "C:\Program Files\Lumi\lumi.exe" -ArgumentList updates -Wait `
  -RedirectStandardOutput "$env:TEMP\lumi-updates.json"
Start-Process "C:\Program Files\Lumi\lumi.exe" -ArgumentList policy -Wait `
  -RedirectStandardOutput "$env:TEMP\lumi-policy.json"
```

## What has been verified

On every change to the app or packaging, CI builds the MSI with WiX 5 on a
Windows runner, then:

- installs it silently with `POLICYFILE` and checks the files, the marker,
  the shortcut and the registry value;
- checks that it made `%ProgramData%\Lumi` owned by Administrators with
  exactly the locked permissions, and that Lumi's own check trusts a file an
  administrator puts there;
- runs the installed `lumi.exe policy` while the `POLICYFILE` folder still
  lets every user add files (it fails closed and says why), then applies the
  icacls recipe and runs it again (the policy applies);
- runs the installed `lumi.exe updates`, which reports updates off, the MSI
  install and the policy the file set, and `lumi.exe terms`, which reports
  the terms accepted by the policy's organization;
- upgrades it with a second build of the same version that doesn't name
  `POLICYFILE`, and checks the `PolicyFile` value and the policy's
  acceptance stay; then deletes the value and upgrades again, and checks
  the value stays deleted and the package's copy is gone;
- uninstalls it and checks nothing is left, `%ProgramData%\Lumi` included;
- makes `%ProgramData%\Lumi` a folder the Users group owns and may change,
  installs again, checks the package took it over with the same owner and
  permissions, and uninstalls (the empty folder goes too).

It hasn't yet been deployed through a real Intune tenant, Configuration Manager
site or Group Policy.
