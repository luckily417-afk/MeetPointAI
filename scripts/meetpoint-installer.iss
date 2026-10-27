#define AppName "MeetPoint AI"
#ifndef AppVersion
  #define AppVersion "0.0.0.1"
#endif
#ifndef GuiDir
  #define GuiDir "..\dist\MeetPoint AI"
#endif
#ifndef CliExe
  #define CliExe "..\dist\meetpoint-debug.exe"
#endif
#ifndef OutputDir
  #define OutputDir "..\dist"
#endif

[Setup]
AppId=MeetPointAI
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=MeetPoint AI
DefaultDirName={localappdata}\Programs\MeetPoint AI
DefaultGroupName=MeetPoint AI
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=MeetPoint-AI-Setup-V0.0.0.1-win-x64
Compression=lzma2/max
SolidCompression=yes
UninstallDisplayIcon={app}\MeetPoint AI.exe
WizardStyle=modern
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "其他选项："; Flags: unchecked

[Files]
Source: "{#GuiDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: ".env,*.log"
Source: "{#CliExe}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\.env.example"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\docs\安装包使用说明.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\MeetPoint AI"; Filename: "{app}\MeetPoint AI.exe"
Name: "{group}\卸载 MeetPoint AI"; Filename: "{uninstallexe}"
Name: "{autodesktop}\MeetPoint AI"; Filename: "{app}\MeetPoint AI.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\MeetPoint AI.exe"; Description: "启动 MeetPoint AI"; Flags: nowait postinstall skipifsilent
