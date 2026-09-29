; -----------------------------------------------------------------------------
; Lumi — Inno Setup script
; -----------------------------------------------------------------------------
;
; Wraps the PyInstaller one-folder output (dist/lumi/) into a single-file
; installer at dist/installer/lumi-setup-{Version}.exe.
;
; Usage:
;   1. Build the bundle and the license page: scripts/build_clean.ps1 (PyInstaller,
;      then packaging/legal_texts.py rtf --out dist/legal for lumi/__init__.py's version,
;      which also writes dist/legal/license-versions.iss, included below)
;   2. Compile this script:     ISCC.exe packaging/installer.iss /DAppVersion=0.2.0
;
; The /DAppVersion= switch lets CI override the version per build. If omitted,
; it defaults to whatever is hardcoded in the #define below.
;
; Design choices:
;   - PrivilegesRequired=admin         — install to Program Files (machine-wide).
;                                         UAC prompt on first install only.
;                                         Trade: per-machine install means the app
;                                         shows up in Windows Search out of the box
;                                         (Win11 search ignores per-user Start Menu
;                                         folders by default — bug #18).
;   - DisableWelcomePage=yes           — skip the "click next to begin" page.
;   - LicenseFile=dist\legal\license.rtf — Lumi's terms on the license page:
;                                         the End User License Agreement, and for
;                                         a pre-release version the Alpha and Beta
;                                         Test Terms after it (packaging/legal_texts.py).
;                                         Setup goes on only once "I accept" is
;                                         chosen. An installation records the
;                                         versions its page showed (HKLM64
;                                         SOFTWARE\Luminary Analytics\Lumi\Setup),
;                                         and an update whose terms are those
;                                         versions skips the page (ShouldSkipPage),
;                                         so WinSparkle's updates, which start this
;                                         installer as it is, don't stop on it; a
;                                         new EULA, or a beta's test terms after a
;                                         stable install, shows it again. The record
;                                         is the installer's own, never a person's
;                                         acceptance: Lumi asks for the terms at
;                                         first launch whatever installed it
;                                         (lumi/terms.py). /SILENT and /VERYSILENT
;                                         skip the page and record nothing.
;   - ChangesAssociations=no           — we don't claim file extensions.
;   - WizardStyle=modern               — built-in modern theme.
;   - SignTool=                        — empty (no code signing in v0.x; users
;                                         get a SmartScreen warning on first
;                                         install only).
;
; The installed app:
;   - Lives at C:\Program Files\Lumi\
;   - Adds Start Menu entry under \All Users\ (visible to Windows Search)
;   - Adds optional desktop shortcut (user picks during install)
;   - Registers in Programs and Features (HKLM hive) for clean uninstall
;   - On uninstall, REMOVES the install dir but PRESERVES ~/.lumi/
;     (user data + settings + the EdDSA-trusted skills dir)
;
; Replacing the pre-rebrand app:
;   Before September 2026 the app shipped as "SONN Client" (earlier "Resonant")
;   under a different AppId, installed in Program Files\Resonant. Lumi has its
;   own AppId, so the [Code] section finds that install (per-machine, or the
;   v0.2.0–v0.2.2 per-user layout) and silently runs its uninstaller first.
;   Apps & Features then lists only Lumi. User data is untouched: the old
;   uninstaller keeps ~/.resonant, and Lumi moves it to ~/.lumi on first launch.
; -----------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

; LicenseEulaVersion and LicenseAlphaTermsVersion: the versions of Lumi's terms in
; license.rtf (empty test terms for a stable version), rendered with it.
#include "..\dist\legal\license-versions.iss"

#define AppName        "Lumi"
#define AppPublisher   "Luminary Analytics"
; The source repository may be private; installer links use public pages.
#define AppURL         "https://luminary-analytics.github.io/resonant-client/"
#define AppExeName     "lumi.exe"
; AppId of the pre-rebrand "SONN Client" / "Resonant" installs to replace.
#define LegacyAppId    "{B7E1F4A2-7C4B-4D8E-9F0A-1234567890AB}"

[Setup]
AppId={{F324242E-23A7-45B0-BEB9-0961AAD3745A}
AppName={#AppName}
AppVersion={#AppVersion}
; Installing an update from a file (lumi/update_file.py) trusts the release
; named here, inside the EdDSA-signed installer, not the unsigned feed's.
; Keep it the release version (Inno Setup's default, made explicit).
VersionInfoProductTextVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\Lumi
DefaultGroupName=Lumi
DisableProgramGroupPage=yes
DisableWelcomePage=yes
; Rendered for this version by scripts/build_clean.ps1; a build without it fails here.
LicenseFile=..\dist\legal\license.rtf
PrivilegesRequired=admin
OutputDir=..\dist\installer
OutputBaseFilename=lumi-setup-{#AppVersion}
SetupIconFile=..\lumi\gui\static\lumi.ico
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; Don't sign in v0.x — users see SmartScreen "unrecognized publisher" once.
; SignTool=signtool

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Pull in everything PyInstaller produced. The recursive subdirs flag picks
; up _internal/ with all the bundled libs and the WinSparkle.dll inside it.
Source: "..\dist\lumi\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Shortcuts the pre-rebrand installers created, in case their uninstaller
; could not run. Application data stays intact.
Type: files; Name: "{commonprograms}\Resonant\SONN Client.lnk"
Type: files; Name: "{commonprograms}\Resonant\Resonant.lnk"
Type: files; Name: "{commonprograms}\Resonant\Uninstall SONN Client.lnk"
Type: files; Name: "{commonprograms}\Resonant\Uninstall Resonant.lnk"
Type: dirifempty; Name: "{commonprograms}\Resonant"
Type: files; Name: "{commondesktop}\SONN Client.lnk"
Type: files; Name: "{userdesktop}\SONN Client.lnk"
Type: files; Name: "{userdesktop}\Resonant.lnk"

[Registry]
; The versions of Lumi's terms this installation's license page showed, so the next
; installation skips the page while they're the same (ShouldSkipPage below). The
; installer's own convenience, never a person's acceptance: Lumi never reads it.
Root: HKLM64; Subkey: "SOFTWARE\Luminary Analytics\Lumi\Setup"; ValueType: string; ValueName: "LicenseEulaVersion"; ValueData: "{#LicenseEulaVersion}"; Flags: uninsdeletekey; Check: not WizardSilent
Root: HKLM64; Subkey: "SOFTWARE\Luminary Analytics\Lumi\Setup"; ValueType: string; ValueName: "LicenseAlphaTermsVersion"; ValueData: "{#LicenseAlphaTermsVersion}"; Flags: uninsdeletekey; Check: not WizardSilent

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "gui"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "gui"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "gui"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ -----------------------------------------------------------------------------
  Remove the pre-rebrand app before installing Lumi.

  "SONN Client" (and "Resonant" before it) registered its uninstaller under
  the legacy AppId: per-machine in HKLM for v0.2.3 and later, per-user in HKCU
  for v0.2.0 - v0.2.2. Each one found is run silently before Lumi installs,
  so Apps & Features is left with a single Lumi entry.
  ----------------------------------------------------------------------------- }
function LegacyUninstallString(RootKey: Integer): String;
var
  uninstallKey: String;
  uninstallString: String;
begin
  Result := '';
  uninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#LegacyAppId}_is1';
  if RegQueryStringValue(RootKey, uninstallKey, 'UninstallString', uninstallString) then
    Result := uninstallString;
end;

procedure RunLegacyUninstaller(RootKey: Integer; const Description: String);
var
  uninstaller: String;
  resultCode: Integer;
begin
  uninstaller := LegacyUninstallString(RootKey);
  if uninstaller = '' then
    exit;
  Log('Removing the pre-rebrand ' + Description + ' install: ' + uninstaller);
  { Strip outer quotes if present so we can pass arguments cleanly. }
  uninstaller := RemoveQuotes(uninstaller);
  Exec(uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
       '', SW_HIDE, ewWaitUntilTerminated, resultCode);
  Log('Pre-rebrand uninstaller exit code: ' + IntToStr(resultCode));
end;

{ -----------------------------------------------------------------------------
  Leave an MSI installation to device management.

  The MSI package (packaging/lumi.wxs) installs into the same folder and puts
  lumi-install.json beside lumi.exe. Installing this EXE over it would give
  two installers one folder, so refuse; the organization updates that copy.
  ----------------------------------------------------------------------------- }
function InitializeSetup(): Boolean;
begin
  Result := True;
  if FileExists(ExpandConstant('{commonpf64}\Lumi\lumi-install.json')) then
  begin
    SuppressibleMsgBox('Lumi on this computer was installed by your organization''s device management (the MSI package). Ask your administrator to update it.',
                       mbError, MB_OK, IDOK);
    Result := False;
  end;
end;

{ -----------------------------------------------------------------------------
  Show Lumi's terms once for each version.

  [Registry] records the versions this installation's license page showed.
  An update whose license.rtf holds the same versions skips the page, so
  WinSparkle's updates don't stop on it every time. A new version of the
  EULA shows it again, and so does a pre-release build over a stable
  installation, whose record has no Alpha and Beta Test Terms. The record is
  the installer's own: Lumi asks each person to accept its terms itself
  (lumi/terms.py) and never reads it.
  ----------------------------------------------------------------------------- }
const
  LicenseKey = 'SOFTWARE\Luminary Analytics\Lumi\Setup';

function LicenseAlreadyShown(): Boolean;
var
  eula: String;
  alphaTerms: String;
begin
  Result := False;
  if not RegQueryStringValue(HKLM64, LicenseKey, 'LicenseEulaVersion', eula) then
    exit;
  if eula <> '{#LicenseEulaVersion}' then
    exit;
  if '{#LicenseAlphaTermsVersion}' <> '' then
  begin
    if not RegQueryStringValue(HKLM64, LicenseKey, 'LicenseAlphaTermsVersion', alphaTerms) then
      exit;
    if alphaTerms <> '{#LicenseAlphaTermsVersion}' then
      exit;
  end;
  Result := True;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
  if PageID = wpLicense then
    Result := LicenseAlreadyShown();
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssInstall then
  begin
    if IsWin64 then
      RunLegacyUninstaller(HKLM64, 'per-machine');
    RunLegacyUninstaller(HKLM32, 'per-machine (32-bit view)');
    RunLegacyUninstaller(HKCU, 'per-user');
  end;
end;
