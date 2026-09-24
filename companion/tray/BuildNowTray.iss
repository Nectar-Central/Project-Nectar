; Nectar Buildnow Presence — Inno Setup installer script.
; Build the exe first (companion\tray\build-exe.bat), then compile this from
; the repo root:
;   "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" companion\tray\BuildNowTray.iss
; Output: dist\NectarBuildnowPresence-Setup-<version>.exe
;
; Per-user install (no admin/UAC): files go to %LOCALAPPDATA%\Programs,
; startup entry + uninstall key live under HKCU.
;
; Identity notes: the exe filename (BuildNowTray.exe), the AppId GUID and the
; AppMutex name are deliberately UNCHANGED from the "BuildNow Tray" days, so
; old installs upgrade in place instead of doubling up next to them.

#define MyAppName "Nectar Buildnow Presence"
#define MyAppVersion "2.33.0"
#define MyAppPublisher "Nectar Labs"
#define MyAppExeName "BuildNowTray.exe"
#define MyAppMutex "BuildNowTraySingleInstance"

[Setup]
AppId={{E37805BC-FFF9-42AD-A21D-38EE50699384}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
VersionInfoVersion={#MyAppVersion}
VersionInfoDescription=Nectar Buildnow Presence - decryptor + Discord rich presence
VersionInfoProductName={#MyAppName}
SetupIconFile=..\nectar-presence.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
; Per-user: no elevation prompt, installs even on locked-down machines.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={localappdata}\Programs\{#MyAppName}
DisableProgramGroupPage=yes
; Detect a running tray on install/upgrade AND on uninstall.
AppMutex={#MyAppMutex}
CloseApplications=yes
RestartApplications=no
OutputDir=..\..\dist
OutputBaseFilename=NectarBuildnowPresence-Setup-{#MyAppVersion}
; zip instead of lzma2/max: the lzma2-packed stub is what Wacatac.B!ml keys on.
; Solid compression is LZMA-only, so it goes off with zip.
Compression=zip
SolidCompression=no
WizardStyle=modern
UninstallDisplayName={#MyAppName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start {#MyAppName} with Windows"; GroupDescription: "{cm:AdditionalIcons}"; Flags: checkedonce

[Files]
; The PyInstaller bundle (bridge + decryptor + node fallback ride inside).
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{userprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{userdesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; HKCU Run key = per-user autostart, no admin needed.
; The legacy "BuildNow Tray" value is deleted first: upgrades otherwise leave
; two autostart entries launching the same exe (the mutex would kill the twin,
; but one entry is cleaner).
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: none; \
  ValueName: "BuildNow Tray"; Flags: deletevalue; Tasks: startupicon
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; \
  ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; \
  Flags: uninsdeletevalue; Tasks: startupicon

[Run]
; Launch the tray when setup finishes (checked by default).
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; \
  Flags: nowait postinstall skipifsilent

[Code]
// Belt and braces behind AppMutex: if a tray survived the close prompt (it has
// no window for Restart Manager to negotiate with), kill it so uninstall and
// reinstall never leave a ghost holding the ports. taskkill exits 128 when the
// process is already gone - that is success here, so ResultCode is ignored.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
  begin
    Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName}',
      '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  end;
end;
