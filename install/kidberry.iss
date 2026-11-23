; Kidberry-Setup.exe: the setup wizard friends download (English or Hebrew). Build it with install/build-setup.ps1.
; The wizard asks for the language and the folder, then runs install.ps1 hidden and shows its progress;
; install.ps1 does the real work (Python, ffmpeg, PyTorch, packages). Uninstall: Windows Settings > Apps > Kidberry.

#ifndef Version
  #define Version "0.0.0"
#endif
#ifndef Stage
  #define Stage "..\dist\stage"
#endif

[Setup]
AppId={{B7D3F1E2-5A4C-4C8E-9F21-3D6A8B0C4E57}
AppName=Kidberry
AppVersion={#Version}
AppVerName=Kidberry {#Version}
AppPublisher=Kidberry
AppPublisherURL=https://github.com/ronvalensi/kidberry
AppSupportURL=https://github.com/ronvalensi/kidberry
DefaultDirName={localappdata}\Kidberry
UsePreviousAppDir=yes
DisableWelcomePage=no
DisableProgramGroupPage=yes
DisableReadyPage=no
DirExistsWarning=no
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
ExtraDiskSpaceRequired=10737418240
ShowLanguageDialog=yes
WizardStyle=modern
WizardImageFile={#Stage}\wizard.bmp
WizardSmallImageFile={#Stage}\wizard-small.bmp
SetupIconFile=..\kidberry\static\kidberry.ico
UninstallDisplayIcon={app}\kidberry.ico
UninstallDisplayName=Kidberry
CloseApplications=no
OutputDir=..\dist
OutputBaseFilename=Kidberry-Setup
Compression=lzma2
SolidCompression=yes

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"
Name: "he"; MessagesFile: "compiler:Languages\Hebrew.isl"

[Messages]
en.WelcomeLabel2=Kidberry turns the kindergarten recorder's recordings into a transcript you can search and read in your language, and points out the moments worth a listen.%n%nSetup checks this PC, downloads what Kidberry needs (about 3 GB) and puts it all in one folder. No admin rights are needed.
he.WelcomeLabel2=Kidberry הופך את ההקלטות מהמקליט בגן לתמלול שאפשר לחפש ולקרוא בשפה שלכם, ומסמן את הרגעים ששווה להקשיב להם.%n%nההתקנה בודקת את המחשב, מורידה את מה ש-Kidberry צריך (בערך 3 GB) ושמה הכול בתיקייה אחת. לא צריך הרשאות מנהל.

[CustomMessages]
en.Working=Installing Kidberry
en.WorkingSub=This takes a few minutes, mostly downloading (about 3 GB).
en.Step1=Checking this PC
en.Step2=Getting tools (uv and ffmpeg)
en.Step3=Getting Kidberry
en.Step4=Installing Python and the speech-recognition packages
en.Step5=Finishing
en.Step6=Almost done
en.Elapsed=Time so far: %1
en.Failed=Kidberry could not be installed:%n%n%1%n%nYou can run Setup again; nothing of yours was changed.%nDetails for support: %2
en.Launch=Open Kidberry
en.DirHint=To use another drive, click Browse or change the drive letter (for example D:\Kidberry). Kidberry needs about 10 GB, and recordings add up over time.
en.Drives=Free space:   %1
en.KeepData=Keep your recordings, transcripts and settings?%n%nYes: they stay in %1, and installing Kidberry again picks them up.%nNo: they are deleted.
he.Working=מתקינים את Kidberry
he.WorkingSub=זה לוקח כמה דקות, בעיקר הורדות (בערך 3 GB).
he.Step1=בודקים את המחשב
he.Step2=מורידים כלים (uv ו-ffmpeg)
he.Step3=מורידים את Kidberry
he.Step4=מתקינים את Python ואת חבילות זיהוי הדיבור
he.Step5=מסיימים
he.Step6=כמעט סיימנו
he.Elapsed=זמן עד עכשיו: %1
he.Failed=לא הצלחנו להתקין את Kidberry:%n%n%1%n%nאפשר להריץ את ההתקנה שוב; שום דבר שלכם לא השתנה.%nפרטים לתמיכה: %2
he.Launch=לפתוח את Kidberry
he.DirHint=כדי להתקין בכונן אחר, לחצו על 'עיון' או שנו את אות הכונן (למשל D:\Kidberry). צריך בערך 10 GB, וההקלטות מצטברות עם הזמן.
he.Drives=מקום פנוי:   %1
he.KeepData=לשמור את ההקלטות, התמלולים וההגדרות?%n%nכן: הם נשארים ב-%1, והתקנה חוזרת של Kidberry תמצא אותם.%nלא: הם יימחקו.

[Files]
Source: "{#Stage}\kidberry-src.zip"; DestDir: "{tmp}"; Flags: dontcopy
Source: "{#Stage}\install.ps1"; DestDir: "{tmp}"; Flags: dontcopy
Source: "..\kidberry\static\kidberry.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autodesktop}\Kidberry"; Filename: "{app}\venv\Scripts\pythonw.exe"; Parameters: "-m kidberry.launch"; WorkingDir: "{app}\app"; IconFilename: "{app}\kidberry.ico"; Comment: "Kidberry"
Name: "{autoprograms}\Kidberry"; Filename: "{app}\venv\Scripts\pythonw.exe"; Parameters: "-m kidberry.launch"; WorkingDir: "{app}\app"; IconFilename: "{app}\kidberry.ico"; Comment: "Kidberry"

[Registry]
Root: HKCU; Subkey: "Software\Kidberry"; ValueType: string; ValueName: "InstallDir"; ValueData: "{app}"; Flags: uninsdeletekey

[Run]
; through Explorer, so Kidberry does not inherit Setup's redirection guard (see StartDetached)
Filename: "{win}\explorer.exe"; Parameters: """{autoprograms}\Kidberry.lnk"""; Description: "{cm:Launch}"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\app"
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\python"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\tmp"
Type: files; Name: "{app}\install.json"
Type: files; Name: "{app}\install.log"

[Code]
function GetTickCount: Cardinal; external 'GetTickCount@kernel32.dll stdcall';

var
  ProgressPage: TOutputProgressWizardPage;
  DrivesLabel: TNewStaticText;

{ Fixed drives bigger than 32 GB, with their free space: "C: 17 GB   ·   D: 605 GB" }
function DriveList: String;
var
  I: Integer;
  Free, Total: Int64;
begin
  Result := '';
  for I := Ord('C') to Ord('Z') do
    if GetSpaceOnDisk64(Chr(I) + ':\', Free, Total) and (Total > 34359738368) then
    begin
      if Result <> '' then Result := Result + '   ·   ';
      Result := Result + Chr(I) + ': ' + IntToStr(Free div 1073741824) + ' GB';
    end;
end;

procedure InitializeWizard;
begin
  ProgressPage := CreateOutputProgressPage(CustomMessage('Working'), CustomMessage('WorkingSub'));

  DrivesLabel := TNewStaticText.Create(WizardForm);
  DrivesLabel.Parent := WizardForm.SelectDirPage;
  DrivesLabel.AutoSize := False;
  DrivesLabel.WordWrap := True;
  DrivesLabel.Left := WizardForm.SelectDirLabel.Left;
  DrivesLabel.Width := WizardForm.SelectDirLabel.Width;
  DrivesLabel.Top := WizardForm.DirEdit.Top + WizardForm.DirEdit.Height + ScaleY(18);
  DrivesLabel.Height := ScaleY(80);
  DrivesLabel.Caption := CustomMessage('DirHint') + #13#10#13#10 + FmtMessage(CustomMessage('Drives'), [DriveList]);
end;

function Field(const S: String; N: Integer): String;
{ the N-th (1-based) '|'-separated field of S; the last field keeps any further '|' }
var
  Rest: String;
  P, K: Integer;
begin
  Rest := S;
  for K := 1 to N - 1 do
  begin
    P := Pos('|', Rest);
    if P = 0 then begin Result := ''; exit; end;
    Rest := Copy(Rest, P + 1, Length(Rest));
  end;
  P := Pos('|', Rest);
  if (P > 0) and (N < 3) then Result := Copy(Rest, 1, P - 1) else Result := Rest;
end;

{ Starts PowerShell through Explorer rather than as Setup's child. Setup runs with Windows' redirection guard, which
  every child inherits (WMI-started ones too); under it uv cannot use the folder junction it makes for Python
  ("untrusted mount point", error 448). Explorer opening a shortcut starts the process from Explorer, without it. }
function StartDetached(const Params: String; var ErrorText: String): Boolean;
var
  Link: String;
  ResultCode: Integer;
begin
  Result := False;
  try
    Link := CreateShellLink(ExpandConstant('{tmp}\kidberry-install.lnk'), 'Kidberry setup',
      ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Params, ExpandConstant('{tmp}'), '', 0, SW_SHOWMINNOACTIVE);
    Result := Exec(ExpandConstant('{win}\explorer.exe'), '"' + Link + '"', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
    if not Result then ErrorText := 'Could not start the installer: ' + SysErrorMessage(ResultCode);
  except
    ErrorText := 'Could not start the installer: ' + GetExceptionMessage;
  end;
end;

{ Runs install.ps1 hidden and follows its status file. An error text stops Setup on the "Preparing" page. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  StatusFile, Params, Detail, FailText, Line, Second, ErrorText: String;
  Lines: TArrayOfString;
  I, Pct, Shown, StepN, Start, StepStart, Secs, StepSecs: Integer;
  Done: Boolean;
begin
  Result := '';
  ExtractTemporaryFile('kidberry-src.zip');
  ExtractTemporaryFile('install.ps1');
  StatusFile := ExpandConstant('{tmp}\status.txt');
  DeleteFile(StatusFile);
  Params := '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + ExpandConstant('{tmp}\install.ps1') + '"' +
    ' -InstallDir "' + ExpandConstant('{app}') + '" -Source "' + ExpandConstant('{tmp}\kidberry-src.zip') + '"' +
    ' -StatusFile "' + StatusFile + '" -Lang ' + ActiveLanguage + ' -Managed -NoShortcuts -NoLaunch';
  if not StartDetached(Params, ErrorText) then
  begin
    Result := FmtMessage(CustomMessage('Failed'), [ErrorText, '-']);
    exit;
  end;

  ProgressPage.Show;
  try
    Start := GetTickCount; StepStart := Start;
    Done := False; Pct := 1; StepN := 1; FailText := ''; Detail := '';
    while not Done do
    begin
      Sleep(250);
      if LoadStringsFromFile(StatusFile, Lines) then
        for I := 0 to GetArrayLength(Lines) - 1 do
        begin
          Line := Lines[I];
          if Line = 'done' then Done := True
          else if Copy(Line, 1, 5) = 'fail|' then begin FailText := Field(Line, 2); Done := True; end
          else if Copy(Line, 1, 2) = 'i|' then Detail := Field(Line, 2)
          else if Copy(Line, 1, 2) = 'p|' then
          begin
            if StrToIntDef(Field(Line, 3), StepN) <> StepN then StepStart := GetTickCount;
            Pct := StrToIntDef(Field(Line, 2), Pct);
            StepN := StrToIntDef(Field(Line, 3), StepN);
          end;
        end;
      Secs := (GetTickCount - Start) div 1000;
      StepSecs := (GetTickCount - StepStart) div 1000;
      Shown := Pct;
      { PyTorch is one long download with no progress of its own: creep towards 78% so the bar keeps moving }
      if Pct = 18 then Shown := 18 + (60 * StepSecs) div (StepSecs + 150);
      Second := FmtMessage(CustomMessage('Elapsed'), [Format('%d:%.2d', [Secs div 60, Secs mod 60])]);
      if (ActiveLanguage = 'en') and (Detail <> '') then Second := Second + '   ' + Detail;
      ProgressPage.SetText(CustomMessage('Step' + IntToStr(StepN)), Second);
      ProgressPage.SetProgress(Shown, 100);
      if (Secs > 90) and not FileExists(StatusFile) then
      begin
        FailText := 'The installer did not start (no progress after 90 seconds).';
        Done := True;
      end;
      if Secs > 5400 then
      begin
        FailText := 'Setup stopped waiting after 90 minutes.';
        Done := True;
      end;
    end;
    if FailText = '' then ProgressPage.SetProgress(100, 100);
  finally
    ProgressPage.Hide;
  end;
  if FailText <> '' then
    Result := FmtMessage(CustomMessage('Failed'), [FailText, ExpandConstant('{app}\install.log')]);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
  App: String;
begin
  App := ExpandConstant('{app}');
  if CurUninstallStep = usPostUninstall then RemoveDir(App);   { only if empty, i.e. the data was deleted too }
  if CurUninstallStep <> usUninstall then exit;
  { stop a running Kidberry from this folder, so its files can be removed }
  Exec('powershell.exe', '-NoProfile -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | ' +
    'Where-Object { $_.Path -like ''' + App + '\*'' } | Stop-Process -Force"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  if (DirExists(App + '\data') or DirExists(App + '\recordings')) and
     (SuppressibleMsgBox(FmtMessage(CustomMessage('KeepData'), [App]), mbConfirmation, MB_YESNO, IDYES) = IDNO) then
  begin
    DelTree(App + '\data', True, True, True);
    DelTree(App + '\recordings', True, True, True);
  end;
end;
