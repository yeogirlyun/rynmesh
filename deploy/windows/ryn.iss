#ifndef BundleDir
  #error BundleDir must name the frozen Ryn application directory
#endif
#ifndef OutputDir
  #error OutputDir must name the candidate output directory
#endif
#ifndef CandidateVersion
  #define CandidateVersion "0.6.2"
#endif
[Setup]
AppId={{7E67DCBE-969E-4541-B79E-A23F1A421F5B}
AppName=Ryn
AppVersion={#CandidateVersion}
AppPublisher=Rynmesh contributors
DefaultDirName={localappdata}\Programs\Ryn
DefaultGroupName=Ryn
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir={#OutputDir}
OutputBaseFilename=Ryn-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\Ryn.exe
ChangesAssociations=yes
CloseApplications=yes
SetupLogging=no

[Files]
Source: "{#BundleDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Retain this actual installer for the provider's invitation download page.
Source: "{srcexe}"; DestDir: "{app}"; DestName: "Ryn-Setup.exe"; Flags: external ignoreversion

[Icons]
Name: "{group}\Ryn"; Filename: "{app}\Ryn.exe"
Name: "{autodesktop}\Ryn"; Filename: "{app}\Ryn.exe"

[Registry]
Root: HKCU; Subkey: "Software\Classes\rynmesh"; ValueType: string; ValueData: "URL:Ryn invitation"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\rynmesh"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\Classes\rynmesh\DefaultIcon"; ValueType: string; ValueData: "{app}\Ryn.exe,0"
Root: HKCU; Subkey: "Software\Classes\rynmesh\shell\open\command"; ValueType: string; ValueData: """{app}\Ryn.exe"" ""%1"""

[Run]
Filename: "{app}\Ryn.exe"; Description: "Open Ryn and continue your invitation"; Flags: nowait postinstall skipifsilent
