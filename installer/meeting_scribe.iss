; MeetingScribe 安裝檔（Inno Setup 6）。由 scripts\build.ps1 呼叫：ISCC /DAppVersion=x.y.z /DRoot=專案根目錄
; 預設只安裝給目前使用者（不需要系統管理員權限），使用者資料（設定、金鑰、工作資料夾）在解除安裝時保留。

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#ifndef Root
  #define Root ".."
#endif
#define AppName "MeetingScribe 會議紀錄"
#define AppExe "MeetingScribe.exe"

[Setup]
AppId={{6B0E7C1E-4F2A-4B8E-9C61-3D2F5A8E7B10}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=Xiang-Yun
DefaultDirName={autopf}\MeetingScribe
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=commandline dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#Root}\dist\installer
OutputBaseFilename=MeetingScribe_Setup_{#AppVersion}
SetupIconFile={#Root}\build\meeting_scribe.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes

[Languages]
Name: "zhtw"; MessagesFile: "compiler:Default.isl"

[Messages]
SetupAppTitle=安裝程式
SetupWindowTitle=安裝 %1
WelcomeLabel1=歡迎安裝 [name]
WelcomeLabel2=這個程式會把 [name/ver] 安裝到您的電腦。%n%n建議先關閉其他應用程式再繼續。
ButtonNext=下一步(&N) >
ButtonBack=< 上一步(&B)
ButtonInstall=安裝(&I)
ButtonCancel=取消
ButtonFinish=完成(&F)
ButtonBrowse=瀏覽(&R)...
ButtonYes=是(&Y)
ButtonNo=否(&N)
ButtonOK=確定
WizardSelectDir=選擇安裝位置
SelectDirDesc=要把 [name] 安裝在哪裡？
SelectDirLabel3=安裝程式會把 [name] 安裝到下面的資料夾。
SelectDirBrowseLabel=按「下一步」繼續；要換其他資料夾請按「瀏覽」。
DiskSpaceMBLabel=至少需要 [mb] MB 的磁碟空間。
WizardSelectTasks=選擇其他工作
SelectTasksDesc=要一併執行哪些工作？
SelectTasksLabel2=請勾選安裝 [name] 時要一併執行的工作，然後按「下一步」。
WizardReady=準備安裝
ReadyLabel1=安裝程式已準備好在您的電腦安裝 [name]。
ReadyLabel2a=按「安裝」開始安裝；要檢查或修改設定請按「上一步」。
ReadyLabel2b=按「安裝」開始安裝。
ReadyMemoDir=安裝位置：
ReadyMemoTasks=其他工作：
WizardInstalling=安裝中
InstallingLabel=正在安裝 [name]，請稍候。
StatusExtractFiles=正在解壓縮檔案...
StatusCreateIcons=正在建立捷徑...
FinishedHeadingLabel=[name] 安裝完成
FinishedLabelNoIcons=[name] 已安裝到您的電腦。
FinishedLabel=[name] 已安裝到您的電腦，可以從捷徑開啟。
ClickFinish=按「完成」結束安裝程式。
ExitSetupTitle=結束安裝
ExitSetupMessage=安裝尚未完成，現在結束的話不會安裝程式。%n%n確定要結束嗎？
ConfirmUninstall=確定要移除 %1 嗎？（您的設定、金鑰與會議資料會保留）
UninstalledAll=%1 已從您的電腦移除。
PrivilegesRequiredOverrideTitle=選擇安裝方式
PrivilegesRequiredOverrideInstruction=選擇安裝方式
PrivilegesRequiredOverrideText1=%1 可以安裝給所有使用者（需要系統管理員權限），或只安裝給您自己。
PrivilegesRequiredOverrideText2=%1 可以只安裝給您自己，或安裝給所有使用者（需要系統管理員權限）。
PrivilegesRequiredOverrideAllUsers=安裝給所有使用者(&A)
PrivilegesRequiredOverrideAllUsersRecommended=安裝給所有使用者(&A)（建議）
PrivilegesRequiredOverrideCurrentUser=只安裝給我(&M)
PrivilegesRequiredOverrideCurrentUserRecommended=只安裝給我(&M)（建議）

[CustomMessages]
DesktopIcon=建立桌面捷徑
LaunchApp=立即開啟 MeetingScribe

[Tasks]
Name: "desktopicon"; Description: "{cm:DesktopIcon}"

[Files]
Source: "{#Root}\dist\MeetingScribe\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchApp}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"