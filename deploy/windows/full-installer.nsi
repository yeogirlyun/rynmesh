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
  File /oname=core-setup.exe "${CORE}"
  ; Retain the installed executable trio for rollback; data stays outside them.
  CreateDirectory "$INSTDIR\rollback\before-full-20261103"
  CopyFiles /SILENT "$INSTDIR\Ryn.exe" "$INSTDIR\rollback\before-full-20261103"
  CopyFiles /SILENT "$INSTDIR\rynmesh-peer.exe" "$INSTDIR\rollback\before-full-20261103"
  CopyFiles /SILENT "$INSTDIR\ryn_computer_use.exe" "$INSTDIR\rollback\before-full-20261103"
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
