; Small optional component selection. Editor ZIP/WASM files are never bundled.
; This records a preference only: the app reviews and downloads after installation.
!include "nsDialogs.nsh"
!include "LogicLib.nsh"
Var CraftDialog
Var CraftPdf
Var CraftWord
Var CraftExcel
Var CraftPpt
Var CraftPdfState
Var CraftWordState
Var CraftExcelState
Var CraftPptState
Var CraftSelectionShown
Var CraftSelected

; Tauri includes hooks before MUI_LANGUAGE defines its language constants.
LangString CraftTitle 1033 "Choose your tools"
LangString CraftTitle 2052 "选择常用工具"
LangString CraftHelp 1033 "Free optional plugins. After installation, review and download them in Apps. They are not included in this installer."
LangString CraftHelp 2052 "可选插件免费使用。安装完成后，在应用页确认并下载，不包含在主安装包中。"
LangString CraftMore 1033 "More tools can be added or removed later. Agent uses enabled plugins by default."
LangString CraftMore 2052 "其他工具可以稍后添加或卸载。Agent 默认可使用已启用插件。"

Page custom CraftChooseTools CraftLeaveTools
Function CraftChooseTools
  IfSilent 0 +2
    Abort
  ; An upgrade keeps previous choices and never queues another download.
  ClearErrors
  ReadRegStr $0 HKCU "Software\Ryn\Craft" "SelectedPlugins"
  IfErrors +2 0
    Abort
  nsDialogs::Create 1018
  Pop $CraftDialog
  StrCmp $CraftDialog error 0 +2
    Abort
  !insertmacro MUI_HEADER_TEXT "$(CraftTitle)" "Ryn"
  ${NSD_CreateLabel} 0 0 100% 35u "$(CraftHelp)"
  Pop $0
  ${NSD_CreateCheckbox} 8u 45u 90% 14u "PDF"
  Pop $CraftPdf
  ${NSD_Check} $CraftPdf
  ${NSD_CreateCheckbox} 8u 67u 90% 14u "Word"
  Pop $CraftWord
  ${NSD_Check} $CraftWord
  ${NSD_CreateCheckbox} 8u 89u 90% 14u "Excel"
  Pop $CraftExcel
  ${NSD_Check} $CraftExcel
  ${NSD_CreateCheckbox} 8u 111u 90% 14u "PPT"
  Pop $CraftPpt
  ${NSD_Check} $CraftPpt
  ${If} $CraftSelectionShown == 1
    ${NSD_SetState} $CraftPdf $CraftPdfState
    ${NSD_SetState} $CraftWord $CraftWordState
    ${NSD_SetState} $CraftExcel $CraftExcelState
    ${NSD_SetState} $CraftPpt $CraftPptState
  ${EndIf}
  ${NSD_CreateLabel} 0 140u 100% 35u "$(CraftMore)"
  Pop $0
  StrCpy $CraftSelectionShown 1
  nsDialogs::Show
FunctionEnd

Function CraftLeaveTools
  StrCpy $CraftSelected ""
  ${NSD_GetState} $CraftPdf $0
  StrCpy $CraftPdfState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "pdfcraft;"
  ${EndIf}
  ${NSD_GetState} $CraftWord $0
  StrCpy $CraftWordState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "$CraftSelectedwordcraft;"
  ${EndIf}
  ${NSD_GetState} $CraftExcel $0
  StrCpy $CraftExcelState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "$CraftSelectedgridcraft;"
  ${EndIf}
  ${NSD_GetState} $CraftPpt $0
  StrCpy $CraftPptState $0
  ${If} $0 == ${BST_CHECKED}
    StrCpy $CraftSelected "$CraftSelecteddeckcraft;"
  ${EndIf}
  StrLen $0 $CraftSelected
  ${If} $0 > 0
    StrCpy $CraftSelected $CraftSelected -1
  ${EndIf}
FunctionEnd

!macro NSIS_HOOK_POSTINSTALL
  ${If} $CraftSelectionShown == 1
    WriteRegStr HKCU "Software\Ryn\Craft" "SelectedPlugins" "$CraftSelected"
  ${EndIf}
