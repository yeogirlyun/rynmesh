Unicode true
!include "MUI2.nsh"
Name "Ryn Complete"
OutFile "${OUTPUT}"
InstallDir "$LOCALAPPDATA\Ryn"
RequestExecutionLevel user
SetCompressor /SOLID lzma
!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_LANGUAGE "SimpChinese"
!insertmacro MUI_LANGUAGE "English"
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
    MessageBox MB_ICONSTOP "WebView2 offline installation failed ($0)."
    SetErrorLevel 1
    Abort
  webview_ready:
  File /oname=core-setup.exe "${CORE}"
  ; Retain the installed executable trio for rollback; data stays outside them.
  IfFileExists "$INSTDIR\rollback\before-full-20261103\Ryn.exe" backup_done
  CreateDirectory "$INSTDIR\rollback\before-full-20261103"
  CopyFiles /SILENT "$INSTDIR\Ryn.exe" "$INSTDIR\rollback\before-full-20261103"
  CopyFiles /SILENT "$INSTDIR\rynmesh-peer.exe" "$INSTDIR\rollback\before-full-20261103"
  CopyFiles /SILENT "$INSTDIR\ryn_computer_use.exe" "$INSTDIR\rollback\before-full-20261103"
  backup_done:
  DetailPrint "Installing Ryn..."
  ExecWait '"$PLUGINSDIR\core-setup.exe" /S /D=$INSTDIR' $0
  StrCmp $0 0 +3
    MessageBox MB_ICONSTOP "Ryn installation failed ($0)."
    Abort
  SetOutPath "$PLUGINSDIR\provision"
  File /r "${HELPER}\*"
  SetOutPath "$PLUGINSDIR\payload"
  File /r "${PAYLOAD}\*"
  DetailPrint "Installing seven included plugins and local document engines..."
  ExecWait '"$PLUGINSDIR\provision\ryn-full-install.exe" --payload "$PLUGINSDIR\payload" --destination "$INSTDIR"' $0
  StrCmp $0 0 +4
    MessageBox MB_ICONSTOP "Plugin installation failed ($0). Your existing data is retained. Run this installer again to retry."
    SetErrorLevel 1
    Abort
  WriteRegStr HKCU "Software\Ryn\Craft" "SelectedPlugins" "pdfcraft;wordcraft;gridcraft;deckcraft"
SectionEnd
