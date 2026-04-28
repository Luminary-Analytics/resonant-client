; -----------------------------------------------------------------------------
; Resonant Client — Inno Setup script
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
; Design choices:
;   - PrivilegesRequired=lowest        — install to %LOCALAPPDATA%, no UAC prompt.
;   - DisableWelcomePage=yes           — modern installers skip the "click next
;                                         to begin" page; users know what to do.
;   - ChangesAssociations=no           — we don't claim file extensions.
;   - WizardStyle=modern               — built-in modern theme.
;   - SignTool=                        — empty (no code signing in v0.x; users
;                                         get a SmartScreen warning on first
;                                         install only).
;
; The installed app:
;   - Lives at %LOCALAPPDATA%\Programs\Resonant Client\
;   - Adds Start Menu entry "Resonant Client"
;   - Adds optional desktop shortcut (user picks during install)
;   - Registers in Programs and Features for clean uninstall
;   - On uninstall, REMOVES the install dir but PRESERVES ~/.resonant/
;     (user data + settings + the EdDSA-trusted skills dir)
; -----------------------------------------------------------------------------

#ifndef AppVersion
  #define AppVersion "0.2.0"
#endif

#define AppName        "Resonant Client"
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
DefaultDirName={localappdata}\Programs\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableWelcomePage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
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
