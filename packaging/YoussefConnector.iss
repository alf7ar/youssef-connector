; Inno Setup script: per-user (no admin) installer for Youssef Connector.
; Compile:  ISCC.exe /DAppVersion=0.1.0 packaging\YoussefConnector.iss   (after PyInstaller: dist\YoussefConnector\)
; Output:   dist\installer\YoussefConnector-Setup-<version>.exe
; Silent:   YoussefConnector-Setup-x.y.z.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /FORCECLOSEAPPLICATIONS (used by the in-app auto-update)
#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef NumVersion
  #define NumVersion AppVersion
#endif
#ifndef SrcDir
  #define SrcDir "..\dist\YoussefConnector"
#endif
#ifndef OutDir
  #define OutDir "..\dist\installer"
#endif
; keep equal to the "youssef-repair-version" line of connector/repair.ps1 (tests/test_repair_setup.py checks it)
#define RepairVer "1"
#define AppName "Youssef Connector"
#define AppExe "YoussefConnector.exe"

[Setup]
AppId={{2F1B6C0E-7A43-4B8D-9E15-C0DE5A7B3E92}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Alfhar
VersionInfoVersion={#NumVersion}
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

{ Elevated Bluetooth repair task ("YoussefRepair", SYSTEM; see connector/repair_setup.py). The app itself stays per-user
  (PrivilegesRequired=lowest): one UAC prompt after an interactive install, only when the installed repair script is missing
  or older. Silent installs (auto-update) never prompt. Declined UAC = no task; the app works and reports "not_installed". }
function RepairScriptPath(): String;
begin
  Result := ExpandConstant('{commonpf}\YoussefConnector\repair\repair.ps1');
end;

function RepairCurrent(): Boolean;
var
  s: AnsiString;
begin
  Result := LoadStringFromFile(RepairScriptPath(), s) and (Pos('youssef-repair-version: {#RepairVer} #', String(s)) > 0);
end;

procedure RunRepairSetup(const Flag: String);
var
  rc: Integer;
begin
  if ShellExec('runas', ExpandConstant('{app}\{#AppExe}'), Flag, '', SW_HIDE, ewWaitUntilTerminated, rc) then
    Log(Format('%s exit code %d', [Flag, rc]))
  else
    Log(Format('%s not run (UAC declined or failed): %d %s', [Flag, rc, SysErrorMessage(rc)]));
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and (not WizardSilent) and (not RepairCurrent()) then
    RunRepairSetup('--install-repair');
end;

{ connector/repair_uninstall.ps1 as -EncodedCommand (UTF-16LE base64 of its code lines, comments and indentation stripped;
  tests/test_repair_setup.py checks it matches). Uninstall elevates only System32 powershell.exe with this fixed command,
  never the per-user (user-writable) app exe. BEGIN-REPAIR-UNINSTALL }
function RepairUninstallCmd(): String;
begin
  Result :=
    'JABFAHIAcgBvAHIAQQBjAHQAaQBvAG4AUAByAGUAZgBlAHIAZQBuAGMAZQAgAD0AIAAnAFMAdABvAHAAJwAKACQAZQBuAHYAOgBQ' +
    'AFMATQBvAGQAdQBsAGUAUABhAHQAaAAgAD0AIABbAEkATwAuAFAAYQB0AGgAXQA6ADoAQwBvAG0AYgBpAG4AZQAoACQAUABTAEgA' +
    'TwBNAEUALAAgACcATQBvAGQAdQBsAGUAcwAnACkACgAkAGUAbgB2ADoAUABBAFQASAAgAD0AIABbAEUAbgB2AGkAcgBvAG4AbQBl' +
    'AG4AdABdADoAOgBTAHkAcwB0AGUAbQBEAGkAcgBlAGMAdABvAHIAeQAKACQAcgBjACAAPQAgADAACgAkAHMAdgAgAD0AIABOAGUA' +
    'dwAtAE8AYgBqAGUAYwB0ACAALQBDAG8AbQBPAGIAagBlAGMAdAAgAFMAYwBoAGUAZAB1AGwAZQAuAFMAZQByAHYAaQBjAGUACgAk' +
    'AHMAdgAuAEMAbwBuAG4AZQBjAHQAKAApAAoAdAByAHkAIAB7ACAAJABzAHYALgBHAGUAdABGAG8AbABkAGUAcgAoACcAXAAnACkA' +
    'LgBEAGUAbABlAHQAZQBUAGEAcwBrACgAJwBZAG8AdQBzAHMAZQBmAFIAZQBwAGEAaQByACcALAAgADAAKQAgAH0AIABjAGEAdABj' +
    'AGgAIAB7ACAAfQAKAHQAcgB5ACAAewAgACQAbgB1AGwAbAAgAD0AIAAkAHMAdgAuAEcAZQB0AEYAbwBsAGQAZQByACgAJwBcACcA' +
    'KQAuAEcAZQB0AFQAYQBzAGsAKAAnAFkAbwB1AHMAcwBlAGYAUgBlAHAAYQBpAHIAJwApADsAIAAnAHQAYQBzAGsAIABZAG8AdQBz' +
    'AHMAZQBmAFIAZQBwAGEAaQByACAAcwB0AGkAbABsACAAcAByAGUAcwBlAG4AdAAnADsAIAAkAHIAYwAgAD0AIAAxACAAfQAgAGMA' +
    'YQB0AGMAaAAgAHsAIAAnAHQAYQBzAGsAIABZAG8AdQBzAHMAZQBmAFIAZQBwAGEAaQByACAAYQBiAHMAZQBuAHQAJwAgAH0ACgAk' +
    'AHAAZgAgAD0AIABbAE0AaQBjAHIAbwBzAG8AZgB0AC4AVwBpAG4AMwAyAC4AUgBlAGcAaQBzAHQAcgB5AEsAZQB5AF0AOgA6AE8A' +
    'cABlAG4AQgBhAHMAZQBLAGUAeQAoACcATABvAGMAYQBsAE0AYQBjAGgAaQBuAGUAJwAsACAAJwBSAGUAZwBpAHMAdAByAHkANgA0' +
    'ACcAKQAuAE8AcABlAG4AUwB1AGIASwBlAHkAKAAnAFMATwBGAFQAVwBBAFIARQBcAE0AaQBjAHIAbwBzAG8AZgB0AFwAVwBpAG4A' +
    'ZABvAHcAcwBcAEMAdQByAHIAZQBuAHQAVgBlAHIAcwBpAG8AbgAnACkALgBHAGUAdABWAGEAbAB1AGUAKAAnAFAAcgBvAGcAcgBh' +
    'AG0ARgBpAGwAZQBzAEQAaQByACcAKQAKACQAYgBhAHMAZQAgAD0AIABKAG8AaQBuAC0AUABhAHQAaAAgACQAcABmACAAJwBZAG8A' +
    'dQBzAHMAZQBmAEMAbwBuAG4AZQBjAHQAbwByACcACgAkAGQAaQByACAAPQAgAEoAbwBpAG4ALQBQAGEAdABoACAAJABiAGEAcwBl' +
    'ACAAJwByAGUAcABhAGkAcgAnAAoAZgB1AG4AYwB0AGkAbwBuACAATwB1AHIAcwAoAFsAcwB0AHIAaQBuAGcAXQAkAHAAKQAgAHsA' +
    'CgBpAGYAIAAoAFsASQBPAC4ARgBpAGwAZQBdADoAOgBHAGUAdABBAHQAdAByAGkAYgB1AHQAZQBzACgAJABwACkAIAAtAGIAYQBu' +
    'AGQAIABbAEkATwAuAEYAaQBsAGUAQQB0AHQAcgBpAGIAdQB0AGUAcwBdADoAOgBSAGUAcABhAHIAcwBlAFAAbwBpAG4AdAApACAA' +
    'ewAgAHIAZQB0AHUAcgBuACAAJABmAGEAbABzAGUAIAB9AAoAJABhACAAPQAgAFsASQBPAC4ARABpAHIAZQBjAHQAbwByAHkAXQA6' +
    'ADoARwBlAHQAQQBjAGMAZQBzAHMAQwBvAG4AdAByAG8AbAAoACQAcAApAAoAJABvACAAPQAgACQAYQAuAEcAZQB0AE8AdwBuAGUA' +
    'cgAoAFsAUwBlAGMAdQByAGkAdAB5AC4AUAByAGkAbgBjAGkAcABhAGwALgBTAGUAYwB1AHIAaQB0AHkASQBkAGUAbgB0AGkAZgBp' +
    'AGUAcgBdACkALgBWAGEAbAB1AGUACgBpAGYAIAAoAC0AbgBvAHQAIAAkAGEALgBBAHIAZQBBAGMAYwBlAHMAcwBSAHUAbABlAHMA' +
    'UAByAG8AdABlAGMAdABlAGQAIAAtAG8AcgAgACQAbwAgAC0AbgBvAHQAaQBuACAAJwBTAC0AMQAtADUALQAzADIALQA1ADQANAAn' +
    'ACwAIAAnAFMALQAxAC0ANQAtADEAOAAnACkAIAB7ACAAcgBlAHQAdQByAG4AIAAkAGYAYQBsAHMAZQAgAH0ACgBmAG8AcgBlAGEA' +
    'YwBoACAAKAAkAHIAIABpAG4AIAAkAGEALgBHAGUAdABBAGMAYwBlAHMAcwBSAHUAbABlAHMAKAAkAHQAcgB1AGUALAAgACQAdABy' +
    'AHUAZQAsACAAWwBTAGUAYwB1AHIAaQB0AHkALgBQAHIAaQBuAGMAaQBwAGEAbAAuAFMAZQBjAHUAcgBpAHQAeQBJAGQAZQBuAHQA' +
    'aQBmAGkAZQByAF0AKQApACAAewAKAGkAZgAgACgAIgAkACgAJAByAC4AQQBjAGMAZQBzAHMAQwBvAG4AdAByAG8AbABUAHkAcABl' +
    'ACkAIgAgAC0AZQBxACAAJwBBAGwAbABvAHcAJwAgAC0AYQBuAGQAIAAkAHIALgBJAGQAZQBuAHQAaQB0AHkAUgBlAGYAZQByAGUA' +
    'bgBjAGUALgBWAGEAbAB1AGUAIAAtAG4AbwB0AGkAbgAgACcAUwAtADEALQA1AC0AMwAyAC0ANQA0ADQAJwAsACAAJwBTAC0AMQAt' +
    'ADUALQAxADgAJwAgAC0AYQBuAGQAIAAoAFsAaQBuAHQANgA0AF0AJAByAC4ARgBpAGwAZQBTAHkAcwB0AGUAbQBSAGkAZwBoAHQA' +
    'cwAgAC0AYgBhAG4AZAAgADAAeAA1ADAAMABEADAAMQA1ADYAKQApACAAewAgAHIAZQB0AHUAcgBuACAAJABmAGEAbABzAGUAIAB9' +
    'AAoAfQAKAHIAZQB0AHUAcgBuACAAJAB0AHIAdQBlAAoAfQAKAGYAdQBuAGMAdABpAG8AbgAgAFQAaABlAHIAZQAoAFsAcwB0AHIA' +
    'aQBuAGcAXQAkAHAAKQAgAHsAIABbAEkATwAuAEQAaQByAGUAYwB0AG8AcgB5AF0AOgA6AEUAeABpAHMAdABzACgAJABwACkAIAAt' +
    'AG8AcgAgAFsASQBPAC4ARgBpAGwAZQBdADoAOgBFAHgAaQBzAHQAcwAoACQAcAApACAAfQAKAHQAcgB5ACAAewAKAGkAZgAgACgA' +
    'VABoAGUAcgBlACAAJABiAGEAcwBlACkAIAB7AAoAaQBmACAAKAAtAG4AbwB0ACAAKABPAHUAcgBzACAAJABiAGEAcwBlACkAKQAg' +
    'AHsAIAB0AGgAcgBvAHcAIAAiACQAYgBhAHMAZQAgAGkAcwAgAG4AbwB0ACAAbwB1AHIAcwAsACAAbABlAGYAdAAgAGkAbgAgAHAA' +
    'bABhAGMAZQAiACAAfQAKAGkAZgAgACgAVABoAGUAcgBlACAAJABkAGkAcgApACAAewAKAGkAZgAgACgALQBuAG8AdAAgACgATwB1' +
    'AHIAcwAgACQAZABpAHIAKQApACAAewAgAHQAaAByAG8AdwAgACIAJABkAGkAcgAgAGkAcwAgAG4AbwB0ACAAbwB1AHIAcwAsACAA' +
    'bABlAGYAdAAgAGkAbgAgAHAAbABhAGMAZQAiACAAfQAKAGYAbwByAGUAYQBjAGgAIAAoACQAZgAgAGkAbgAgACcAcgBlAHAAYQBp' +
    'AHIALgBwAHMAMQAnACwAIAAnAGwAYQBzAHQALgBqAHMAbwBuACcALAAgACcAbABhAHMAdAAuAGoAcwBvAG4ALgB0AG0AcAAnACkA' +
    'IAB7ACAAWwBJAE8ALgBGAGkAbABlAF0AOgA6AEQAZQBsAGUAdABlACgAKABKAG8AaQBuAC0AUABhAHQAaAAgACQAZABpAHIAIAAk' +
    'AGYAKQApACAAfQAKAFsASQBPAC4ARABpAHIAZQBjAHQAbwByAHkAXQA6ADoARABlAGwAZQB0AGUAKAAkAGQAaQByACwAIAAkAGYA' +
    'YQBsAHMAZQApAAoAfQAKAFsASQBPAC4ARABpAHIAZQBjAHQAbwByAHkAXQA6ADoARABlAGwAZQB0AGUAKAAkAGIAYQBzAGUALAAg' +
    'ACQAZgBhAGwAcwBlACkACgB9AAoAIgByAGUAbQBvAHYAZQBkACAAJABiAGEAcwBlACIACgB9ACAAYwBhAHQAYwBoACAAewAgACcA' +
    'RgBBAEkATABFAEQAOgAgACcAIAArACAAJABfAC4ARQB4AGMAZQBwAHQAaQBvAG4ALgBNAGUAcwBzAGEAZwBlADsAIAAkAHIAYwAg' +
    'AD0AIAAxACAAfQAKAGUAeABpAHQAIAAkAHIAYwA=';
end;
{ END-REPAIR-UNINSTALL }

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  rc: Integer;
begin
  { a standard user cannot delete the task or the folder, hence UAC. Users cannot create Program Files\YoussefConnector,
    so a standard user cannot make the uninstaller ask for elevation. }
  if (CurUninstallStep = usUninstall) and DirExists(ExpandConstant('{commonpf}\YoussefConnector')) then
    if ShellExec('runas', ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
                 '-NoProfile -NonInteractive -EncodedCommand ' + RepairUninstallCmd(), '', SW_HIDE, ewWaitUntilTerminated, rc) then
      Log(Format('repair uninstall exit code %d', [rc]))
    else
      Log(Format('repair uninstall not run (UAC declined or failed): %d %s', [rc, SysErrorMessage(rc)]));
end;
