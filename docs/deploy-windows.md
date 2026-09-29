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
  file path. Uninstalling removes the value. Group Policy or Intune can set the
  policy instead; see [Organization policy](enterprise-policy.md). Lumi uses
  the file only when it and the folders above it can't be changed by anyone
  but administrators; if it can't read the file (a share out of reach) or
  others can change it, Lumi refuses model requests until that's fixed
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
- **No wizard pages.** Opened by hand, it shows Windows Installer's progress
  bar and a UAC prompt, and installs.

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
reads settings and policy only and never checks for updates.

`lumi policy` prints the organization policy in force as JSON: `organization`,
`source`, `error` and `blocked` (why Lumi refuses model requests, if it does),
`ignored` (every policy, key or license file Lumi didn't use, with the
reason, such as a folder that lets `BUILTIN\Users` add files) and `summary`.
It exits 1 while Lumi refuses model requests under the policy. It reads the
policy as the app does; it only adds a `policy.file_ignored` record to the
audit log for a file it ignored.

`lumi.exe` is a windowed program. To see its output from PowerShell, redirect
it:

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
  install and the policy the file set;
- uninstalls it and checks nothing is left, `%ProgramData%\Lumi` included;
- makes `%ProgramData%\Lumi` a folder the Users group owns and may change,
  installs again, checks the package took it over with the same owner and
  permissions, and uninstalls (the empty folder goes too).

It hasn't yet been deployed through a real Intune tenant, Configuration Manager
site or Group Policy.
