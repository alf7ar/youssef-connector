; Inno Setup script: per-user (no admin) installer for Youssef Connector.
; Compile:  ISCC.exe /DAppVersion=0.1.0 packaging\YoussefConnector.iss   (after PyInstaller: dist\YoussefConnector\)
; Output:   dist\installer\YoussefConnector-Setup-<version>.exe
; Silent:   YoussefConnector-Setup-x.y.z.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FORCECLOSEAPPLICATIONS (used by the in-app auto-update)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef SrcDir
  #define SrcDir "..\dist\YoussefConnector"
#endif
#ifndef OutDir
  #define OutDir "..\dist\installer"
#endif
#define AppName "Youssef Connector"
#define AppExe "YoussefConnector.exe"

[Setup]
AppId={{2F1B6C0E-7A43-4B8D-9E15-C0DE5A7B3E92}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Alfhar
VersionInfoVersion={#AppVersion}
DefaultDirName={localappdata}\Programs\YoussefConnector
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutDir}
OutputBaseFilename=YoussefConnector-Setup-{#AppVersion}
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=force
RestartApplications=no
ShowLanguageDialog=auto
UsePreviousLanguage=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "arabic"; MessagesFile: "Arabic.isl"

[CustomMessages]
english.LaunchApp=Launch Youssef Connector
arabic.LaunchApp=تشغيل Youssef Connector

[Files]
Source: "{#SrcDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Run]
; normal install = checkbox on the last page; silent install (auto-update) = start right away
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExe}"; Flags: nowait; Check: WizardSilent

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#AppExe}"; Flags: runhidden; RunOnceId: "KillApp"

[Code]
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  rc: Integer;
begin
  { an old version may still be shutting down after it launched this (silent) update: make sure it is gone }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#AppExe}', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(800);
  Result := '';
end;
