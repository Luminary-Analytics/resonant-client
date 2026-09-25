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
  policy instead; see [Organization policy](enterprise-policy.md).
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
  [Organization policy](enterprise-policy.md#group-policy-and-intune).

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

## Checking a computer

`lumi updates` prints the update settings in effect as JSON: the mode, channel,
pin, feed, who manages them, and `"installed_by": "msi"` for an MSI copy. It
reads settings and policy only and never checks for updates.

`lumi.exe` is a windowed program. To see its output from PowerShell, redirect
it:

```
Start-Process "C:\Program Files\Lumi\lumi.exe" -ArgumentList updates -Wait `
  -RedirectStandardOutput "$env:TEMP\lumi-updates.json"
```

## What has been verified

On every change to the app or packaging, CI builds the MSI with WiX 5 on a
Windows runner, then:

- installs it silently with `POLICYFILE` and checks the files, the marker,
  the shortcut and the registry value;
- runs the installed `lumi.exe updates`, which reports updates off, the MSI
  install and the policy the file set;
- uninstalls it and checks nothing is left.

It hasn't yet been deployed through a real Intune tenant, Configuration Manager
site or Group Policy.