!macroend

; Remove only a startup entry owned by the installation being removed.
!macro NSIS_HOOK_POSTUNINSTALL
  ${If} $UpdateMode <> 1
  ReadRegStr $0 HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ryn"
  StrCmp $0 '$\"$INSTDIR\Ryn.exe$\" --background' 0 +2
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ryn"
  StrCmp $0 '$\"$INSTDIR\Ryn.exe$\"' 0 +2
    DeleteRegValue HKCU "Software\Microsoft\Windows\CurrentVersion\Run" "Ryn"
  !insertmacro RYN_REMOVE_SYSTEM_APPS
  ${EndIf}
!macroend

; Start menu apps and "Open with" entries Ryn creates per installed editor (src/system_apps).
; Upgrades keep them; Ryn rewrites them on its next start. A unit test keeps this list complete.
!macro RYN_REMOVE_SYSTEM_APPS
  Delete "$SMPROGRAMS\Ryn PDF.lnk"
  Delete "$SMPROGRAMS\Ryn Word.lnk"
  Delete "$SMPROGRAMS\Ryn Excel.lnk"
  Delete "$SMPROGRAMS\Ryn PPT.lnk"
  Delete "$SMPROGRAMS\Ryn CAD.lnk"
  Delete "$SMPROGRAMS\Ryn Layout.lnk"
  Delete "$SMPROGRAMS\Ryn Effects.lnk"
  Delete "$SMPROGRAMS\Ryn UI Design.lnk"
  Delete "$SMPROGRAMS\Ryn Photo.lnk"
  Delete "$SMPROGRAMS\Ryn Image.lnk"
  Delete "$SMPROGRAMS\Ryn Audio.lnk"
  Delete "$SMPROGRAMS\Ryn Vector.lnk"
  Delete "$SMPROGRAMS\Ryn Resume.lnk"
  Delete "$SMPROGRAMS\Ryn Recruit.lnk"
  Delete "$SMPROGRAMS\Ryn Desk.lnk"
  Delete "$SMPROGRAMS\Ryn Drive.lnk"
  !insertmacro RYN_REMOVE_FILE_TYPE "pdfcraft" "pdf"
  !insertmacro RYN_REMOVE_FILE_TYPE "wordcraft" "docx"
  !insertmacro RYN_REMOVE_FILE_TYPE "wordcraft" "doc"
  !insertmacro RYN_REMOVE_FILE_TYPE "wordcraft" "odt"
  !insertmacro RYN_REMOVE_FILE_TYPE "wordcraft" "rtf"
  !insertmacro RYN_REMOVE_FILE_TYPE "gridcraft" "xlsx"
  !insertmacro RYN_REMOVE_FILE_TYPE "gridcraft" "xls"
  !insertmacro RYN_REMOVE_FILE_TYPE "gridcraft" "ods"
  !insertmacro RYN_REMOVE_FILE_TYPE "gridcraft" "csv"
  !insertmacro RYN_REMOVE_FILE_TYPE "deckcraft" "pptx"
  !insertmacro RYN_REMOVE_FILE_TYPE "deckcraft" "ppsx"
  DeleteRegValue HKCU "Software\RegisteredApplications" "Ryn PDF"
  DeleteRegValue HKCU "Software\RegisteredApplications" "Ryn Word"
  DeleteRegValue HKCU "Software\RegisteredApplications" "Ryn Excel"
  DeleteRegValue HKCU "Software\RegisteredApplications" "Ryn PPT"
  DeleteRegKey HKCU "Software\Ryn\Apps"
  ; SHCNE_ASSOCCHANGED: Explorer drops the removed "Open with" choices.
  System::Call 'shell32::SHChangeNotify(i 0x08000000, i 0, p 0, p 0)'
!macroend

!macro RYN_REMOVE_FILE_TYPE APP EXT
  DeleteRegKey HKCU "Software\Classes\Ryn.${APP}.${EXT}"
  DeleteRegValue HKCU "Software\Classes\.${EXT}\OpenWithProgids" "Ryn.${APP}.${EXT}"
!macroend
