; -----------------------------------------------------------------------------
; Resonant — Inno Setup script
; -----------------------------------------------------------------------------
;
; Wraps the PyInstaller one-folder output (dist/resonant/) into a single-file
; installer at dist/installer/resonant-setup-{Version}.exe.
;
; Usage:
;   1. Run PyInstaller first:   pyinstaller packaging/resonant.spec --clean --noconfirm
;   2. Compile this script:     ISCC.exe packaging/installer.iss /DAppVersion=0.2.0
;
; The /DAppVersion= switch lets CI override the version per build. If omitted,
; it defaults to whatever is hardcoded in the #define below.
;
; Design choices (v0.2.3+):
;   - PrivilegesRequired=admin         — install to Program Files (machine-wide).
;                                         UAC prompt on first install only.
;                                         Trade: per-machine install means the app
;                                         shows up in Windows Search out of the box
;                                         (Win11 search ignores per-user Start Menu
;                                         folders by default — bug #18).
;   - DisableWelcomePage=yes           — skip the "click next to begin" page.
;   - ChangesAssociations=no           — we don't claim file extensions.
;   - WizardStyle=modern               — built-in modern theme.
;   - SignTool=                        — empty (no code signing in v0.x; users
;                                         get a SmartScreen warning on first
;                                         install only).
;
; The installed app:
;   - Lives at C:\Program Files\Resonant\
;   - Adds Start Menu entry under \All Users\ (visible to Windows Search)
;   - Adds optional desktop shortcut (user picks during install)
;   - Registers in Programs and Features (HKLM hive) for clean uninstall
;   - On uninstall, REMOVES the install dir but PRESERVES ~/.resonant/
;     (user data + settings + the EdDSA-trusted skills dir)
;
; Auto-cleanup of v0.2.0–v0.2.2 per-user installs:
;   The [Code] section detects the previous per-user install at
;   HKCU\...\Uninstall\<AppId>_is1 and silently runs its uninstaller before
;   proceeding. Users upgrading from v0.2.x get a clean per-machine install
;   without a leftover per-user entry cluttering Apps & Features.
; -----------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

#define AppName        "Resonant"
#define AppPublisher   "Luminary Analytics"
#define AppURL         "https://github.com/Luminary-Analytics/resonant-client"
#define AppExeName     "resonant.exe"

[Setup]
AppId={{B7E1F4A2-7C4B-4D8E-9F0A-1234567890AB}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=yes
PrivilegesRequired=admin
; Drop the per-user override for v0.2.3+ — per-machine is the default and the
; only sensible mode now. Users who really want per-user can build from source.
OutputDir=..\dist\installer
OutputBaseFilename=resonant-setup-{#AppVersion}
SetupIconFile=..\resonant_client\gui\static\resonant.ico
UninstallDisplayIcon={app}\{#AppExeName}
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
Source: "..\dist\resonant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "gui"
Name: "{group}\{cm:UninstallProgram,{#AppName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Parameters: "gui"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Parameters: "gui"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
{ -----------------------------------------------------------------------------
  Auto-cleanup of prior per-user installs (v0.2.0 - v0.2.2).

  v0.2.0 - v0.2.2 installed to %LOCALAPPDATA%\Programs\Resonant Client\ with
  PrivilegesRequired=lowest, registering an uninstaller in
  HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\<AppId>_is1.

  v0.2.3+ installs per-machine (Program Files + HKLM). Without cleanup, the
  per-user uninstaller entry would linger in Apps & Features even after the
  app files were overwritten. This procedure runs the prior uninstaller in
  silent mode (/VERYSILENT /SUPPRESSMSGBOXES) before the new install begins,
  leaving Apps & Features clean.
  ----------------------------------------------------------------------------- }
function GetPerUserUninstallString(): String;
var
  uninstallKey: String;
  uninstallString: String;
begin
  Result := '';
  uninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}_is1';
  if RegQueryStringValue(HKCU, uninstallKey, 'UninstallString', uninstallString) then
    Result := uninstallString;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  perUserUninstaller: String;
  resultCode: Integer;
begin
  if CurStep = ssInstall then
  begin
    perUserUninstaller := GetPerUserUninstallString();
    if perUserUninstaller <> '' then
    begin
      Log('Detected prior per-user install. Running uninstaller: ' + perUserUninstaller);
      { Strip outer quotes if present so we can pass arguments cleanly. }
      perUserUninstaller := RemoveQuotes(perUserUninstaller);
      Exec(perUserUninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART',
           '', SW_HIDE, ewWaitUntilTerminated, resultCode);
      Log('Per-user uninstaller exit code: ' + IntToStr(resultCode));
    end;
  end;
end;
