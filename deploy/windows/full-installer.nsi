Unicode true
!include "MUI2.nsh"
Name "Ryn Complete"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Ryn"
RequestExecutionLevel user
SetCompressor /SOLID lzma
Var ExistingInstall
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!define MUI_PAGE_CUSTOMFUNCTION_PRE UpgradeDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN "$INSTDIR\Ryn.exe"
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
Function .onInit
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Uninstall\Ryn" "InstallLocation"
  StrCmp $0 "" detect_done
  StrCpy $1 $0 1
  StrCmp $1 '$\"' 0 +2
    StrCpy $0 $0 -1 1
  IfFileExists "$0\Ryn.exe" 0 detect_done
    StrCpy $INSTDIR $0
    StrCpy $ExistingInstall "1"
  detect_done:
FunctionEnd
Function UpgradeDirectory
  StrCmp $ExistingInstall "1" 0 +2
    Abort
FunctionEnd
Section
  InitPluginsDir
  SetOutPath "$PLUGINSDIR"
  File /oname=webview2.exe "${WEBVIEW}"
  SetRegView 32
  ReadRegStr $1 HKLM "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  StrCmp $1 "" 0 webview_ready
  ReadRegStr $1 HKCU "SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" "pv"
  StrCmp $1 "" 0 webview_ready
  ExecWait '"$PLUGINSDIR\webview2.exe" /silent /install' $0
  StrCmp $0 0 webview_ready
  StrCmp $0 3010 webview_ready
    MessageBox MB_ICONSTOP "WebView2 offline installation failed ($0)." /SD IDOK
    SetErrorLevel 1
    Abort
  webview_ready:
  File /oname=core-setup.exe "${CORE}"
  SetOutPath "$PLUGINSDIR\provision"
  File /r "${HELPER}\*"
  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD}\*"
  DetailPrint "Verifying package, stopping and removing the previous Ryn program (keeping your data)..."
  ExecWait '"$PLUGINSDIR\provision\ryn-full-install.exe" --prepare-upgrade --payload "$PLUGINSDIR\payload" --destination "$INSTDIR"' $0
  StrCmp $0 0 upgrade_ready
    SetErrorLevel 1
    MessageBox MB_ICONSTOP "Unable to prepare the previous Ryn installation ($0). Installation stopped; your data is retained." /SD IDOK
    Abort
  upgrade_ready:
  DetailPrint "Installing Ryn..."
  ExecWait '"$PLUGINSDIR\core-setup.exe" /S /D=$INSTDIR' $0
  StrCmp $0 0 core_ready
    SetErrorLevel 1
    MessageBox MB_ICONSTOP "Ryn installation failed ($0)." /SD IDOK
    Abort
  core_ready:
  DetailPrint "Installing seven included plugins and local document engines..."
  ExecWait '"$PLUGINSDIR\provision\ryn-full-install.exe" --payload "$PLUGINSDIR\payload" --destination "$INSTDIR"' $0
  StrCmp $0 0 +4
    MessageBox MB_ICONSTOP "Plugin installation failed ($0). Your existing data is retained. Run this installer again to retry." /SD IDOK
    SetErrorLevel 1
    Abort
  WriteRegStr HKCU "Software\Ryn\Craft" "SelectedPlugins" "pdfcraft;wordcraft;gridcraft;deckcraft"
SectionEnd
