; BiG Equity 360 - Inno Setup 6 script (O3). Per-user install, no admin rights.
; Built by packaging\build.ps1 -Installer, which passes:
;   /DAppVersion=<VERSION>  /DSourceDir=<packaging\out\BiG360>  /DDefaultDataDir=<existing data folder>
; Manual: "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.0 packaging\installer\BiG360.iss
;
; Installs the portable app folder (runtime + app + built UI + config) to %LOCALAPPDATA%\Programs\BiG360.
; Data is NOT copied: the wizard asks for the existing data folder and writes BIG360_DATA_DIR to {app}\.env.
; models_store (trained models, next to the data folder) is linked with a directory junction.
; Uninstall removes the program files only; the data folder, logs and Credential Manager keys are kept.

#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif
#ifndef SourceDir
  #define SourceDir "..\out\BiG360"
#endif
#ifndef DefaultDataDir
  #define DefaultDataDir "{userdocs}\STOCK SCANNER\data"
#endif

[Setup]
AppId={{08AD2D2A-CE09-4A51-879B-2C8C74F7C05A}
AppName=BiG Equity 360
AppVersion={#AppVersion}
AppVerName=BiG Equity 360 {#AppVersion}
AppPublisher=BiG
DefaultDirName={localappdata}\Programs\BiG360
DefaultGroupName=BiG Equity 360
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputBaseFilename=BiG360Setup-{#AppVersion}
SetupIconFile=..\assets\big360.ico
UninstallDisplayIcon={app}\big360.ico
UninstallDisplayName=BiG Equity 360
WizardStyle=modern
Compression=lzma2/normal
SolidCompression=no
DiskSpanning=no
CloseApplications=no
SetupLogging=yes
VersionInfoVersion={#AppVersion}

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"
Name: "autostart"; Description: "Start BiG360 in the background when I log in (tray icon, no admin rights)"
Name: "migratekeys"; Description: "Copy API keys from an existing .env next to the data folder (if any) into Windows Credential Manager"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Excludes: ".env,__pycache__,*.pyc"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
Name: "{app}\backend"

[Icons]
Name: "{autoprograms}\BiG Equity 360"; Filename: "{app}\BiG360.exe"; IconFilename: "{app}\big360.ico"; Comment: "Open the BiG Equity 360 dashboard"
Name: "{autoprograms}\BiG Equity 360 - Stop"; Filename: "{app}\BiG360.exe"; Parameters: "--quit"; IconFilename: "{app}\big360.ico"; Comment: "Stop BiG360 (running jobs finish first)"
Name: "{autoprograms}\BiG Equity 360 - User guide"; Filename: "{app}\docs\USER_GUIDE.md"
Name: "{autodesktop}\BiG Equity 360"; Filename: "{app}\BiG360.exe"; IconFilename: "{app}\big360.ico"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "BiG360"; ValueData: """{app}\BiG360.exe"" --background"; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\runtime\python.exe"; Parameters: "-m app.desktop.credentials migrate --env-file ""{code:OldEnvFile}"""; WorkingDir: "{app}\backend"; StatusMsg: "Copying API keys to Windows Credential Manager..."; Flags: runhidden waituntilterminated; Tasks: migratekeys; Check: OldEnvFileExists
Filename: "{app}\BiG360.exe"; Description: "Start BiG Equity 360 now"; Flags: postinstall nowait skipifsilent

[UninstallRun]
Filename: "{app}\runtime\python.exe"; Parameters: "-m app.desktop.tray --quit"; WorkingDir: "{app}\backend"; Flags: runhidden waituntilterminated; RunOnceId: "StopBiG360"

[UninstallDelete]
Type: filesandordirs; Name: "{app}\backend\app"
Type: filesandordirs; Name: "{app}\runtime"
Type: files; Name: "{app}\.env"

[Code]
var
  DataDirPage: TInputDirWizardPage;

function DataDir(): String;
begin
  Result := RemoveBackslashUnlessRoot(DataDirPage.Values[0]);
end;

function OldEnvFile(Param: String): String;
begin
  { the development tree keeps .env next to data\ (e.g. ...\STOCK SCANNER\.env) }
  Result := AddBackslash(ExtractFileDir(DataDir())) + '.env';
end;

function OldEnvFileExists(): Boolean;
begin
  Result := (DataDirPage <> nil) and FileExists(OldEnvFile(''));
end;

procedure InitializeWizard();
begin
  DataDirPage := CreateInputDirPage(wpSelectDir, 'Data folder',
    'Where is the BiG Equity 360 data?',
    'BiG360 reuses an existing data folder (tens of GB: it is not copied or moved). ' +
    'Pick the folder that contains db\, normalized\, raw\ ... (for a first install, an empty folder).',
    False, '');
  DataDirPage.Add('');
  DataDirPage.Values[0] := GetPreviousData('DataDir', ExpandConstant('{#DefaultDataDir}'));
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'DataDir', DataDir());
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = DataDirPage.ID) and not DirExists(DataDir()) then
    Result := MsgBox('The folder ' + DataDir() + ' does not exist. Create a new, empty data folder there?',
                     mbConfirmation, MB_YESNO) = IDYES;
end;

function UpdateReadyMemo(Space, NewLine, MemoUserInfoInfo, MemoDirInfo, MemoTypeInfo, MemoComponentsInfo,
  MemoGroupInfo, MemoTasksInfo: String): String;
begin
  Result := MemoDirInfo + NewLine + NewLine + 'Data folder (kept, not copied):' + NewLine + Space + DataDir() +
            NewLine + NewLine + MemoTasksInfo;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  Code: Integer;
begin
  Result := '';
  { upgrade: stop a running instance gracefully (running jobs finish / cancel) }
  if FileExists(ExpandConstant('{app}\runtime\python.exe')) then
    Exec(ExpandConstant('{app}\runtime\python.exe'), '-m app.desktop.tray --quit', ExpandConstant('{app}\backend'),
         SW_HIDE, ewWaitUntilTerminated, Code);
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Models, Link: String;
  Code: Integer;
begin
  if CurStep = ssPostInstall then
  begin
    ForceDirectories(DataDir());
    SaveStringToFile(ExpandConstant('{app}\.env'),
      '# BiG Equity 360 app settings (written by the installer). API keys live in Windows Credential Manager.' + #13#10 +
      'BIG360_DATA_DIR=' + DataDir() + #13#10, False);
    Models := AddBackslash(ExtractFileDir(DataDir())) + 'models_store';
    Link := ExpandConstant('{app}\models_store');
    if DirExists(Models) and not DirExists(Link) then
      Exec(ExpandConstant('{cmd}'), '/c mklink /J "' + Link + '" "' + Models + '"', '', SW_HIDE, ewWaitUntilTerminated, Code)
    else if not DirExists(Link) then
      ForceDirectories(Link);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Code: Integer;
begin
  if CurUninstallStep = usUninstall then
    { remove the models_store junction itself (rmdir without /s never touches the linked folder) }
    Exec(ExpandConstant('{cmd}'), '/c rmdir "' + ExpandConstant('{app}\models_store') + '"', '', SW_HIDE,
         ewWaitUntilTerminated, Code);
end;
