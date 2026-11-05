; APMLogViewer 安装包脚本（Inno Setup 6）
;
; 用法（仓库根目录）：
;   "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" packaging\installer.iss
; 前置：先跑 `packaging\build.py build` 生成 dist\APMLogViewer\
; 产物：release\APMLogViewer-<版本>-win64-setup.exe（未签名；企业签名由发布方另做）
;
; 设计要点：
; * 目标平台只有 Windows 10/11 x64 —— 见 docs/BUILD.md「系统要求」
;   （Qt 6.12 与 Python 3.14 都不支持 Win7/8.1 与 x86，ARM64 未验证）
; * PrivilegesRequired=lowest：装到当前用户 %LOCALAPPDATA%\Programs\APMLogViewer，
;   不需要管理员。BUILD.md 说明了原因：`_internal/config/alerts.yaml` 要能直接改。
; * 装的就是完整 onedir 包：`_internal/data/knowledge`（事件说明知识库）、
;   `_internal/data/codebooks`（31 子系统码本 + 源码证据）、`_internal/config/alerts.yaml`
;   全部随包安装，缺任一项软件都跑不起来或功能残缺。

#define MyAppName "APMLogViewer"
#define MyAppVersion "0.1.2"
#define MyAppPublisher "KonuriMaki2004"
#define MyAppURL "https://github.com/KonuriMaki2004/APMLogViewer"
#define MyAppExeName "APMLogViewer.exe"
#define DistDir "..\dist\APMLogViewer"

[Setup]
; AppId 一旦发布就不能改，否则升级会变成两套并存
AppId={{7C4D2F1A-9E3B-4A6D-8C1F-2B5A7D9E0C33}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
VersionInfoVersion={#MyAppVersion}.0
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersion}

; 免管理员：装到当前用户目录（BUILD.md §4）
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

LicenseFile=..\LICENSE
OutputDir=..\release
OutputBaseFilename=APMLogViewer-{#MyAppVersion}-win64-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes

; 目标平台：Windows 10/11 x64
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName} {#MyAppVersion}
AllowNoIcons=yes
CloseApplications=yes
RestartApplications=no

[Languages]
; 中文在前 = 默认语言；English 供非中文系统
Name: "cn"; MessagesFile: "ChineseSimplified.isl"
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
; 整个 onedir 目录：exe + _internal（含 data/knowledge、data/codebooks、config/alerts.yaml）
Source: "{#DistDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "ArduCopter DataFlash 日志分析"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 只清安装目录内可能残留的运行时文件；%LOCALAPPDATA%\APMLogViewer 下的设置/缓存/插件保留
Type: filesandordirs; Name: "{app}\_internal\__pycache__"
