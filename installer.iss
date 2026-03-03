; Inno Setup script for RecipeParser
; Build: ISCC.exe installer.iss
; Output: output\RecipeParser-Setup-2.0.2.exe

#define AppName      "RecipeParser"
#define AppVersion   "2.0.2"
#define AppPublisher "Ian Ballard"
#define AppURL       "https://github.com/your-repo/recipeparser"
#define AppExeName   "RecipeParser.exe"

[Setup]
AppId={{B7F3C1A2-4D5E-4F6A-8B9C-0D1E2F3A4B5C}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
AllowNoIcons=yes
; Output installer to a dedicated folder so it doesn't get mixed with build artefacts
OutputDir=output
OutputBaseFilename=RecipeParser-Setup-{#AppVersion}
; LZMA gives the best compression for Python bundles
Compression=lzma2/ultra64
SolidCompression=yes
; Require 64-bit Windows
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
WizardStyle=modern
; Show "Open RecipeParser" checkbox on the finish page
UninstallDisplayIcon={app}\{#AppExeName}
MinVersion=10.0
PrivilegesRequired=admin

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; Copy the entire PyInstaller output directory
Source: "dist\RecipeParser\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}";          Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";    Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(AppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Remove program files — leave {userappdata}\RecipeParser\ intact so the API key survives
Type: filesandordirs; Name: "{app}"

; ─────────────────────────────────────────────────────────────────────────────
; Custom wizard page: Google API key
; ─────────────────────────────────────────────────────────────────────────────
[Code]

var
  ApiKeyPage:   TWizardPage;
  ApiKeyEdit:   TEdit;
  ApiKeyLabel:  TLabel;
  ApiKeyHint:   TLabel;
  ApiKeyLink:   TLabel;

{ Create the custom page after the "Select Destination Location" page }
procedure InitializeWizard();
var
  PageAfter: Integer;
begin
  PageAfter := wpSelectDir;

  ApiKeyPage := CreateCustomPage(
    PageAfter,
    'Google API Key',
    'Enter your Google Gemini API key to enable recipe extraction'
  );

  { Main label }
  ApiKeyLabel        := TLabel.Create(ApiKeyPage);
  ApiKeyLabel.Parent := ApiKeyPage.Surface;
  ApiKeyLabel.Left   := 0;
  ApiKeyLabel.Top    := 8;
  ApiKeyLabel.Width  := ApiKeyPage.SurfaceWidth;
  ApiKeyLabel.Height := 20;
  ApiKeyLabel.Caption := 'Google Gemini API Key (free at aistudio.google.com):';

  { Text input }
  ApiKeyEdit           := TEdit.Create(ApiKeyPage);
  ApiKeyEdit.Parent    := ApiKeyPage.Surface;
  ApiKeyEdit.Left      := 0;
  ApiKeyEdit.Top       := 32;
  ApiKeyEdit.Width     := ApiKeyPage.SurfaceWidth;
  ApiKeyEdit.Height    := 24;
  ApiKeyEdit.MaxLength := 256;
  ApiKeyEdit.PasswordChar := #0;  { visible — keys are long and error-prone to type blind }

  { Hint }
  ApiKeyHint        := TLabel.Create(ApiKeyPage);
  ApiKeyHint.Parent := ApiKeyPage.Surface;
  ApiKeyHint.Left   := 0;
  ApiKeyHint.Top    := 64;
  ApiKeyHint.Width  := ApiKeyPage.SurfaceWidth;
  ApiKeyHint.Height := 36;
  ApiKeyHint.WordWrap := True;
  ApiKeyHint.Caption  :=
    'You can leave this blank and enter the key later using the Save button ' +
    'inside the application.  The key is stored in:' + #13#10 +
    '%APPDATA%\RecipeParser\.env';

  { Link label — cosmetic only; no ShellExec in modern Inno without extra import }
  ApiKeyLink        := TLabel.Create(ApiKeyPage);
  ApiKeyLink.Parent := ApiKeyPage.Surface;
  ApiKeyLink.Left   := 0;
  ApiKeyLink.Top    := 110;
  ApiKeyLink.Width  := ApiKeyPage.SurfaceWidth;
  ApiKeyLink.Height := 20;
  ApiKeyLink.Font.Color := clBlue;
  ApiKeyLink.Font.Style := [fsUnderline];
  ApiKeyLink.Caption    := 'https://aistudio.google.com/app/apikey';
end;

{ Write the .env file after files have been installed }
procedure CurStepChanged(CurStep: TSetupStep);
var
  ApiKey:   String;
  EnvDir:   String;
  EnvFile:  String;
  Contents: String;
begin
  if CurStep = ssPostInstall then
  begin
    ApiKey := Trim(ApiKeyEdit.Text);
    if ApiKey = '' then
      Exit;   { user skipped — nothing to write }

    { %APPDATA% expands to the current user's roaming app-data folder }
    EnvDir  := ExpandConstant('{userappdata}') + '\RecipeParser';
    EnvFile := EnvDir + '\.env';

    { Create directory if it doesn't exist }
    if not DirExists(EnvDir) then
      CreateDir(EnvDir);

    { Write (or overwrite) the .env file }
    Contents := 'GOOGLE_API_KEY=' + ApiKey + #13#10;
    if not SaveStringToFile(EnvFile, Contents, False) then
      MsgBox('Warning: could not write API key to ' + EnvFile + '.'#13#10 +
             'You can enter it manually inside the application.',
             mbInformation, MB_OK);
  end;
end;

{ Offer to remove the .env / API key on uninstall }
function InitializeUninstall(): Boolean;
var
  EnvFile: String;
  Answer:  Integer;
begin
  Result  := True;
  EnvFile := ExpandConstant('{userappdata}') + '\RecipeParser\.env';

  if FileExists(EnvFile) then
  begin
    Answer := MsgBox(
      'Do you also want to remove your saved API key?' + #13#10 +
      '(' + EnvFile + ')',
      mbConfirmation, MB_YESNO
    );
    if Answer = IDYES then
      DeleteFile(EnvFile);
  end;
end;
